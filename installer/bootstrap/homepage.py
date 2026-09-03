"""Homepage dashboard bootstrap: generate the config YAML files.

Homepage reads settings.yaml / services.yaml / widgets.yaml / docker.yaml from
its config dir (mounted at /app/config). We generate a dashboard that links every
service (external LAN URLs for the browser) and wires status widgets using each
service's API key (internal container URLs).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import ui, constants


BACKGROUND_FILE = "piratefish.png"
BACKGROUND_IMAGE_PATH = f"/images/{BACKGROUND_FILE}"

WIDGETS_YAML = """---
- resources:
    cpu: true
    memory: true
    disk: /data
- search:
    provider: duckduckgo
    target: _blank
"""

DOCKER_YAML = """---
arrstack:
  socket: /var/run/docker.sock
"""


def _project_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _background_image_source() -> Path:
    return _project_dir() / BACKGROUND_FILE


def _copy_background_image(config_dir: Path) -> bool:
    source = _background_image_source()
    if not source.exists():
        return False
    images_dir = config_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, images_dir / BACKGROUND_FILE)
    return True


def render_settings(use_background: bool = True) -> str:
    lines = [
        "---",
        "title: ARR Stack",
        "theme: dark",
        "color: slate",
        "headerStyle: clean",
    ]
    if use_background:
        lines.append(f"background: {BACKGROUND_IMAGE_PATH}")
    lines.extend([
        "layout:",
        "  Media Management:",
        "    style: row",
        "    columns: 3",
        "  Downloads:",
        "    style: row",
        "    columns: 2",
        "  Media Server:",
        "    style: row",
        "    columns: 2",
        "",
    ])
    return "\n".join(lines)


def _service_entry(label, href, icon, widget_type=None,
                   internal_url=None, api_key=None, extra=None):
    lines = [f"    - {label}:",
             f"        href: {href}",
             f"        icon: {icon}",
             f"        siteMonitor: {href}"]
    if widget_type and internal_url:
        lines.append("        widget:")
        lines.append(f"          type: {widget_type}")
        lines.append(f"          url: {internal_url}")
        if api_key:
            lines.append(f"          key: {api_key}")
        for k, v in (extra or {}).items():
            lines.append(f"          {k}: {v}")
    return "\n".join(lines)


def render_services(lan_ip: str, ports: dict, api_keys: dict,
                    qbit_user: str, qbit_pass: str) -> str:
    def href(name):
        return f"http://{lan_ip}:{ports[name]}"

    def internal(name):
        svc = constants.SERVICES[name]
        return f"http://{name}:{svc.port}"

    blocks = []

    # --- Media Management -------------------------------------------------
    mgmt = ["- Media Management:"]
    mgmt.append(_service_entry(
        "Prowlarr", href("prowlarr"), "prowlarr.png",
        "prowlarr", internal("prowlarr"), api_keys.get("prowlarr")))
    mgmt.append(_service_entry(
        "Sonarr", href("sonarr"), "sonarr.png",
        "sonarr", internal("sonarr"), api_keys.get("sonarr")))
    mgmt.append(_service_entry(
        "Radarr", href("radarr"), "radarr.png",
        "radarr", internal("radarr"), api_keys.get("radarr")))
    mgmt.append(_service_entry(
        "Bazarr", href("bazarr"), "bazarr.png"))
    blocks.append("\n".join(mgmt))

    # --- Downloads --------------------------------------------------------
    dl = ["- Downloads:"]
    dl.append(_service_entry(
        "qBittorrent", href("qbittorrent"), "qbittorrent.png",
        "qbittorrent", f"http://qbittorrent:8080",
        None, extra={"username": qbit_user, "password": qbit_pass}))
    blocks.append("\n".join(dl))

    # --- Media Server -----------------------------------------------------
    ms = ["- Media Server:"]
    ms.append(_service_entry(
        "Jellyseerr", href("jellyseerr"), "jellyseerr.png"))
    ms.append(_service_entry(
        "Jellyfin", href("jellyfin"), "jellyfin.png"))
    blocks.append("\n".join(ms))

    return "---\n" + "\n".join(blocks) + "\n"


def render_bookmarks(lan_ip: str, ports: dict) -> str:
    """No bookmarks needed; power control is injected via custom.js button."""
    return (
        "---\n"
        "[]\n"
    )


def render_custom_css() -> str:
    return """#pf-shutdown-stack-btn {
  position: fixed;
  left: 50%;
  bottom: 24px;
  transform: translateX(-50%);
  z-index: 9999;
  border: none;
  border-radius: 12px;
  padding: 12px 16px;
  font-weight: 700;
  font-size: 14px;
  color: #fff;
  background: #c62828;
  cursor: pointer;
  box-shadow: 0 8px 22px rgba(0, 0, 0, 0.35);
}

#pf-shutdown-stack-btn:hover:not(:disabled) {
  background: #b71c1c;
}

#pf-shutdown-stack-btn:disabled {
  opacity: 0.75;
  cursor: wait;
}
"""


def render_custom_js(lan_ip: str) -> str:
    shutdown_url = f"http://{lan_ip}:{constants.CONTROL_PORT}/api/down"
    return f"""(function () {{
  const shutdownUrl = "{shutdown_url}";
  const buttonId = "pf-shutdown-stack-btn";

  function ensureButton() {{
    if (document.getElementById(buttonId)) return;
    if (!document.body) return;
    const btn = document.createElement("button");
    btn.id = buttonId;
    btn.type = "button";
    btn.textContent = "Shut down stack";
    btn.title = "Stop all PirateFish services";
    btn.addEventListener("click", function (event) {{
      event.preventDefault();
      event.stopPropagation();
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = "Shutting down...";
      fetch(shutdownUrl, {{ method: "POST", mode: "no-cors", keepalive: true }})
        .catch(function () {{}})
        .finally(function () {{
          setTimeout(function () {{
            btn.disabled = false;
            btn.textContent = "Shut down stack";
          }}, 2000);
        }});
    }});
    document.body.appendChild(btn);
  }}

  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", ensureButton);
  }} else {{
    ensureButton();
  }}

  setInterval(ensureButton, 2000);
}})();
"""


def bootstrap(config_dir: Path, lan_ip: str, ports: dict, api_keys: dict,
              qbit_user: str, qbit_pass: str, container="homepage") -> bool:
    ui.info("Generating Homepage dashboard...")
    config_dir = Path(config_dir)
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        has_background = _copy_background_image(config_dir)
        (config_dir / "settings.yaml").write_text(
            render_settings(use_background=has_background))
        (config_dir / "widgets.yaml").write_text(WIDGETS_YAML)
        (config_dir / "docker.yaml").write_text(DOCKER_YAML)
        (config_dir / "bookmarks.yaml").write_text(render_bookmarks(lan_ip, ports))
        (config_dir / "services.yaml").write_text(
            render_services(lan_ip, ports, api_keys, qbit_user, qbit_pass))
        (config_dir / "custom.css").write_text(render_custom_css())
        (config_dir / "custom.js").write_text(render_custom_js(lan_ip))
    except OSError as e:
        ui.warn(f"Could not write Homepage config: {e}")
        return False

    import subprocess
    try:
        subprocess.run(["docker", "restart", container],
                       capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    ui.ok("Homepage dashboard generated (links + status widgets).")
    return True
