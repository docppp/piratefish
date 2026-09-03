"""The Windows-picked data path must be locked in the GUI so it can never be
edited to point inside the WSL2 filesystem by accident."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_api(initial_path):
    with mock.patch("installer.gui.app.detect.detect"):
        from installer.gui.app import Api
        return Api(initial_path=initial_path)


class PathLockTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PIRATEFISH_DELEGATED", None)

    def tearDown(self):
        os.environ.pop("PIRATEFISH_DELEGATED", None)

    def test_not_locked_without_delegation(self):
        api = _make_api("/mnt/d/Media")
        self.assertFalse(api._path_locked())
        self.assertFalse(api.guess_defaults()["data_path_locked"])

    def test_not_locked_without_initial_path(self):
        os.environ["PIRATEFISH_DELEGATED"] = "1"
        api = _make_api("")
        self.assertFalse(api._path_locked())

    def test_locked_when_delegated_with_path(self):
        os.environ["PIRATEFISH_DELEGATED"] = "1"
        api = _make_api("/mnt/d/Media")
        self.assertTrue(api._path_locked())
        d = api.guess_defaults()
        self.assertEqual(d["data_path"], "/mnt/d/Media")
        self.assertTrue(d["data_path_locked"])

    def test_locked_install_ignores_tampered_form_path(self):
        """Even if the form carries a WSL-internal path, a locked install must
        use the host-picked Windows-drive path."""
        os.environ["PIRATEFISH_DELEGATED"] = "1"
        api = _make_api("/mnt/d/Media")

        captured = {}

        def fake_build(form, env):
            captured["data_path"] = form["data_path"]
            raise RuntimeError("stop after capture")

        with mock.patch("installer.deps.ensure_docker"), \
             mock.patch("installer.cli.build_config_from_dict", side_effect=fake_build):
            api._emit = lambda *a, **k: None
            api._run_install({"data_path": "/root/evil-inside-wsl",
                              "qbit_pass": "x"})

        self.assertEqual(captured["data_path"], "/mnt/d/Media")


if __name__ == "__main__":
    unittest.main()
