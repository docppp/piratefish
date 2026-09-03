"""Host-side control panel: a tiny stdlib HTTP server that can stop/start the
whole stack.

Why this exists (hardening §G1): Homepage is itself a container, so power
actions need an executor that survives the stack being stopped. The installer
runs this minimal control server on the HOST. Homepage's Power buttons call it
directly, and the panel page remains available for manual access too.
Because it runs outside Docker it can cleanly `docker compose down` every service
(Homepage included) and bring them back up.

Guarantees:
  * Never deletes data/volumes (delegates to lifecycle.down, which is `down`
    without `-v`).
  * Single-instance: binding the control port fails fast if one is already
    running (see `already_serving`).
  * Every action is idempotent and returns a clear confirmation message.

Run it with:  python install.py control-serve
The `up` subcommand starts it in the background automatically.
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import constants, lifecycle, detect, firewall, env as envmod


# ---------------------------------------------------------------------------
# HTML (pure render function -- unit-tested without a running server)
# ---------------------------------------------------------------------------

def render_page(running: bool, dashboard: str, message: str = "") -> str:
    status_txt = "running" if running else "stopped"
    status_cls = "up" if running else "down"
    banner = (f'<div class="msg">{_escape(message)}</div>' if message else "")
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PirateFish Control</title>
<style>
 body{{margin:0;background:#0f1419;color:#e6edf3;font-family:-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif;}}
 .wrap{{max-width:520px;margin:8vh auto;padding:24px;}}
 h1{{font-size:20px;margin:0 0 4px;}}
 .sub{{color:#8b98a5;margin:0 0 22px;}}
 .card{{background:#1a2230;border:1px solid #2d3a4d;border-radius:10px;padding:20px;margin-bottom:16px;}}
 .status{{font-weight:700;}}
 .status.up{{color:#3ddc84;}} .status.down{{color:#d9a441;}}
 button{{padding:11px 18px;border-radius:8px;border:none;font-weight:700;cursor:pointer;font-size:14px;margin:4px 6px 4px 0;}}
 .stop{{background:#e5534b;color:#fff;}} .start{{background:#3ddc84;color:#04121a;}}
 .open{{background:#4aa3df;color:#04121a;}} button:disabled{{opacity:.5;cursor:not-allowed;}}
 a{{color:#4aa3df;}} .msg{{background:#12324a;border:1px solid #2d3a4d;border-radius:8px;padding:10px 12px;margin-bottom:16px;}}
</style></head>
<body><div class="wrap">
 <h1>PirateFish Control</h1>
 <p class="sub">Turn the whole media stack on or off. Your data and settings are never deleted.</p>
 {banner}
 <div class="card">
   Stack is <span class="status {status_cls}">{status_txt}</span>.
   <div style="margin-top:14px;">
     <button class="stop" onclick="act('down',this)">Shut down stack</button>
     <button class="start" onclick="act('up',this)">Start stack</button>
     <button class="open" onclick="location.href='{dashboard}'">Open dashboard</button>
   </div>
 </div>
 <p class="sub">Dashboard: <a href="{dashboard}">{dashboard}</a></p>
</div>
<script>
async function act(what, btn){{
  const all=[...document.querySelectorAll('button')];
  all.forEach(b=>b.disabled=true);
  btn.textContent='Working\u2026';
  try{{
    const r=await fetch('/api/'+what,{{method:'POST'}});
    const j=await r.json();
    location.href='/?msg='+encodeURIComponent(j.message||'')+'&t='+Date.now();
  }}catch(e){{
    location.href='/?msg='+encodeURIComponent('Action failed: '+e)+'&t='+Date.now();
  }}
}}
</script>
</body></html>
""".format(banner=banner, status_cls=status_cls, status_txt=status_txt,
           dashboard=_escape(dashboard))


def _escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _safe_next_url(raw: str, fallback: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return fallback


def render_action_result(message: str, next_url: str) -> str:
    """Simple result page for one-click Homepage power actions."""
    msg = _escape(message or "")
    nxt = _escape(next_url or "")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PirateFish Power Action</title>
<style>
 body{{margin:0;background:#0f1419;color:#e6edf3;font-family:-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif;}}
 .wrap{{max-width:680px;margin:10vh auto;padding:24px;}}
 .card{{background:#1a2230;border:1px solid #2d3a4d;border-radius:10px;padding:20px;}}
 a{{color:#4aa3df;}}
</style></head>
<body><div class="wrap">
  <div class="card">
    <p>{msg}</p>
    <p><a href="{nxt}">Continue</a></p>
  </div>
</div></body></html>
"""


def _delegate_windows(action: str) -> tuple[bool, str]:
    """On a Windows host the stack lives inside WSL2; drive it by re-invoking the
    installer, which bootstraps WSL2 and delegates the compose action."""
    project = lifecycle.project_dir()
    install_py = str(project / "install.py")
    if action == "restart":
        rc = subprocess.run([sys.executable, install_py, "down"],
                            capture_output=True, text=True)
        if rc.returncode != 0:
            return False, (rc.stderr or rc.stdout or "down failed").strip()[-400:]
        action = "up"
    cmd = [sys.executable, install_py, action]
    if action == "up":
        cmd.append("--no-open")
    p = subprocess.run(cmd, capture_output=True, text=True)
    ok = p.returncode == 0
    if ok:
        return True, "Stack started." if action == "up" else "Stack stopped."
    return False, (p.stderr or p.stdout or "action failed").strip()[-400:]


def stack_running() -> bool:
    """Whether the stack is up. On Windows the stack lives inside WSL2, so the
    native `docker` check would always report stopped; query WSL2 instead."""
    if os.name != "nt":
        return lifecycle.is_running()
    from .windows_bootstrap import WSL_PROJECT_DIR, DISTRO_NAME, _distro_registered
    if not _distro_registered(DISTRO_NAME):
        return False
    cmd = ["wsl", "-d", DISTRO_NAME, "-u", "root", "--", "docker", "compose",
           "--project-name", constants.COMPOSE_PROJECT,
           "-f", f"{WSL_PROJECT_DIR}/docker-compose.yml",
           "ps", "--services", "--filter", "status=running"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.returncode == 0 and bool(p.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _perform_action(path: str) -> tuple[bool, str] | None:
    action = {"/api/down": "down", "/api/up": "up",
              "/api/restart": "restart"}.get(path)
    if action is None:
        return None
    if os.name == "nt":
        return _delegate_windows(action)

    if path == "/api/down":
        ok, message, _ = lifecycle.down()
        return ok, message
    if path == "/api/up":
        ok, message, _ = lifecycle.up(force_recreate=False)
        if ok:
            host_env = detect.detect()
            project_dir = lifecycle.project_dir()
            envvars = envmod.load_env(project_dir / ".env")
            ports = [constants.CONTROL_PORT]
            ports.extend(int(envvars.get(f"{name.upper()}_PORT", constants.SERVICES[name].port))
                         for name in constants.SERVICE_ORDER)
            firewall.open_ports(host_env, ports)
            message += f"  Open the dashboard: {lifecycle.dashboard_url()}"
        return ok, message
    if path == "/api/restart":
        lifecycle.down()
        ok, message, _ = lifecycle.up(force_recreate=True)
        return ok, message
    return None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "PirateFishControl/1.0"

    def log_message(self, *args):  # silence default stderr logging
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path == "/action/ping":
            self._send(200, json.dumps({"ok": True}), "application/json")
            return
        if parsed.path in ("/action/down", "/action/up"):
            action = "/api/down" if parsed.path.endswith("/down") else "/api/up"
            result = _perform_action(action)
            if not result:
                self._send(404, "not found")
                return
            _ok, message = result
            nxt = _safe_next_url(parse_qs(parsed.query).get("next", [""])[0],
                                 lifecycle.dashboard_url())
            self._send(200, render_action_result(message, nxt))
            return
        if parsed.path not in ("/", "/index.html"):
            self._send(404, "not found")
            return
        msg = parse_qs(parsed.query).get("msg", [""])[0]
        running = stack_running()
        self._send(200, render_page(running, lifecycle.dashboard_url(), msg))

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        result = _perform_action(path)
        if not result:
            self._send(404, json.dumps({"ok": False, "message": "unknown action"}),
                       "application/json")
            return
        ok, message = result
        self._send(200, json.dumps({"ok": ok, "message": message}),
                   "application/json")


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def already_serving(port: int = None) -> bool:
    """True if a control server (or anything) already holds the control port."""
    import socket
    port = port or constants.CONTROL_PORT
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def serve(port: int = None, host: str = "0.0.0.0") -> int:
    """Run the control server (blocking). Returns an exit code."""
    from . import ui, detect
    port = port or constants.CONTROL_PORT
    try:
        httpd = ThreadingHTTPServer((host, port), _Handler)
    except OSError as e:
        ui.warn(f"Control server could not bind port {port}: {e} "
                "(is one already running?)")
        return 1
    ip = detect.detect_lan_ip()
    ui.ok(f"PirateFish control panel on http://{ip}:{port} "
          "(Shut down / Start the stack from here).")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def serve_in_thread(port: int = None) -> threading.Thread | None:
    """Start the control server on a daemon thread if not already up."""
    if already_serving(port):
        return None
    t = threading.Thread(target=serve, kwargs={"port": port}, daemon=True)
    t.start()
    return t
