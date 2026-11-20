"""Offline Phase 1 tests: never contact TelDrive or a real Restic repository."""

import contextlib
import io
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import teldrive_backup as backup

GOOD_ID = "a" * 64
OLDER_ID = "b" * 64
OTHER_ID = "c" * 64


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.documents = self.root / "My Documents"
        self.projects = self.root / "projects"
        self.documents.mkdir()
        self.projects.mkdir()
        self.bin = self.root / "rclone-teldrive"
        self.bin.write_text("#!/bin/sh\nexit 0\n")
        self.bin.chmod(0o755)
        self.password = self.root / "restic-password"
        self.config = self.root / "config.toml"
        self.write_config()

    def write_config(self, *, source=None, extra="", repository=None):
        source = self.documents if source is None else source
        repository = repository or "rclone:teldrive-crypt:backups/cachyos"
        self.config.write_text(
            '[repository]\n'
            f'url = "{repository}"\n'
            f'rclone_program = "{self.bin}"\n'
            f'password_file = "{self.password}"\n'
            'bandwidth_limit = "1M"\n'
            '\n[[profiles]]\nname = "documents"\n'
            f'sources = ["{source}"]\n'
            'excludes = ["**/node_modules/**"]\n'
            'one_file_system = true\n'
            + extra
        )

    def load(self):
        return backup.load_settings(self.config)

    def with_key(self):
        backup.generate_password(self.password)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = backup.main(["--config", str(self.config), *args])
        return result, out.getvalue(), err.getvalue()

    def test_valid_configuration_expands_paths(self):
        s = self.load()
        self.assertEqual(s.bandwidth_limit, "1M")
        self.assertEqual(s.profiles["documents"].sources, (self.documents,))
        self.assertTrue(s.profiles["documents"].one_file_system)

    def test_dirs_add_new_profile_preserves_settings_and_private_original(self):
        original = self.config.read_bytes()
        before = self.load()
        with patch.object(backup, "run_restic") as run:
            code, _, err = self.invoke("dirs", "add", "projects", str(self.projects), "--yes")
        self.assertEqual(code, 0, err)
        run.assert_not_called()
        after = self.load()
        self.assertEqual(after.repository, before.repository)
        self.assertEqual(after.password_file, before.password_file)
        self.assertEqual(after.profiles["documents"], before.profiles["documents"])
        self.assertEqual(after.profiles["projects"].sources, (self.projects,))
        copies = list(self.root.glob("config.toml.before-dirs-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), original)
        self.assertEqual(copies[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_dirs_add_and_remove_source_in_existing_profile(self):
        self.assertEqual(self.invoke("dirs", "add", "documents", str(self.projects), "--yes")[0], 0)
        self.assertEqual(len(self.load().profiles["documents"].sources), 2)
        self.assertEqual(self.invoke("dirs", "remove", "documents", str(self.projects), "--yes")[0], 0)
        self.assertEqual(self.load().profiles["documents"].sources, (self.documents,))
        self.assertTrue(self.projects.is_dir())

    def test_dirs_invalid_additions_preserve_config(self):
        original = self.config.read_bytes()
        for profile, source in [("bad name", self.projects), ("documents", self.documents),
                                ("documents", self.documents / "nested"), ("new", self.root / "missing")]:
            with self.subTest(profile=profile, source=source):
                self.assertEqual(self.invoke("dirs", "add", profile, str(source), "--yes")[0], 1)
                self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".config-edit-*")), [])

    def test_dirs_remove_final_profile_refused(self):
        original = self.config.read_bytes()
        code, _, err = self.invoke("dirs", "remove", "documents", str(self.documents), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("last profile", err)
        self.assertEqual(self.config.read_bytes(), original)

    def test_dirs_remove_profile_with_missing_source_retains_history_and_data(self):
        self.write_config(extra=f'\n[[profiles]]\nname="projects"\nsources=["{self.projects}"]\n')
        self.projects.rmdir()
        with patch.object(backup, "run_restic") as run:
            code, out, err = self.invoke("dirs", "remove", "projects", str(self.projects), "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("saved history remains", out)
        self.assertNotIn("projects", self.load().profiles)
        run.assert_not_called()

    def test_dirs_confirmation_cancel_and_concurrent_edit(self):
        original = self.config.read_bytes()
        with patch.object(backup.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="n"):
            self.assertEqual(self.invoke("dirs", "add", "projects", str(self.projects))[0], 0)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.root.glob("config.toml.before-dirs-*")), [])
        def concurrent_edit(prompt):
            self.config.write_bytes(original + b"\n# concurrent edit\n")
            return "y"
        with patch.object(backup.sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=concurrent_edit):
            code, _, err = self.invoke("dirs", "add", "projects", str(self.projects))
        self.assertEqual(code, 1)
        self.assertIn("config changed", err)
        self.assertEqual(self.config.read_bytes(), original + b"\n# concurrent edit\n")

    def test_dirs_refuses_symlink_config(self):
        link = self.root / "link.toml"
        link.symlink_to(self.config)
        original = self.config.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            code = backup.main(["--config", str(link), "dirs", "add", "projects", str(self.projects), "--yes"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), original)

    def test_dirs_interactive_menu(self):
        with (patch.object(backup.sys.stdin, "isatty", return_value=True),
              patch("builtins.input", side_effect=["a", "projects", str(self.projects), "y"])):
            code, _, err = self.invoke("dirs")
        self.assertEqual(code, 0, err)
        self.assertIn("projects", self.load().profiles)

    def test_dirs_roundtrips_special_paths(self):
        special = self.root / 'quote"-backslash\\-日本語'
        special.mkdir()
        code, _, err = self.invoke("dirs", "add", "special", str(special), "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.load().profiles["special"].sources, (special,))

    def test_dirs_refuses_remote_mount_addition(self):
        original = self.config.read_bytes()
        with patch.object(backup, "mounted_rclone_paths", return_value=(self.projects,)):
            code, _, _ = self.invoke("dirs", "add", "projects", str(self.projects), "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_bytes(), original)

    def test_browse_mounts_only_new_local_target(self):
        target = self.root / "browse"
        with patch.object(backup, "run_restic") as run:
            code, _, err = self.invoke("browse", "--target", str(target))
        self.assertEqual(code, 0, err)
        self.assertTrue(target.is_dir())
        self.assertEqual(run.call_args.args[1], ["mount", str(target)])

    def test_browse_refuses_existing_data_and_sources(self):
        for target in (self.projects, self.documents):
            (target / "keep").write_text("existing data")
            with patch.object(backup, "run_restic") as run:
                code, _, _ = self.invoke("browse", "--target", str(target))
            self.assertEqual(code, 1)
            run.assert_not_called()
            self.assertEqual((target / "keep").read_text(), "existing data")

    def test_browse_refuses_already_mounted_empty_target(self):
        target = self.root / "browse"
        target.mkdir()
        with patch.object(backup.os.path, "ismount", return_value=True), patch.object(backup, "run_restic") as run:
            code, _, err = self.invoke("browse", "--target", str(target))
        self.assertEqual(code, 1)
        self.assertIn("already mounted", err)
        run.assert_not_called()

    def test_invalid_repository_url_rejected(self):
        self.write_config(repository="teldrive-crypt:")
        with self.assertRaisesRegex(backup.BackupError, "repository.url"):
            self.load()

    def test_duplicate_profile_names_rejected(self):
        self.write_config(extra=f'\n[[profiles]]\nname = "documents"\nsources = ["{self.projects}"]\n')
        with self.assertRaisesRegex(backup.BackupError, "duplicate profile"):
            self.load()

    def test_nested_source_dirs_rejected(self):
        self.write_config()
        text = self.config.read_text().replace(
            f'sources = ["{self.documents}"]',
            f'sources = ["{self.documents}", "{self.documents}/nested"]'
        )
        self.config.write_text(text)
        with self.assertRaisesRegex(backup.BackupError, "overlapping"):
            self.load()

    def test_unknown_configuration_keys_rejected(self):
        self.config.write_text(self.config.read_text() + '\nunsupported = true\n')
        with self.assertRaisesRegex(backup.BackupError, "unknown"):
            self.load()

    def test_profiles_can_be_listed_without_restic(self):
        code, out, err = self.invoke("profiles")
        self.assertEqual(code, 0, err)
        self.assertIn("documents:", out)
        self.assertIn(str(self.documents), out)

    def test_relative_config_path_is_accepted(self):
        relative = os.path.relpath(self.config, Path.cwd())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = backup.main(["--config", relative, "profiles"])
        self.assertEqual(code, 0)
        self.assertIn("documents", output.getvalue())

    def test_password_generated_with_private_permissions_and_not_printed(self):
        result, out, err = self.invoke("keygen")
        self.assertEqual(result, 0, err)
        self.assertEqual(self.password.stat().st_mode & 0o777, 0o600)
        self.assertGreater(len(self.password.read_text().strip()), 40)
        self.assertNotIn(self.password.read_text().strip(), out)
        code, _, err = self.invoke("keygen")
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", err)

    def test_world_readable_password_rejected(self):
        self.password.write_text("secret\n")
        self.password.chmod(0o644)
        with self.assertRaisesRegex(backup.BackupError, "mode 0600"):
            backup.preflight_password(self.password)

    def test_password_symlink_rejected(self):
        true_password = self.root / "real"
        true_password.write_text("secret")
        true_password.chmod(0o600)
        self.password.symlink_to(true_password)
        with self.assertRaisesRegex(backup.BackupError, "non-symlink"):
            backup.preflight_password(self.password)

    def test_missing_source_blocks_backup(self):
        self.with_key()
        self.write_config(source=self.root / "disconnected-drive")
        with patch.object(backup, "run_restic") as run:
            code, _, err = self.invoke("run", "documents")
        self.assertEqual(code, 1)
        self.assertIn("backup cancelled", err)
        run.assert_not_called()

    def test_blocked_mount_source_rejected(self):
        blocked = self.root / "blocked" / "subdir"
        blocked.mkdir(parents=True)
        text = self.config.read_text() + f'\n[safety]\nblocked_sources = ["{self.root / "blocked"}"]\n'
        self.config.write_text(text.replace(str(self.documents), str(blocked)))
        with self.assertRaisesRegex(backup.BackupError, "inside a blocked path"):
            backup.preflight_sources(self.load(), self.load().profiles["documents"])

    def test_run_constructs_profile_tag_and_exclusions(self):
        self.with_key()
        with patch.object(backup, "run_restic", return_value="") as run:
            code, _, err = self.invoke("run", "documents")
        self.assertEqual(code, 0, err)
        command = run.call_args.args[1]
        self.assertEqual(command[:4], ["backup", "--tag", "teldrive-backup:documents", "--one-file-system"])
        self.assertIn("**/node_modules/**", command)
        self.assertEqual(command[-2:], ["--", str(self.documents)])

    def test_dry_run_is_forwarded_without_adding_a_new_snapshot(self):
        self.with_key()
        with patch.object(backup, "run_restic") as run:
            code, _, err = self.invoke("run", "documents", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("--dry-run", run.call_args.args[1])
        self.assertIn("--json", run.call_args.args[1])
        self.assertTrue(run.call_args.kwargs["capture"])

    def test_dry_run_reports_estimated_bytes_without_printing_raw_json(self):
        payload = ('{"message_type":"summary","dry_run":true,"files_new":2,'
                   '"files_changed":1,"files_unmodified":4,"data_added":4096,'
                   '"data_added_packed":2048,"unsafe_field":"SECRET_TOKEN"}\n')
        with patch.object(backup, "run_restic", return_value=payload):
            code, output, err = self.invoke("run", "documents", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("2.0 KiB", output)
        self.assertIn("4.0 KiB", output)
        self.assertIn("2 new, 1 changed, 4 unchanged", output)
        self.assertNotIn("SECRET_TOKEN", output)

    def test_dry_run_without_summary_does_not_claim_zero_upload(self):
        with patch.object(backup, "run_restic", return_value='{"message_type":"status"}\n'):
            code, output, err = self.invoke("run", "documents", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("estimate unknown", output)

    def test_required_mount_prevents_empty_disconnected_source_backup(self):
        mount = self.root / "external"
        source = mount / "documents"
        source.mkdir(parents=True)
        self.write_config(source=source)
        self.config.write_text(self.config.read_text() + f'required_mounts = ["{mount}"]\n')
        with (patch.object(backup, "mounted_rclone_paths", return_value=()),
              patch.object(backup, "mounted_directories", return_value=frozenset({Path("/"), self.root})),
              patch.object(backup, "run_restic") as run):
            code, _, err = self.invoke("run", "documents")
        self.assertEqual(code, 1)
        self.assertIn("required filesystem is not mounted", err)
        run.assert_not_called()

        with (patch.object(backup, "mounted_rclone_paths", return_value=()),
              patch.object(backup, "mounted_directories", return_value=frozenset({mount})),
              patch.object(backup, "run_restic") as run):
            code, _, err = self.invoke("run", "documents")
        self.assertEqual(code, 0, err)
        run.assert_called_once()

    def test_required_mount_must_contain_source_and_cannot_be_root(self):
        for mount in (self.root / "unrelated", Path("/")):
            with self.subTest(mount=mount):
                self.write_config()
                self.config.write_text(self.config.read_text() + f'required_mounts = ["{mount}"]\n')
                with self.assertRaisesRegex(backup.BackupError, "required mount"):
                    self.load()

    def test_mountinfo_parser_includes_bind_mounts(self):
        table = "36 25 0:32 / /mnt/USB\\040Drive rw - ext4 /dev/sdb1 rw\n"
        with patch.object(Path, "read_text", return_value=table):
            self.assertEqual(backup.mounted_directories(), frozenset({Path("/mnt/USB Drive")}))

    def test_automount_trigger_is_not_considered_a_connected_disk(self):
        table = "36 25 0:32 / /mnt/USB rw - autofs systemd-1 rw\n"
        with patch.object(Path, "read_text", return_value=table):
            self.assertEqual(backup.mounted_directories(), frozenset())

    def test_failure_categories_do_not_reveal_child_text(self):
        for raw, expected in (("no such host: secret.invalid", "network"),
                              ("wrong password: secret-value", "password"),
                              ("repository is already locked by secret", "lock"),
                              ("custom SECRET_TOKEN", "unclassified")):
            with self.subTest(raw=raw):
                reason = backup.safe_error_reason(raw, 1)
                self.assertIn(expected, reason)
                self.assertNotIn("SECRET_TOKEN", reason)
                self.assertNotIn("secret-value", reason)
        self.assertIn("incomplete", backup.safe_error_reason("token=secret", 3))

    def test_broad_source_excludes_nested_teldrive_mount(self):
        self.with_key()
        self.write_config(source=self.root)
        blocked = self.root / "Mounts" / "teldrive-crypt"
        self.config.write_text(self.config.read_text() +
                               f'\n[safety]\nblocked_sources = ["{blocked}"]\n')
        with patch.object(backup, "run_restic") as run:
            code, _, err = self.invoke("run", "documents")
        self.assertEqual(code, 0, err)
        command = run.call_args.args[1]
        self.assertIn(str(blocked), command)
        self.assertIn("--exclude", command)

    def test_ambient_password_does_not_override_configured_file(self):
        with patch.dict(os.environ, {"RESTIC_PASSWORD": "wrong", "RESTIC_PASSWORD_COMMAND": "echo wrong"}):
            env = backup.restic_env(self.load())
        self.assertNotIn("RESTIC_PASSWORD", env)
        self.assertNotIn("RESTIC_PASSWORD_COMMAND", env)
        self.assertEqual(env["RESTIC_PASSWORD_FILE"], str(self.password))

    def test_init_uses_custom_rclone_and_bandwidth_environment(self):
        self.with_key()
        with (patch.object(backup.shutil, "which", return_value="/usr/bin/restic"),
              patch.object(backup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")) as run):
            code, out, err = self.invoke("init")
        self.assertEqual(code, 0, err)
        args, kwargs = run.call_args
        self.assertIn(f"rclone.program={self.bin}", args[0])
        self.assertEqual(args[0][-1], "init")
        self.assertEqual(kwargs["env"]["RESTIC_PASSWORD_FILE"], str(self.password))
        self.assertEqual(kwargs["env"]["RCLONE_BWLIMIT"], "1M")
        self.assertIn("initialized", out)

    def test_subprocess_failure_is_reported(self):
        self.with_key()
        with (patch.object(backup.shutil, "which", return_value="/usr/bin/restic"),
              patch.object(backup.subprocess, "run", return_value=subprocess.CompletedProcess([], 3))):
            code, _, err = self.invoke("init")
        self.assertEqual(code, 1)
        self.assertIn("code 3", err)

    def test_json_snapshots_are_filtered_by_profile(self):
        s = self.load()
        data = [
            {"id": GOOD_ID, "tags": ["teldrive-backup:documents"], "time": "2026-11-20T00:00:00Z"},
            {"id": OTHER_ID, "tags": ["teldrive-backup:other"], "time": "2026-11-21T00:00:00Z"},
        ]
        with patch.object(backup, "run_restic", return_value=__import__("json").dumps(data)):
            self.assertEqual([x["id"] for x in backup.snapshot_list(s, s.profiles["documents"])], [GOOD_ID])

    def test_invalid_snapshot_json_rejected(self):
        s = self.load()
        with (patch.object(backup, "run_restic", return_value="not-json"),
              self.assertRaisesRegex(backup.BackupError, "invalid snapshot JSON")):
            backup.snapshot_list(s, s.profiles["documents"])

    def test_snapshot_prefix_lookup_and_ambiguity(self):
        self.assertEqual(backup.resolve_snapshot([{"id": GOOD_ID}], "aaaaaaaa"), GOOD_ID)
        with self.assertRaisesRegex(backup.BackupError, "ambiguous"):
            backup.resolve_snapshot([{"id": GOOD_ID}, {"id": "a" * 63 + "b"}], "aaaaaaaa")
        with self.assertRaisesRegex(backup.BackupError, "at least 8"):
            backup.resolve_snapshot([{"id": GOOD_ID}], "-rf")

    def test_restore_latest_resolves_snapshot_and_uses_new_target(self):
        self.with_key()
        restore_dir = self.root / "restore-output"
        docs = [
            {"id": OLDER_ID, "time": "2026-11-18T00:00:00Z"},
            {"id": GOOD_ID, "time": "2026-11-20T00:00:00Z"},
        ]
        with patch.object(backup, "snapshot_list", return_value=docs), patch.object(backup, "run_restic") as run:
            code, out, err = self.invoke("restore", "documents", "latest", "--target", str(restore_dir))
        self.assertEqual(code, 0, err)
        self.assertTrue(restore_dir.is_dir())
        self.assertEqual(run.call_args.args[1], ["restore", GOOD_ID, "--target", str(restore_dir), "--overwrite", "never"])
        self.assertIn("Restore complete", out)

    def test_restore_refuses_nonempty_destination(self):
        self.with_key()
        target = self.root / "restore-output"
        target.mkdir()
        (target / "important.txt").write_text("keep me")
        with patch.object(backup, "snapshot_list") as snaps, patch.object(backup, "run_restic") as run:
            code, _, err = self.invoke("restore", "documents", "latest", "--target", str(target))
        self.assertEqual(code, 1)
        self.assertIn("new or empty", err)
        self.assertEqual((target / "important.txt").read_text(), "keep me")
        snaps.assert_not_called()
        run.assert_not_called()

    def test_restore_refuses_source_destination(self):
        self.with_key()
        with patch.object(backup, "run_restic") as run:
            code, _, err = self.invoke("restore", "documents", "latest", "--target", str(self.documents / "recover"))
        self.assertEqual(code, 1)
        self.assertIn("outside", err)
        run.assert_not_called()

    def test_restore_refuses_symlink_destination(self):
        target = self.root / "shortcut"
        target.symlink_to(self.root / "other", target_is_directory=True)
        with self.assertRaisesRegex(backup.BackupError, "symlink"):
            backup.restore_target(str(target), self.load(), self.load().profiles["documents"])

    def test_lock_refuses_second_mutating_operation(self):
        state = self.root / "state"
        with (patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}),
              backup.exclusive_backup_lock(),
              self.assertRaisesRegex(backup.BackupError, "already running"),
              backup.exclusive_backup_lock()):
            pass

    def test_config_does_not_hide_password_symlink(self):
        real = self.root / "real-key"
        real.write_text("secret")
        real.chmod(0o600)
        self.password.symlink_to(real)
        with self.assertRaises(backup.BackupError):
            backup.preflight_password(self.load().password_file)

    def test_inline_remote_credentials_rejected(self):
        self.write_config(repository="rclone::teldrive,api_key=secret:path")
        with self.assertRaises(backup.BackupError):
            self.load()

    def test_restore_rechecks_destination_after_snapshot_lookup(self):
        target = self.root / "restore-race"
        def snapshots(*args):
            target.mkdir()
            (target / "keep").write_text("existing data")
            return [{"id": GOOD_ID}]
        with patch.object(backup, "snapshot_list", side_effect=snapshots), patch.object(backup, "run_restic") as run:
            code, _, _ = self.invoke("restore", "documents", "latest", "--target", str(target))
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertEqual((target / "keep").read_text(), "existing data")

    def test_restore_uses_never_overwrite(self):
        with patch.object(backup, "snapshot_list", return_value=[{"id": GOOD_ID}]), patch.object(backup, "run_restic") as run:
            code, _, err = self.invoke("restore", "documents", "latest", "--target", str(self.root / "new"))
        self.assertEqual(code, 0, err)
        self.assertIn("--overwrite", run.call_args.args[1])
        self.assertEqual(run.call_args.args[1][-1], "never")

    def test_restore_refuses_symlink_parent(self):
        link = self.root / "linked-parent"
        link.symlink_to(self.projects, target_is_directory=True)
        with self.assertRaisesRegex(backup.BackupError, "symlink"):
            backup.restore_target(str(link / "new"), self.load(), self.load().profiles["documents"])

    def test_snapshot_ids_from_remote_must_be_full_hex(self):
        settings = self.load()
        with (patch.object(backup, "run_restic", return_value='[{"id":"--unsafe","tags":["teldrive-backup:documents"]}]'),
              self.assertRaises(backup.BackupError)):
            backup.snapshot_list(settings, settings.profiles["documents"])

    def test_remote_error_details_are_not_logged(self):
        self.with_key()
        with patch.object(backup.shutil, "which", return_value="/usr/bin/restic"), patch.object(backup.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="Authorization: secret-token")):
            code, out, err = self.invoke("snapshots", "documents")
        self.assertEqual(code, 1)
        self.assertNotIn("secret-token", out + err)

    def test_unlisted_rclone_mount_is_rejected_even_for_broad_source(self):
        for source, mount in [(self.documents, self.documents / "cloud"),
                              (self.documents, self.root),
                              (self.documents, self.documents)]:
            with (self.subTest(source=source, mount=mount),
                  patch.object(backup, "mounted_rclone_paths", return_value=(mount,)),
                  self.assertRaisesRegex(backup.BackupError, "active rclone")):
                backup.preflight_sources(self.load(), self.load().profiles["documents"])

    def test_mount_table_escaped_paths_are_decoded(self):
        table = "36 25 0:32 / /mnt/cloud\\040files rw - fuse.rclone alias: rw\n"
        with patch.object(Path, "read_text", return_value=table):
            self.assertEqual(backup.mounted_rclone_paths(), (Path("/mnt/cloud files"),))

    def test_restic_browse_mount_is_also_blocked_as_backup_source(self):
        table = "36 25 0:32 / /mnt/backups ro - fuse restic:123456 ro\n"
        with patch.object(Path, "read_text", return_value=table):
            self.assertEqual(backup.mounted_rclone_paths(), (Path("/mnt/backups"),))

    def test_restore_refuses_other_profile_sources(self):
        self.write_config(extra=f'\n[[profiles]]\nname="projects"\nsources=["{self.projects}"]\n')
        settings = self.load()
        with self.assertRaisesRegex(backup.BackupError, "outside"):
            backup.restore_target(str(self.projects / "restore"), settings, settings.profiles["documents"])

    def test_failure_and_interruption_do_not_run_cleanup_commands(self):
        for failure, expected in [(backup.BackupError("failed"), 1), (KeyboardInterrupt(), 130)]:
            with self.subTest(failure=type(failure).__name__), patch.object(backup, "run_restic", side_effect=failure) as run:
                code, _, _ = self.invoke("run", "documents")
                self.assertEqual(code, expected)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[1][0], "backup")

    def test_dangerous_repository_paths_rejected(self):
        for repository in ["rclone:remote:../old", "rclone:remote:/", "rclone:remote:a/../b", "rclone:remote:."]:
            with self.subTest(repository=repository):
                self.write_config(repository=repository)
                with self.assertRaises(backup.BackupError):
                    self.load()

    def test_rclone_executable_path_with_spaces_is_quoted(self):
        binary = self.root / "custom rclone"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        self.config.write_text(self.config.read_text().replace(str(self.bin), str(binary)))
        self.with_key()
        with patch.object(backup.shutil, "which", return_value="/usr/bin/restic"):
            command = backup.restic_command(self.load())
        value = command[2].removeprefix("rclone.program=")
        self.assertEqual(shlex.split(value), [str(binary)])


if __name__ == "__main__":
    unittest.main()
