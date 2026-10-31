import os
import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fake_bin = self.base / "fake bin"
        self.curl_bin = self.base / "curl bin"
        self.tool_bin = self.base / "tool bin"
        self.fake_bin.mkdir()
        self.curl_bin.mkdir()
        self.tool_bin.mkdir()
        self.uv_log = self.base / "uv.log"
        self._write_executable(self.fake_bin / "uv", self.fake_uv())
        self._write_executable(self.fake_bin / "curl", self.fake_curl())

    def _write_executable(self, path, content):
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    @staticmethod
    def fake_uv():
        return """#!/bin/sh
printf '%s\\n' "$*" >> "$UV_LOG"
case "$1 $2" in
  "tool dir")
    if [ "$3" = "--bin" ]; then printf '%s\\n' "$UV_TOOL_BIN_DIR"; else printf '%s\\n' "$UV_TOOL_ROOT"; fi
    ;;
  "tool list")
    if [ -f "$UV_TOOL_BIN_DIR/.installed" ]; then
      printf '%s\\n' 'youtube-transcript-local v0.2.0' '- youtube-transcript'
    fi
    ;;
  "tool install")
    touch "$UV_TOOL_BIN_DIR/.installed"
    printf '%s\\n' '#!/bin/sh' 'exit 0' > "$UV_TOOL_BIN_DIR/youtube-transcript"
    chmod +x "$UV_TOOL_BIN_DIR/youtube-transcript"
    ;;
  "tool uninstall")
    [ "$3" = "youtube-transcript-local" ] || exit 2
    rm -f "$UV_TOOL_BIN_DIR/.installed" "$UV_TOOL_BIN_DIR/youtube-transcript"
    rm -rf "$UV_TOOL_ROOT/youtube-transcript-local"
    ;;
esac
"""

    @staticmethod
    def fake_curl():
        return """#!/bin/sh
output=
url=
while [ $# -gt 0 ]; do
  case $1 in -o) output=$2; shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac
done
case $url in
  *.sha256) printf '%s  youtube-transcript-source.zip\\n' "${FAKE_SOURCE_CHECKSUM:-0eb3e36bfb24dcd9bb1d1bece1531216b59539a8fde17ee80224af0653c92aa3}" > "$output" ;;
  *install.sh) cp "$FAKE_UV_INSTALLER" "$output" ;;
  *) printf archive > "$output" ;;
esac
"""

    def env(self):
        env = os.environ.copy()
        env.update(
            HOME=str(self.base / "home"),
            UV_TOOL_BIN_DIR=str(self.tool_bin),
            UV_TOOL_ROOT=str(self.base / "tools"),
            UV_LOG=str(self.uv_log),
            PATH=f"{self.fake_bin}:/usr/bin:/bin",
        )
        return env

    def run_script(self, name, env=None):
        return subprocess.run(
            ["/bin/bash", str(ROOT / name)],
            env=env or self.env(),
            capture_output=True,
            text=True,
            check=False,
        )

    def add_runtime_stubs(self, deno_version="2.2.0", node_version="v20.0.0"):
        self._write_executable(
            self.fake_bin / "deno",
            f"#!/bin/sh\nprintf 'deno {deno_version} (stable)\\n'\n",
        )
        self._write_executable(
            self.fake_bin / "node",
            f"#!/bin/sh\nprintf '{node_version}\\n'\n",
        )

    def test_install_provisions_deno_only_when_no_usable_runtime_exists(self):
        self.add_runtime_stubs()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Installed. Run: youtube-transcript", result.stdout)
        args = self.uv_log.read_text(encoding="utf-8")
        self.assertIn("tool install --force --with deno", args)
        self.assertTrue((self.tool_bin / "youtube-transcript").exists())

    def test_install_reuses_a_supported_deno_runtime(self):
        self.add_runtime_stubs(deno_version="2.4.1")
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("--with deno", self.uv_log.read_text(encoding="utf-8"))

    def test_installer_preserves_a_conflicting_launcher(self):
        self.add_runtime_stubs(deno_version="2.4.1")
        launcher = self.tool_bin / "youtube-transcript"
        launcher.write_text("user-owned command\n", encoding="utf-8")
        result = self.run_script("install.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("left untouched", result.stderr)
        self.assertEqual(launcher.read_text(encoding="utf-8"), "user-owned command\n")
        self.assertNotIn("tool install", self.uv_log.read_text(encoding="utf-8"))

    def test_installer_stops_when_release_checksum_does_not_match(self):
        self.add_runtime_stubs(deno_version="2.4.1")
        env = self.env()
        env["FAKE_SOURCE_CHECKSUM"] = sha256(b"different archive").hexdigest()
        result = self.run_script("install.sh", env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed its integrity check", result.stderr)
        self.assertFalse(self.uv_log.exists())

    def test_update_preserves_modified_launcher_from_the_managed_tool(self):
        self.add_runtime_stubs(deno_version="2.4.1")
        (self.tool_bin / ".installed").touch()
        launcher = self.tool_bin / "youtube-transcript"
        launcher.write_text("user-edited launcher\n", encoding="utf-8")
        result = self.run_script("install.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("left untouched", result.stderr)
        self.assertEqual(launcher.read_text(encoding="utf-8"), "user-edited launcher\n")
        self.assertNotIn("tool install", self.uv_log.read_text(encoding="utf-8"))

    def test_update_accepts_uv_managed_absolute_launcher_link(self):
        self.add_runtime_stubs(deno_version="2.4.1")
        (self.tool_bin / ".installed").touch()
        managed_bin = self.base / "tools" / "youtube-transcript-local" / "bin"
        managed_bin.mkdir(parents=True)
        executable = managed_bin / "youtube-transcript"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        launcher = self.tool_bin / "youtube-transcript"
        launcher.symlink_to(executable)
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(launcher.is_symlink())
        self.assertIn("tool install --force", self.uv_log.read_text(encoding="utf-8"))

    def test_update_rejects_external_symlink_with_same_executable_name(self):
        self.add_runtime_stubs(deno_version="2.4.1")
        (self.tool_bin / ".installed").touch()
        (self.base / "tools" / "youtube-transcript-local" / "bin").mkdir(parents=True)
        external_bin = self.base / "external bin"
        external_bin.mkdir()
        external = external_bin / "youtube-transcript"
        external.write_text("user-owned command\n", encoding="utf-8")
        launcher = self.tool_bin / "youtube-transcript"
        launcher.symlink_to(external)
        result = self.run_script("install.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("points outside this package", result.stderr)
        self.assertEqual(launcher.resolve(), external)
        self.assertNotIn("tool install", self.uv_log.read_text(encoding="utf-8"))

    def test_linux_without_glibc_227_stops_before_package_install(self):
        self.add_runtime_stubs()
        self._write_executable(
            self.fake_bin / "uname",
            "#!/bin/sh\ncase $1 in -m) printf 'x86_64\\n' ;; *) printf 'Linux\\n' ;; esac\n",
        )
        self._write_executable(self.fake_bin / "getconf", "#!/bin/sh\nprintf 'glibc 2.26\\n'\n")
        result = self.run_script("install.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("glibc 2.27 or newer", result.stderr)
        self.assertNotIn("tool install", self.uv_log.read_text(encoding="utf-8"))

    def test_installer_bootstraps_uv_without_editing_a_shell_profile(self):
        self.add_runtime_stubs(deno_version="2.4.1")
        fake_uv = self.fake_bin / "uv"
        fake_curl = self.fake_bin / "curl"
        fake_installer = self.base / "uv-installer.sh"
        fake_installer.write_text(
            "#!/bin/sh\nmkdir -p \"$UV_INSTALL_DIR\"\ncp \"$FAKE_UV_SOURCE\" \"$UV_INSTALL_DIR/uv\"\n",
            encoding="utf-8",
        )
        self._write_executable(
            fake_curl,
            "#!/bin/sh\n"
            "output=\nurl=\nwhile [ $# -gt 0 ]; do\n"
            "  case $1 in -o) output=$2; shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac\n"
            "done\n"
            "case $url in *.sha256) printf '0eb3e36bfb24dcd9bb1d1bece1531216b59539a8fde17ee80224af0653c92aa3  youtube-transcript-source.zip\\n' > \"$output\" ;; "
            "*install.sh) cp \"$FAKE_UV_INSTALLER\" \"$output\" ;; *) printf archive > \"$output\" ;; esac\n",
        )
        env = self.env()
        copied_curl = self.curl_bin / "curl"
        copied_curl.write_text(fake_curl.read_text(encoding="utf-8"), encoding="utf-8")
        copied_curl.chmod(0o755)
        env["PATH"] = f"{self.curl_bin}:/usr/bin:/bin"
        env["FAKE_UV_SOURCE"] = str(fake_uv)
        env["FAKE_UV_INSTALLER"] = str(fake_installer)
        result = self.run_script("install.sh", env)
        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )
        self.assertTrue((Path(env["HOME"]) / ".local/bin/uv").exists())
        self.assertNotIn(".bashrc", result.stdout + result.stderr)

    def test_uninstall_removes_only_the_managed_tool(self):
        (self.tool_bin / ".installed").touch()
        result = self.run_script("uninstall.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.tool_bin / ".installed").exists())
        self.assertIn("tool uninstall youtube-transcript-local", self.uv_log.read_text(encoding="utf-8"))

    def test_uninstall_accepts_uv_managed_absolute_launcher_link(self):
        (self.tool_bin / ".installed").touch()
        managed_bin = self.base / "tools" / "youtube-transcript-local" / "bin"
        managed_bin.mkdir(parents=True)
        executable = managed_bin / "youtube-transcript"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher = self.tool_bin / "youtube-transcript"
        launcher.symlink_to(executable)
        result = self.run_script("uninstall.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(launcher.exists())
        self.assertFalse(executable.exists())

    def test_uninstall_preserves_a_replaced_command_path(self):
        (self.tool_bin / ".installed").touch()
        launcher = self.tool_bin / "youtube-transcript"
        launcher.write_text("user-owned command\n", encoding="utf-8")
        result = self.run_script("uninstall.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("left it untouched", result.stderr)
        self.assertEqual(launcher.read_text(encoding="utf-8"), "user-owned command\n")
        self.assertNotIn("tool uninstall", self.uv_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
