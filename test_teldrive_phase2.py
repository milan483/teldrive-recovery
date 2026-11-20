"""Offline tests for opt-in scheduling, bounded retries and encrypted kits."""

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

import teldrive_backup as backup


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.config = self.root / "config.toml"
        self.key = "age1" + "q" * 58
        self.write_config()
        environment = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)

    def write_config(self, *, profile_schedule="daily", recovery=False, automation=""):
        recovery_text = (
            '\n[recovery]\n'
            f'script = "{self.root / "backup-teldrive.sh"}"\n'
            f'destination = "{self.root / "drive" / "kit"}"\n'
            f'required_mount = "{self.root / "drive"}"\n'
            f'age_recipient = "{self.key}"\n'
            'schedule = "weekly"\n' if recovery else ""
        )
        self.config.write_text(
            '[repository]\nurl = "rclone:crypt:backups/test"\n'
            f'password_file = "{self.root / "key"}"\n'
            + '[automation]\nmax_attempts = 3\nretry_delay_seconds = 2\n'
            + 'notify_success = false\nnotify_failure = true\n' + automation + '\n'
            + '[[profiles]]\nname = "docs"\n'
            + f'sources = ["{self.source}"]\n'
            + (f'schedule = "{profile_schedule}"\n' if profile_schedule else "")
            + recovery_text
        )

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = backup.main(["--config", str(self.config), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_configuration_is_opt_in_by_default(self):
        self.write_config(profile_schedule=None)
        settings = backup.load_settings(self.config)
        self.assertIsNone(settings.profiles["docs"].schedule)
        self.assertEqual(settings.automation.max_attempts, 3)
        self.assertIsNone(settings.recovery)
        self.assertIn("manual only", self.invoke("schedule", "list")[1])

    def test_schedule_and_retry_parameters_are_validated(self):
        self.write_config(profile_schedule="every millisecond")
        with self.assertRaisesRegex(backup.BackupError, "docs.schedule"):
            backup.load_settings(self.config)
        self.write_config(automation="max_attempts = 9\n")
        with self.assertRaises(backup.BackupError):
            backup.load_settings(self.config)
        self.write_config(automation='notify_success = "yes"\n')
        with self.assertRaises(backup.BackupError):
            backup.load_settings(self.config)

    def test_recovery_configuration_requires_public_key_and_mount(self):
        self.write_config(recovery=True)
        settings = backup.load_settings(self.config)
        self.assertEqual(settings.recovery.schedule, "weekly")
        self.config.write_text(self.config.read_text().replace(self.key, "not-a-key"))
        with self.assertRaisesRegex(backup.BackupError, "age_recipient"):
            backup.load_settings(self.config)
        self.write_config(recovery=True)
        self.config.write_text(self.config.read_text().replace('required_mount = "', 'required_mount = "/tmp/fake/'))
        with self.assertRaisesRegex(backup.BackupError, "inside"):
            backup.load_settings(self.config)

    def test_retry_only_for_recognized_network_failures(self):
        settings = backup.load_settings(self.config)
        errors = [backup.ResticFailure("network or TelDrive connection failure", 1)] * 2
        with (patch.object(backup, "run_restic", side_effect=[*errors, ""]) as run,
              patch.object(backup.time, "sleep") as sleep,
              patch.object(backup, "notify_desktop") as notify):
            backup.execute_backup(settings, settings.profiles["docs"], scheduled=True)
        self.assertEqual(run.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        notify.assert_not_called()

    def test_no_retry_on_non_network_errors(self):
        settings = backup.load_settings(self.config)
        with (patch.object(backup, "run_restic", side_effect=backup.ResticFailure("repository password or key mismatch", 1)) as run,
              patch.object(backup.time, "sleep") as sleep,
              patch.object(backup, "notify_desktop") as notify):
            with self.assertRaises(backup.ResticFailure):
                backup.execute_backup(settings, settings.profiles["docs"], scheduled=True)
        run.assert_called_once()
        sleep.assert_not_called()
        notify.assert_called_once()
        self.assertNotIn("password", str(notify.call_args))

    def test_missing_mount_does_not_trigger_retries(self):
        self.config.write_text(self.config.read_text() + f'required_mounts = ["{self.source}"]\n')
        settings = backup.load_settings(self.config)
        with (patch.object(backup, "mounted_directories", return_value=frozenset()),
              patch.object(backup, "run_restic") as run,
              patch.object(backup.time, "sleep") as sleep):
            with self.assertRaisesRegex(backup.BackupError, "not mounted"):
                backup.execute_backup(settings, settings.profiles["docs"], scheduled=True)
        run.assert_not_called()
        sleep.assert_not_called()

    def test_manual_backup_has_no_automatic_retry(self):
        settings = backup.load_settings(self.config)
        with (patch.object(backup, "run_restic", side_effect=backup.ResticFailure("network or TelDrive connection failure", 1)) as run,
              patch.object(backup.time, "sleep") as sleep):
            with self.assertRaises(backup.ResticFailure):
                backup.execute_backup(settings, settings.profiles["docs"])
        run.assert_called_once()
        sleep.assert_not_called()

    def test_schedule_writes_private_units_with_bounded_jitter(self):
        installed = self.root / ".local/bin/teldrive-backup"
        installed.parent.mkdir(parents=True)
        installed.write_text("#!/usr/bin/env python3\n")
        installed.chmod(0o700)
        settings = backup.load_settings(self.config)
        with (patch.object(backup.Path, "home", return_value=self.root),
              patch.object(backup, "systemctl_user") as systemctl):
            backup.schedule_unit(settings, self.config, "docs", enable=True)
        units = self.root / ".config/systemd/user"
        timer = (units / "teldrive-backup@docs.timer").read_text()
        service = (units / "teldrive-backup@docs.service").read_text()
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=30m", timer)
        self.assertIn("--scheduled", service)
        self.assertIn(str(self.config), service)
        self.assertTrue(service.startswith(backup.MANAGED_UNIT_MARKER))
        self.assertEqual(systemctl.call_args_list[-1].args, ("enable", "--now", "teldrive-backup@docs.timer"))
        if shutil.which("systemd-analyze"):
            validation = subprocess.run(["systemd-analyze", "verify",
                                         str(units / "teldrive-backup@docs.service"),
                                         str(units / "teldrive-backup@docs.timer")],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(validation.returncode, 0, validation.stderr)

    def test_schedule_requires_explicit_calendar_and_binary(self):
        settings = backup.load_settings(self.config)
        with patch.object(backup.Path, "home", return_value=self.root):
            with self.assertRaisesRegex(backup.BackupError, "install"):
                backup.schedule_unit(settings, self.config, "docs", enable=True)
        self.write_config(profile_schedule=None)
        installed = self.root / ".local/bin/teldrive-backup"
        installed.parent.mkdir(parents=True)
        installed.write_text("script")
        installed.chmod(0o700)
        with patch.object(backup.Path, "home", return_value=self.root):
            with self.assertRaisesRegex(backup.BackupError, "set a schedule"):
                backup.schedule_unit(backup.load_settings(self.config), self.config, "docs", enable=True)

    def test_schedule_never_overwrites_unmanaged_units(self):
        installed = self.root / ".local/bin/teldrive-backup"
        installed.parent.mkdir(parents=True)
        installed.write_text("script")
        installed.chmod(0o700)
        units = self.root / ".config/systemd/user"
        units.mkdir(parents=True)
        unowned = units / "teldrive-backup@docs.service"
        unowned.write_text("user content")
        with patch.object(backup.Path, "home", return_value=self.root):
            with self.assertRaisesRegex(backup.BackupError, "unmanaged"):
                backup.schedule_unit(backup.load_settings(self.config), self.config, "docs", enable=True)
        self.assertEqual(unowned.read_text(), "user content")
        self.assertFalse((units / "teldrive-backup@docs.timer").exists())

    def test_disable_only_targets_owned_timer_and_preserves_files(self):
        units = self.root / ".config/systemd/user"
        units.mkdir(parents=True)
        timer = units / "teldrive-backup@docs.timer"
        timer.write_text(backup.MANAGED_UNIT_MARKER + "[Timer]\nOnCalendar=daily\n")
        with (patch.object(backup.Path, "home", return_value=self.root),
              patch.object(backup, "systemctl_user") as systemctl):
            backup.schedule_unit(backup.load_settings(self.config), self.config, "docs", enable=False)
        self.assertTrue(timer.exists())
        systemctl.assert_called_once_with("disable", "--now", "teldrive-backup@docs.timer")

    def test_recovery_script_uses_public_recipient_no_secret_env(self):
        self.write_config(recovery=True)
        script = self.root / "backup-teldrive.sh"
        script.write_text("#!/bin/bash\n")
        settings = backup.load_settings(self.config)
        with patch.object(backup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            backup.run_recovery_kit(settings)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["bash", str(script)])
        self.assertIn("--age-recipient", argv)
        self.assertIn(self.key, argv)
        self.assertNotIn("--identity", argv)

    def test_recovery_timer_is_separate_and_explicit(self):
        self.write_config(recovery=True)
        installed = self.root / ".local/bin/teldrive-backup"
        installed.parent.mkdir(parents=True)
        installed.write_text("#!/usr/bin/env python3\n")
        installed.chmod(0o700)
        with (patch.object(backup.Path, "home", return_value=self.root),
              patch.object(backup, "systemctl_user") as systemctl):
            code, _, error = self.invoke("schedule", "enable-recovery")
        self.assertEqual(code, 0, error)
        units = self.root / ".config/systemd/user"
        self.assertIn("OnCalendar=weekly", (units / "teldrive-recovery-kit.timer").read_text())
        self.assertIn("recovery-kit", (units / "teldrive-recovery-kit.service").read_text())
        self.assertFalse((units / "teldrive-backup@docs.timer").exists())
        self.assertEqual(systemctl.call_args_list[-1].args, ("enable", "--now", "teldrive-recovery-kit.timer"))

    def test_recovery_failure_hides_raw_output(self):
        self.write_config(recovery=True)
        script = self.root / "backup-teldrive.sh"
        script.write_text("#!/bin/bash\n")
        with (patch.object(backup.subprocess, "run", return_value=subprocess.CompletedProcess([], 4, "TOKEN", "SECRET")),
              patch.object(backup, "notify_desktop") as notify):
            code, output, error = self.invoke("recovery-kit", "run")
        self.assertEqual(code, 1)
        self.assertNotIn("TOKEN", output + error)
        self.assertNotIn("SECRET", output + error)
        notify.assert_called_once()

    def test_systemd_escapes_percent_quotes_and_backslashes(self):
        value = backup.systemd_argument('a%$"\\path')
        self.assertEqual(value, '"a%%$$\\"\\\\path"')
        with self.assertRaises(backup.BackupError):
            backup.systemd_argument("bad\nline")

    def test_dirs_roundtrip_keeps_new_sections(self):
        self.write_config(recovery=True)
        other = self.root / "other"
        other.mkdir()
        with patch.object(backup, "mounted_rclone_paths", return_value=()):
            code, _, error = self.invoke("dirs", "add", "docs", str(other), "--yes")
        self.assertEqual(code, 0, error)
        after = backup.load_settings(self.config)
        self.assertEqual(after.profiles["docs"].schedule, "daily")
        self.assertEqual(after.recovery.age_recipient, self.key)
        self.assertEqual(after.automation.retry_delay_seconds, 2)


class RecoveryScriptTests(unittest.TestCase):
    """Fake external services; exercises packaging, not real age cryptography."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.mount = self.root / "usb"
        self.destination = self.mount / "kits"
        self.destination.mkdir(parents=True)
        self.deployment = self.home / ".local/share/teldrive"
        self.deployment.mkdir(parents=True)
        (self.deployment / "compose.yaml").write_text(
            "services:\n  postgres:\n    image: postgres:18\n  teldrive:\n    image: teldrive:latest\n")
        (self.deployment / ".env").write_text("SERVER_SECRET=test-value\n")
        (self.deployment / "rclone-crypt-recovery.txt").write_text("test-crypt-secret\n")
        rclone = self.home / ".config/rclone/rclone.conf"
        rclone.parent.mkdir(parents=True)
        rclone.write_text("[crypt]\ntype = crypt\n")
        binary = self.home / ".local/bin/rclone-teldrive"
        binary.parent.mkdir(parents=True)
        self.make_script(binary, '#!/bin/sh\necho "rclone test"\n')
        service_dir = self.home / ".config/systemd/user"
        service_dir.mkdir(parents=True)
        for name in ("teldrive-server.service", "teldrive-mount.service"):
            (service_dir / name).write_text("[Service]\n")
        self.bin = self.root / "fakebin"
        self.bin.mkdir()
        self.make_script(self.bin / "mountpoint", "#!/bin/sh\nexit 0\n")
        self.make_script(self.bin / "age", "#!/bin/sh\ncat\n")
        self.make_script(self.bin / "podman", '''#!/bin/sh
case "$*" in
  *RepoDigests*) echo 'example.test/image@sha256:abcd1234' ;;
  *'{{.Image}}'*) echo 'sha256:abcd1234' ;;
  'compose '* ) echo '-- test db dump' ;;
  *) echo 'container image sha256:abcd1234' ;;
esac
''')
        self.script = Path(__file__).resolve().parent / "backup-teldrive.sh"
        self.env = os.environ.copy()
        self.env.update({"HOME": str(self.home), "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]})
        self.recipient = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"

    @staticmethod
    def make_script(path, content):
        path.write_text(content)
        path.chmod(0o700)

    def invoke(self, *, mount=None, destination=None):
        return subprocess.run(["bash", str(self.script), "--age-recipient", self.recipient,
                               "--require-mount", str(mount or self.mount), str(destination or self.destination)],
                              env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_unattended_creates_only_completed_archive_and_private_staging(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = list(self.destination.glob("teldrive-recovery-*.tar.age"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.destination.glob(".teldrive-recovery-*")), [])
        self.assertEqual(list(self.deployment.glob("backup-staging.*")), [])
        # Fake 'age' streams a tar unencrypted for structure testing only.
        with tarfile.open(archives[0]) as bundle:
            names = bundle.getnames()
            self.assertIn("./config/.env", names)
            self.assertIn("./database/teldrive.sql", names)
            self.assertIn("./SHA256SUMS", names)
            self.assertIn("./restore.sh", names)
            database = bundle.extractfile("./database/teldrive.sql").read()
            self.assertIn(b"test db dump", database)

    def test_mount_failure_never_creates_archive(self):
        self.make_script(self.bin / "mountpoint", "#!/bin/sh\nexit 1\n")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not mounted", result.stderr)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_destination_must_be_inside_required_mount(self):
        other = self.root / "outside"
        other.mkdir()
        result = self.invoke(destination=other)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside", result.stderr)
        self.assertEqual(list(other.iterdir()), [])

    def test_age_failure_does_not_publish_partial(self):
        self.make_script(self.bin / "age", "#!/bin/sh\nexit 8\n")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(list(self.deployment.glob("backup-staging.*")), [])


if __name__ == "__main__":
    unittest.main()
