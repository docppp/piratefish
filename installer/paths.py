"""Data-path normalization across Linux, WSL2 and native Windows.

The user may type a Windows path (D:\\Media\\ArrStack) or a Linux path
(/mnt/d/Media/ArrStack or /mnt/media/ArrStack). We need two forms:
  * fs_path     -- what Python uses for local file operations on THIS host.
  * mount_path  -- what goes into docker-compose as the bind source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_MNT_DRIVE_RE = re.compile(r"^/mnt/([a-zA-Z])/(.*)$")


@dataclass
class DataPath:
    raw: str
    fs_path: str      # for local filesystem ops on the current host
    mount_path: str   # for the docker-compose bind source
    display: str


def normalize(raw: str, os_name: str, is_wsl: bool) -> DataPath:
    raw = raw.strip().strip('"').strip("'")

    win_m = _WIN_DRIVE_RE.match(raw)
    mnt_m = _MNT_DRIVE_RE.match(raw)

    if os_name == "windows":
        # Native Windows host with Docker engine inside WSL2.
        if mnt_m:
            drive, rest = mnt_m.group(1).upper(), mnt_m.group(2)
            fs = f"{drive}:\\" + rest.replace("/", "\\")
        else:
            fs = raw
        # Docker runs inside WSL2, so compose bind paths must be Linux /mnt/<drive>.
        mount = to_wsl_path(fs)
        return DataPath(raw=raw, fs_path=fs, mount_path=mount, display=fs)

    # Linux or WSL2: convert Windows drive paths to /mnt/<drive>/...
    if win_m:
        drive, rest = win_m.group(1).lower(), win_m.group(2).replace("\\", "/")
        linux = f"/mnt/{drive}/{rest}"
        return DataPath(raw=raw, fs_path=linux, mount_path=linux, display=linux)

    return DataPath(raw=raw, fs_path=raw, mount_path=raw, display=raw)


def to_wsl_path(path: str) -> str:
    """Convert a Windows path to /mnt/<drive>/... for WSL usage."""
    path = (path or "").strip().strip('"').strip("'")
    m = _WIN_DRIVE_RE.match(path)
    if not m:
        return path.replace("\\", "/")
    drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"
