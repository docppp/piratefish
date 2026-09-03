"""Windows host driver: run the Linux installer *inside* WSL2.

On native Windows the installer does not run the stack itself. It only:

  1. Ensures a WSL2 Ubuntu distribution exists (the single step that needs
     Administrator rights) and that it boots with systemd (so Docker runs as a
     normal Linux service).
  2. Copies this project into the distro.
  3. Re-executes the very same Linux installer *inside* WSL2, where Docker is
     installed and run exactly like on native Linux -- no path translation, no
     per-command `wsl` wrapping.
  4. Runs a thin LAN shim (Windows Firewall + `netsh portproxy`) so the ports
     published inside WSL2 are reachable from other devices on the network.

Everything heavy therefore happens in Linux userspace. `run()` is the entry
point used by `cli.main` on native Windows.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from . import ui, constants, firewall
from .detect import detect
from .state import State

WSL_PROJECT_DIR = "/opt/piratefish"
# A dedicated distro so PirateFish never touches the user's other WSL distros.
DISTRO_NAME = os.environ.get("PIRATEFISH_WSL_DISTRO") or "PirateFish-Ubuntu"
# Base image used when creating that distro (installed under the custom name).
BASE_DISTRO = "Ubuntu"
# Fallback rootfs used with `wsl --import` when `wsl --install --name` is
# unavailable. Overridable for offline/mirrored setups.
DEFAULT_ROOTFS_URL = os.environ.get(
    "PIRATEFISH_ROOTFS_URL",
    "https://cloud-images.ubuntu.com/wsl/noble/current/"
    "ubuntu-noble-wsl-amd64-24.04lts.rootfs.tar.gz")


class WindowsBootstrapError(Exception):
    pass


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(cmd, timeout=1800):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _wsl(distro, args, timeout=1800, as_root=True):
    cmd = ["wsl", "-d", distro]
    if as_root:
        cmd += ["-u", "root"]
    cmd += ["--"] + list(args)
    return _run(cmd, timeout=timeout)


def _to_wsl_path(win_path: str) -> str:
    p = (win_path or "").replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return f"/mnt/{p[0].lower()}{p[2:]}"
    return p


# ---------------------------------------------------------------------------
# WSL2 presence / distro / systemd
# ---------------------------------------------------------------------------

def _wsl_exe() -> str:
    exe = shutil.which("wsl")
    if exe:
        return exe
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "wsl.exe"
    return str(candidate) if candidate.exists() else ""


def _wsl_out(args, timeout=60):
    """Run wsl.exe and return (rc, text). wsl.exe emits UTF-16LE when its output
    is redirected; decoding as latin-1 and stripping NUL bytes reliably recovers
    the ASCII keywords/distro names we match on."""
    exe = _wsl_exe()
    if not exe:
        return 1, ""
    try:
        p = subprocess.run([exe, *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    raw = (p.stdout or b"") + (p.stderr or b"")
    text = raw.decode("latin-1", "replace").replace("\x00", "")
    return p.returncode, text


def _wsl_installed() -> bool:
    """True if the WSL feature itself is installed (with or without a distro)."""
    if not _wsl_exe():
        return False
    rc, out = _wsl_out(["--status"], 30)
    low = out.lower()
    if "optional component is not enabled" in low:
        return False
    if rc == 0 and low.strip():
        return True
    # `--version` (Store WSL) succeeding also proves the feature is present.
    rc2, out2 = _wsl_out(["--version"], 30)
    if rc2 == 0 and out2.strip():
        return True
    # Finally, listing: a distro list OR a "no installed distributions" message
    # both prove the feature is present; only "component is not enabled" doesn't.
    rc3, out3 = _wsl_out(["-l", "-q"], 30)
    low3 = out3.lower()
    if "optional component is not enabled" in low3:
        return False
    if rc3 == 0:
        return True
    return "no installed distribution" in low3 or "no installed distro" in low3


def _list_distros() -> list:
    """Registered distro names (NUL-safe)."""
    rc, out = _wsl_out(["-l", "-q"], 30)
    if rc != 0 and not out:
        return []
    names = []
    for ln in out.replace("\r", "").splitlines():
        name = ln.strip()
        if name and "no installed" not in name.lower():
            names.append(name)
    return names


def _distro_registered(name: str) -> bool:
    return any(d.lower() == name.lower() for d in _list_distros())


def _virtualization_ok() -> bool:
    rc, out, _ = _run(["powershell", "-NoProfile", "-Command",
                       "(Get-CimInstance Win32_Processor)."
                       "VirtualizationFirmwareEnabled"], timeout=30)
    if rc == 0 and out.strip().lower() == "false":
        return False
    return True


def _install_wsl_core() -> None:
    ui.step("Enabling WSL2...")
    rc, _, _ = _run(["wsl", "--install", "--no-distribution"], timeout=1200)
    if rc != 0:
        _run(["dism.exe", "/online", "/enable-feature",
              "/featurename:Microsoft-Windows-Subsystem-Linux", "/all", "/norestart"])
        _run(["dism.exe", "/online", "/enable-feature",
              "/featurename:VirtualMachinePlatform", "/all", "/norestart"])
    # Best-effort: pull the latest kernel and default to WSL2. Harmless if already done.
    _run(["wsl", "--update"], timeout=600)
    _run(["wsl", "--set-default-version", "2"], timeout=60)


def _import_distro() -> bool:
    """Create the dedicated distro from a downloaded Ubuntu rootfs (root-only,
    no interactive account). Returns True on success."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    install_dir = Path(local) / "PirateFish" / "distro"
    install_dir.mkdir(parents=True, exist_ok=True)
    import tempfile
    rootfs = Path(tempfile.gettempdir()) / "piratefish-rootfs.tar.gz"

    ui.step(f"Downloading Ubuntu rootfs for '{DISTRO_NAME}'...")
    dl = (f"$ErrorActionPreference='Stop'; "
          f"[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
          f"Invoke-WebRequest -Uri '{DEFAULT_ROOTFS_URL}' -OutFile '{rootfs}' -UseBasicParsing")
    rc, _, err = _run(["powershell", "-NoProfile", "-Command", dl], timeout=3600)
    if rc != 0 or not rootfs.exists():
        ui.warn(f"Could not download the Ubuntu rootfs: {err.strip()[-200:]}")
        return False

    ui.step(f"Importing '{DISTRO_NAME}' into WSL2...")
    rc, out, err = _run(["wsl", "--import", DISTRO_NAME, str(install_dir),
                         str(rootfs), "--version", "2"], timeout=1800)
    try:
        rootfs.unlink()
    except OSError:
        pass
    if rc != 0:
        ui.warn(f"wsl --import failed: {(err or out).strip()[-200:]}")
        return False
    return _distro_registered(DISTRO_NAME)


def _install_distro(state: State):
    """Create the dedicated PirateFish distro without disturbing other distros.
    Returns the distro name, or None if a reboot is required first."""
    if _distro_registered(DISTRO_NAME):
        return DISTRO_NAME

    # Preferred: modern WSL can install a base image under a custom name headless.
    ui.step(f"Installing '{DISTRO_NAME}' into WSL2 (headless)...")
    rc, out, err = _run(["wsl", "--install", "-d", BASE_DISTRO,
                         "--name", DISTRO_NAME, "--no-launch", "--web-download"],
                        timeout=3600)
    if rc == 0 and _distro_registered(DISTRO_NAME):
        state.mark_done("distro_requested")
        return DISTRO_NAME

    # Fallback: import a rootfs under the custom name (works on older WSL too).
    if _import_distro():
        state.mark_done("distro_requested")
        return DISTRO_NAME

    detail = (err or out or "").strip()
    raise WindowsBootstrapError(
        f"Could not create the '{DISTRO_NAME}' WSL2 distro automatically. "
        "Update WSL ('wsl --update') and retry, or set PIRATEFISH_ROOTFS_URL to "
        "a reachable Ubuntu WSL rootfs and re-run. "
        + (f"Details: {detail[-200:]}" if detail else ""))


def _distro_version(distro: str) -> str:
    """Return '1' or '2' for the given distro's WSL version, or '' if unknown."""
    rc, out = _wsl_out(["-l", "-v"], 30)
    if rc != 0 and not out:
        return ""
    for line in out.replace("\r", "").splitlines():
        parts = line.replace("*", "").split()
        if len(parts) >= 3 and parts[0].lower() == distro.lower():
            return parts[-1]
    return ""


_SYSTEMD_CONF_SCRIPT = r'''
conf=/etc/wsl.conf
[ -f "$conf" ] || : > "$conf"
tmp=$(mktemp)
awk '
  BEGIN { done = 0 }
  /^[[:space:]]*systemd[[:space:]]*=/ { next }
  /^[[:space:]]*\[boot\]/ { print; print "systemd=true"; done = 1; next }
  { print }
  END { if (!done) print "[boot]\nsystemd=true" }
' "$conf" > "$tmp" && cat "$tmp" > "$conf"
rm -f "$tmp"
'''


def _pid1_is_systemd(distro: str) -> bool:
    rc, out, _ = _wsl(distro, ["sh", "-lc", "cat /proc/1/comm 2>/dev/null"], timeout=30)
    return rc == 0 and out.strip() == "systemd"


def _ensure_systemd(distro: str) -> None:
    """Make the distro run systemd as init so `systemctl enable --now docker`
    works from the standard Linux install path."""
    if _pid1_is_systemd(distro):
        return
    ui.step("Enabling systemd in WSL2 (restarting the distro)...")
    _wsl(distro, ["sh", "-lc", _SYSTEMD_CONF_SCRIPT], timeout=60)
    _run(["wsl", "--terminate", distro], timeout=120)
    _wsl(distro, ["sh", "-lc", "true"], timeout=120)
    if not _pid1_is_systemd(distro):
        ui.warn("systemd could not be enabled in WSL2; Docker may need to be "
                "started manually inside the distro.")


# ---------------------------------------------------------------------------
# Provision + delegate
# ---------------------------------------------------------------------------

def _ensure_wsl_prereqs(distro: str) -> None:
    # python3 + curl/tar are required to run the installer and fetch Docker;
    # python3-venv/pip let the GUI (pywebview) set up under WSLg when needed.
    script = (
        "set -e; "
        "need=0; "
        "for c in python3 curl tar pip3; do command -v $c >/dev/null 2>&1 || need=1; done; "
        "python3 -c 'import venv' >/dev/null 2>&1 || need=1; "
        "if [ \"$need\" = 0 ]; then exit 0; fi; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update && apt-get install -y python3 python3-venv python3-pip "
        "curl ca-certificates tar"
    )
    rc, out, err = _wsl(distro, ["bash", "-lc", script], timeout=1800)
    if rc != 0:
        raise WindowsBootstrapError(
            "Could not install prerequisites inside WSL2. "
            f"Details: {(err or out).strip()[-300:]}")


def _project_present(distro: str) -> bool:
    rc, _, _ = _wsl(distro, ["sh", "-lc",
                             f"test -f {WSL_PROJECT_DIR}/install.py"], timeout=30)
    return rc == 0


def _sync_project_into_wsl(distro: str) -> None:
    project = Path(__file__).resolve().parent.parent
    src = _to_wsl_path(str(project))
    # Copy code only; never overwrite WSL-owned runtime/generated state.
    excludes = ("--exclude=./.git --exclude=__pycache__ --exclude='*.pyc' "
                "--exclude=./.venv --exclude=./.env --exclude=./.install.lock "
                "--exclude=./install-report.txt")
    script = (
        f"set -e; mkdir -p {WSL_PROJECT_DIR}; cd {shlex.quote(src)}; "
        f"tar {excludes} -cf - . | tar -C {WSL_PROJECT_DIR} -xf -"
    )
    rc, out, err = _wsl(distro, ["bash", "-lc", script], timeout=600)
    if rc != 0:
        raise WindowsBootstrapError(
            "Could not copy the installer into WSL2. "
            f"Details: {(err or out).strip()[-300:]}")


def _inner_argv(argv) -> list:
    """Strip only Windows-only flags before handing the args to the WSL
    installer. `--gui`/`--no-gui` are preserved so the user's GUI choice (served
    through WSLg) is honoured inside the distro."""
    out, it = [], iter(argv or [])
    for tok in it:
        if tok == "gui-run":
            continue
        if tok == "--state":
            next(it, None)
            continue
        if tok.startswith("--state="):
            continue
        out.append(tok)
    return out


# WSLg exposes the Windows desktop to Linux GUI apps. Its env vars are set for
# the distro's default user; when we run the installer as root we set an X11
# display explicitly so the pywebview (WebKitGTK) window still appears.
_WSLG_ENV = (
    "if [ -d /mnt/wslg ]; then "
    "export DISPLAY=\"${DISPLAY:-:0}\"; "
    "export GDK_BACKEND=x11; "
    "export XDG_RUNTIME_DIR=\"${XDG_RUNTIME_DIR:-/run/user/0}\"; "
    "mkdir -p \"$XDG_RUNTIME_DIR\" 2>/dev/null || true; "
    "chmod 700 \"$XDG_RUNTIME_DIR\" 2>/dev/null || true; "
    "fi; "
)


def _delegate(distro: str, argv, host_lan_ip: str = "") -> int:
    exports = "export PIRATEFISH_DELEGATED=1; "
    if host_lan_ip:
        exports += f"export PIRATEFISH_HOST_LAN_IP={shlex.quote(host_lan_ip)}; "
    inner = "{w}{e}cd {d} && exec python3 install.py {a}".format(
        w=_WSLG_ENV, e=exports, d=WSL_PROJECT_DIR,
        a=" ".join(shlex.quote(a) for a in _inner_argv(argv)))
    cmd = ["wsl", "-d", distro, "-u", "root", "--", "bash", "-lic", inner]
    try:
        return subprocess.call(cmd)  # inherit stdio -> stays interactive
    except OSError as e:
        raise WindowsBootstrapError(f"Failed to launch installer inside WSL2: {e}")


# ---------------------------------------------------------------------------
# Media folder selection (a Windows drive, e.g. a separate SATA/M.2 disk)
# ---------------------------------------------------------------------------

def _pick_media_folder() -> str:
    """Open a native Windows folder picker so the user chooses a real Windows
    path (any fixed drive) for media/data. Returns the Windows path, or '' if the
    user cancelled or the dialog is unavailable."""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = 'Choose a Windows folder for PirateFish media and data "
        "(any drive, e.g. D:\\Media). Other devices will stream from here.'; "
        "$f.ShowNewFolderButton = $true; "
        "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($f.SelectedPath) }"
    )
    # -STA is required for Windows.Forms dialogs.
    rc, out, _ = _run(["powershell", "-NoProfile", "-STA", "-Command", ps], timeout=600)
    return (out or "").strip() if rc == 0 else ""


def _has_path_arg(argv) -> bool:
    return any(a == "--path" or a.startswith("--path=") for a in (argv or []))


# ---------------------------------------------------------------------------
# LAN reachability (must survive reboots with no user action)
# ---------------------------------------------------------------------------


def _read_ports(distro: str) -> list:
    rc, out, _ = _wsl(distro, ["bash", "-lc", f"cat {WSL_PROJECT_DIR}/.env 2>/dev/null"],
                      timeout=30)
    values = {}
    for line in (out or "").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    ports = [constants.CONTROL_PORT]
    for name in constants.SERVICE_ORDER:
        raw = values.get(f"{name.upper()}_PORT", str(constants.SERVICES[name].port))
        try:
            ports.append(int(raw))
        except ValueError:
            ports.append(constants.SERVICES[name].port)
    return ports


def _desktop_dir() -> Path:
    """Resolve the real Desktop folder (handles OneDrive/redirected desktops)."""
    rc, out, _ = _run(["powershell", "-NoProfile", "-Command",
                       "[Environment]::GetFolderPath('Desktop')"], timeout=30)
    p = (out or "").strip()
    if p:
        return Path(p)
    return Path(os.environ.get("USERPROFILE") or str(Path.home())) / "Desktop"


def _env_ports(distro: str) -> dict:
    """Return {SERVICE_NAME_UPPER_PORT: int} read from the WSL .env, filling
    defaults for anything missing."""
    rc, out, _ = _wsl(distro, ["bash", "-lc", f"cat {WSL_PROJECT_DIR}/.env 2>/dev/null"],
                      timeout=30)
    values = {}
    for line in (out or "").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    ports = {}
    for name in constants.SERVICE_ORDER:
        key = f"{name.upper()}_PORT"
        try:
            ports[name] = int(values.get(key, constants.SERVICES[name].port))
        except ValueError:
            ports[name] = constants.SERVICES[name].port
    return ports


STARTUP_BAT_NAME = "piratefish_startup.bat"


def _startup_bat_text(service_ports: list, dash_port: int) -> str:
    """A self-contained launcher that drives WSL2 directly. It does NOT depend on
    the Python installer files, so the user may delete them and this still works
    (the compose bundle lives inside WSL at /opt/piratefish)."""
    ports = " ".join(str(p) for p in service_ports)
    lines = [
        "@echo off",
        "setlocal EnableExtensions EnableDelayedExpansion",
        "title PirateFish",
        "",
        "rem --- self-elevate: LAN firewall + port-forwarding need admin ---",
        "net session >nul 2>&1",
        "if %errorlevel% neq 0 (",
        "  powershell -NoProfile -Command \"Start-Process -FilePath '%~f0' -Verb RunAs\"",
        "  exit /b",
        ")",
        "",
        f'set "DISTRO={DISTRO_NAME}"',
        f'set "COMPOSE={WSL_PROJECT_DIR}/docker-compose.yml"',
        f'set "PROJECT={constants.COMPOSE_PROJECT}"',
        f'set "PORTS={ports}"',
        f'set "DASHPORT={dash_port}"',
        "",
        "echo Starting PirateFish services inside WSL2 (%DISTRO%)...",
        "",
        "rem --- ensure the distro + Docker daemon are running ---",
        'wsl -d "%DISTRO%" -u root -- sh -lc "docker info >/dev/null 2>&1 || '
        '(systemctl start docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1); '
        'for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done; '
        'docker info >/dev/null 2>&1"',
        "if errorlevel 1 (",
        "  echo Could not start Docker inside WSL2. Open 'wsl -d %DISTRO%' and check 'docker info'.",
        "  pause",
        "  exit /b 1",
        ")",
        "",
        "rem --- start the stack ---",
        'wsl -d "%DISTRO%" -u root -- docker compose --project-name %PROJECT% -f "%COMPOSE%" up -d --remove-orphans',
        "if errorlevel 1 (",
        "  echo Failed to start the services.",
        "  pause",
        "  exit /b 1",
        ")",
        "",
        "rem --- LAN access: forward host ports to the current WSL2 IP ---",
        'set "WSLIP="',
        'for /f "tokens=1" %%I in (\'wsl -d "%DISTRO%" -- hostname -I 2^>nul\') do if not defined WSLIP set "WSLIP=%%I"',
        "if defined WSLIP (",
        "  for %%P in (%PORTS%) do (",
        '    netsh advfirewall firewall delete rule name="PirateFish TCP %%P" >nul 2>&1',
        '    netsh advfirewall firewall add rule name="PirateFish TCP %%P" dir=in action=allow protocol=TCP localport=%%P profile=any >nul 2>&1',
        "    netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=%%P >nul 2>&1",
        "    netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=%%P connectaddress=!WSLIP! connectport=%%P >nul 2>&1",
        "  )",
        ")",
        "",
        "rem --- open the dashboard in the default Windows browser ---",
        'start "" "http://127.0.0.1:%DASHPORT%"',
        "echo PirateFish is running.  Dashboard: http://127.0.0.1:%DASHPORT%",
        "timeout /t 5 >nul",
        "endlocal",
        "",
    ]
    return "\r\n".join(lines)


def _write_startup_bat(distro: str) -> Path:
    """Write the self-contained launcher into the install dir. Returns its path."""
    project = Path(__file__).resolve().parent.parent
    ports = _env_ports(distro)
    dash_port = ports.get("homepage", constants.SERVICES["homepage"].port)
    # Forward every service port to the LAN (the :8787 control panel is a Windows
    # host service, so it is not forwarded into WSL).
    service_ports = [ports[n] for n in constants.SERVICE_ORDER]
    bat = project / STARTUP_BAT_NAME
    bat.write_text(_startup_bat_text(service_ports, dash_port),
                   encoding="ascii", errors="replace")
    return bat


def lan_sync(distro: str) -> None:
    """(Re)establish LAN reachability: persistent Windows Firewall rules plus
    port-forwarding pointed at the current WSL IP."""
    env = detect()
    env.docker_backend = "wsl2"
    env.wsl_distro = distro
    firewall.open_ports(env, _read_ports(distro))


def _create_desktop_shortcut(bat_path: Path) -> None:
    """Create a Desktop shortcut that points directly at the self-contained
    startup .bat, using piratefish.ico as its icon."""
    desktop = _desktop_dir()
    if not desktop.exists():
        return
    project = Path(__file__).resolve().parent.parent
    icon = project / "piratefish.ico"
    lnk = str(desktop / "PirateFish.lnk")

    def _ps(s: str) -> str:  # escape for a PowerShell single-quoted literal
        return str(s).replace("'", "''")

    parts = [
        "$w = New-Object -ComObject WScript.Shell; ",
        f"$s = $w.CreateShortcut('{_ps(lnk)}'); ",
        f"$s.TargetPath = '{_ps(str(bat_path))}'; ",
        f"$s.WorkingDirectory = '{_ps(str(bat_path.parent))}'; ",
        "$s.Description = 'Start PirateFish'; ",
    ]
    if icon.exists():
        parts.append(f"$s.IconLocation = '{_ps(str(icon))}'; ")
    parts.append("$s.Save()")
    _run(["powershell", "-NoProfile", "-Command", "".join(parts)], timeout=60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _bootstrap_wsl(env, state: State):
    """Ensure WSL2 + Ubuntu + systemd. Returns the distro, or None if a reboot
    or another manual step is required (caller should stop cleanly). The caller
    guarantees Administrator rights before this runs."""
    if not _wsl_installed():
        if not _virtualization_ok():
            raise WindowsBootstrapError(
                "CPU virtualization is disabled in firmware (BIOS/UEFI). Enable "
                "Intel VT-x / AMD-V, then re-run. This cannot be automated.")
        if state.is_done("wsl2_requested"):
            raise WindowsBootstrapError(
                "WSL2 is still not available after reboot. Open an Administrator "
                "PowerShell, run 'wsl --install', reboot, then re-run.")
        _install_wsl_core()
        state.mark_done("wsl2_requested")
        ui.warn("WSL2 has been enabled. A RESTART IS REQUIRED.")
        ui.warn("Please REBOOT now, then run this installer again to continue.")
        return None

    distro = DISTRO_NAME if _distro_registered(DISTRO_NAME) else None
    if not distro:
        distro = _install_distro(state)
        if not distro:
            return None

    ui.ok(f"WSL2 distro ready: {distro}")

    version = _distro_version(distro)
    if version == "1":
        ui.step(f"Converting '{distro}' to WSL2...")
        rc, out, err = _run(["wsl", "--set-version", distro, "2"], timeout=1800)
        if rc != 0 or _distro_version(distro) != "2":
            raise WindowsBootstrapError(
                f"The distro '{distro}' is running on WSL1, which cannot run "
                "Docker for this setup. Convert it with "
                f"'wsl --set-version {distro} 2' and re-run. "
                f"Details: {(err or out).strip()[-200:]}")

    _ensure_systemd(distro)
    return distro


def _relaunch_elevated(argv) -> bool:
    """Relaunch this installer with Administrator rights via UAC, in a console
    window that STAYS OPEN so the user can follow progress, answer prompts, and
    read any error or reboot message. Returns True if an elevated process was
    started (this one should exit), False if elevation was declined or failed."""
    import sys
    import tempfile
    project = Path(__file__).resolve().parent.parent
    exe = sys.executable
    script = str(project / "install.py")

    def _bq(s):  # quote for a cmd.exe argument
        s = str(s)
        return f'"{s}"' if (" " in s or "\t" in s) else s

    args = " ".join(_bq(a) for a in argv)
    # A wrapper .cmd keeps the elevated console open (via 'pause') so a fast
    # failure or a "reboot required" message is never lost when the window would
    # otherwise close instantly.
    cmd_body = "\r\n".join([
        "@echo off",
        "title PirateFish installer (Administrator)",
        f'cd /d {_bq(str(project))}',
        f'{_bq(exe)} {_bq(script)} {args}',
        "set PF_RC=%ERRORLEVEL%",
        "echo.",
        "echo ============================================================",
        "echo   PirateFish installer finished (exit code %PF_RC%).",
        "echo   You can close this window.",
        "echo ============================================================",
        "pause",
        "",
    ])
    launcher = Path(tempfile.gettempdir()) / "piratefish-elevate.cmd"
    try:
        launcher.write_text(cmd_body, encoding="ascii", errors="replace")
    except OSError as e:
        ui.warn(f"Could not prepare the elevated launcher: {e}")
        return False

    def _ps(s):  # single-quote for a PowerShell literal
        return "'" + str(s).replace("'", "''") + "'"

    ps = (f"Start-Process -FilePath {_ps(str(launcher))} -Verb RunAs "
          f"-WorkingDirectory {_ps(str(project))}")
    rc, _, err = _run(["powershell", "-NoProfile", "-Command", ps], timeout=120)
    if rc != 0:
        # Non-zero here usually means the user dismissed the UAC prompt.
        return False
    return True


def _require_admin(argv) -> bool:
    """Ensure the process is elevated. Admin is needed both to set up WSL2 and to
    open Windows Firewall + port-forwarding so LAN devices can reach the services.
    Returns True to continue, False if the caller should stop (elevated relaunch
    started, or elevation refused)."""
    env = detect()
    if env.is_admin:
        return True
    ui.warn("Administrator rights are required: to enable WSL2 and to open the "
            "Windows firewall / port-forwarding so other devices on your network "
            "can reach the services.")
    ui.step("Requesting elevation (a Windows UAC prompt will appear)...")
    if _relaunch_elevated(argv):
        ui.ok("Continuing in a new Administrator window -- you can close this one.")
        return False
    raise WindowsBootstrapError(
        "Elevation was declined. Right-click the installer (or your terminal) and "
        "choose 'Run as administrator', then try again.")


def run(args, argv) -> int:
    """Native-Windows entry point: bootstrap WSL2, then delegate to the Linux
    installer inside it, and make LAN access reboot-proof."""
    ui.banner()
    ui.header("Windows -> WSL2 bootstrap")

    command = getattr(args, "command", None)
    is_install = command in (None, "install")

    # Only the install flow needs elevation (enabling WSL2, adding firewall
    # rules, and registering the start task). Afterwards the shortcut triggers
    # that elevated task, so the user never sees a UAC prompt again.
    if is_install and not _require_admin(argv):
        return 0

    # Media/data must live on a Windows drive (it is bind-mounted into the
    # containers via /mnt/<drive>). Pick it with a native Windows dialog so the
    # user can choose any drive -- including a separate/secondary disk -- rather
    # than being limited to the WSL Linux filesystem inside the installer.
    media_win = ""
    if is_install and not _has_path_arg(argv) and not getattr(args, "path", None):
        ui.step("Choose the Windows folder for media & data (a picker will open)...")
        media_win = _pick_media_folder()
        if media_win:
            ui.ok(f"Media & data folder: {media_win}")
        else:
            ui.info("No folder chosen; you can enter a path in the installer.")

    env = detect()
    state = State(Path(getattr(args, "state", None) or
                       (Path.home() / ".arrstack_state.json")))

    distro = _bootstrap_wsl(env, state)
    if distro is None:
        ui.info("Installer stopped cleanly. Re-run after the reboot / manual step.")
        return 0

    _ensure_wsl_prereqs(distro)
    # Copy the project into WSL only when (re)installing or when it is missing;
    # never on a plain up/down (that would clobber WSL-owned runtime state).
    if is_install or not _project_present(distro):
        _sync_project_into_wsl(distro)

    # Make the chosen Windows drive path available to the installer as its Linux
    # /mnt/<drive> form. Fixed SATA/M.2 drives auto-mount in WSL2 at every boot,
    # so no manual mounting is needed.
    if media_win:
        argv = list(argv) + ["--path", _to_wsl_path(media_win)]

    ui.header("Running the installer inside WSL2")
    rc = _delegate(distro, argv, host_lan_ip=env.lan_ip)

    if rc == 0 and command in (None, "install", "up"):
        ui.header("Configuring LAN access from Windows")
        try:
            lan_sync(distro)
        except Exception as e:  # noqa - LAN shim must never crash the install
            ui.warn(f"LAN access could not be configured: {e}")
        # Start the host power-control server for THIS session so the dashboard
        # Stop button works now. (Note: it is a Python service; the self-contained
        # startup .bat below does not depend on it.)
        try:
            from .cli import _start_control_server
            _start_control_server(Path(__file__).resolve().parent.parent)
        except Exception as e:  # noqa
            ui.warn(f"Could not start the host control panel: {e}")
        if is_install:
            try:
                bat = _write_startup_bat(distro)
                ui.ok(f"Created self-contained launcher: {bat.name}")
                _create_desktop_shortcut(bat)
                ui.ok("Desktop shortcut 'PirateFish' created.")
            except Exception as e:  # noqa
                ui.warn(f"Could not create the startup launcher/shortcut: {e}")
    return rc
