"""Opt-in real Restic + custom rclone tests, using ONLY isolated local storage.

Run with restic on PATH and TELDRIVE_TEST_RCLONE=/path/to/rclone-teldrive.
No user's rclone config, TelDrive server or existing repository is accessed.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import teldrive_backup as backup


@unittest.skipUnless(shutil.which("restic") and os.environ.get("TELDRIVE_TEST_RCLONE"),
                     "requires restic and explicit TELDRIVE_TEST_RCLONE (local transport only)")
class LocalTransportTests(unittest.TestCase):
    def test_encrypted_transport_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix="teldrive-transport-") as temp:
            root = Path(temp)
            program = Path(os.environ["TELDRIVE_TEST_RCLONE"]).resolve()
            # Exercise Restic's command-line parsing of a binary path with spaces.
            binary = root / "custom rclone"
            binary.symlink_to(program)
            source = root / "selected"
            source.mkdir()
            document = source / "document.txt"
            document.write_text("original contents\n")
            (source / "excluded").write_text("not selected\n")
            other = root / "other"
            other.mkdir()
            (other / "private").write_text("other profile\n")
            password = root / "key"
            config = root / "config.toml"
            config.write_text(
                '[repository]\nurl="rclone:encrypted:repository"\n'
                f'rclone_program={json.dumps(str(binary))}\n'
                f'password_file={json.dumps(str(password))}\n'
                '[[profiles]]\nname="selected"\n'
                f'sources=[{json.dumps(str(source))}]\nexcludes=["excluded"]\n'
                '[[profiles]]\nname="other"\n'
                f'sources=[{json.dumps(str(other))}]\n'
            )
            # Explicit isolated configuration; crypt wraps a local backend.
            rclone_config = root / "rclone.conf"
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("RESTIC_", "RCLONE_", "XDG_"))}
            env.update(HOME=str(root), RCLONE_CONFIG=str(rclone_config),
                       XDG_STATE_HOME=str(root / "state"), RESTIC_CACHE_DIR=str(root / "cache"))
            obscured = subprocess.run([str(program), "obscure", "-"], input="local disposable crypt key\n",
                                      env=env, text=True, capture_output=True, check=True).stdout.strip()
            rclone_config.write_text('[encrypted]\ntype = crypt\n'
                                     f'remote = {root / "storage"}\npassword = {obscured}\n')
            rclone_config.chmod(0o600)
            summaries = []
            actual_run = backup.run_restic

            def observed_run(settings, args, *, capture=False):
                if args[0] == "backup":
                    output = actual_run(settings, ["--json", *args], capture=True)
                    summaries.extend(json.loads(line) for line in output.splitlines()
                                     if json.loads(line).get("message_type") == "summary")
                    return output
                return actual_run(settings, args, capture=capture)

            def invoke(*args, expected=0):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                    code = backup.main(["--config", str(config), *args])
                self.assertEqual(code, expected, err.getvalue())

            with patch.dict(os.environ, env, clear=True), patch.object(backup, "run_restic", side_effect=observed_run):
                invoke("keygen")
                invoke("init")
                invoke("init", expected=1)  # Never replace an existing repository.
                invoke("run", "selected")
                settings = backup.load_settings(config)
                profile = settings.profiles["selected"]
                original = backup.snapshot_list(settings, profile)[0]["id"]
                invoke("run", "selected")
                # Ancestor directory metadata may change as the local test
                # repository/cache grows, but unchanged file data is not sent.
                self.assertEqual(summaries[-1]["data_blobs"], 0)
                self.assertEqual(summaries[-1]["files_unmodified"], 1)
                invoke("run", "selected", "--dry-run")
                self.assertEqual(len(backup.snapshot_list(settings, profile)), 2)
                document.write_text("changed contents\n")
                invoke("run", "selected")
                invoke("run", "other")
                self.assertEqual(len(backup.snapshot_list(settings, profile)), 3)
                other_snapshot = backup.snapshot_list(settings, settings.profiles["other"])[0]["id"]
                invoke("restore", "selected", other_snapshot, "--target", str(root / "wrong"), expected=1)
                self.assertFalse((root / "wrong").exists())
                for snapshot, expected in [(original, "original contents\n"), ("latest", "changed contents\n")]:
                    target = root / snapshot
                    invoke("restore", "selected", snapshot, "--target", str(target))
                    files = [p for p in target.rglob("*") if p.is_file()]
                    self.assertEqual(len(files), 1)
                    self.assertEqual(files[0].read_text(), expected)
                    invoke("restore", "selected", snapshot, "--target", str(target), expected=1)
                source.rename(root / "disconnected")
                invoke("run", "selected", expected=1)
                self.assertEqual(len(backup.snapshot_list(settings, profile)), 3)
                actual_run(settings, ["check", "--read-data"])


if __name__ == "__main__":
    unittest.main()
