#!/usr/bin/env python3
"""Opt-in Restic backups through the rclone TelDrive backend.

Phase 1: profile selection, repository initialization, snapshots and safe restore.
This program does not install services, schedule jobs, or prune snapshots.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = "~/.config/teldrive-backup/config.toml"
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
BANDWIDTH_RE = re.compile(r"^[0-9]+(?:[KMGTP]|[kmgpt])?(?::[0-9]+(?:[KMGTP]|[kmgpt])?)?$")


class BackupError(Exception):
    """An expected configuration, preflight, or backup failure."""


@dataclass(frozen=True)
class Profile:
    name: str
    sources: tuple[Path, ...]
    excludes: tuple[str, ...]
    one_file_system: bool


@dataclass(frozen=True)
class Settings:
    repository: str
    rclone_program: str
    password_file: Path
    bandwidth_limit: str | None
    blocked_sources: tuple[Path, ...]
    profiles: dict[str, Profile]


def expand_path(value: str, field: str, *, resolve: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise BackupError(f"{field} must be a non-empty path string")
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        raise BackupError(f"{field} must be an absolute or home-relative path: {value}")
    return path.resolve(strict=False) if resolve else path


def string_list(value: object, field: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise BackupError(f"{field} must be a list of non-empty strings")
    if nonempty and not value:
        raise BackupError(f"{field} must contain at least one entry")
    return tuple(value)


def load_settings(config_path: Path) -> Settings:
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise BackupError(f"config not found: {config_path} (copy examples/teldrive-backup.toml)") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BackupError(f"cannot read config {config_path}: {exc}") from exc

    if not isinstance(data, dict) or set(data) - {"repository", "safety", "profiles"}:
        raise BackupError("config requires only [repository], [safety], and [[profiles]]")
    repo = data.get("repository")
    if not isinstance(repo, dict):
        raise BackupError("missing [repository] section")
    if set(repo) - {"url", "rclone_program", "password_file", "bandwidth_limit"}:
        raise BackupError("unknown [repository] setting")
    url = repo.get("url")
    if not isinstance(url, str) or not re.fullmatch(r"rclone:[a-zA-Z0-9_-]+:[^\r\n\x00]+", url):
        raise BackupError("repository.url must be rclone:<remote>:<nonempty-path>")
    remote_path = url.split(":", 2)[2]
    if remote_path.startswith("/") or any(p in {"", ".", ".."} for p in remote_path.split("/")):
        raise BackupError("repository.url requires a dedicated relative directory without dot components")
    program = repo.get("rclone_program", "~/.local/bin/rclone-teldrive")
    if not isinstance(program, str) or not program.strip():
        raise BackupError("repository.rclone_program must be a program path or name")
    if "/" in program or program.startswith("~"):
        program = str(expand_path(program, "repository.rclone_program"))
    password_file = expand_path(repo.get("password_file", "~/.config/teldrive-backup/restic-password"), "repository.password_file", resolve=False)
    bandwidth = repo.get("bandwidth_limit")
    if bandwidth is not None and (not isinstance(bandwidth, str) or not BANDWIDTH_RE.fullmatch(bandwidth)):
        raise BackupError("repository.bandwidth_limit must be like '1M' or '1M:512K'")

    safety = data.get("safety", {})
    if not isinstance(safety, dict) or set(safety) - {"blocked_sources"}:
        raise BackupError("[safety] accepts only blocked_sources")
    blocked = string_list(safety.get("blocked_sources", ["~/Mounts/teldrive-crypt"]), "safety.blocked_sources")
    blocked_paths = tuple(expand_path(x, "safety.blocked_sources") for x in blocked)

    profiles_raw = data.get("profiles")
    if not isinstance(profiles_raw, list) or not profiles_raw:
        raise BackupError("configure at least one [[profiles]] entry")
    profiles: dict[str, Profile] = {}
    for item in profiles_raw:
        if not isinstance(item, dict) or set(item) - {"name", "sources", "excludes", "one_file_system"}:
            raise BackupError("unknown [[profiles]] setting")
        name = item.get("name")
        if not isinstance(name, str) or not PROFILE_RE.fullmatch(name):
            raise BackupError("profile names must match [a-z][a-z0-9_-]{0,39}")
        if name in profiles:
            raise BackupError(f"duplicate profile: {name}")
        raw_sources = string_list(item.get("sources"), f"{name}.sources", nonempty=True)
        sources = tuple(expand_path(p, f"{name}.sources") for p in raw_sources)
        if len(set(sources)) != len(sources):
            raise BackupError(f"{name}: duplicate source paths")
        for i, left in enumerate(sources):
            if any(left == right or left.is_relative_to(right) or right.is_relative_to(left)
                   for right in sources[i + 1:]):
                raise BackupError(f"{name}: nested/overlapping source paths are not supported")
        excludes = string_list(item.get("excludes", []), f"{name}.excludes")
        one_fs = item.get("one_file_system", True)
        if not isinstance(one_fs, bool):
            raise BackupError(f"{name}.one_file_system must be true or false")
        profiles[name] = Profile(name, sources, excludes, one_fs)
    return Settings(url, program, password_file, bandwidth, blocked_paths, profiles)


def is_inside(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def mounted_rclone_paths() -> tuple[Path, ...]:
    """Read the kernel mount table, not user-supplied remote names.

    Block every rclone FUSE mount: a crypt or alias mount can hide TelDrive.
    Failure to inspect mounts must fail closed on this Linux-only tool.
    """
    paths = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        filesystem, mount_source = after.split()[:2]
        if (filesystem == "fuse.rclone" or "teldrive" in filesystem.lower()
                or mount_source.startswith("restic:")):
            mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), before.split()[4])
            paths.append(Path(mount).resolve())
    return tuple(paths)


def preflight_sources(settings: Settings, profile: Profile) -> None:
    mounts = mounted_rclone_paths()
    for source in profile.sources:
        if any(is_inside(source, mount) or is_inside(mount, source) for mount in mounts):
            raise BackupError(f"{profile.name}: source overlaps an active rclone/TelDrive mount: {source}")
        if any(is_inside(source, blocked) for blocked in settings.blocked_sources):
            raise BackupError(f"{profile.name}: source is inside a blocked path: {source}")
        if not source.exists():
            raise BackupError(f"{profile.name}: missing source directory: {source}; backup cancelled")
        if not source.is_dir():
            raise BackupError(f"{profile.name}: source is not a directory: {source}")
        if not os.access(source, os.R_OK | os.X_OK):
            raise BackupError(f"{profile.name}: cannot read source: {source}")


def preflight_password(password_file: Path) -> None:
    try:
        mode = password_file.lstat().st_mode
    except FileNotFoundError as exc:
        raise BackupError(f"password file missing: {password_file}; run 'keygen' first") from exc
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise BackupError(f"password file must be a regular non-symlink file with mode 0600: {password_file}")
    if not os.access(password_file, os.R_OK) or password_file.stat().st_size == 0:
        raise BackupError(f"password file is unreadable or empty: {password_file}")


def require_program(value: str, label: str) -> str:
    if "/" in value:
        path = Path(value)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise BackupError(f"{label} binary is missing or not executable: {value}")
        return str(path)
    found = shutil.which(value)
    if not found:
        raise BackupError(f"{label} is not on PATH: {value}")
    return found


def restic_command(settings: Settings) -> list[str]:
    restic = require_program("restic", "restic")
    rclone = require_program(settings.rclone_program, "rclone-teldrive")
    preflight_password(settings.password_file)
    # Restic parses rclone.program as a command line, so quote executable paths.
    return [restic, "-o", f"rclone.program={shlex.quote(rclone)}", "-r", settings.repository]


def restic_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    # Prevent ambient credentials from overriding the configured password file.
    env.pop("RESTIC_PASSWORD", None)
    env.pop("RESTIC_PASSWORD_COMMAND", None)
    for key in ("RESTIC_REPOSITORY", "RESTIC_REPOSITORY_FILE", "RCLONE_LOG_FILE",
                "RCLONE_DUMP", "RCLONE_VERBOSE"):
        env.pop(key, None)
    env["RCLONE_LOG_LEVEL"] = "OFF"
    env["RESTIC_PASSWORD_FILE"] = str(settings.password_file)
    if settings.bandwidth_limit:
        env["RCLONE_BWLIMIT"] = settings.bandwidth_limit
    return env


def run_restic(settings: Settings, args: list[str], *, capture: bool = False) -> str:
    command = restic_command(settings) + args
    # Backend errors may include URLs, tokens or headers. Never relay arbitrary
    # child output to the terminal or a log (including successful warnings).
    result = subprocess.run(command, env=restic_env(settings), text=True,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        raise BackupError(f"restic exited with code {result.returncode}; backend output suppressed to protect credentials")
    return result.stdout or ""


@contextmanager
def exclusive_backup_lock() -> Iterator[None]:
    state = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    if not state.is_absolute():
        raise BackupError("XDG_STATE_HOME must be an absolute path")
    lock_dir = state / "teldrive-backup"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_file = lock_dir / "backup.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_file, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("another backup command is already running") from exc
        yield
    finally:
        os.close(fd)


def generate_password(password_file: Path) -> None:
    password_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(password_file, flags, 0o600)
    except FileExistsError as exc:
        raise BackupError(f"refusing to overwrite an existing password file: {password_file}") from exc
    try:
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(secrets.token_urlsafe(48) + "\n")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        password_file.unlink(missing_ok=True)
        raise
    print(f"Created password file: {password_file}")
    print("IMPORTANT: Save this password securely and separately from TelDrive before uploading backups.")


def snapshot_list(settings: Settings, profile: Profile) -> list[dict]:
    payload = run_restic(settings, ["snapshots", "--json", "--tag", f"teldrive-backup:{profile.name}"], capture=True)
    try:
        snapshots = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BackupError("restic returned invalid snapshot JSON") from exc
    if not isinstance(snapshots, list):
        raise BackupError("restic returned an unexpected snapshot format")
    tag = f"teldrive-backup:{profile.name}"
    selected = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tags"), list) or tag not in snapshot["tags"]:
            continue
        if not isinstance(snapshot.get("id"), str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot["id"]):
            raise BackupError("restic returned an invalid snapshot ID")
        if not isinstance(snapshot.get("time", ""), str) or not isinstance(snapshot.get("paths", []), list) or any(not isinstance(p, str) for p in snapshot.get("paths", [])):
            raise BackupError("restic returned invalid snapshot metadata")
        selected.append(snapshot)
    return selected


def resolve_snapshot(snapshots: list[dict], specified: str) -> str:
    if not snapshots:
        raise BackupError("this profile has no snapshots")
    if specified == "latest":
        return max(snapshots, key=lambda s: s.get("time", ""))["id"]
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", specified):
        raise BackupError("provide 'latest' or a snapshot ID prefix of at least 8 hex digits")
    matches = [s["id"] for s in snapshots if s["id"].lower().startswith(specified.lower())]
    if not matches:
        raise BackupError(f"snapshot not found in this profile: {specified}")
    if len(matches) != 1:
        raise BackupError(f"ambiguous snapshot prefix: {specified}")
    return matches[0]


def restore_target(raw: str, settings: Settings, profile: Profile) -> Path:
    raw_path = Path(os.path.expandvars(raw)).expanduser()
    if any(p.is_symlink() for p in (raw_path, *raw_path.parents)):
        raise BackupError("restore target must not be a symlink")
    if not raw_path.is_absolute():
        raise BackupError("restore target must be an absolute or home-relative path")
    target = raw_path.resolve(strict=False)
    if any(is_inside(target, mount) for mount in mounted_rclone_paths()):
        raise BackupError("restore target is inside an active rclone/TelDrive mount")
    if any(is_inside(target, blocked) for blocked in settings.blocked_sources):
        raise BackupError(f"restore target is inside a blocked location: {target}")
    if any(is_inside(target, source) or is_inside(source, target)
           for configured in settings.profiles.values() for source in configured.sources):
        raise BackupError("restore target must be outside all configured source directories")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BackupError(f"restore target must be a new or empty directory: {target}")
    return target


def config_text(data: dict) -> str:
    """Serialize the validated configuration schema without a TOML dependency.

    Comments/formatting are intentionally normalized; unknown keys are rejected
    by load_settings before this is called. JSON strings are TOML basic strings.
    """
    def literal(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, list):
            return "[" + ", ".join(literal(item) for item in value) + "]"
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        raise BackupError("unsupported configuration value")

    lines = ["# Managed by teldrive-backup dirs; manual edits are supported."]
    for section in ("repository", "safety"):
        if section in data:
            lines.extend(["", f"[{section}]"])
            lines.extend(f"{key} = {literal(value)}" for key, value in data[section].items())
    for profile in data["profiles"]:
        lines.extend(["", "[[profiles]]"])
        lines.extend(f"{key} = {literal(value)}" for key, value in profile.items())
    return "\n".join(lines) + "\n"


def edit_directory(config_path: Path, action: str, name: str, raw_source: str,
                   *, assume_yes: bool = False) -> None:
    """Validate and atomically update sources, retaining the original config."""
    if not PROFILE_RE.fullmatch(name):
        raise BackupError("invalid profile name")
    with exclusive_backup_lock():
        if config_path.is_symlink() or not config_path.is_file():
            raise BackupError("directory editor requires a regular non-symlink config")
        original = config_path.read_bytes()
        load_settings(config_path)  # Reject unknown schema before serializing.
        data = tomllib.loads(original.decode("utf-8"))
        source = expand_path(raw_source, "source")
        profile = next((p for p in data["profiles"] if p["name"] == name), None)
        if action == "add":
            if profile is None:
                profile = {"name": name, "sources": [], "excludes": [], "one_file_system": True}
                data["profiles"].append(profile)
            if any(expand_path(p, "source") == source for p in profile["sources"]):
                raise BackupError("directory is already selected in this profile")
            profile["sources"].append(str(source))
        elif action == "remove":
            if profile is None:
                raise BackupError(f"unknown profile: {name}")
            retained = [p for p in profile["sources"] if expand_path(p, "source") != source]
            if len(retained) == len(profile["sources"]):
                raise BackupError("directory is not selected in this profile")
            profile["sources"] = retained
            if not retained:
                if len(data["profiles"]) == 1:
                    raise BackupError("cannot remove the last directory of the last profile; the config requires at least one source")
                data["profiles"].remove(profile)
                print(f"This also removes profile '{name}' from the config. Its saved history remains; re-add the same name to access it.")
        else:
            raise BackupError("directory action must be add or remove")

        # Validate the candidate before any change to the active config. Keep the
        # temporary file in the same directory for an atomic final replacement.
        fd, temporary = tempfile.mkstemp(prefix=".config-edit-", dir=config_path.parent)
        candidate = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(config_text(data))
                handle.flush()
                os.fsync(handle.fileno())
            settings = load_settings(candidate)
            if action == "add":
                preflight_sources(settings, settings.profiles[name])
            print(f"{action.capitalize()} {source} {'to' if action == 'add' else 'from'} profile '{name}'.")
            print("Only configuration changes. No backup, restore, or deletion will run.")
            print("Config comments/formatting will be normalized; original bytes will be saved separately.")
            if not assume_yes:
                if not sys.stdin.isatty():
                    raise BackupError("confirmation requires a terminal; use --yes for an explicit noninteractive change")
                if input("Save this change? [y/N] ").strip().lower() != "y":
                    print("Cancelled; configuration unchanged.")
                    return
            if config_path.is_symlink() or config_path.read_bytes() != original:
                raise BackupError("config changed during editing; refusing to overwrite it")
            backup_fd, backup_name = tempfile.mkstemp(prefix="config.toml.before-dirs-", dir=config_path.parent)
            with os.fdopen(backup_fd, "wb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(candidate, config_path)
            print(f"Saved {config_path}. Previous config: {backup_name}")
        finally:
            candidate.unlink(missing_ok=True)


def directory_menu(config_path: Path, settings: Settings) -> None:
    if not sys.stdin.isatty():
        raise BackupError("the directory menu needs a terminal; use 'dirs list', 'dirs add' or 'dirs remove'")
    for profile in settings.profiles.values():
        print(f"{profile.name}:")
        for source in profile.sources:
            print(f"  {source}")
    action = input("Add or remove a directory? [a/r, Enter to cancel] ").strip().lower()
    if action not in {"a", "r"}:
        print("Cancelled; configuration unchanged.")
        return
    name = input("Profile name (adding can create a new profile): ").strip()
    source = input("Directory path (spaces are OK; do not add shell quotes): ").strip()
    edit_directory(config_path, "add" if action == "a" else "remove", name, source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="TOML configuration file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("profiles", help="show configured source directories")
    commands.add_parser("keygen", help="generate a new protected Restic password file")
    commands.add_parser("init", help="initialize the remote Restic repository once")
    browse = commands.add_parser("browse", help="browse snapshots through a foreground read-only FUSE mount")
    browse.add_argument("--target", default="~/Mounts/teldrive-backups", help="empty local mount directory")
    dirs = commands.add_parser("dirs", help="add/remove source directories or open the interactive menu")
    directory_commands = dirs.add_subparsers(dest="directory_action")
    directory_commands.add_parser("list", help="list configured sources")
    for action in ("add", "remove"):
        change = directory_commands.add_parser(action, help=f"{action} a directory from a profile")
        change.add_argument("profile")
        change.add_argument("source", help="absolute or home-relative directory")
        change.add_argument("--yes", action="store_true", help="save without interactive confirmation")
    for name, help_text in (("run", "back up a selected profile"), ("snapshots", "list a profile's versions")):
        p = commands.add_parser(name, help=help_text)
        p.add_argument("profile", help="configured profile name")
        if name == "run":
            p.add_argument("--dry-run", action="store_true", help="estimate changed data without writing snapshots")
    restore = commands.add_parser("restore", help="restore a profile version to a new/empty directory")
    restore.add_argument("profile")
    restore.add_argument("snapshot", help="snapshot ID/prefix or 'latest'")
    restore.add_argument("--target", required=True, help="new or empty destination directory")
    args = parser.parse_args(argv)
    os.umask(0o077)

    try:
        config_path = Path(os.path.abspath(Path(os.path.expandvars(args.config)).expanduser()))
        settings = load_settings(config_path)
        if args.command == "profiles" or (args.command == "dirs" and args.directory_action == "list"):
            for profile in settings.profiles.values():
                print(f"{profile.name}:")
                for source in profile.sources:
                    print(f"  {source}")
        elif args.command == "dirs":
            if args.directory_action is None:
                directory_menu(config_path, settings)
            else:
                edit_directory(config_path, args.directory_action, args.profile, args.source, assume_yes=args.yes)
        elif args.command == "keygen":
            generate_password(settings.password_file)
        elif args.command == "init":
            with exclusive_backup_lock():
                run_restic(settings, ["init"])
            print("Repository initialized. Keep the Restic password and rclone keys separately.")
        elif args.command == "browse":
            # Reuse restore destination protection for all configured sources.
            profile = next(iter(settings.profiles.values()))
            target = restore_target(args.target, settings, profile)
            if os.path.ismount(target):
                raise BackupError("browse target is already mounted")
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            print(f"Mounting snapshots read-only at {target}. Keep this terminal open.", flush=True)
            print("Use fusermount -u on this directory from another terminal to unmount.", flush=True)
            run_restic(settings, ["mount", str(target)])
        else:
            profile = settings.profiles.get(args.profile)
            if profile is None:
                raise BackupError(f"unknown profile: {args.profile}; available: {', '.join(settings.profiles)}")
            if args.command == "run":
                preflight_sources(settings, profile)
                command = ["backup", "--tag", f"teldrive-backup:{profile.name}"]
                if args.dry_run:
                    command.append("--dry-run")
                if profile.one_file_system:
                    command.append("--one-file-system")
                for exclusion in profile.excludes:
                    command.extend(["--exclude", exclusion])
                # A broad source (such as ~/.) must never traverse a known
                # TelDrive mount, even with one_file_system=false.
                for blocked in settings.blocked_sources:
                    if any(is_inside(blocked, source) for source in profile.sources):
                        command.extend(["--exclude", str(blocked)])
                command.extend(["--", *(str(source) for source in profile.sources)])
                with exclusive_backup_lock():
                    preflight_sources(settings, profile)
                    run_restic(settings, command)
                print("Dry run complete; no snapshot written." if args.dry_run else f"Backup complete: {profile.name}")
            elif args.command == "snapshots":
                snapshots = snapshot_list(settings, profile)
                for s in sorted(snapshots, key=lambda x: x.get("time", ""), reverse=True):
                    print(f"{s.get('time', 'unknown time')}  {s['id'][:12]}  {', '.join(s.get('paths', []))}")
                if not snapshots:
                    print("No snapshots for this profile.")
            elif args.command == "restore":
                target = restore_target(args.target, settings, profile)
                snapshot_id = resolve_snapshot(snapshot_list(settings, profile), args.snapshot)
                with exclusive_backup_lock():
                    target = restore_target(args.target, settings, profile)
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    print(f"Restoring {snapshot_id} to {target}. Existing source files are untouched.")
                    run_restic(settings, ["restore", snapshot_id, "--target", str(target), "--overwrite", "never"])
                print(f"Restore complete: {target}")
        return 0
    except (BackupError, OSError, EOFError, tomllib.TOMLDecodeError, UnicodeError) as exc:
        print(f"teldrive-backup: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nteldrive-backup: interrupted; existing snapshots are retained", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
