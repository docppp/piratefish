import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer import api
from installer.bootstrap import qbittorrent, prowlarr


CONFIG_XML = """<?xml version="1.0" encoding="utf-8"?>
<Config>
  <ApiKey>abc123def456</ApiKey>
  <Port>8989</Port>
  <UrlBase></UrlBase>
</Config>
"""


class ConfigXmlTests(unittest.TestCase):
    def test_read_api_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.xml"
            p.write_text(CONFIG_XML)
            cfg = api.read_config_xml(p)
            self.assertEqual(cfg["ApiKey"], "abc123def456")
            self.assertEqual(cfg["Port"], "8989")

    def test_missing_file_returns_none(self):
        self.assertIsNone(api.read_config_xml("/no/such/file.xml"))

    def test_corrupt_xml_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.xml"
            p.write_text("<Config><ApiKey>oops")
            self.assertIsNone(api.read_config_xml(p))


class QbitPasswordScrapeTests(unittest.TestCase):
    def test_parses_temp_password(self):
        log = ("WebUI: Web UI: password not set. A temporary password is "
               "provided for this session: Ax7Kp9Qm2\nother line\n")
        self.assertEqual(qbittorrent.parse_temp_password(log), "Ax7Kp9Qm2")

    def test_does_not_match_the_word_provided(self):
        # Regression: an earlier regex matched the literal word 'provided'.
        log = "A temporary password is provided for this session:\n"
        # No password token on the same run -> should not return 'provided'.
        result = qbittorrent.parse_temp_password(log)
        self.assertNotEqual(result, "provided")

    def test_strips_trailing_punctuation(self):
        log = "temporary password is provided for this session: abc123.\n"
        self.assertEqual(qbittorrent.parse_temp_password(log), "abc123")

    def test_none_when_absent(self):
        self.assertIsNone(qbittorrent.parse_temp_password("nothing here"))
        self.assertIsNone(qbittorrent.parse_temp_password(""))


class ShortErrTests(unittest.TestCase):
    def test_list_of_validation_errors(self):
        body = '[{"errorMessage": "Should be unique"}]'
        self.assertIn("Should be unique", prowlarr._short_err(body))

    def test_dict_message(self):
        body = '{"message": "boom"}'
        self.assertEqual(prowlarr._short_err(body), "boom")

    def test_plain_text(self):
        self.assertEqual(prowlarr._short_err("plain"), "plain")


if __name__ == "__main__":
    unittest.main()
