"""Filesystem capability checks (the `doctor`): permissions, hardlinks, inotify.

These verify the *real* behaviour of the chosen data path rather than assuming
ext4 semantics. Crucial for the Windows/WSL2 case where media lives on NTFS via
/mnt/<drive> (drvfs/9p), where chmod/chown are no-ops and inotify is unreliable.
"""

from __future__ import annotations

import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FsReport:
    path: str = ""
    writable: bool = False
    fs_type: str = ""
    same_filesystem: bool = False      # Torr and Media share one FS (hardlink req.)
    hardlink_ok: bool = False
    hardlink_detail: str = ""
    chmod_ok: bool = False
    chmod_detail: str = ""
    inotify_ok: bool = False
    inotify_detail: str = ""
    on_windows_mount: bool = False     # /mnt/<drive> under WSL2 from a Windows host
    caveats: list = field(default_factory=list)
    fatal: list = field(default_factory=list)

    @property
    def safe_to_proceed(self) -> bool:
        return not self.fatal


def _detect_fs_type(path: Path) -> str:
    # Linux: parse /proc/mounts for the longest matching mountpoint.
    try:
        real = os.path.realpath(path)
        best_mp, best_type = "", ""
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                _, mp, fstype = parts[0], parts[1], parts[2]
                if real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/":
                    if len(mp) >= len(best_mp):
                        best_mp, best_type = mp, fstype
        return best_type or "unknown"
    except OSError:
        return "unknown"


def _is_windows_mount(path: Path, fs_type: str) -> bool:
    p = str(path).replace("\\", "/").lower()
    if p.startswith("/mnt/") and len(p) > 6 and p[5].isalpha():
        return True
    if fs_type in ("9p", "drvfs", "cifs", "v9fs"):
        return True
    if os.name == "nt":
        return True
    return False


def check_writable(path: Path, report: FsReport, create_if_missing: bool = True) -> None:
    probe = path / ".arr_write_probe"
    try:
        if path.exists() and not path.is_dir():
            report.writable = False
            report.fatal.append("Data path points to a file, not a directory.")
            return
        if not path.exists():
            if not create_if_missing:
                report.writable = False
                report.fatal.append(
                    "Path does not exist yet. It will be created during install."
                )
                return
            path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        report.writable = True
    except OSError as e:
        report.writable = False
        report.fatal.append(f"Data path is not writable: {e}")
    finally:
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass


def check_hardlink(torr_dir: Path, media_dir: Path, report: FsReport) -> None:
    """Create a file in Torr and hardlink it into Media; verify link count."""
    src = torr_dir / ".arr_hardlink_src"
    dst = media_dir / ".arr_hardlink_dst"
    try:
        torr_dir.mkdir(parents=True, exist_ok=True)
        media_dir.mkdir(parents=True, exist_ok=True)
        for p in (src, dst):
            if p.exists() or p.is_symlink():
                p.unlink()

        # Same filesystem? (hardlinks impossible across devices)
        st_torr = os.stat(torr_dir)
        st_media = os.stat(media_dir)
        report.same_filesystem = (st_torr.st_dev == st_media.st_dev)

        src.write_text("hardlink-probe")
        os.link(src, dst)
        nlink = os.stat(src).st_nlink
        same_inode = os.stat(src).st_ino == os.stat(dst).st_ino
        if nlink >= 2 and same_inode:
            report.hardlink_ok = True
            report.hardlink_detail = f"link count = {nlink}, shared inode"
        else:
            report.hardlink_ok = False
            report.hardlink_detail = f"unexpected link count {nlink}"
    except OSError as e:
        report.hardlink_ok = False
        report.hardlink_detail = str(e)
    finally:
        for p in (src, dst):
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass

    if not report.same_filesystem:
        report.fatal.append(
            "Torr and Media are on different filesystems -- hardlinks impossible. "
            "Keep the whole data path X on ONE disk/volume.")
    elif not report.hardlink_ok:
        report.fatal.append(
            f"Hardlink test failed ({report.hardlink_detail}). Imports would fall "
            "back to slow full copies and break seeding-while-organized.")


def check_chmod(path: Path, report: FsReport) -> None:
    probe = path / ".arr_chmod_probe"
    try:
        probe.write_text("x")
        os.chmod(probe, 0o600)
        mode1 = os.stat(probe).st_mode & 0o777
        os.chmod(probe, 0o644)
        mode2 = os.stat(probe).st_mode & 0o777
        report.chmod_ok = (mode1 != mode2) or (mode2 == 0o644)
        if report.chmod_ok:
            report.chmod_detail = "POSIX permission changes honored"
        else:
            report.chmod_detail = "chmod is a no-op (non-POSIX filesystem)"
    except OSError as e:
        report.chmod_ok = False
        report.chmod_detail = f"chmod not supported ({e})"
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def check_inotify(path: Path, report: FsReport) -> None:
    """Best-effort: on Linux native FS inotify fires; on /mnt/<drive> it usually
    does not. We don't hard-fail on this -- Sonarr/Radarr fall back to polling."""
    if report.on_windows_mount:
        report.inotify_ok = False
        report.inotify_detail = ("file-change events unreliable on Windows-hosted "
                                 "files; Sonarr/Radarr will use periodic scans")
        return
    # Native Linux path: assume working (a full inotify round-trip test is racy).
    report.inotify_ok = True
    report.inotify_detail = "native filesystem, inotify supported"


def _is_trashed_path(path: Path) -> bool:
    """True if the chosen data path lives inside a desktop Trash folder.

    Installing into (or resurrecting a stack bound to) a Trash location is the
    #1 data-loss footgun: a file manager can purge it at any moment while the
    stack runs, detaching every bind mount. Reject it early.
    """
    p = str(path).replace("\\", "/").lower()
    markers = ("/.local/share/trash/", "/.trash/", "/$recycle.bin/",
               "/recycler/", "/.trash-")
    return any(m in p for m in markers)


def run_doctor(data_path: str, preview: bool = False) -> FsReport:
    path = Path(data_path)
    report = FsReport(path=str(path))

    if _is_trashed_path(path):
        report.fatal.append(
            "The chosen data path is inside a Trash/Recycle Bin folder. Pick a "
            "normal location -- a file manager could delete it at any time and "
            "detach the running containers' storage.")
        return report

    check_writable(path, report, create_if_missing=not preview)
    if not report.writable:
        return report

    report.fs_type = _detect_fs_type(path)
    report.on_windows_mount = _is_windows_mount(path, report.fs_type)

    if preview:
        # Non-destructive mode for GUI path previews: probe in a temporary folder
        # so typing/editing the path never leaves Torr/Media directories behind.
        with tempfile.TemporaryDirectory(prefix=".arr_probe_", dir=str(path)) as d:
            probe_root = Path(d)
            check_hardlink(probe_root / "Torr", probe_root / "Media", report)
    else:
        torr = path / "Torr"
        media = path / "Media"
        check_hardlink(torr, media, report)
    check_chmod(path, report)
    check_inotify(path, report)

    if report.on_windows_mount:
        report.caveats.append(
            "Media is on a Windows-hosted filesystem (NTFS via WSL2 /mnt). "
            "Expect slower I/O than native Linux storage.")
        if not report.chmod_ok:
            report.caveats.append(
                "chmod/chown are no-ops here; container PUID/PGID ownership is "
                "handled by WSL2 and is fine for these images.")
        if not report.inotify_ok:
            report.caveats.append(
                "inotify/file-watch does not fire on this mount; Sonarr/Radarr "
                "use periodic library scans instead (functional, slightly slower "
                "to notice manual drops).")
    return report


def print_report(report: FsReport) -> None:
    from . import ui
    # Bailed out before running the capability probes (e.g. Trash path or a path
    # we could not even create): show only the actionable fatal reason(s).
    if report.fatal and not report.fs_type:
        for f in report.fatal:
            ui.fail(f)
        return
    ui.info(f"Filesystem: {report.fs_type}  (path: {report.path})")
    if report.writable:
        ui.ok("Path writable")
    else:
        ui.fail("Path NOT writable")
    if report.hardlink_ok:
        ui.ok(f"Hardlinks SUPPORTED ({report.hardlink_detail})")
    else:
        ui.fail(f"Hardlinks FAILED ({report.hardlink_detail})")
    if report.chmod_ok:
        ui.ok(f"Permissions: {report.chmod_detail}")
    else:
        ui.warn(f"Permissions: {report.chmod_detail}")
    if report.inotify_ok:
        ui.ok(f"inotify: {report.inotify_detail}")
    else:
        ui.warn(f"inotify: {report.inotify_detail}")
    for c in report.caveats:
        ui.warn(c)
    for f in report.fatal:
        ui.fail(f)
