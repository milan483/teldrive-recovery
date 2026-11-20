#!/usr/bin/env python3
"""Opt-in Restic backups through the rclone TelDrive backend.

Profile snapshots, safe restore, and explicitly enabled user-systemd timers.
This tool never prunes snapshots or automatically enables new schedules.
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
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = "~/.config/teldrive-backup/config.toml"
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
BANDWIDTH_RE = re.compile(r"^[0-9]+(?:[KMGTP]|[kmgpt])?(?::[0-9]+(?:[KMGTP]|[kmgpt])?)?$")
SCHEDULES = {"hourly", "daily", "weekly"}
MANAGED_UNIT_MARKER = "# Managed by teldrive-backup schedule.\n"


class BackupError(Exception):
    """An expected configuration, preflight, or backup failure."""


class ResticFailure(BackupError):
    """Sanitized Restic failure; only known network failures are retried."""

    def __init__(self, reason: str, returncode: int):
        super().__init__(f"restic exited with code {returncode}: {reason}; raw backend output withheld")
        self.retryable = reason == "network or TelDrive connection failure"


@dataclass(frozen=True)
class Profile:
    name: str
    sources: tuple[Path, ...]
    excludes: tuple[str, ...]
    one_file_system: bool
    required_mounts: tuple[Path, ...]
    schedule: str | None = None


@dataclass(frozen=True)
class Automation:
    max_attempts: int = 3
    retry_delay_seconds: int = 30
    notify_success: bool = False
    notify_failure: bool = True


@dataclass(frozen=True)
class Recovery:
    script: Path
    destination: Path
    required_mount: Path
    age_recipient: str
    schedule: str | None


@dataclass(frozen=True)
class Settings:
    repository: str
    rclone_program: str
    password_file: Path
    bandwidth_limit: str | None
    blocked_sources: tuple[Path, ...]
    profiles: dict[str, Profile]
    automation: Automation = Automation()
    recovery: Recovery | None = None


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


def optional_schedule(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in SCHEDULES:
        raise BackupError(f"{field} must be hourly, daily, or weekly")
    return value


def load_settings(config_path: Path) -> Settings:
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise BackupError(f"config not found: {config_path} (copy examples/teldrive-backup.toml)") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BackupError(f"cannot read config {config_path}: {exc}") from exc

    if not isinstance(data, dict) or set(data) - {"repository", "safety", "profiles", "automation", "recovery"}:
        raise BackupError("unknown top-level config section")
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
        if not isinstance(item, dict) or set(item) - {"name", "sources", "excludes", "one_file_system", "required_mounts", "schedule"}:
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
        required_raw = string_list(item.get("required_mounts", []), f"{name}.required_mounts")
        required_mounts = tuple(expand_path(p, f"{name}.required_mounts") for p in required_raw)
        for mount in required_mounts:
            if mount == Path("/") or not any(is_inside(source, mount) for source in sources):
                raise BackupError(f"{name}: each required mount must contain a selected source directory and cannot be /")
        profiles[name] = Profile(name, sources, excludes, one_fs, required_mounts,
                                 optional_schedule(item.get("schedule"), f"{name}.schedule"))

    automation_raw = data.get("automation", {})
    if not isinstance(automation_raw, dict) or set(automation_raw) - {"max_attempts", "retry_delay_seconds", "notify_success", "notify_failure"}:
        raise BackupError("unknown [automation] setting")
    max_attempts = automation_raw.get("max_attempts", 3)
    delay = automation_raw.get("retry_delay_seconds", 30)
    if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
        raise BackupError("automation.max_attempts must be an integer between 1 and 5")
    if type(delay) is not int or not 1 <= delay <= 300:
        raise BackupError("automation.retry_delay_seconds must be an integer between 1 and 300")
    for key, default in (("notify_success", False), ("notify_failure", True)):
        if not isinstance(automation_raw.get(key, default), bool):
            raise BackupError(f"automation.{key} must be true or false")
    automation = Automation(max_attempts, delay, automation_raw.get("notify_success", False),
                            automation_raw.get("notify_failure", True))

    recovery_raw = data.get("recovery")
    recovery = None
    if recovery_raw is not None:
        if not isinstance(recovery_raw, dict) or set(recovery_raw) - {"script", "destination", "required_mount", "age_recipient", "schedule"}:
            raise BackupError("unknown [recovery] setting")
        for key in ("script", "destination", "required_mount", "age_recipient"):
            if key not in recovery_raw:
                raise BackupError(f"recovery.{key} is required when [recovery] is configured")
        script = expand_path(recovery_raw["script"], "recovery.script", resolve=False)
        destination = expand_path(recovery_raw["destination"], "recovery.destination", resolve=False)
        mount = expand_path(recovery_raw["required_mount"], "recovery.required_mount", resolve=False)
        if mount == Path("/") or not is_inside(destination.resolve(), mount.resolve()):
            raise BackupError("recovery.destination must be inside recovery.required_mount (not /)")
        recipient = recovery_raw["age_recipient"]
        if not isinstance(recipient, str) or not re.fullmatch(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}", recipient):
            raise BackupError("recovery.age_recipient must be a single native age public key")
        recovery = Recovery(script, destination, mount, recipient,
                            optional_schedule(recovery_raw.get("schedule"), "recovery.schedule"))
    return Settings(url, program, password_file, bandwidth, blocked_paths, profiles, automation, recovery)


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


def mounted_directories() -> frozenset[Path]:
    """Return kernel-reported mountpoints, including same-device bind mounts."""
    paths = set()
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        fields = before.split()
        if len(fields) < 5 or not after.split():
            raise BackupError("cannot parse the kernel mount table")
        if after.split()[0] == "autofs":
            # An automount trigger isn't proof the underlying disk is present.
            continue
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
        paths.add(Path(mount).resolve())
    return frozenset(paths)


def preflight_sources(settings: Settings, profile: Profile) -> None:
    mounts = mounted_rclone_paths()
    if profile.required_mounts:
        active_mounts = mounted_directories()
        for required in profile.required_mounts:
            if required not in active_mounts:
                raise BackupError(f"{profile.name}: required filesystem is not mounted: {required}; backup cancelled")
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


def safe_error_reason(stderr: str, returncode: int) -> str:
    """Classify failures without reproducing potentially sensitive child output."""
    message = stderr.casefold()
    if returncode == 3:
        return "incomplete backup (one or more source files could not be saved)"
    categories = (
        (("wrong password", "incorrect password", "no key found", "password is incorrect"),
         "repository password or key mismatch"),
        (("repository does not exist", "is there a repository", "unable to open config file"),
         "repository missing or not initialized"),
        (("already initialized", "already exists"), "repository already exists"),
        (("repository is already locked", "unable to create lock", "already locked"),
         "repository lock conflict"),
        (("no space left on device", "storage quota exceeded", "insufficient storage"),
         "storage space or quota exhausted"),
        (("permission denied", "access denied", "unauthorized", "forbidden", "authentication required"),
         "permission or remote authentication failure"),
        (("connection refused", "network is unreachable", "no such host", "i/o timeout", "timed out", "tls handshake", "connection reset"),
         "network or TelDrive connection failure"),
    )
    for needles, description in categories:
        if any(needle in message for needle in needles):
            return description
    return "unclassified Restic or storage failure"


def backup_summary(payload: str) -> str:
    """Show only documented numeric fields from Restic JSON, never raw output."""
    summary = None
    for line in payload.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("message_type") == "summary":
            summary = record
    if summary is None:
        return "No structured Restic summary was available; upload estimate unknown."

    def count(key: str) -> str:
        value = summary.get(key)
        return str(value) if type(value) is int and value >= 0 else "unknown"

    def size(key: str) -> str:
        value = summary.get(key)
        if type(value) is not int or value < 0:
            return "unknown"
        amount = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if amount < 1024 or unit == "TiB":
                return f"{amount:.1f} {unit}"
            amount /= 1024
        return "unknown"

    return (f"Files: {count('files_new')} new, {count('files_changed')} changed, "
            f"{count('files_unmodified')} unchanged. "
            f"Estimated new repository data: {size('data_added_packed')} compressed "
            f"({size('data_added')} before compression). "
            "Actual network transfer can differ due to protocol overhead and retries.")


def run_restic(settings: Settings, args: list[str], *, capture: bool = False) -> str:
    command = restic_command(settings) + args
    # Backend errors may include URLs, tokens or headers. Never relay arbitrary
    # child output to the terminal or a log (including successful warnings).
    result = subprocess.run(command, env=restic_env(settings), text=True,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        reason = safe_error_reason(result.stderr or "", result.returncode)
        raise ResticFailure(reason, result.returncode)
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
        if type(value) is int:
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(literal(item) for item in value) + "]"
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        raise BackupError("unsupported configuration value")

    lines = ["# Managed by teldrive-backup dirs; manual edits are supported."]
    for section in ("repository", "safety", "automation", "recovery"):
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


def notify_desktop(title: str, message: str, *, failure: bool = False) -> None:
    """Best-effort user-bus notification. Never include backend output or paths."""
    if not (os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("DBUS_SESSION_BUS_ADDRESS")):
        return
    program = shutil.which("notify-send")
    if not program:
        return
    try:
        subprocess.run([program, "--app-name=TelDrive Backup", "--urgency=critical" if failure
                        else "--urgency=normal", title, message],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # Journal output remains the authoritative result.


def execute_backup(settings: Settings, profile: Profile, *, dry_run: bool = False,
                   scheduled: bool = False) -> None:
    command = ["backup", "--tag", f"teldrive-backup:{profile.name}"]
    if dry_run:
        command.append("--dry-run")
    if profile.one_file_system:
        command.append("--one-file-system")
    for exclusion in profile.excludes:
        command.extend(["--exclude", exclusion])
    for blocked in settings.blocked_sources:
        if any(is_inside(blocked, source) for source in profile.sources):
            command.extend(["--exclude", str(blocked)])
    command.extend(["--", *(str(source) for source in profile.sources)])

    try:
        with exclusive_backup_lock():
            preflight_sources(settings, profile)
            if dry_run:
                result = run_restic(settings, ["--json", *command], capture=True)
            else:
                attempts = settings.automation.max_attempts if scheduled else 1
                for attempt in range(1, attempts + 1):
                    preflight_sources(settings, profile)
                    try:
                        run_restic(settings, command)
                        break
                    except ResticFailure as exc:
                        if not exc.retryable or attempt == attempts:
                            raise
                        wait = min(settings.automation.retry_delay_seconds * 2 ** (attempt - 1), 300)
                        print(f"Network failure on attempt {attempt}/{attempts}; retrying in {wait}s.",
                              file=sys.stderr, flush=True)
                        time.sleep(wait)
        if dry_run:
            print(backup_summary(result))
            print("Dry run complete; no snapshot written.")
        else:
            print(f"Backup complete: {profile.name}")
            if scheduled and settings.automation.notify_success:
                notify_desktop("TelDrive backup complete", f"Profile {profile.name} saved.")
    except (BackupError, OSError):
        if scheduled and settings.automation.notify_failure:
            notify_desktop("TelDrive backup failed", f"Profile {profile.name} failed. Check the user journal.", failure=True)
        raise


def run_recovery_kit(settings: Settings) -> None:
    recovery = settings.recovery
    if recovery is None:
        raise BackupError("configure [recovery] before running an unattended recovery kit")
    if not recovery.script.is_file() or recovery.script.is_symlink():
        raise BackupError("recovery.script must be a regular non-symlink file")
    require_program("bash", "bash")
    with exclusive_backup_lock():
        args = ["bash", str(recovery.script), "--age-recipient", recovery.age_recipient,
                "--require-mount", str(recovery.required_mount), str(recovery.destination)]
        try:
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    check=False, text=True)
        except OSError as exc:
            raise BackupError("cannot start unattended recovery kit") from exc
        if result.returncode != 0:
            if settings.automation.notify_failure:
                notify_desktop("TelDrive recovery kit failed", "Check the user journal.", failure=True)
            raise BackupError(f"recovery kit failed (exit {result.returncode}); raw output withheld")
    print("Encrypted recovery kit complete. Keep the offline age identity safe.")
    if settings.automation.notify_success:
        notify_desktop("TelDrive recovery kit complete", "Encrypted recovery archive saved.")


def systemd_argument(raw: str) -> str:
    """Quote an ExecStart argument using systemd syntax, including %% specifiers."""
    if not raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise BackupError("unsupported control character in systemd argument")
    return '"' + raw.replace("%", "%%").replace("$", "$$").replace("\\", "\\\\").replace('"', '\\"') + '"'


def user_unit_dir() -> Path:
    return Path.home() / ".config/systemd/user"


def managed_unit_write(path: Path, content: str) -> None:
    if path.is_symlink():
        raise BackupError(f"refusing to overwrite a symlinked unit: {path}")
    if path.exists():
        if not path.is_file() or not path.read_text(encoding="utf-8").startswith(MANAGED_UNIT_MARKER):
            raise BackupError(f"refusing to overwrite an unmanaged unit: {path}")
        if path.read_text(encoding="utf-8") == content:
            return
    fd, raw_path = tempfile.mkstemp(prefix=".teldrive-unit-", dir=path.parent)
    candidate = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def systemctl_user(*args: str) -> None:
    command = [require_program("systemctl", "systemctl"), "--user", *args]
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            check=False, text=True)
    if result.returncode != 0:
        raise BackupError(f"systemctl --user {args[0]} failed (exit {result.returncode}); "
                          "check 'systemctl --user status' and the user journal")


def schedule_unit(settings: Settings, config_path: Path, name: str,
                  *, enable: bool, recovery_kit: bool = False) -> None:
    if recovery_kit:
        if settings.recovery is None:
            raise BackupError("configure [recovery] first")
        calendar = settings.recovery.schedule
        unit_name = "teldrive-recovery-kit"
        argv = ["recovery-kit", "run"]
    else:
        if name not in settings.profiles:
            raise BackupError(f"unknown profile: {name}")
        calendar = settings.profiles[name].schedule
        unit_name = f"teldrive-backup@{name}"
        argv = ["run", name, "--scheduled"]
    timer = f"{unit_name}.timer"
    unit_dir = user_unit_dir()
    if enable:
        if calendar is None:
            raise BackupError("set a schedule (hourly, daily, weekly) in the config before enabling")
        executable = Path.home() / ".local/bin/teldrive-backup"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise BackupError(f"install teldrive_backup.py as executable {executable} first")
        unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        service_text = (MANAGED_UNIT_MARKER + "[Unit]\nDescription=TelDrive backup: " + unit_name +
                        "\n[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=0\n"
                        "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin\nExecStart=" +
                        " ".join(systemd_argument(v) for v in [str(executable), "--config", str(config_path), *argv]) + "\n")
        timer_text = (MANAGED_UNIT_MARKER + f"[Unit]\nDescription=Schedule {unit_name}\n"
                      f"[Timer]\nOnCalendar={calendar}\nPersistent=true\n"
                      "RandomizedDelaySec=30m\nAccuracySec=5m\n"
                      f"Unit={unit_name}.service\n[Install]\nWantedBy=timers.target\n")
        # Inspect both targets before writing either to avoid partial collisions.
        for unit in (f"{unit_name}.service", timer):
            path = unit_dir / unit
            if path.is_symlink() or (path.exists() and (not path.is_file() or
                    not path.read_text(encoding="utf-8").startswith(MANAGED_UNIT_MARKER))):
                raise BackupError(f"refusing to overwrite an unmanaged unit: {path}")
        managed_unit_write(unit_dir / f"{unit_name}.service", service_text)
        managed_unit_write(unit_dir / timer, timer_text)
        systemctl_user("daemon-reload")
        systemctl_user("enable", "--now", timer)
        print(f"Enabled {timer} ({calendar}, persistent catch-up, up to 30m jitter).")
    else:
        # Refuse to touch a similarly named unit owned by another tool/user.
        path = unit_dir / timer
        if not path.is_file() or path.is_symlink() or not path.read_text(encoding="utf-8").startswith(MANAGED_UNIT_MARKER):
            raise BackupError(f"no managed timer to disable: {path}")
        systemctl_user("disable", "--now", timer)
        print(f"Disabled {timer}. Existing snapshots and unit files are retained.")


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
            p.add_argument("--scheduled", action="store_true", help="enable bounded network retries and desktop notifications")
    schedule = commands.add_parser("schedule", help="manage opt-in user-systemd timers")
    schedule_commands = schedule.add_subparsers(dest="schedule_action", required=True)
    schedule_commands.add_parser("list", help="list configured backup and recovery schedules")
    for action in ("enable", "disable"):
        p = schedule_commands.add_parser(action, help=f"{action} a profile's timer")
        p.add_argument("profile")
        schedule_commands.add_parser(f"{action}-recovery", help=f"{action} the recovery-kit timer")
    recovery_kit = commands.add_parser("recovery-kit", help="create an age-encrypted unattended recovery archive")
    recovery_kit.add_argument("action", choices=["run"])
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
        elif args.command == "schedule":
            if args.schedule_action == "list":
                for profile in settings.profiles.values():
                    print(f"{profile.name}: {profile.schedule or 'manual only'}")
                print(f"recovery-kit: {settings.recovery.schedule if settings.recovery and settings.recovery.schedule else 'manual only'}")
                print("Schedules shown are configuration values, not timer activation state.")
            elif args.schedule_action in {"enable-recovery", "disable-recovery"}:
                schedule_unit(settings, config_path, "", enable=args.schedule_action == "enable-recovery", recovery_kit=True)
            else:
                schedule_unit(settings, config_path, args.profile, enable=args.schedule_action == "enable")
        elif args.command == "recovery-kit":
            run_recovery_kit(settings)
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
                if args.dry_run and args.scheduled:
                    raise BackupError("--scheduled and --dry-run cannot be combined")
                execute_backup(settings, profile, dry_run=args.dry_run, scheduled=args.scheduled)
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
