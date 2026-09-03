"""A tiny cross-platform single-instance lock (hardening §E).

Prevents two installer runs from clobbering each other's .env / compose /
containers. Uses a PID lock file with a liveness check so a crashed run leaves no
permanent stale lock behind.
"""

from __future__ import annotations

import os
from pathlib import Path


class LockHeld(Exception):
    def __init__(self, pid):
        self.pid = pid
        super().__init__(f"another installer instance is running (pid {pid})")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":  # pragma: no cover - windows only
            import ctypes
            PROCESS_QUERY = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


class InstallLock:
    """Context manager. Raises LockHeld if a live instance already holds it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._acquired = False

    def acquire(self) -> "InstallLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Fast path: create exclusively.
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self._acquired = True
            return self
        except FileExistsError:
            pass
        # Existing lock: is the owner still alive?
        try:
            owner = int(self.path.read_text().strip() or "0")
        except (OSError, ValueError):
            owner = 0
        if _pid_alive(owner) and owner != os.getpid():
            raise LockHeld(owner)
        # Stale lock -> steal it.
        try:
            self.path.write_text(str(os.getpid()))
            self._acquired = True
        except OSError:
            # Could not write; proceed without a lock rather than blocking install.
            self._acquired = False
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            if self.path.exists():
                try:
                    owner = int(self.path.read_text().strip() or "0")
                except (OSError, ValueError):
                    owner = os.getpid()
                if owner == os.getpid():
                    self.path.unlink()
        except OSError:
            pass
        self._acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
