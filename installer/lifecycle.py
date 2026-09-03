"""Central stack lifecycle helpers (start / stop / status) for project `arrstack`.

Everything that starts or stops the compose stack routes through here so the
behaviour is identical and idempotent whether it is triggered by the installer,
the `up`/`down` CLI subcommands, the desktop shortcuts, or the dashboard control
endpoint.

Design goals (hardening §A/§C/§G):
  * Idempotent + re-entrant: starting an already-running stack is a friendly
    no-op that converges; stopping an already-stopped stack likewise.
  * Never delete data volumes/config -- we only ever run `up`/`down`, never
    `down -v`.
  * Every docker invocation is bounded by a timeout and returns a describable
    (ok, message) result instead of hanging or raising opaquely.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import constants


def project_dir() -> Path:
    """The directory that holds docker-compose.yml / .env (the repo root)."""
    return Path(__file__).resolve().parent.parent


def compose_file(pdir: Path | None = None) -> Path:
    return (pdir or project_dir()) / "docker-compose.yml"


def compose_base(pdir: Path | None = None) -> list:
    pdir = pdir or project_dir()
    return ["docker", "compose", "--project-name", constants.COMPOSE_PROJECT,
            "-f", str(compose_file(pdir))]


def _run(cmd, timeout=600):
    """Run a command, capturing output. Returns (rc, combined_output)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    except OSError as e:
        return 127, str(e)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def docker_available(timeout=20) -> bool:
    rc, _ = _run(["docker", "info"], timeout=timeout)
    return rc == 0


def running_services(pdir: Path | None = None, timeout=30) -> list:
    """Return the names of currently-running compose services (may be empty)."""
    rc, out = _run(compose_base(pdir) + ["ps", "--services", "--filter",
                                         "status=running"], timeout=timeout)
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def is_running(pdir: Path | None = None, timeout=30) -> bool:
    return bool(running_services(pdir, timeout=timeout))


def bound_data_paths(pdir: Path | None = None, timeout=30) -> set:
    """Host paths the *running* containers are actually bind-mounted to.

    Used to detect the #1 failure mode: the data dir was moved/trashed while the
    stack ran, so containers stay bound to a now-detached inode. We compare this
    against the current DATA_PATH to spot drift and force a recreate.
    """
    paths = set()
    rc, out = _run(compose_base(pdir) + ["ps", "-q"], timeout=timeout)
    if rc != 0 or not out.strip():
        return paths
    ids = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for cid in ids:
        rc2, mounts = _run(
            ["docker", "inspect", "-f",
             "{{range .Mounts}}{{.Source}}\n{{end}}", cid], timeout=timeout)
        if rc2 != 0:
            continue
        for src in mounts.splitlines():
            src = src.strip()
            # We only care about the /data bind (the big shared mount), not
            # /config subdirs or the docker socket.
            if src and src not in ("/var/run/docker.sock",):
                paths.add(src)
    return paths


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------

def up(pdir: Path | None = None, force_recreate=False, timeout=1800):
    """Start the stack. Idempotent: returns (ok, message, already_running)."""
    pdir = pdir or project_dir()
    if not compose_file(pdir).exists():
        return (False, "No docker-compose.yml found -- run the installer first.",
                False)
    already = is_running(pdir)
    args = ["up", "-d", "--remove-orphans"]
    if force_recreate:
        args.append("--force-recreate")
    rc, out = _run(compose_base(pdir) + args, timeout=timeout)
    ok = rc == 0
    if not ok:
        return (False, out.strip()[-500:] or "docker compose up failed", already)
    if already:
        return (True, "Stack was already running -- reconciled.", True)
    return (True, "Stack started.", False)


def pull(pdir: Path | None = None, services=None, timeout=1800, progress=None):
    """Pull images for compose services.

    Returns (ok, message). `progress`, when provided, is called with dict events:
    {"status": "pulling"|"pulled", "service": str, "index": int, "total": int}.
    """
    pdir = pdir or project_dir()
    if not compose_file(pdir).exists():
        return (False, "No docker-compose.yml found -- run the installer first.")

    svc_list = list(services) if services else list(constants.SERVICE_ORDER)
    total = len(svc_list)
    if total == 0:
        return (True, "No services to pull.")

    def _emit(evt):
        if not progress:
            return
        try:
            progress(evt)
        except Exception:
            pass

    for idx, service in enumerate(svc_list, start=1):
        _emit({"status": "pulling", "service": service, "index": idx, "total": total})
        rc, out = _run(compose_base(pdir) + ["pull", service], timeout=timeout)
        if rc != 0:
            return (False, out.strip()[-500:] or f"docker compose pull {service} failed")
        _emit({"status": "pulled", "service": service, "index": idx, "total": total})
    return (True, "Images pulled.")


def down(pdir: Path | None = None, timeout=300):
    """Stop the stack (never removes volumes). Idempotent: friendly no-op when
    already stopped. Returns (ok, message, was_running)."""
    pdir = pdir or project_dir()
    if not compose_file(pdir).exists():
        return (False, "No docker-compose.yml found -- nothing to stop.", False)
    was_running = is_running(pdir)
    if not was_running:
        return (True, "Stack is already stopped -- nothing to do.", False)
    rc, out = _run(compose_base(pdir) + ["down"], timeout=timeout)
    ok = rc == 0
    if not ok:
        return (False, out.strip()[-500:] or "docker compose down failed", True)
    return (True, "Stack stopped. Your data and config are untouched.", True)


# ---------------------------------------------------------------------------
# Dashboard helpers
# ---------------------------------------------------------------------------

def dashboard_url(pdir: Path | None = None) -> str:
    from . import env as envmod, detect
    pdir = pdir or project_dir()
    try:
        ev = envmod.load_env(pdir / ".env")
        port = ev.get("HOMEPAGE_PORT", str(constants.SERVICES["homepage"].port))
    except OSError:
        port = str(constants.SERVICES["homepage"].port)
    ip = detect.detect_lan_ip()
    return f"http://{ip}:{port}"


def control_url(pdir: Path | None = None) -> str:
    from . import detect
    ip = detect.detect_lan_ip()
    return f"http://{ip}:{constants.CONTROL_PORT}"


def open_in_browser(url: str) -> None:
    import os
    import shutil
    import subprocess
    import webbrowser
    # Inside WSL2 there is usually no Linux browser; open the Windows one.
    is_wsl = "microsoft" in os.uname().release.lower() if hasattr(os, "uname") else False
    if os.environ.get("PIRATEFISH_DELEGATED") or is_wsl:
        candidates = []
        if shutil.which("wslview"):
            candidates.append(["wslview", url])
        candidates.append(["powershell.exe", "-NoProfile", "-Command",
                           f"Start-Process '{url}'"])
        candidates.append(["cmd.exe", "/c", "start", "", url])
        for cmd in candidates:
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return
            except (OSError, subprocess.SubprocessError):
                continue
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass
