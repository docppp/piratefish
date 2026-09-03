import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from installer.state import State
from installer import lifecycle, constants, fs_checks, lock


class StateTests(unittest.TestCase):
    def test_roundtrip_and_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            s = State(p)
            s.set("data_path", "/mnt/x")
            s.mark_done("install")
            # Reload from disk.
            s2 = State(p)
            self.assertEqual(s2.get("data_path"), "/mnt/x")
            self.assertTrue(s2.is_done("install"))

    def test_corrupt_file_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            p.write_text("{ this is not json ")
            s = State(p)  # must not raise
            self.assertEqual(s.data, {})
            s.set("k", "v")  # still writable
            self.assertEqual(State(p).get("k"), "v")

    def test_no_temp_files_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            s = State(p)
            s.set("a", 1)
            leftovers = [f for f in os.listdir(d) if f.startswith(".state-")]
            self.assertEqual(leftovers, [])


class LifecycleTests(unittest.TestCase):
    def test_compose_base_uses_project_name(self):
        base = lifecycle.compose_base(Path("/opt/pf"))
        self.assertIn("--project-name", base)
        self.assertIn(constants.COMPOSE_PROJECT, base)
        self.assertEqual(base[1], "compose")

    def test_up_without_compose_file(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg, already = lifecycle.up(Path(d))
            self.assertFalse(ok)
            self.assertIn("docker-compose.yml", msg)

    def test_down_without_compose_file(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg, was = lifecycle.down(Path(d))
            self.assertFalse(was)


class TrashDetectionTests(unittest.TestCase):
    def test_detects_trash_paths(self):
        self.assertTrue(fs_checks._is_trashed_path(
            Path("/home/u/.local/share/Trash/files/demo")))
        self.assertTrue(fs_checks._is_trashed_path(Path(r"C:\$Recycle.Bin\x")))

    def test_normal_path_ok(self):
        self.assertFalse(fs_checks._is_trashed_path(Path("/mnt/media/ArrStack")))


class LockTests(unittest.TestCase):
    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".install.lock"
            with lock.InstallLock(p):
                self.assertTrue(p.exists())
            self.assertFalse(p.exists())

    def test_second_live_acquire_raises(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".install.lock"
            # A real, live process with a PID different from ours.
            proc = subprocess.Popen(["sleep", "30"])
            try:
                p.write_text(str(proc.pid))
                with self.assertRaises(lock.LockHeld):
                    lock.InstallLock(p).acquire()
            finally:
                proc.terminate()
                proc.wait()

    def test_stale_lock_stolen(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".install.lock"
            p.write_text("999999")  # almost certainly-dead PID
            with lock.InstallLock(p):
                self.assertEqual(p.read_text().strip(), str(os.getpid()))


if __name__ == "__main__":
    unittest.main()
