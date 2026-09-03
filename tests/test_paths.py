import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer import paths


class PathNormalizationTests(unittest.TestCase):
    def test_plain_linux_path(self):
        dp = paths.normalize("/mnt/media/ArrStack", "linux", False)
        self.assertEqual(dp.fs_path, "/mnt/media/ArrStack")
        self.assertEqual(dp.mount_path, "/mnt/media/ArrStack")

    def test_windows_drive_on_linux_converts_to_mnt(self):
        dp = paths.normalize(r"D:\Media\ArrStack", "linux", True)
        self.assertEqual(dp.fs_path, "/mnt/d/Media/ArrStack")
        self.assertEqual(dp.mount_path, "/mnt/d/Media/ArrStack")

    def test_windows_native_drive_path(self):
        dp = paths.normalize(r"D:\Media\ArrStack", "windows", False)
        self.assertEqual(dp.fs_path, "D:\\Media\\ArrStack")
        # Windows host + Docker in WSL2 uses /mnt/<drive>/... bind paths.
        self.assertEqual(dp.mount_path, "/mnt/d/Media/ArrStack")

    def test_windows_from_mnt_form(self):
        dp = paths.normalize("/mnt/d/Media/ArrStack", "windows", False)
        self.assertEqual(dp.fs_path, "D:\\Media\\ArrStack")
        self.assertEqual(dp.mount_path, "/mnt/d/Media/ArrStack")

    def test_to_wsl_path_passthrough(self):
        self.assertEqual(paths.to_wsl_path("/mnt/d/Media/ArrStack"),
                         "/mnt/d/Media/ArrStack")

    def test_strips_surrounding_quotes(self):
        dp = paths.normalize('"/mnt/media/X"', "linux", False)
        self.assertEqual(dp.fs_path, "/mnt/media/X")


if __name__ == "__main__":
    unittest.main()
