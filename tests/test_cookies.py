import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer.gui import browser


class CookieFormattingTests(unittest.TestCase):
    def test_base_domain(self):
        self.assertEqual(browser.base_domain("https://www.torrentleech.org/x"),
                         "torrentleech.org")
        self.assertEqual(browser.base_domain("http://sub.a.example.co/"),
                         "example.co")

    def test_cookies_from_tuples(self):
        cookies = [("cf_clearance", "abc"), ("PHPSESSID", "xyz")]
        s = browser.cookies_to_string(cookies)
        self.assertIn("cf_clearance=abc", s)
        self.assertIn("PHPSESSID=xyz", s)
        self.assertIn("; ", s)

    def test_cookies_dedup(self):
        cookies = [("a", "1"), ("a", "2"), ("b", "3")]
        s = browser.cookies_to_string(cookies)
        self.assertEqual(s.count("a="), 1)

    def test_cookies_simplecookie(self):
        from http.cookies import SimpleCookie
        c = SimpleCookie()
        c["session"] = "tok"
        s = browser.cookies_to_string([c])
        self.assertEqual(s, "session=tok")

    def test_empty(self):
        self.assertEqual(browser.cookies_to_string([]), "")
        self.assertEqual(browser.cookies_to_string(None), "")


if __name__ == "__main__":
    unittest.main()
