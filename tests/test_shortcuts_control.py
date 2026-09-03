import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer import shortcut, control, constants
from installer.bootstrap import homepage


class LinuxShortcutTests(unittest.TestCase):
    def test_start_launcher_shape(self):
        sh = shortcut.linux_launcher("/opt/pf", "/usr/bin/python3", "up")
        self.assertTrue(sh.startswith("#!/usr/bin/env bash"))
        self.assertIn('PROJECT_DIR="/opt/pf"', sh)
        self.assertIn('CONTROL_SCRIPT="$PROJECT_DIR/PirateFish-control.py"', sh)
        self.assertIn("docker compose --project-name arrstack", sh)
        self.assertIn("up -d --remove-orphans", sh)
        self.assertIn("already running", sh)
        self.assertIn("nohup \"$PY\" \"$CONTROL_SCRIPT\"", sh)
        self.assertNotIn('"$PY" "$PROJECT_DIR/install.py"', sh)

    def test_stop_launcher(self):
        sh = shortcut.linux_launcher("/opt/pf", "/usr/bin/python3", "down")
        self.assertIn("docker compose --project-name arrstack", sh)
        self.assertIn("-f \"$COMPOSE_FILE\" down", sh)
        self.assertIn("already stopped", sh)
        self.assertNotIn('"$PY" "$PROJECT_DIR/install.py"', sh)

    def test_desktop_entry_wellformed(self):
        d = shortcut.desktop_entry("PirateFish (Start)", "/opt/pf/PirateFish.sh",
                                   "Start it")
        self.assertIn("[Desktop Entry]", d)
        self.assertIn("Name=PirateFish (Start)", d)
        self.assertIn("Type=Application", d)
        self.assertIn("Terminal=true", d)


class WindowsShortcutTests(unittest.TestCase):
    def test_bat_uses_crlf_and_pause(self):
        b = shortcut.windows_launcher(r"C:\pf", r"C:\Python\python.exe", "up")
        self.assertIn("\r\n", b)
        # Every newline must be a CRLF (no lone LF).
        self.assertEqual(b.count("\n"), b.count("\r\n"))
        self.assertTrue(b.startswith("@echo off"))
        self.assertIn("pause", b)
        # The launcher delegates to the installer, which drives WSL2.
        self.assertIn('%INSTALLER%" up', b)
        self.assertIn('set "INSTALLER=%PROJECT_DIR%\\install.py"', b)
        self.assertNotIn("wsl -d", b)
        self.assertNotIn("netsh", b)

    def test_bat_stop(self):
        b = shortcut.windows_launcher(r"C:\pf", r"C:\Python\python.exe", "down")
        self.assertIn('%INSTALLER%" down', b)
        self.assertNotIn("wsl -d", b)

    def test_ps_lnk_wellformed(self):
        ps = shortcut.windows_lnk_ps("a.lnk", "t.bat", r"C:\pf", "shell32.dll,27",
                                     "desc")
        self.assertIn("WScript.Shell", ps)
        self.assertIn("CreateShortcut('a.lnk')", ps)
        self.assertIn("$s.TargetPath='t.bat'", ps)
        self.assertIn("$s.Save()", ps)


class ControlPageTests(unittest.TestCase):
    def test_running_page(self):
        html = control.render_page(True, "http://192.168.1.5:3000", "")
        self.assertIn("Shut down stack", html)
        self.assertIn("running", html)
        self.assertIn("http://192.168.1.5:3000", html)

    def test_stopped_page_and_message(self):
        html = control.render_page(False, "http://x:3000", "Stack stopped.")
        self.assertIn("stopped", html)
        self.assertIn("Stack stopped.", html)

    def test_message_is_escaped(self):
        html = control.render_page(True, "http://x:3000", "<script>bad</script>")
        self.assertNotIn("<script>bad", html)
        self.assertIn("&lt;script&gt;", html)

    def test_action_result_has_continue_link(self):
        html = control.render_action_result("Done", "http://x:3000")
        self.assertIn("PirateFish Power Action", html)
        self.assertIn("Continue", html)
        self.assertIn("http://x:3000", html)


class HomepagePowerBookmarksTests(unittest.TestCase):
    def test_bookmarks_are_empty_when_power_tile_is_in_services(self):
        yml = homepage.render_bookmarks("192.168.1.5", {"homepage": 3000})
        self.assertEqual(yml, "---\n[]\n")

    def test_services_do_not_include_power_links(self):
        ports = {name: svc.port for name, svc in constants.SERVICES.items()}
        yml = homepage.render_services("192.168.1.5", ports, {}, "admin", "pass")
        self.assertNotIn("/action/down", yml)
        self.assertNotIn("/api/down", yml)
        self.assertNotIn("Shut down stack", yml)

    def test_custom_js_sends_post_without_navigation(self):
        js = homepage.render_custom_js("192.168.1.5")
        self.assertIn("http://192.168.1.5:8787/api/down", js)
        self.assertIn('fetch(shutdownUrl, { method: "POST", mode: "no-cors"', js)
        self.assertIn("event.preventDefault()", js)
        self.assertNotIn("/action/down", js)

    def test_render_settings_includes_background_when_enabled(self):
        settings = homepage.render_settings(use_background=True)
        self.assertIn("background: /images/piratefish.png", settings)

    def test_bootstrap_copies_background_image_and_writes_setting(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source = base / "src"
            config_dir = base / "config"
            source.mkdir()
            config_dir.mkdir()
            (source / "piratefish.png").write_bytes(b"png")

            ports = {name: svc.port for name, svc in constants.SERVICES.items()}
            with mock.patch.object(homepage, "_project_dir", return_value=source):
                ok = homepage.bootstrap(
                    config_dir=config_dir,
                    lan_ip="192.168.1.5",
                    ports=ports,
                    api_keys={},
                    qbit_user="admin",
                    qbit_pass="pass",
                    container="homepage",
                )

            self.assertTrue(ok)
            self.assertTrue((config_dir / "images" / "piratefish.png").exists())
            settings = (config_dir / "settings.yaml").read_text()
            self.assertIn("background: /images/piratefish.png", settings)


class ShortcutCreationTests(unittest.TestCase):
    def test_create_linux_makes_start_only_and_removes_legacy_stop(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source = base / "src"
            runtime = base / "runtime"
            desktop = base / "Desktop"
            source.mkdir()
            runtime.mkdir()
            desktop.mkdir()

            (source / "docker-compose.yml").write_text("name: arrstack\n")
            (source / ".env").write_text("HOMEPAGE_PORT=3000\n")
            (source / "piratefish.ico").write_bytes(b"ico")

            # Legacy artifacts from older installer versions.
            (runtime / "piratefish-start.sh").write_text("old-start")
            (runtime / "piratefish-stop.sh").write_text("old")
            (desktop / "PirateFish (Stop).desktop").write_text("old")

            with mock.patch.object(shortcut, "_project_dir", return_value=source), \
                    mock.patch.object(shortcut, "_runtime_dir", return_value=runtime), \
                    mock.patch.object(shortcut, "_desktop_dir", return_value=desktop), \
                    mock.patch("installer.shortcut.subprocess.run"):
                out = shortcut.create(SimpleNamespace(os_name="linux"))

            self.assertEqual(len(out), 1)
            self.assertEqual(out[0], desktop / "PirateFish (Start).desktop")
            self.assertTrue((runtime / "PirateFish.sh").exists())
            self.assertTrue((runtime / "piratefish.ico").exists())
            self.assertTrue((runtime / "PirateFish-control.py").exists())
            self.assertFalse((runtime / "piratefish-start.sh").exists())
            self.assertFalse((runtime / "piratefish-stop.sh").exists())
            self.assertFalse((desktop / "PirateFish (Stop).desktop").exists())
            control_py = (runtime / "PirateFish-control.py").read_text()
            self.assertIn("Shut down stack", control_py)
            self.assertIn("/api/down", control_py)
            self.assertIn("/action/down", control_py)
            self.assertIn("/action/ping", control_py)
            desktop_entry_text = (desktop / "PirateFish (Start).desktop").read_text()
            self.assertIn(f"Icon={runtime / 'piratefish.ico'}", desktop_entry_text)

    def test_create_windows_uses_runtime_ico_for_lnk_icon(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            source = base / "src"
            runtime = base / "runtime"
            desktop = base / "Desktop"
            source.mkdir()
            runtime.mkdir()
            desktop.mkdir()

            (source / "docker-compose.yml").write_text("name: arrstack\n")
            (source / ".env").write_text("HOMEPAGE_PORT=3000\n")
            (source / "piratefish.ico").write_bytes(b"ico")

            with mock.patch.object(shortcut, "_project_dir", return_value=source), \
                    mock.patch.object(shortcut, "_runtime_dir", return_value=runtime), \
                    mock.patch.object(shortcut, "_desktop_dir", return_value=desktop), \
                    mock.patch("installer.shortcut.subprocess.run") as run_mock:
                out = shortcut.create(SimpleNamespace(os_name="windows"))

            self.assertEqual(len(out), 1)
            self.assertEqual(out[0], runtime / "PirateFish.bat")
            self.assertEqual(run_mock.call_count, 1)
            cmd = run_mock.call_args[0][0]
            self.assertEqual(cmd[:3], ["powershell", "-NoProfile", "-Command"])
            self.assertIn(str(runtime / "piratefish.ico"), cmd[3])


class ConstantsTests(unittest.TestCase):
    def test_control_port_defined(self):
        self.assertIsInstance(constants.CONTROL_PORT, int)
        self.assertNotIn(constants.CONTROL_PORT,
                         [s.port for s in constants.SERVICES.values()])


if __name__ == "__main__":
    unittest.main()
