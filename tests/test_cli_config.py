import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer import cli


class _Env:
    os_name = "linux"
    is_wsl = False
    lan_ip = "192.168.1.10"


class CliConfigTests(unittest.TestCase):
    def test_gui_config_reuses_qbit_user_for_jellyfin(self):
        cfg = cli.build_config_from_dict({
            "data_path": "/tmp/pf",
            "qbit_user": "alice",
            "qbit_pass": "secret123",
        }, _Env())
        self.assertEqual(cfg["qbit_user"], "alice")
        self.assertEqual(cfg["jellyfin_user"], "alice")
        self.assertEqual(cfg["jellyfin_pass"], "secret123")

    def test_gui_config_keeps_explicit_jellyfin_user_override(self):
        cfg = cli.build_config_from_dict({
            "data_path": "/tmp/pf",
            "qbit_user": "alice",
            "qbit_pass": "secret123",
            "jellyfin_user": "mediaadmin",
            "jellyfin_pass": "different",
        }, _Env())
        self.assertEqual(cfg["jellyfin_user"], "mediaadmin")
        self.assertEqual(cfg["jellyfin_pass"], "different")


if __name__ == "__main__":
    unittest.main()
