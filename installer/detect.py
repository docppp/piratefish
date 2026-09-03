"""Environment detection: OS, WSL2, Docker/Compose backend, LAN IP."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass, field


@dataclass
class Environment:
    os_name: str = ""             # "linux" | "windows" | "darwin"
    os_pretty: str = ""
    arch: str = ""
    is_wsl: bool = False
    distro_id: str = ""           # ubuntu, debian, fedora, ...
    pkg_manager: str = ""         # apt, dnf, yum, pacman, ...
    docker_path: str = ""
    docker_backend: str = ""      # "native" | "wsl2"
    wsl_distro: str = ""
    docker_ok: bool = False       # daemon reachable
    compose_ok: bool = False      # `docker compose` works
    is_admin: bool = False
    lan_ip: str = "127.0.0.1"
    notes: list = field(default_factory=list)


def _run(cmd, timeout=15):
    """Run a command, return (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _wsl_distro_names() -> list[str]:
    rc, out, err = _run(["wsl", "-l", "-q"], timeout=30)
    if rc != 0:
        return []
    names = []
    for ln in (out or "").splitlines():
        name = ln.replace("\x00", "").strip()
        if name:
            names.append(name)
    return names


def resolve_wsl_distro(preferred: str | None = None) -> str:
    """Pick the WSL distro used as the Docker host on Windows."""
    candidates = _wsl_distro_names()
    if not candidates:
        return ""

    pref = (preferred or os.environ.get("PIRATEFISH_WSL_DISTRO", "")).strip()
    if pref:
        by_lower = {d.lower(): d for d in candidates}
        hit = by_lower.get(pref.lower())
        if hit:
            return hit

    # Stable default: prefer Ubuntu if available, else first listed distro.
    for d in candidates:
        if d.lower().startswith("ubuntu"):
            return d
    return candidates[0]


def detect_lan_ip() -> str:
    """Best-effort primary LAN IP (the address other devices would use)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def _detect_linux_distro():
    distro_id, pkg = "", ""
    osr = "/etc/os-release"
    if os.path.exists(osr):
        data = {}
        for line in open(osr):
            if "=" in line:
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip().strip('"')
        distro_id = data.get("ID", "").lower()
        like = data.get("ID_LIKE", "").lower()
        if distro_id in ("ubuntu", "debian") or "debian" in like:
            pkg = "apt"
        elif distro_id in ("fedora", "rhel", "centos", "rocky", "almalinux") or "rhel" in like or "fedora" in like:
            pkg = "dnf" if shutil.which("dnf") else "yum"
        elif distro_id in ("arch", "manjaro") or "arch" in like:
            pkg = "pacman"
    return distro_id, pkg


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _check_admin(os_name: str) -> bool:
    if os_name == "windows":  # pragma: no cover - windows only
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def detect_docker(env: Environment) -> None:
    env.docker_ok = False
    env.compose_ok = False
    env.docker_path = shutil.which("docker") or ""
    if not env.docker_path:
        return

    rc, _, err = _run(["docker", "info"], timeout=25)
    env.docker_ok = rc == 0
    if not env.docker_ok:
        low = (err or "").lower()
        if "permission denied" in low or "dial unix" in low and "permission" in low:
            note = ("Docker is installed but the current user cannot talk to the "
                    "daemon (permission denied). Add your user to the 'docker' "
                    "group (sudo usermod -aG docker $USER) and log out/in, or run "
                    "with sudo.")
        elif "cannot connect" in low or "is the docker daemon running" in low:
            note = ("Docker is installed but the daemon is not running. Start it "
                    "(e.g. sudo systemctl start docker).")
        else:
            note = ""
        if note and note not in env.notes:
            env.notes.append(note)
    rc, out, _ = _run(["docker", "compose", "version"], timeout=15)
    env.compose_ok = rc == 0


def detect() -> Environment:
    env = Environment()
    sysname = platform.system().lower()
    env.os_name = {"linux": "linux", "windows": "windows",
                   "darwin": "darwin"}.get(sysname, sysname)
    env.arch = platform.machine()
    env.is_wsl = _is_wsl()

    if env.os_name == "linux":
        env.distro_id, env.pkg_manager = _detect_linux_distro()
        env.os_pretty = f"Linux ({env.distro_id or 'unknown'})"
        if env.is_wsl:
            env.os_pretty += " [WSL2]"
    elif env.os_name == "windows":
        env.os_pretty = f"Windows {platform.release()}"
    else:
        env.os_pretty = platform.platform()

    env.is_admin = _check_admin(env.os_name)
    # When delegated into WSL2 from a Windows host, the real LAN address is the
    # host's, not the WSL NAT address; the host passes it through.
    env.lan_ip = os.environ.get("PIRATEFISH_HOST_LAN_IP", "").strip() or detect_lan_ip()
    detect_docker(env)
    return env


def summarize(env: Environment):
    """Return a list of (label, value, level) tuples for pretty printing."""
    def lvl(b):
        return "ok" if b else "fail"
    rows = [
        ("OS", env.os_pretty, "ok"),
        ("Arch", env.arch, "ok"),
        ("Admin/root", "yes" if env.is_admin else "no",
         "ok" if env.is_admin else "warn"),
        ("LAN IP", env.lan_ip, "ok"),
        ("Docker", "installed" if env.docker_path else "not found",
         lvl(bool(env.docker_path))),
        ("Docker daemon", "running" if env.docker_ok else "not running",
         lvl(env.docker_ok)),
        ("Compose plugin", "available" if env.compose_ok else "missing",
         lvl(env.compose_ok)),
    ]
    return rows
