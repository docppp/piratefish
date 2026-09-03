"""Unit tests for the Windows -> WSL2 bootstrap/delegation layer."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer import windows_bootstrap as wb
from installer.windows_bootstrap import WindowsBootstrapError
from installer.state import State


class PathHelperTests(unittest.TestCase):
    def test_drive_path_to_wsl(self):
        self.assertEqual(wb._to_wsl_path(r"C:\Users\me\piratefish"),
                         "/mnt/c/Users/me/piratefish")
        self.assertEqual(wb._to_wsl_path(r"D:\Media"), "/mnt/d/Media")

    def test_non_drive_passthrough(self):
        self.assertEqual(wb._to_wsl_path("/opt/piratefish"), "/opt/piratefish")

    def test_has_path_arg(self):
        self.assertFalse(wb._has_path_arg([]))
        self.assertFalse(wb._has_path_arg(["up"]))
        self.assertTrue(wb._has_path_arg(["--path", "D:\\Media"]))
        self.assertTrue(wb._has_path_arg(["--path=/mnt/d/Media"]))


class InnerArgvTests(unittest.TestCase):
    def test_preserves_gui_choice_strips_windows_only(self):
        # --gui/--no-gui are honoured inside WSL2 (served via WSLg).
        self.assertEqual(
            wb._inner_argv(["--no-gui", "up", "--no-open"]),
            ["--no-gui", "up", "--no-open"])
        self.assertEqual(
            wb._inner_argv(["--state", "C:\\s.json", "install"]), ["install"])
        self.assertEqual(wb._inner_argv(["gui-run"]), [])
        self.assertEqual(
            wb._inner_argv(["--path", "D:\\Media"]), ["--path", "D:\\Media"])


class ReadPortsTests(unittest.TestCase):
    def test_reads_env_ports_with_fallbacks(self):
        env_text = "PROWLARR_PORT=1111\nSONARR_PORT=bad\n# comment\n"
        with mock.patch.object(wb, "_wsl", return_value=(0, env_text, "")):
            ports = wb._read_ports("Ubuntu")
        from installer import constants
        self.assertIn(constants.CONTROL_PORT, ports)
        self.assertIn(1111, ports)                       # explicit override
        self.assertIn(constants.SERVICES["sonarr"].port, ports)  # bad -> default


class DistroVersionTests(unittest.TestCase):
    def test_parses_version_column(self):
        listing = ("  NAME      STATE    VERSION\n"
                   "* Ubuntu    Running  2\n"
                   "  Legacy    Stopped  1\n")
        with mock.patch.object(wb, "_wsl_out", return_value=(0, listing)):
            self.assertEqual(wb._distro_version("Ubuntu"), "2")
            self.assertEqual(wb._distro_version("Legacy"), "1")
            self.assertEqual(wb._distro_version("Missing"), "")


class BootstrapGatingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self._tmp.close()
        self.state = State(Path(self._tmp.name))

    def tearDown(self):
        os.unlink(self._tmp.name)

    def _env(self, is_admin):
        from installer.detect import Environment
        return Environment(os_name="windows", is_admin=is_admin)

    def test_missing_wsl_with_admin_requests_reboot(self):
        with mock.patch.object(wb, "_wsl_installed", return_value=False), \
             mock.patch.object(wb, "_virtualization_ok", return_value=True), \
             mock.patch.object(wb, "_install_wsl_core") as core:
            result = wb._bootstrap_wsl(self._env(is_admin=True), self.state)
        self.assertIsNone(result)
        core.assert_called_once()
        self.assertTrue(self.state.is_done("wsl2_requested"))

    def test_ready_distro_ensures_systemd(self):
        with mock.patch.object(wb, "_wsl_installed", return_value=True), \
             mock.patch.object(wb, "_distro_registered", return_value=True), \
             mock.patch.object(wb, "_distro_version", return_value="2"), \
             mock.patch.object(wb, "_ensure_systemd") as sysd:
            distro = wb._bootstrap_wsl(self._env(is_admin=True), self.state)
        self.assertEqual(distro, wb.DISTRO_NAME)
        sysd.assert_called_once_with(wb.DISTRO_NAME)


class RequireAdminTests(unittest.TestCase):
    def test_admin_continues(self):
        from installer.detect import Environment
        with mock.patch.object(wb, "detect",
                               return_value=Environment(os_name="windows", is_admin=True)):
            self.assertTrue(wb._require_admin([]))

    def test_non_admin_relaunches_and_stops(self):
        from installer.detect import Environment
        with mock.patch.object(wb, "detect",
                               return_value=Environment(os_name="windows", is_admin=False)), \
             mock.patch.object(wb, "_relaunch_elevated", return_value=True) as relaunch:
            self.assertFalse(wb._require_admin(["up"]))
        relaunch.assert_called_once_with(["up"])

    def test_non_admin_declined_raises(self):
        from installer.detect import Environment
        with mock.patch.object(wb, "detect",
                               return_value=Environment(os_name="windows", is_admin=False)), \
             mock.patch.object(wb, "_relaunch_elevated", return_value=False):
            with self.assertRaises(WindowsBootstrapError) as ctx:
                wb._require_admin(["up"])
        self.assertIn("administrator", str(ctx.exception).lower())


class StartupLauncherTests(unittest.TestCase):
    def test_bat_is_self_contained_and_drives_wsl(self):
        text = wb._startup_bat_text([9696, 8989, 3000], 3000)
        # Self-elevates for firewall/portproxy.
        self.assertIn("net session", text)
        self.assertIn("-Verb RunAs", text)
        # Drives the dedicated distro directly (no dependency on install.py).
        self.assertIn(wb.DISTRO_NAME, text)
        self.assertNotIn("install.py", text)
        # Starts docker + compose from the in-WSL bundle.
        self.assertIn("docker compose --project-name %PROJECT%", text)
        self.assertIn("PROJECT=arrstack", text)
        self.assertIn(f"{wb.WSL_PROJECT_DIR}/docker-compose.yml", text)
        # Configures LAN forwarding for each service port.
        self.assertIn("netsh interface portproxy add", text)
        self.assertIn("9696", text)
        # Opens the dashboard.
        self.assertIn("http://127.0.0.1:%DASHPORT%", text)
        self.assertIn("DASHPORT=3000", text)
        # CRLF line endings for a Windows .bat.
        self.assertIn("\r\n", text)

    def test_shortcut_targets_bat_with_icon(self):
        from pathlib import Path
        captured = {}

        def fake_run(cmd, timeout=60):
            captured["ps"] = cmd[-1]
            return (0, "", "")

        with mock.patch.object(wb, "_desktop_dir", return_value=Path("/tmp")), \
             mock.patch.object(wb, "_run", side_effect=fake_run):
            wb._create_desktop_shortcut(Path("/some/dir/piratefish_startup.bat"))
        ps = captured["ps"]
        self.assertIn("piratefish_startup.bat", ps)
        self.assertIn("PirateFish.lnk", ps)
        self.assertIn("IconLocation", ps)
        self.assertNotIn("schtasks", ps)


class WslDetectionTests(unittest.TestCase):
    """wsl.exe emits UTF-16LE; detection must survive the NUL bytes."""

    class _FakeProc:
        def __init__(self, rc, text):
            self.returncode = rc
            self.stdout = text.encode("utf-16-le")
            self.stderr = b""

    def _patch(self, router):
        return mock.patch.object(wb.subprocess, "run",
                                 side_effect=lambda cmd, capture_output, timeout:
                                 self._FakeProc(*router(" ".join(cmd[1:]))))

    def test_feature_present_no_distro_is_installed(self):
        def router(a):
            if a == "-l -q":
                return (1, "Windows Subsystem for Linux has no installed distributions.")
            return (1, "")
        with mock.patch.object(wb, "_wsl_exe", return_value="wsl"), self._patch(router):
            self.assertTrue(wb._wsl_installed())

    def test_feature_absent_is_not_installed(self):
        def router(a):
            return (1, "The Windows Subsystem for Linux optional component is not "
                       "enabled. Please enable it and try again.")
        with mock.patch.object(wb, "_wsl_exe", return_value="wsl"), self._patch(router):
            self.assertFalse(wb._wsl_installed())

    def test_distro_listing_is_nul_safe(self):
        def router(a):
            if a == "--status":
                return (0, "Default Version: 2\n")
            if a == "-l -q":
                return (0, "PirateFish-Ubuntu\nUbuntu\n")
            if a == "-l -v":
                return (0, "  NAME               STATE    VERSION\n"
                           "* PirateFish-Ubuntu  Running  2\n"
                           "  Ubuntu             Stopped  2\n")
            return (0, "")
        with mock.patch.object(wb, "_wsl_exe", return_value="wsl"), self._patch(router):
            self.assertTrue(wb._wsl_installed())
            self.assertEqual(wb._list_distros(), ["PirateFish-Ubuntu", "Ubuntu"])
            self.assertTrue(wb._distro_registered("PirateFish-Ubuntu"))
            self.assertFalse(wb._distro_registered("Nope"))
            self.assertEqual(wb._distro_version("PirateFish-Ubuntu"), "2")


if __name__ == "__main__":
    unittest.main()
