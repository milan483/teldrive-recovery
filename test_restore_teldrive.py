"""Non-destructive recovery-kit validation tests; no containers are started."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class RestoreChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kit = self.root / "kit"
        self.home = self.root / "home"
        self.home.mkdir()
        self.kit.mkdir()
        shutil.copyfile(Path(__file__).with_name("restore-teldrive.sh"), self.kit / "restore.sh")
        files = [
            "config/compose.yaml", "config/.env", "config/rclone.conf",
            "config/rclone-crypt-recovery.txt", "database/teldrive.sql",
            "bin/rclone-teldrive", "systemd/teldrive-server.service",
            "systemd/teldrive-mount.service",
        ]
        manifest = []
        for name in files:
            path = self.kit / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test fixture\n")
            manifest.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n")
        (self.kit / "SHA256SUMS").write_text("".join(manifest))
        tools = self.root / "tools"
        tools.mkdir()
        for name, body in {"uname": 'echo "${1/-s/Linux}"', "pacman": "exit 0"}.items():
            # uname must be independent of the test machine architecture.
            if name == "uname":
                body = 'if [[ $1 == -s ]]; then echo Linux; else echo x86_64; fi'
            path = tools / name
            path.write_text("#!/bin/bash\n" + body + "\n")
            path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{tools}:{os.environ['PATH']}")

    def run_check(self):
        return subprocess.run(
            ["bash", str(self.kit / "restore.sh"), "--check"],
            env=self.env, text=True, capture_output=True, check=False,
        )

    def test_valid_kit_has_no_side_effects(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No changes made", result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_corrupted_file_is_rejected(self):
        (self.kit / "config/.env").write_text("corruption")
        self.assertNotEqual(self.run_check().returncode, 0)

    def test_missing_file_is_rejected(self):
        (self.kit / "database/teldrive.sql").unlink()
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or empty", result.stderr)

    def test_existing_config_is_preserved(self):
        config = self.home / ".config/rclone/rclone.conf"
        config.parent.mkdir(parents=True)
        config.write_text("existing")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual(config.read_text(), "existing")

    def test_script_normalizes_short_postgres_image(self):
        script = (self.kit / "restore.sh").read_text()
        self.assertIn("docker.io/library/postgres:18-alpine", script)


if __name__ == "__main__":
    unittest.main()
