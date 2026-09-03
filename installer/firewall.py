"""Open host firewall ports so LAN devices can reach the services.

Windows: create inbound `netsh advfirewall` rules (idempotent by rule name).
Linux: if ufw or firewalld is active, add allow rules; otherwise no-op (most
desktop Linux has no host firewall enabled).
"""

from __future__ import annotations

import shutil
import subprocess

from . import ui, constants


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _open_windows(ports, sudo=False):  # pragma: no cover - windows only
    added = []
    for port in ports:
        rule = f"ARRStack TCP {port}"
        # Recreate to guarantee correct action/port/profile even if a stale or
        # differently-configured rule with this name already exists.
        _run(["netsh", "advfirewall", "firewall", "delete", "rule",
              f"name={rule}"])
        rc, out = _run(["netsh", "advfirewall", "firewall", "add", "rule",
                        f"name={rule}", "dir=in", "action=allow",
                        "protocol=TCP", f"localport={port}",
                        "profile=any"])
        if rc == 0:
            added.append(port)
    return added


def _wsl_ipv4(distro: str) -> str:
    if not distro:
        return ""
    rc, out = _run(["wsl", "-d", distro, "--", "sh", "-lc", "hostname -I 2>/dev/null"])
    if rc != 0:
        return ""
    for token in out.split():
        ip = token.strip()
        if ip.count(".") == 3 and not ip.startswith("127."):
            return ip
    return ""


def _sync_wsl_portproxy(ports, distro: str):
    """Expose WSL2-published docker ports to LAN via Windows host."""
    ip = _wsl_ipv4(distro)
    if not ip:
        return [], ""

    updated = []
    for port in ports:
        _run(["netsh", "interface", "portproxy", "delete", "v4tov4",
              f"listenport={port}", "listenaddress=0.0.0.0"])
        rc, _ = _run(["netsh", "interface", "portproxy", "add", "v4tov4",
                      f"listenport={port}", "listenaddress=0.0.0.0",
                      f"connectport={port}", f"connectaddress={ip}"])
        if rc == 0:
            updated.append(port)
    return updated, ip


def _firewalld_active():
    if not shutil.which("firewall-cmd"):
        return False
    rc, out = _run(["firewall-cmd", "--state"])
    return rc == 0 and "running" in out


def _ufw_active():
    if not shutil.which("ufw"):
        return False
    rc, out = _run(["ufw", "status"])
    return rc == 0 and "Status: active" in out


def _open_linux(ports, use_root):
    prefix = [] if not use_root else (["sudo"] if shutil.which("sudo") else [])
    added = []
    if _ufw_active():
        for port in ports:
            rc, _ = _run(prefix + ["ufw", "allow", f"{port}/tcp"])
            if rc == 0:
                added.append(port)
        return added, "ufw"
    if _firewalld_active():
        for port in ports:
            _run(prefix + ["firewall-cmd", "--permanent", f"--add-port={port}/tcp"])
            added.append(port)
        _run(prefix + ["firewall-cmd", "--reload"])
        return added, "firewalld"
    return [], None


def open_ports(env, ports) -> None:
    ports = sorted(set(int(p) for p in ports))
    if env.os_name == "windows":
        added = _open_windows(ports)
        if added:
            ui.ok(f"Windows Firewall: opened ports {', '.join(map(str, added))}.")
        else:
            ui.ok("Windows Firewall: rules already present (or none needed).")

        if getattr(env, "docker_backend", "") == "wsl2":
            proxy_ports = [p for p in ports if p != constants.CONTROL_PORT]
            proxied, ip = _sync_wsl_portproxy(proxy_ports, getattr(env, "wsl_distro", ""))
            if proxied:
                ui.ok("Windows port forwarding to WSL2 configured for ports "
                      f"{', '.join(map(str, proxied))} (WSL IP {ip}).")
            else:
                ui.warn("Could not configure Windows port forwarding to WSL2. "
                        "LAN access may be limited to this PC until forwarding is fixed.")
        return

    # Linux
    added, backend = _open_linux(ports, use_root=not env.is_admin)
    if backend and added:
        ui.ok(f"{backend}: opened ports {', '.join(map(str, added))} for LAN access.")
    else:
        ui.info("No active host firewall detected (ufw/firewalld) -- ports already "
                "reachable on the LAN.")
