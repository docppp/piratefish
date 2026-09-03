"""Integration verification + the final "READY" report."""

from __future__ import annotations

from pathlib import Path

from . import ui, constants
from .api import ArrClient, wait_for_http, HttpError, request


def run_verify(ctx) -> dict:
    results = {}

    # Basic reachability of every WebUI.
    for name in constants.SERVICE_ORDER:
        url = f"http://127.0.0.1:{ctx.ports[name]}"
        up = wait_for_http(url, timeout=20)
        results[name] = up
        (ui.ok if up else ui.warn)(
            f"{constants.SERVICES[name].label}: {'reachable' if up else 'not reachable'}")

    # Prowlarr applications wired?
    if ctx.api_keys.get("prowlarr"):
        try:
            pc = ArrClient(f"http://127.0.0.1:{ctx.ports['prowlarr']}",
                           ctx.api_keys["prowlarr"], "v1")
            apps = [a.get("implementation") for a in pc.get("applications")]
            if "Sonarr" in apps and "Radarr" in apps:
                ui.ok("Prowlarr <-> Sonarr/Radarr applications registered.")
            else:
                ui.warn(f"Prowlarr applications incomplete: {apps}")
            idx = pc.get("indexer")
            ui.ok(f"Prowlarr indexers configured: {len(idx)}")
        except HttpError as e:
            ui.warn(f"Prowlarr check failed: {e}")

    # Sonarr/Radarr download client + root folder.
    for name in ("sonarr", "radarr"):
        if ctx.api_keys.get(name):
            try:
                c = ArrClient(f"http://127.0.0.1:{ctx.ports[name]}",
                              ctx.api_keys[name], "v3")
                dcs = [d.get("implementation") for d in c.get("downloadclient")]
                rfs = [r.get("path") for r in c.get("rootfolder")]
                ok = "QBittorrent" in dcs and rfs
                (ui.ok if ok else ui.warn)(
                    f"{constants.SERVICES[name].label}: "
                    f"download client={'yes' if 'QBittorrent' in dcs else 'no'}, "
                    f"root folder={rfs[0] if rfs else 'none'}")
            except HttpError as e:
                ui.warn(f"{name} check failed: {e}")

    # Jellyseerr -> Jellyfin linked?
    try:
        r = request("GET", f"http://127.0.0.1:{ctx.ports['jellyseerr']}/api/v1/settings/public",
                    timeout=20)
        linked = False
        if r.status == 200 and r._body:
            try:
                linked = bool((r.json() or {}).get("jellyfinHost"))
            except Exception:
                linked = False
        (ui.ok if linked else ui.warn)(
            f"Jellyseerr: Jellyfin link {'configured' if linked else 'not configured'}")
    except HttpError as e:
        ui.warn(f"Jellyseerr check failed: {e}")

    return results


def print_final_report(ctx, cfg, fs_report, project_dir: Path) -> None:
    ip = ctx.lan_ip
    lines = []
    lines.append("=" * 60)
    lines.append("  ARR STACK -- READY")
    lines.append("=" * 60)
    lines.append("")
    lines.append("  Open these from any device on your network:")
    label_url = [
        ("Dashboard (Homepage)", ctx.ports["homepage"]),
        ("Prowlarr", ctx.ports["prowlarr"]),
        ("Sonarr", ctx.ports["sonarr"]),
        ("Radarr", ctx.ports["radarr"]),
        ("Bazarr", ctx.ports["bazarr"]),
        ("qBittorrent", ctx.ports["qbittorrent"]),
        ("Jellyseerr", ctx.ports["jellyseerr"]),
        ("Jellyfin", ctx.ports["jellyfin"]),
    ]
    for label, port in label_url:
        lines.append(f"    {label:<22} http://{ip}:{port}")
    lines.append("")
    lines.append(f"  Web UI login:  user '{ctx.qbit_user}'  (password in .env / saved report)")
    lines.append("")
    lines.append("  Two ways to add media:")
    lines.append("    1. Automated  -> add shows/movies in Sonarr/Radarr; they")
    lines.append("                     download via qBittorrent and auto-organize.")
    lines.append(f"    2. Manual     -> drop files into {cfg['data_path_obj'].display}/Torr")
    lines.append("                     then use 'Manual Import' in Sonarr/Radarr.")
    lines.append("")
    lines.append("  Control the stack (from the installer folder):")
    lines.append("    docker compose up -d      # start everything")
    lines.append("    docker compose down       # stop everything")
    lines.append("    python install.py up      # start (idempotent) + open dashboard")
    lines.append("    python install.py down    # stop the whole stack")
    lines.append("")
    lines.append("  Turn it off from any device:")
    lines.append(f"    Control panel   http://{ip}:{constants.CONTROL_PORT}")
    lines.append("    Desktop         'PirateFish (Start)'")
    lines.append("")
    # Summary of anything skipped or failed during bootstrap (§B).
    issues = getattr(ctx, "issues", None)
    if issues:
        skipped = [i for i in issues if i[1] == "skip"]
        failed = [i for i in issues if i[1] == "fail"]
        if failed or skipped:
            lines.append("  Needs your attention:")
            for comp, _lvl, detail in failed:
                lines.append(f"    - [failed]  {comp}: {detail}")
            for comp, _lvl, detail in skipped:
                lines.append(f"    - [skipped] {comp}: {detail}")
            lines.append("")
    if fs_report.caveats:
        lines.append("  Notes for this system:")
        for c in fs_report.caveats:
            lines.append(f"    - {c}")
        lines.append("")

    text = "\n".join(lines)
    print()
    print(ui.green(text) if hasattr(ui, "green") else text)

    # Persist a plaintext report alongside the data.
    try:
        report_path = Path(cfg["fs_path"]) / constants.REPORT_FILENAME
        extra = [
            "",
            "Credentials:",
            f"  Web UI user: {ctx.qbit_user}",
            f"  Web UI pass: {ctx.qbit_pass}",
            "",
            "Service API keys:",
        ] + [f"  {k}: {v}" for k, v in ctx.api_keys.items()]
        report_path.write_text(text + "\n" + "\n".join(extra) + "\n")
        import os
        try:
            os.chmod(report_path, 0o600)
        except OSError:
            pass
        ui.ok(f"Full report saved to {report_path}")
    except OSError as e:
        ui.warn(f"Could not save report: {e}")


def verify_standalone(args) -> int:
    """`verify` subcommand: rebuild a minimal context from .env and re-check."""
    from . import env as envmod, detect
    project_dir = Path(__file__).resolve().parent.parent
    env_file = project_dir / ".env"
    if not env_file.exists():
        ui.fail("No .env found -- run the installer first.")
        return 1
    envvars = envmod.load_env(env_file)

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.ports = {n: int(envvars.get(f"{n.upper()}_PORT",
                                    constants.SERVICES[n].port))
                 for n in constants.SERVICE_ORDER}
    ctx.lan_ip = detect.detect_lan_ip()
    ctx.qbit_user = envvars.get("QBIT_USER", "admin")
    ctx.qbit_pass = envvars.get("QBIT_PASS", "")
    ctx.api_keys = {}
    fs_path = args.path or envvars.get("DATA_PATH", "")
    from .api import read_config_xml, read_api_key_from_container
    for name in ("prowlarr", "sonarr", "radarr"):
        # Prefer the container read (reliable even if the host bind-mount is
        # stale); fall back to the host config.xml.
        key = read_api_key_from_container(name)
        if not key:
            cfg_xml = Path(fs_path) / constants.SERVICES[name].config_subdir / "config.xml"
            c = read_config_xml(cfg_xml)
            key = c.get("ApiKey") if c else None
        if key:
            ctx.api_keys[name] = key

    ui.header("Verifying running stack")
    run_verify(ctx)
    return 0
