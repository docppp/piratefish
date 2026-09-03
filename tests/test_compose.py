import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer import compose, constants


class ComposeRenderTests(unittest.TestCase):
    def test_healthcheck_uses_nc_not_bash_devtcp(self):
        # Regression: the old bash /dev/tcp probe left Homepage (no bash) stuck
        # "unhealthy" forever.
        out = compose.render_compose()
        self.assertIn("nc -z 127.0.0.1", out)
        self.assertNotIn("/dev/tcp/", out)

    def test_every_service_has_a_healthcheck(self):
        out = compose.render_compose()
        self.assertEqual(out.count("healthcheck:"), len(constants.SERVICE_ORDER))

    def test_project_name_and_network(self):
        out = compose.render_compose()
        self.assertIn("name: arrstack", out)
        for name in constants.SERVICE_ORDER:
            self.assertIn(f"  {name}:", out)

    def test_data_mount_present_for_each(self):
        out = compose.render_compose()
        # Every service binds the shared /data tree.
        self.assertGreaterEqual(out.count(":/data"), len(constants.SERVICE_ORDER))

    def test_jellyseerr_block_uses_expected_port_and_config_path(self):
        out = compose.render_compose()
        self.assertIn("  jellyseerr:", out)
        self.assertIn("${JELLYSEERR_PORT}:5055", out)
        self.assertIn("${DATA_PATH}/Arr/jellyseerr:/app/config", out)

    def test_homepage_public_images_mount_present_for_background_asset(self):
        out = compose.render_compose()
        self.assertIn("${DATA_PATH}/Arr/homepage/images:/app/public/images:ro", out)


if __name__ == "__main__":
    unittest.main()
