import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer.bootstrap import jellyfin


class _Resp:
    def __init__(self, status=200, body=None):
        import json
        self.status = status
        self._body = b""
        if body is not None:
            self._body = json.dumps(body).encode("utf-8")

    def json(self):
        import json
        return json.loads(self._body.decode("utf-8")) if self._body else None


class JellyfinBootstrapTests(unittest.TestCase):
    @patch("installer.bootstrap.jellyfin.request")
    def test_startup_configuration_sets_server_name(self, mock_request):
        mock_request.side_effect = [
            _Resp(status=204),  # Startup/Configuration
            _Resp(status=200, body={"Name": "admin"}),  # Startup/User GET
            _Resp(status=204),  # Startup/User POST
            _Resp(status=204),  # Startup/RemoteAccess
            _Resp(status=204),  # Startup/Complete
        ]
        ok = jellyfin._startup("http://127.0.0.1:8096", "alice", "pw")
        self.assertTrue(ok)
        first = mock_request.call_args_list[0]
        self.assertIn("/Startup/Configuration", first.args[1])
        self.assertEqual(first.kwargs["data"]["ServerName"], "PirateFish")

    @patch("installer.bootstrap.jellyfin.request")
    def test_ensure_server_name_updates_existing_config(self, mock_request):
        mock_request.side_effect = [
            _Resp(status=200, body={"ServerName": "RandomBox"}),  # GET config
            _Resp(status=204),  # POST config
            _Resp(status=200, body={"ServerName": "PirateFish"}),  # GET public
        ]
        ok = jellyfin._ensure_server_name(
            "http://127.0.0.1:8096", "token123", "PirateFish"
        )
        self.assertTrue(ok)
        update = mock_request.call_args_list[1]
        self.assertIn("/System/Configuration", update.args[1])
        self.assertEqual(update.kwargs["data"]["ServerName"], "PirateFish")


if __name__ == "__main__":
    unittest.main()
