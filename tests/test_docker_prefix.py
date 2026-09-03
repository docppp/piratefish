"""Regression guard: container helpers must invoke `docker` as argv[0].

A refactor once stripped the `docker` command from several `subprocess.run`
calls (e.g. `["logs", container]`), which silently broke qBittorrent password
recovery, Prowlarr/servarr API-key reads, and Bazarr configuration inside WSL2.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DockerPrefixTests(unittest.TestCase):
    def test_qbittorrent_logs_uses_docker(self):
        from installer.bootstrap import qbittorrent as qb
        captured = {}

        class _R:
            stdout = ("A temporary password is provided for this session: "
                      "ABC123\n")
            stderr = ""

        def fake_run(cmd, capture_output, text, timeout):
            captured["cmd"] = cmd
            return _R()

        with mock.patch.object(qb.subprocess, "run", side_effect=fake_run):
            pw = qb._recover_temp_password(attempts=1, delay=0)
        self.assertEqual(captured["cmd"][:2], ["docker", "logs"])
        self.assertEqual(pw, "ABC123")

    def test_servarr_api_key_uses_docker_exec(self):
        from installer import api
        captured = {}

        class _R:
            returncode = 0
            stdout = "<Config><ApiKey>KEY123</ApiKey></Config>"
            stderr = ""

        def fake_run(cmd, capture_output, text, timeout):
            captured["cmd"] = cmd
            return _R()

        with mock.patch.object(api.subprocess, "run", side_effect=fake_run):
            key = api.read_api_key_from_container("prowlarr")
        self.assertEqual(captured["cmd"][:2], ["docker", "exec"])
        self.assertEqual(key, "KEY123")

    def test_bazarr_api_key_uses_docker_exec(self):
        from installer.bootstrap import bazarr as bz
        captured = {}

        class _R:
            returncode = 0
            stdout = "auth:\n  apikey: BZKEY\n"
            stderr = ""

        def fake_run(cmd, capture_output, text, timeout):
            captured["cmd"] = cmd
            return _R()

        with mock.patch.object(bz.subprocess, "run", side_effect=fake_run):
            bz._bazarr_api_key()
        self.assertEqual(captured["cmd"][:2], ["docker", "exec"])


if __name__ == "__main__":
    unittest.main()
