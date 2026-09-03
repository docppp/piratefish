"""Ensure Docker Engine + Compose plugin are installed and running.

Linux: install via the distro package manager or the official get.docker.com
script. On native Windows, Docker is managed in WSL2 by `windows_bootstrap.py`;
this module keeps the Linux install path plus shared usability checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from . import ui
from .detect import Environment, detect_docker, _run


class DependencyError(Exception):
    pass


def _sudo_prefix(env: Environment):
    """Return a command prefix list to gain root when needed."""
    if env.is_admin:
        return []
    if shutil.which("sudo"):
        return ["sudo"]
    return []  # will likely fail; caller surfaces a clear error


def _run_live(cmd, env: Environment, use_root=False):
    """Run a command streaming output; return exit code."""
    if use_root:
        cmd = _sudo_prefix(env) + cmd
    ui.step("$ " + " ".join(cmd))
    try:
        return subprocess.call(cmd)
    except OSError as e:
        ui.fail(str(e))
        return 1


def _wait_for_daemon(env: Environment, attempts=30, delay=2) -> bool:
    for i in range(attempts):
        detect_docker(env)
        if env.docker_ok:
            return True
        if i and i % 5 == 0:
            ui.info(f"  still waiting for the Docker daemon... ({i * delay}s)")
        time.sleep(delay)
    return False


def _install_compose_plugin(env: Environment) -> bool:
    """Install the compose plugin, preferring the distro package but falling
    back to a rootless user-local binary when we lack root."""
    # Rootless fallback first if we cannot elevate.
    if not env.is_admin and not shutil.which("sudo"):
        if _install_compose_rootless(env):
            return True
    if env.pkg_manager == "apt":
        _run_live(["apt-get", "update"], env, use_root=True)
        rc = _run_live(["apt-get", "install", "-y", "docker-compose-plugin"],
                       env, use_root=True)
        if rc == 0:
            return True
    elif env.pkg_manager in ("dnf", "yum"):
        rc = _run_live([env.pkg_manager, "install", "-y", "docker-compose-plugin"],
                       env, use_root=True)
        if rc == 0:
            return True
    elif env.pkg_manager == "pacman":
        rc = _run_live(["pacman", "-S", "--noconfirm", "docker-compose"],
                       env, use_root=True)
        if rc == 0:
            return True
    # Last resort: rootless user-local install.
    return _install_compose_rootless(env)


def _install_compose_rootless(env: Environment) -> bool:
    """Install the compose CLI plugin into ~/.docker/cli-plugins (no root)."""
    import os
    import platform

    arch = platform.machine()
    arch_map = {"x86_64": "x86_64", "amd64": "x86_64",
                "aarch64": "aarch64", "arm64": "aarch64"}
    march = arch_map.get(arch, arch)
    version = "v2.32.4"
    url = (f"https://github.com/docker/compose/releases/download/{version}/"
           f"docker-compose-linux-{march}")
    dest_dir = os.path.expanduser("~/.docker/cli-plugins")
    dest = os.path.join(dest_dir, "docker-compose")
    ui.info("Installing docker compose plugin (user-local, no root needed)...")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return False
    if shutil.which("curl"):
        cmd = ["curl", "-fsSL", url, "-o", dest]
    elif shutil.which("wget"):
        cmd = ["wget", "-qO", dest, url]
    else:
        return False
    rc = _run_live(cmd, env, use_root=False)
    if rc != 0:
        return False
    try:
        os.chmod(dest, 0o755)
    except OSError:
        pass
    return True


def _install_docker_linux(env: Environment) -> None:
    ui.info("Installing Docker Engine via the official convenience script...")
    # get.docker.com installs engine + CLI + compose plugin for all major distros.
    if shutil.which("curl"):
        get = ["sh", "-c",
               "curl -fsSL https://get.docker.com | sh"]
    elif shutil.which("wget"):
        get = ["sh", "-c",
               "wget -qO- https://get.docker.com | sh"]
    else:
        raise DependencyError(
            "Neither curl nor wget is available to download Docker. "
            "Install Docker manually, then re-run.")
    rc = _run_live(get, env, use_root=not env.is_admin)
    if rc != 0:
        raise DependencyError("Docker installation script failed.")


def _start_daemon(env: Environment) -> None:
    if shutil.which("systemctl"):
        _run_live(["systemctl", "enable", "--now", "docker"], env, use_root=True)
    elif shutil.which("service"):
        _run_live(["service", "docker", "start"], env, use_root=True)


def ensure_docker(env: Environment, non_interactive=False) -> None:
    """Make sure `docker` + `docker compose` work; install if missing (Linux)."""
    if env.docker_ok and env.compose_ok:
        ui.ok("Docker Engine and Compose plugin are ready.")
        return

    if env.os_name != "linux":
        # Non-Linux install path is handled by dedicated bootstrap logic.
        for note in env.notes:
            ui.warn(note)
        raise DependencyError(
            "Docker is not ready. On Windows, complete the WSL2 Docker bootstrap "
            "first, then re-run the installer.")

    # If docker exists but the daemon is unreachable due to permissions, surface
    # the actionable note before attempting a (futile) reinstall.
    if env.docker_path and not env.docker_ok:
        for note in env.notes:
            ui.warn(note)

    # Engine present but compose missing -> install just the plugin.
    if env.docker_path and env.docker_ok and not env.compose_ok:
        ui.info("Docker is running but the Compose plugin is missing.")
        if not non_interactive and not ui.ask_yes_no(
                "Install the docker compose plugin now?", True):
            raise DependencyError("Compose plugin is required.")
        if not _install_compose_plugin(env):
            raise DependencyError(
                "Could not install the compose plugin automatically. "
                "Install 'docker-compose-plugin' manually and re-run.")
        detect_docker(env)
        if not env.compose_ok:
            raise DependencyError("Compose plugin still not detected after install.")
        ui.ok("Compose plugin installed.")
        return

    # No usable docker at all.
    if not non_interactive and not ui.ask_yes_no(
            "Docker is not installed. Install Docker Engine now?", True):
        raise DependencyError("Docker is required to continue.")

    if not env.docker_path:
        _install_docker_linux(env)

    _start_daemon(env)

    if not _wait_for_daemon(env):
        raise DependencyError(
            "Docker was installed but the daemon is not reachable. "
            "You may need to add your user to the 'docker' group and re-log, "
            "or start the service manually, then re-run.")

    detect_docker(env)
    if not env.compose_ok:
        _install_compose_plugin(env)
        detect_docker(env)

    if not (env.docker_ok and env.compose_ok):
        raise DependencyError("Docker/Compose still not fully working after install.")
    ui.ok("Docker Engine and Compose plugin are ready.")
