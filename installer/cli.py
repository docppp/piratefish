"""Installer orchestrator: argument parsing + the end-to-end install flow.

Subcommands:
  install  (default)  full flow: deps -> checks -> compose up -> bootstrap -> verify
  doctor              filesystem capability report for a path
  verify              re-run integration checks against a running stack
  down                stop the stack
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import (ui, detect, deps, fs_checks, env as envmod, compose,
               constants, firewall, windows_bootstrap, paths, api, lifecycle)
from .state import State
from .api import ArrClient, wait_for_api_key, wait_for_http
from .bootstrap import qbittorrent as bs_qbit
from .bootstrap import servarr as bs_servarr
from .bootstrap import prowlarr as bs_prowlarr
from .bootstrap import bazarr as bs_bazarr
from .bootstrap import jellyfin as bs_jellyfin
from .bootstrap import jellyseerr as bs_jellyseerr
from .bootstrap import homepage as bs_homepage


_DEFAULT_QUALITY = {
    "resolution": "1080p",
    "release_types": ["bluray", "webdl", "webrip", "hdtv"],
    "max_bitrate_mbps": 8.0,
}


# ---------------------------------------------------------------------------
# Compose helpers
# ---------------------------------------------------------------------------

def _compose_cmd(project_dir: Path, *args):
    return lifecycle.compose_base(project_dir) + list(args)


def _emit_progress(progress, payload):
    if not progress:
        return
    try:
        progress(payload)
    except Exception:
        pass


def _check_port_conflicts(project_dir: Path, ports) -> list:
    """Return a list of (port, pid_or_desc) for host ports already in use by a
    process that is NOT our own stack. Prevents a confusing compose failure."""
    import socket
    conflicts = []
    # Ports our own already-running stack legitimately holds are fine.
    ours = set(lifecycle.running_services(project_dir))
    if ours:
        return conflicts  # our stack is up; `up` will just reconcile
    for p in ports:
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", p))
        except OSError:
            conflicts.append(p)
        finally:
            s.close()
    return conflicts


def _detect_data_drift(project_dir: Path, current_data: str) -> bool:
    """Detect the #1 failure mode: running containers bound to a different/stale
    host data path than the one we're about to install with."""
    try:
        bound = lifecycle.bound_data_paths(project_dir)
    except Exception:  # noqa
        return False
    if not bound or not current_data:
        return False
    cur = os.path.normpath(current_data)
    # Drift if NONE of the running binds point at the current data path.
    return cur not in {os.path.normpath(b) for b in bound}


def compose_up(project_dir: Path, ports=None, data_path: str = "",
               progress=None) -> bool:
    ui.step("Downloading Docker images and starting containers...")

    if ports:
        conflicts = _check_port_conflicts(project_dir, ports)
        if conflicts:
            ui.fail(f"Host port(s) already in use by another process: "
                    f"{', '.join(map(str, conflicts))}. Stop whatever is using "
                    "them (or change the ports in .env) and re-run.")
            return False

    # If containers are bound to a stale/moved data dir, force a full recreate so
    # they re-bind to the CURRENT path (moved/trashed dir is the #1 failure mode).
    force = True
    if data_path and _detect_data_drift(project_dir, data_path):
        ui.warn("Running containers are bound to a different data path than the "
                "one selected. Recreating them to bind to the current path.")

    total = len(constants.SERVICE_ORDER)
    _emit_progress(progress, {
        "phase": "docker_pull",
        "status": "start",
        "current": 0,
        "total": total,
        "percent": 0,
        "message": "Downloading Docker images...",
    })

    def _on_pull(evt):
        service = evt.get("service") or ""
        index = int(evt.get("index") or 0)
        status = evt.get("status") or "pulling"
        label = constants.SERVICES.get(service).label if service in constants.SERVICES else service
        current = index if status == "pulled" else max(index - 1, 0)
        percent = int((current * 100) / total) if total else 100
        _emit_progress(progress, {
            "phase": "docker_pull",
            "status": status,
            "service": service,
            "label": label,
            "index": index,
            "current": current,
            "total": total,
            "percent": percent,
        })

    ok_pull, pull_message = lifecycle.pull(
        project_dir, services=constants.SERVICE_ORDER, progress=_on_pull)
    if not ok_pull:
        _emit_progress(progress, {
            "phase": "docker_pull",
            "status": "error",
            "current": 0,
            "total": total,
            "percent": 0,
            "message": pull_message,
        })
        ui.fail(pull_message)
        return False

    _emit_progress(progress, {
        "phase": "docker_pull",
        "status": "done",
        "current": total,
        "total": total,
        "percent": 100,
        "message": "Docker images downloaded.",
    })

    ui.step("Starting containers...")
    ok, message, _ = lifecycle.up(project_dir, force_recreate=force)
    if not ok:
        ui.fail(message)
    return ok


# ---------------------------------------------------------------------------
# Install flow
# ---------------------------------------------------------------------------

class Context:
    def __init__(self):
        self.env = None
        self.data = None          # DataPath
        self.envvars = {}
        self.api_keys = {}
        self.ports = {}
        self.lan_ip = "127.0.0.1"
        self.qbit_user = ""
        self.qbit_pass = ""
        self.issues = []          # (component, level, detail): "skip"/"fail"

    def record(self, component, level, detail):
        self.issues.append((component, level, detail))


def _gather_config(args, environment):
    """Return a config dict from flags and interactive prompts."""
    ni = args.non_interactive

    # Data path
    raw_path = args.path
    if not raw_path:
        raw_path = ui.ask_path("Where should the ARR stack store its data?",
                               non_interactive=ni)
    dp = paths.normalize(raw_path, environment.os_name, environment.is_wsl)

    tz = _guess_tz()
    if not ni:
        tz = ui.ask("Timezone", default=tz)

    qbit_user = constants.DEFAULT_QBIT_USER
    qbit_pass = None
    if not qbit_pass:
        if not ni:
            qbit_user = ui.ask("Web UI username", default=qbit_user)
            qbit_pass = ui.ask_secret("Web UI password")
        else:
            raise RuntimeError(
                "Non-interactive console install is no longer supported. "
                "Run interactively, or use the GUI flow."
            )

    lan_subnet = _guess_subnet(environment.lan_ip)

    return {
        "data_path_obj": dp,
        "data_path": dp.mount_path,
        "fs_path": dp.fs_path,
        "tz": tz,
        "qbit_user": qbit_user,
        "qbit_pass": qbit_pass,
        "lan_subnet": lan_subnet,
        "trackers": [],
        "quality": {
            "resolution": _DEFAULT_QUALITY["resolution"],
            "release_types": list(_DEFAULT_QUALITY["release_types"]),
            "max_bitrate_mbps": _DEFAULT_QUALITY["max_bitrate_mbps"],
        },
        "jellyfin_user": qbit_user,
        "jellyfin_pass": qbit_pass,
    }


def build_config_from_dict(form: dict, environment):
    """Build the internal config dict from a GUI form (no prompting)."""
    raw_path = form["data_path"]
    dp = paths.normalize(raw_path, environment.os_name, environment.is_wsl)
    qbit_user = form.get("qbit_user") or constants.DEFAULT_QBIT_USER
    qbit_pass = form["qbit_pass"]
    return {
        "data_path_obj": dp,
        "data_path": dp.mount_path,
        "fs_path": dp.fs_path,
        "tz": form.get("tz") or _guess_tz(),
        "qbit_user": qbit_user,
        "qbit_pass": qbit_pass,
        "lan_subnet": form.get("lan_subnet") or _guess_subnet(environment.lan_ip),
        "trackers": form.get("trackers", []),
        "quality": form.get("quality", {
            "resolution": _DEFAULT_QUALITY["resolution"],
            "release_types": list(_DEFAULT_QUALITY["release_types"]),
            "max_bitrate_mbps": _DEFAULT_QUALITY["max_bitrate_mbps"],
        }),
        "jellyfin_user": form.get("jellyfin_user") or qbit_user,
        "jellyfin_pass": form.get("jellyfin_pass") or qbit_pass,
    }


def _guess_tz():
    tzfile = "/etc/timezone"
    if os.path.exists(tzfile):
        try:
            return open(tzfile).read().strip() or "Etc/UTC"
        except OSError:
            pass
    link = "/etc/localtime"
    if os.path.islink(link):
        p = os.readlink(link)
        if "zoneinfo/" in p:
            return p.split("zoneinfo/")[-1]
    return "Etc/UTC"


def _guess_subnet(ip: str) -> str:
    if ip and ip.count(".") == 3 and not ip.startswith("127."):
        a, b, c, _ = ip.split(".")
        return f"{a}.{b}.{c}.0/24"
    return "0.0.0.0/0"


def create_directories(fs_path: str) -> None:
    base = Path(fs_path)
    for sub in constants.HOST_SUBDIRS:
        (base / sub).mkdir(parents=True, exist_ok=True)
    ui.ok(f"Directory tree ready under {fs_path}")


def _base_url(ctx, name):
    return f"http://127.0.0.1:{ctx.ports[name]}"


def _wait_all_healthy(ctx) -> None:
    ui.step("Waiting for service WebUIs to respond...")
    for name in constants.SERVICE_ORDER:
        url = _base_url(ctx, name)
        label = constants.SERVICES[name].label
        if wait_for_http(url, timeout=240, label=label):
            ui.ok(f"  {label} is up ({url})")
        else:
            ui.warn(f"  {label} not responding yet ({url})")


def _read_servarr_keys(ctx, fs_path):
    for name in ("prowlarr", "sonarr", "radarr"):
        svc = constants.SERVICES[name]
        cfg_xml = Path(fs_path) / svc.config_subdir / "config.xml"
        key = wait_for_api_key(cfg_xml, timeout=120, container=name,
                               label=f"{svc.label} API key")
        if key:
            ctx.api_keys[name] = key
            ui.ok(f"  {svc.label} API key acquired.")
        else:
            ui.warn(f"  {svc.label} API key not found (checked container + {cfg_xml})")


def run_install(args) -> int:
    ui.banner()
    environment = detect.detect()

    ui.header("Phase 1 -- Environment detection")
    for label, val, level in detect.summarize(environment):
        {"ok": ui.ok, "warn": ui.warn, "fail": ui.fail}[level](f"{label}: {val}")

    state_path = Path(args.state or (Path.home() / ".arrstack_state.json"))
    state = State(state_path)

    # Ensure Docker + Compose (Linux/WSL2 install path). On Windows the whole
    # installer is delegated into WSL2 before reaching here.
    ui.header("Phase 2 -- Ensuring Docker + Compose")
    try:
        deps.ensure_docker(environment, non_interactive=args.non_interactive)
    except deps.DependencyError as e:
        ui.fail(str(e))
        return 2

    # Gather user configuration.
    ui.header("Phase 3 -- Configuration")
    cfg = _gather_config(args, environment)
    ctx = Context()
    ctx.env = environment
    ctx.data = cfg["data_path_obj"]
    ctx.lan_ip = environment.lan_ip
    ctx.qbit_user = cfg["qbit_user"]
    ctx.qbit_pass = cfg["qbit_pass"]
    ui.ok(f"Data path: {cfg['data_path_obj'].display}")
    ui.ok(f"Timezone: {cfg['tz']}   LAN subnet: {cfg['lan_subnet']}")

    code = execute_install(cfg, environment, ctx,
                           non_interactive=args.non_interactive,
                           run_console_wizard=True)
    if code == 0:
        state.set("data_path", cfg["fs_path"])
        state.set("api_keys", ctx.api_keys)
        state.mark_done("install")
    return code


def execute_install(cfg, environment, ctx, non_interactive=False,
                    run_console_wizard=True, progress=None):
    """Run the install flow under a single-instance lock.

    The lock lives here (not in `main`) because this is the one funnel every
    front-end goes through -- console `run_install`, the GUI worker thread, and
    the re-exec'd `gui-run` subprocess all call it. Locking here (rather than in
    the parent process) means the GUI's venv re-exec doesn't deadlock against a
    lock held by its own parent.
    """
    from .lock import InstallLock, LockHeld
    lock_path = Path(__file__).resolve().parent.parent / ".install.lock"
    try:
        lk = InstallLock(lock_path).acquire()
    except LockHeld as e:
        ui.fail(f"Another PirateFish installer is already running (pid {e.pid}). "
                "Wait for it to finish, or stop it, then try again.")
        return 5
    try:
        return _execute_install(cfg, environment, ctx,
                                non_interactive=non_interactive,
                                run_console_wizard=run_console_wizard,
                                progress=progress)
    finally:
        lk.release()


def _execute_install(cfg, environment, ctx, non_interactive=False,
                     run_console_wizard=True, progress=None):
    """Run phases 5-9 (doctor -> provision -> bootstrap -> firewall -> verify).

    Shared by the console and GUI front-ends. `ctx` is mutated in place with
    ports, api_keys, etc. Returns an exit code (0 = success).
    """
    # Filesystem doctor.
    ui.header("Phase 4 -- Filesystem capability check")
    report = fs_checks.run_doctor(cfg["fs_path"])
    fs_checks.print_report(report)
    ctx.fs_report = report
    if not report.safe_to_proceed:
        ui.fail("Filesystem is not suitable for the stack. Aborting.")
        for f in report.fatal:
            ui.fail(f)
        return 3

    # Create dirs + generate compose/env.
    ui.header("Phase 5 -- Provisioning")
    create_directories(cfg["fs_path"])

    project_dir = Path(__file__).resolve().parent.parent
    envvars = envmod.build_env({
        "data_path": cfg["data_path"],
        "tz": cfg["tz"],
        "qbit_user": cfg["qbit_user"],
        "qbit_pass": cfg["qbit_pass"],
        "lan_subnet": cfg["lan_subnet"],
    })
    ctx.envvars = envvars
    ctx.ports = {n: int(envvars[f"{n.upper()}_PORT"])
                 for n in constants.SERVICE_ORDER}

    envmod.write_env(envvars, project_dir / ".env")
    compose.write_compose(project_dir / "docker-compose.yml")
    ui.ok("Generated docker-compose.yml and .env")

    if not compose_up(project_dir, ports=ctx.ports.values(),
                      data_path=cfg["data_path"], progress=progress):
        ui.fail("docker compose up failed. Check the output above.")
        return 4
    ui.ok("Containers started.")

    _wait_all_healthy(ctx)

    # ------------------------------------------------------------------
    # Bootstrap integrations
    # ------------------------------------------------------------------
    ui.header("Phase 6 -- Automatic integration bootstrap")
    _read_servarr_keys(ctx, cfg["fs_path"])

    # qBittorrent
    try:
        bs_qbit.bootstrap(_base_url(ctx, "qbittorrent"),
                          ctx.qbit_user, ctx.qbit_pass, cfg["lan_subnet"])
    except Exception as e:  # noqa
        ui.warn(f"qBittorrent bootstrap issue: {e}")
        ctx.record("qBittorrent", "fail", str(e))

    # Sonarr / Radarr
    if "sonarr" in ctx.api_keys:
        try:
            c = ArrClient(_base_url(ctx, "sonarr"), ctx.api_keys["sonarr"], "v3")
            bs_servarr.bootstrap_sonarr(c, "qbittorrent", 8080,
                                        ctx.qbit_user, ctx.qbit_pass)
            bs_servarr.configure_authentication(c, ctx.qbit_user, ctx.qbit_pass)
            from .bootstrap import quality as bs_quality
            bs_quality.apply(c, cfg.get("quality", _DEFAULT_QUALITY), "series",
                             update_existing=False, prune_other_profiles=True)
        except Exception as e:  # noqa
            ui.warn(f"Sonarr bootstrap issue: {e}")
            ctx.record("Sonarr", "fail", str(e))
    else:
        ctx.record("Sonarr", "skip", "API key unavailable")
    if "radarr" in ctx.api_keys:
        try:
            c = ArrClient(_base_url(ctx, "radarr"), ctx.api_keys["radarr"], "v3")
            bs_servarr.bootstrap_radarr(c, "qbittorrent", 8080,
                                        ctx.qbit_user, ctx.qbit_pass)
            bs_servarr.configure_authentication(c, ctx.qbit_user, ctx.qbit_pass)
            from .bootstrap import quality as bs_quality
            bs_quality.apply(c, cfg.get("quality", _DEFAULT_QUALITY), "movie",
                             update_existing=False, prune_other_profiles=True)
        except Exception as e:  # noqa
            ui.warn(f"Radarr bootstrap issue: {e}")
            ctx.record("Radarr", "fail", str(e))
    else:
        ctx.record("Radarr", "skip", "API key unavailable")

    # Prowlarr: register apps
    if all(k in ctx.api_keys for k in ("prowlarr", "sonarr", "radarr")):
        try:
            pc = ArrClient(_base_url(ctx, "prowlarr"), ctx.api_keys["prowlarr"], "v1")
            bs_prowlarr.register_apps(
                pc, "http://prowlarr:9696",
                "http://sonarr:8989", ctx.api_keys["sonarr"],
                "http://radarr:7878", ctx.api_keys["radarr"])
            bs_servarr.configure_authentication(pc, ctx.qbit_user, ctx.qbit_pass)
        except Exception as e:  # noqa
            ui.warn(f"Prowlarr application registration issue: {e}")
            ctx.record("Prowlarr apps", "fail", str(e))
    else:
        ctx.record("Prowlarr apps", "skip",
                   "needs Prowlarr+Sonarr+Radarr API keys")

    # Bazarr
    try:
        bs_bazarr.bootstrap(_base_url(ctx, "bazarr"),
                            ctx.api_keys.get("sonarr", ""),
                            ctx.api_keys.get("radarr", ""))
    except Exception as e:  # noqa
        ui.warn(f"Bazarr bootstrap issue: {e}")
        ctx.record("Bazarr", "fail", str(e))

    # Jellyfin
    try:
        bs_jellyfin.bootstrap(_base_url(ctx, "jellyfin"),
                             cfg["jellyfin_user"], cfg["jellyfin_pass"])
    except Exception as e:  # noqa
        ui.warn(f"Jellyfin bootstrap issue: {e}")
        ctx.record("Jellyfin", "fail", str(e))

    # Jellyseerr
    try:
        bs_jellyseerr.bootstrap(_base_url(ctx, "jellyseerr"),
                                cfg["jellyfin_user"], cfg["jellyfin_pass"],
                                jellyfin_host="jellyfin",
                                jellyfin_port=constants.SERVICES["jellyfin"].port)
    except Exception as e:  # noqa
        ui.warn(f"Jellyseerr bootstrap issue: {e}")
        ctx.record("Jellyseerr", "fail", str(e))

    # Homepage
    try:
        bs_homepage.bootstrap(Path(cfg["fs_path"]) / "Arr" / "homepage",
                             ctx.lan_ip, ctx.ports, ctx.api_keys,
                             ctx.qbit_user, ctx.qbit_pass)
    except Exception as e:  # noqa
        ui.warn(f"Homepage bootstrap issue: {e}")
        ctx.record("Homepage", "fail", str(e))

    # Guided indexer wizard (console only; the GUI runs its own tracker wizard).
    if run_console_wizard and "prowlarr" in ctx.api_keys:
        try:
            pc = ArrClient(_base_url(ctx, "prowlarr"), ctx.api_keys["prowlarr"], "v1")
            bs_prowlarr.indexer_wizard(pc, non_interactive=non_interactive,
                                       tracker_configs=cfg.get("trackers"))
        except Exception as e:  # noqa
            ui.warn(f"Indexer wizard issue: {e}")
            ctx.record("Indexer wizard", "fail", str(e))

    # Firewall
    ui.header("Phase 7 -- LAN access (firewall)")
    firewall_ports = list(ctx.ports.values()) + [constants.CONTROL_PORT]
    firewall.open_ports(environment, firewall_ports)

    # Desktop Start shortcut + host control panel (dashboard Power stop/start).
    ui.header("Phase 8 -- Desktop shortcuts & control panel")
    from . import shortcut
    if not os.environ.get("PIRATEFISH_DELEGATED"):
        shortcut.create(environment)
    _start_control_server(project_dir)

    # Verify + report
    ui.header("Phase 9 -- Verification & report")
    from .verify import run_verify, print_final_report
    run_verify(ctx)
    print_final_report(ctx, cfg, report, project_dir)
    ctx.cfg = cfg
    return 0


def _start_control_server(project_dir: Path) -> None:
    """Launch the host control panel (Shut down/Start the stack) as a detached
    background process, unless one is already running."""
    from . import control
    # When the installer runs inside WSL2 under a Windows host, the control
    # server is owned by the Windows host process instead (so it is reachable at
    # the host's address and can drive `wsl`), not by this delegated Linux run.
    if os.environ.get("PIRATEFISH_DELEGATED"):
        return
    try:
        if control.already_serving():
            ui.ok(f"Control panel already running at {lifecycle.control_url()}.")
            return
    except OSError:
        pass
    try:
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:  # Windows: fully detach so it survives the installer exiting.
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0x8)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
        subprocess.Popen(
            [sys.executable, str(project_dir / "install.py"), "control-serve"],
            cwd=str(project_dir),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, **kwargs)
        ui.ok(f"Control panel started at {lifecycle.control_url()} "
              "(use the dashboard Power button to shut the stack down).")
    except OSError as e:
        ui.warn(f"Could not start the control panel: {e}")


# ---------------------------------------------------------------------------
# GUI launch (with graceful console fallback)
# ---------------------------------------------------------------------------

def _try_launch_gui(args):
    """Attempt to run the graphical installer. Returns an exit code if the GUI
    ran (or re-exec happened), or None to signal 'fall back to console'."""
    try:
        from . import gui_deps
        environment = detect.detect()
        gui_python = gui_deps.ensure_gui(environment)
        if not gui_python:
            return None
        # If the GUI needs a different interpreter (venv), re-exec there.
        if os.path.abspath(gui_python) != os.path.abspath(sys.executable):
            cmd = [gui_python, "-m", "installer", "gui-run"]
            for flag, val in (("--path", args.path), ("--state", args.state)):
                if val:
                    cmd += [flag, val]
            return subprocess.call(cmd, cwd=str(Path(__file__).resolve().parent.parent))
        from .gui.app import run_gui
        return run_gui(args)
    except Exception as e:  # noqa - GUI must never hard-crash the installer
        ui.warn(f"GUI could not start ({e}); using the console installer.")
        return None




def _run_up(args) -> int:
    """`up` subcommand: idempotent start used by the desktop Start shortcut.

    Safe to run twice in a row: if the stack is already running it converges,
    re-opens the dashboard, and reports "already running" instead of erroring."""
    project_dir = Path(__file__).resolve().parent.parent
    environment = detect.detect()
    if not lifecycle.docker_available():
        ui.fail("Docker is not running. Start Docker in WSL2 and try again.")
        return 2
    ok, message, already = lifecycle.up(project_dir, force_recreate=False)
    if already:
        ui.ok("PirateFish stack is already running.")
    (ui.ok if ok else ui.fail)(message)
    if not ok:
        return 1

    envvars = envmod.load_env(project_dir / ".env")
    firewall_ports = [constants.CONTROL_PORT]
    firewall_ports.extend(
        int(envvars.get(f"{name.upper()}_PORT", constants.SERVICES[name].port))
        for name in constants.SERVICE_ORDER
    )
    firewall.open_ports(environment, firewall_ports)

    # Ensure the host control panel is running so the dashboard Stop button works.
    _start_control_server(project_dir)
    if not getattr(args, "no_open", False):
        url = lifecycle.dashboard_url(project_dir)
        ui.info(f"Opening the dashboard: {url}")
        lifecycle.open_in_browser(url)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="arr-installer",
        description="Near zero-touch installer for a full *Arr media stack.")
    sub = p.add_subparsers(dest="command")

    inst = sub.add_parser("install", help="Install and configure the whole stack.")
    inst.add_argument("--path", help="Data path X (e.g. /mnt/media/ArrStack).")
    inst.add_argument("--state", help="Path to installer state file.")
    inst.add_argument("--gui", dest="gui", action="store_true", default=None,
                      help="Force the graphical installer.")
    inst.add_argument("--no-gui", dest="gui", action="store_false",
                      help="Force the console installer.")

    doc = sub.add_parser("doctor", help="Check filesystem capabilities of a path.")
    doc.add_argument("path", help="Path to test (data path X).")

    ver = sub.add_parser("verify", help="Re-check a running stack's integrations.")
    ver.add_argument("--path", help="Data path X (to locate config).")

    dn = sub.add_parser("down", help="Stop the whole stack (data is kept).")

    upp = sub.add_parser("up", help="Start the whole stack (idempotent) and open "
                                    "the dashboard.")
    upp.add_argument("--no-open", action="store_true",
                     help="Do not open the dashboard in a browser.")

    # Internal: run the host control panel (Shut down/Start button backend).
    sub.add_parser("control-serve", help=argparse.SUPPRESS)

    # Internal: run the GUI directly (used when re-exec'ing under a venv python).
    gr = sub.add_parser("gui-run", help=argparse.SUPPRESS)
    gr.add_argument("--path", help=argparse.SUPPRESS)
    gr.add_argument("--state", help=argparse.SUPPRESS)

    # Allow `install`-style flags on the bare command too.
    p.add_argument("--path", help=argparse.SUPPRESS)
    p.add_argument("--state", help=argparse.SUPPRESS)
    p.add_argument("--gui", dest="gui", action="store_true", default=None,
                   help=argparse.SUPPRESS)
    p.add_argument("--no-gui", dest="gui", action="store_false", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    # Native Windows: all Docker work happens inside WSL2. This process only
    # bootstraps WSL2 and re-executes the installer there. The host-side control
    # server stays native (it delegates its own actions), so it is excluded.
    if os.name == "nt" and args.command != "control-serve":
        from . import windows_bootstrap
        try:
            return windows_bootstrap.run(args, argv)
        except windows_bootstrap.WindowsBootstrapError as e:
            ui.fail(str(e))
            return 2

    if args.command in (None, "install"):
        # Normalize attributes for the bare-invocation case.
        for attr in ("path", "non_interactive", "state", "gui"):
            if not hasattr(args, attr):
                setattr(args, attr, None)
        # Decide GUI vs console. Default: GUI when a display is available and not
        # running unattended; console otherwise. --gui/--no-gui force it.
        want_gui = args.gui
        if want_gui is None:
            want_gui = not args.non_interactive
        if want_gui:
            launched = _try_launch_gui(args)
            if launched is not None:
                return launched
            # else: fall through to console installer
        try:
            return run_install(args)
        except KeyboardInterrupt:
            print()
            ui.warn("Interrupted by user.")
            return 130

    if args.command == "doctor":
        report = fs_checks.run_doctor(args.path)
        fs_checks.print_report(report)
        return 0 if report.safe_to_proceed else 3

    if args.command == "verify":
        from .verify import verify_standalone
        return verify_standalone(args)

    if args.command == "down":
        project_dir = Path(__file__).resolve().parent.parent
        ok, message, _ = lifecycle.down(project_dir)
        (ui.ok if ok else ui.fail)(message)
        return 0 if ok else 1

    if args.command == "up":
        return _run_up(args)

    if args.command == "control-serve":
        from . import control
        return control.serve()

    if args.command == "gui-run":
        for attr in ("path", "state"):
            if not hasattr(args, attr):
                setattr(args, attr, None)
        args.non_interactive = False
        args.gui = True
        from .gui.app import run_gui
        return run_gui(args)

    parser.print_help()
    return 1
