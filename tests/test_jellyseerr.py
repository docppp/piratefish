import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer.bootstrap import jellyseerr


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        import json
        self.status = status
        self._body = b""
        if body is not None:
            self._body = json.dumps(body).encode("utf-8")
        self.headers = headers or {}

    @property
    def text(self):
        return self._body.decode("utf-8", "replace")

    def json(self):
        import json
        return json.loads(self._body.decode("utf-8")) if self._body else None


class JellyseerrBootstrapTests(unittest.TestCase):
    def test_extract_connect_sid(self):
        sid = jellyseerr._extract_connect_sid({
            "Set-Cookie": "connect.sid=s%3Atoken123.abc; Path=/; HttpOnly"
        })
        self.assertEqual(sid, "connect.sid=s%3Atoken123.abc")

    def test_libraries_to_enable_prefers_media_libraries(self):
        libs = [
            {"id": "music", "name": "Music", "type": "music"},
            {"id": "movies", "name": "Movies", "type": "movies"},
            {"id": "series", "name": "Series", "type": "tvshows"},
        ]
        picked = jellyseerr._libraries_to_enable(libs)
        self.assertEqual([p["id"] for p in picked], ["movies", "series"])

    @patch("installer.bootstrap.jellyseerr.ui")
    @patch("installer.bootstrap.jellyseerr.wait_for_http", return_value=True)
    @patch("installer.bootstrap.jellyseerr.request")
    def test_bootstrap_happy_path(self, mock_request, _mock_wait, _mock_ui):
        mock_request.side_effect = [
            _Resp(body={"initialized": False, "jellyfinHost": ""}),
            _Resp(headers={"Set-Cookie": "connect.sid=s%3Aabc.def; Path=/; HttpOnly"}),
            _Resp(body=[
                {"id": "movies", "name": "Movies", "type": "movies", "enabled": False},
                {"id": "series", "name": "Series", "type": "tvshows", "enabled": False},
            ]),
            _Resp(body=[
                {"id": "movies", "name": "Movies", "type": "movies", "enabled": True},
                {"id": "series", "name": "Series", "type": "tvshows", "enabled": True},
            ]),
            _Resp(body={"initialized": True}),
        ]

        result = jellyseerr.bootstrap("http://127.0.0.1:5055", "admin", "secret")
        self.assertTrue(result["configured"])
        self.assertTrue(result["linked"])
        self.assertEqual(result["libraries_enabled"], 2)

        auth_call = mock_request.call_args_list[1]
        self.assertEqual(auth_call.args[0], "POST")
        self.assertIn("/api/v1/auth/jellyfin", auth_call.args[1])
        self.assertEqual(auth_call.kwargs["data"]["hostname"], "jellyfin")
        self.assertEqual(auth_call.kwargs["data"]["serverType"], 2)
        sync_call = mock_request.call_args_list[2]
        self.assertEqual(sync_call.args[0], "GET")
        self.assertIn("/api/v1/settings/jellyfin/library?sync=1", sync_call.args[1])
        enable_call = mock_request.call_args_list[3]
        self.assertEqual(enable_call.args[0], "GET")
        self.assertIn("/api/v1/settings/jellyfin/library?enable=movies,series",
                      enable_call.args[1])
        init_call = mock_request.call_args_list[4]
        self.assertEqual(init_call.args[0], "POST")
        self.assertIn("/api/v1/settings/initialize", init_call.args[1])

    @patch("installer.bootstrap.jellyseerr.ui")
    @patch("installer.bootstrap.jellyseerr.wait_for_http", return_value=True)
    @patch("installer.bootstrap.jellyseerr.request")
    def test_bootstrap_skips_when_already_linked(self, mock_request, _mock_wait, _mock_ui):
        mock_request.return_value = _Resp(body={
            "initialized": True,
            "jellyfinHost": "http://jellyfin:8096",
        })
        result = jellyseerr.bootstrap("http://127.0.0.1:5055", "admin", "secret")
        self.assertTrue(result["configured"])
        self.assertTrue(result["linked"])
        self.assertEqual(mock_request.call_count, 1)

    @patch("installer.bootstrap.jellyseerr.ui")
    @patch("installer.bootstrap.jellyseerr.wait_for_http", return_value=True)
    @patch("installer.bootstrap.jellyseerr.request")
    def test_bootstrap_sync_404_stops(self, mock_request, _mock_wait, _mock_ui):
        mock_request.side_effect = [
            _Resp(body={"initialized": False, "jellyfinHost": ""}),
            _Resp(headers={"Set-Cookie": "connect.sid=s%3Aabc.def; Path=/; HttpOnly"}),
            _Resp(status=404, body={"message": "Not Found"}),
        ]

        result = jellyseerr.bootstrap("http://127.0.0.1:5055", "admin", "secret")
        self.assertTrue(result["configured"])
        self.assertTrue(result["linked"])
        self.assertEqual(result["manual_note"], "Sync libraries manually in Jellyseerr")
        self.assertEqual(mock_request.call_count, 3)
        sync_call = mock_request.call_args_list[2]
        self.assertEqual(sync_call.args[0], "GET")
        self.assertIn("/api/v1/settings/jellyfin/library?sync=1", sync_call.args[1])


if __name__ == "__main__":
    unittest.main()
