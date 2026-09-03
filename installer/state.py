"""Persistent installer state so re-runs skip completed phases (idempotency).

Written atomically (temp file + os.replace) so a crash mid-write can never leave
a truncated/corrupt state.json, and a corrupt file is tolerated on load.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                if not isinstance(self.data, dict):
                    self.data = {}
            except (ValueError, OSError):
                # Corrupt/unreadable state must never abort the installer.
                self.data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                       prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self.data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except OSError:
            pass

    def is_done(self, phase: str) -> bool:
        return bool(self.data.get("phases", {}).get(phase))

    def mark_done(self, phase: str) -> None:
        self.data.setdefault("phases", {})[phase] = True
        self.save()

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value
        self.save()
