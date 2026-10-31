import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class InstallerTests(unittest.TestCase):
    def test_install_and_uninstall_work_without_asr_tools_or_model(self):
        python3 = shutil.which("python3")
        self.assertIsNotNone(python3)
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            home = base / "home user"
            fake_bin = base / "fake bin"
            install_bin = base / "installed bin"
            home.mkdir()
            fake_bin.mkdir()
            install_bin.mkdir()
            (fake_bin / "python3").symlink_to(python3)
            ytdlp = fake_bin / "yt-dlp"
            ytdlp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            ytdlp.chmod(0o755)

            env = os.environ.copy()
            env.update(
                HOME=str(home),
                XDG_BIN_HOME=str(install_bin),
                PATH=f"{fake_bin}:/usr/bin:/bin",
            )

            installed = subprocess.run(
                ["/usr/bin/bash", str(ROOT / "install.sh")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            target = install_bin / "youtube-transcript"
            self.assertEqual(target.resolve(), (ROOT / "youtube_transcript.py").resolve())

            removed = subprocess.run(
                ["/usr/bin/bash", str(ROOT / "uninstall.sh")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
