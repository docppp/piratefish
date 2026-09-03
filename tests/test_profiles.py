import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer.bootstrap import quality, bazarr_providers


def _fake_quality_schema():
    """A minimal Sonarr/Radarr-style qualityprofile schema (ascending order)."""
    return {
        "items": [
            {"quality": {"id": 1, "name": "SDTV"}, "allowed": False},
            {"quality": {"id": 2, "name": "HDTV-720p"}, "allowed": False},
            {"quality": {"id": 3, "name": "WEBDL-720p"}, "allowed": False},
            {"quality": {"id": 4, "name": "HDTV-1080p"}, "allowed": False},
            {"quality": {"id": 5, "name": "WEBDL-1080p"}, "allowed": False},
            {"quality": {"id": 6, "name": "Bluray-1080p"}, "allowed": False},
            {"quality": {"id": 7, "name": "WEBDL-2160p"}, "allowed": False},
            {"quality": {"id": 8, "name": "Bluray-2160p"}, "allowed": False},
        ]
    }


def _fake_grouped_quality_schema():
    return {
        "items": [
            {"quality": {"id": 1, "name": "SDTV"}, "allowed": False},
            {"quality": {"id": 9, "name": "HDTV-1080p"}, "allowed": False},
            {
                "id": 1002,
                "name": "WEB 1080p",
                "allowed": False,
                "items": [
                    {"quality": {"id": 15, "name": "WEBRip-1080p"}, "allowed": False},
                    {"quality": {"id": 3, "name": "WEBDL-1080p"}, "allowed": False},
                ],
            },
            {"quality": {"id": 7, "name": "Bluray-1080p"}, "allowed": False},
        ]
    }


class QualityProfileTests(unittest.TestCase):
    def test_1080p_allows_only_selected_types(self):
        prof = quality._build_profile(_fake_quality_schema(), {
            "resolution": "1080p",
            "release_types": ["webdl", "bluray"],
            "max_bitrate_mbps": 8,
        })
        allowed = [i["quality"]["name"] for i in prof["items"] if i["allowed"]]
        self.assertTrue(all("1080p" in n for n in allowed))
        self.assertIn("WEBDL-1080p", allowed)
        self.assertIn("Bluray-1080p", allowed)
        self.assertNotIn("HDTV-1080p", allowed)
        self.assertNotIn("HDTV-720p", allowed)
        self.assertEqual(prof["name"], "piratefish_default")
        self.assertTrue(prof["upgradeAllowed"])

    def test_cutoff_uses_highest_allowed_item(self):
        prof = quality._build_profile(_fake_quality_schema(), {
            "resolution": "1080p",
            "release_types": ["webdl", "bluray"],
            "max_bitrate_mbps": 8,
        })
        # Bluray-1080p id is 6 and is the highest matching quality in the schema.
        self.assertEqual(prof["cutoff"], 6)

    def test_cutoff_uses_group_id_for_nested_web_qualities(self):
        prof = quality._build_profile(_fake_grouped_quality_schema(), {
            "resolution": "1080p",
            "release_types": ["webdl", "webrip"],
            "max_bitrate_mbps": 8,
        })
        self.assertEqual(prof["cutoff"], 1002)

    def test_no_match_raises(self):
        schema = {"items": [{"quality": {"id": 1, "name": "SDTV"}}]}
        with self.assertRaises(RuntimeError):
            quality._build_profile(schema, {
                "resolution": "2160p",
                "release_types": ["webdl"],
                "max_bitrate_mbps": 8,
            })

    def test_string_selection_rejected(self):
        with self.assertRaises(ValueError):
            quality.normalize_selection("1080p")

    def test_bitrate_cap_applies_only_to_selected_release_types(self):
        class _FakeClient:
            def __init__(self, defs):
                self.defs = defs
                self.put_calls = []

            def get(self, path):
                self.assertEqual(path, "qualitydefinition")
                return self.defs

            def put(self, path, payload):
                self.put_calls.append((path, payload))
                return None

            def assertEqual(self, left, right):
                if left != right:
                    raise AssertionError(f"{left!r} != {right!r}")

        defs = [
            {"id": 1, "quality": {"name": "HDTV-1080p"}, "minSize": 0, "preferredSize": 0, "maxSize": 500},
            {"id": 2, "quality": {"name": "Bluray-1080p"}, "minSize": 0, "preferredSize": 0, "maxSize": 500},
            {"id": 3, "quality": {"name": "WEBDL-1080p"}, "minSize": 0, "preferredSize": 0, "maxSize": 500},
            {"id": 5, "quality": {"name": "WEBRip-1080p"}, "minSize": 0, "preferredSize": 0, "maxSize": 500},
            {"id": 4, "quality": {"name": "WEBDL-720p"}, "minSize": 0, "preferredSize": 0, "maxSize": 500},
        ]
        client = _FakeClient(defs)
        changed = quality._apply_bitrate_cap(client, {
            "resolution": "1080p",
            "release_types": ["webdl"],
            "max_bitrate_mbps": 12.0,
        }, "movie")

        self.assertEqual(changed, 1)
        self.assertEqual(client.put_calls[0][0], "qualitydefinition/update")
        self.assertEqual(defs[0]["maxSize"], 500)
        self.assertEqual(defs[1]["maxSize"], 500)
        self.assertEqual(defs[2]["maxSize"], 90.0)
        self.assertEqual(defs[3]["maxSize"], 500)
        self.assertEqual(defs[4]["maxSize"], 500)

    def test_remove_other_profiles_keeps_only_target(self):
        class _FakeClient:
            def __init__(self):
                self.deleted = []

            def get(self, path):
                self.assertEqual(path, "qualityprofile")
                return [
                    {"id": 1, "name": "Any"},
                    {"id": 2, "name": "piratefish_default"},
                    {"id": 3, "name": "Legacy"},
                ]

            def delete(self, path):
                self.deleted.append(path)
                return None

            def assertEqual(self, left, right):
                if left != right:
                    raise AssertionError(f"{left!r} != {right!r}")

        client = _FakeClient()
        removed = quality._remove_other_profiles(client, 2)
        self.assertEqual(removed, 2)
        self.assertEqual(client.deleted, ["qualityprofile/1", "qualityprofile/3"])


class LanguageProfileTests(unittest.TestCase):
    def test_non_english_adds_english_fallback(self):
        langs, profile = bazarr_providers.build_language_profile("pl")
        self.assertEqual(langs, ["pl", "en"])
        items = profile[0]["items"]
        self.assertEqual([i["language"] for i in items], ["pl", "en"])

    def test_english_primary_no_duplicate(self):
        langs, profile = bazarr_providers.build_language_profile("en")
        self.assertEqual(langs, ["en"])

    def test_default_when_empty(self):
        langs, _ = bazarr_providers.build_language_profile("")
        self.assertEqual(langs, ["en"])

    def test_profile_shape(self):
        _, profile = bazarr_providers.build_language_profile("es")
        self.assertEqual(profile[0]["profileId"], 1)
        self.assertEqual(profile[0]["name"], "piratefish_default")


if __name__ == "__main__":
    unittest.main()
