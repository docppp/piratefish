"""Desktop shortcuts to START the PirateFish stack + dashboard power control.

One shortcut is created on the user's Desktop:
  * "PirateFish (Start)" -- brings the stack up and opens the dashboard.

Start is idempotent / re-entrant (hardening §G2): double-clicking while the
stack is already up is a friendly no-op that just re-opens the dashboard.

The launchers are intentionally standalone: they execute `docker compose`
directly against a copied runtime bundle (`docker-compose.yml` + `.env`) so they
continue to work even if the Python installer code is removed afterwards.

Stopping is done from the dashboard Power control (host control panel), which
can shut down the whole stack including Homepage itself.

Cross-platform:
  * Linux   -> a launcher .sh + a .desktop file on the Desktop (start).
  * Windows -> a .bat launcher + a .lnk shortcut on the Desktop (start).
  * macOS   -> a .command launcher on the Desktop (start) [best-effort].

The content generators (`linux_launcher`, `windows_launcher`, `desktop_entry`,
`windows_lnk_ps`, `macos_launcher`) are pure string builders so they can be
unit-tested without a desktop environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import ui, constants


APP_NAME = "PirateFish"
START_LABEL = f"{APP_NAME} (Start)"
CONTROL_SCRIPT = f"{APP_NAME}-control.py"
ICON_FILE = "piratefish.ico"


def _project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _desktop_dir() -> Path:
    if os.name != "nt":
        try:
            out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                 capture_output=True, text=True, timeout=5)
            p = out.stdout.strip()
            if p and os.path.isdir(p):
                return Path(p)
        except (OSError, subprocess.SubprocessError):
            pass
    return Path.home() / "Desktop"


def _runtime_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData"
                                                       / "Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME",
                                   str(Path.home() / ".local" / "share")))
    return base / APP_NAME


def _prepare_runtime_bundle(source_dir: Path) -> Path:
    """Copy compose runtime files to a durable, installer-independent folder."""
    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    required = ("docker-compose.yml", ".env")
    for name in required:
        src = source_dir / name
        if not src.exists():
            raise FileNotFoundError(f"Missing required runtime file: {src}")
        dst = runtime / name
        shutil.copy2(src, dst)
        if name == ".env":
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass
    icon_src = source_dir / ICON_FILE
    if icon_src.exists():
        shutil.copy2(icon_src, runtime / ICON_FILE)
    _write_exec(runtime / CONTROL_SCRIPT, control_server_script())
    return runtime


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _cleanup_legacy_stop_shortcuts(runtime_dir: Path) -> None:
    """Remove old generated launchers/entries from prior installer versions."""
    desktop = _desktop_dir()
    for p in (
            runtime_dir / "piratefish-start.sh",
            runtime_dir / "piratefish-start.bat",
            runtime_dir / "piratefish-start.command",
            runtime_dir / "piratefish-stop.sh",
            runtime_dir / "piratefish-stop.bat",
            runtime_dir / "piratefish-stop.command",
            desktop / f"{APP_NAME} (Stop).desktop",
            desktop / f"{APP_NAME} (Stop).lnk",
            desktop / f"{APP_NAME} (Stop).command"):
        _unlink_if_exists(p)


# ---------------------------------------------------------------------------
# Pure content generators (unit-tested)
# ---------------------------------------------------------------------------

def control_server_script() -> str:
    """Standalone host control panel served from the runtime bundle folder."""
    return (
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n"
        "import html\n"
        "import json\n"
        "import socket\n"
        "import subprocess\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "from pathlib import Path\n"
        "from urllib.parse import parse_qs, urlparse\n"
        "\n"
        f"CONTROL_PORT = {constants.CONTROL_PORT}\n"
        f"COMPOSE_PROJECT = \"{constants.COMPOSE_PROJECT}\"\n"
        "ROOT = Path(__file__).resolve().parent\n"
        "COMPOSE_FILE = ROOT / \"docker-compose.yml\"\n"
        "ENV_FILE = ROOT / \".env\"\n"
        "\n"
        "def _run(cmd, timeout=600):\n"
        "    try:\n"
        "        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)\n"
        "        return p.returncode, (p.stdout or \"\") + (p.stderr or \"\")\n"
        "    except subprocess.TimeoutExpired:\n"
        "        return 124, f\"timed out after {timeout}s\"\n"
        "    except OSError as e:\n"
        "        return 127, str(e)\n"
        "\n"
        "def _compose(*args, timeout=600):\n"
        "    cmd = [\"docker\", \"compose\", \"--project-name\", COMPOSE_PROJECT,\n"
        "           \"-f\", str(COMPOSE_FILE)] + list(args)\n"
        "    return _run(cmd, timeout=timeout)\n"
        "\n"
        "def _running():\n"
        "    rc, out = _compose(\"ps\", \"--services\", \"--filter\", \"status=running\", timeout=30)\n"
        "    return rc == 0 and bool(out.strip())\n"
        "\n"
        "def _dashboard_port():\n"
        "    port = \"3000\"\n"
        "    try:\n"
        "        for line in ENV_FILE.read_text().splitlines():\n"
        "            if line.startswith(\"HOMEPAGE_PORT=\"):\n"
        "                v = line.split(\"=\", 1)[1].strip()\n"
        "                if v:\n"
        "                    port = v\n"
        "                    break\n"
        "    except OSError:\n"
        "        pass\n"
        "    return port\n"
        "\n"
        "def _dashboard_url():\n"
        "    return f\"http://127.0.0.1:{_dashboard_port()}\"\n"
        "\n"
        "def _up():\n"
        "    already = _running()\n"
        "    rc, out = _compose(\"up\", \"-d\", \"--remove-orphans\", timeout=1800)\n"
        "    if rc != 0:\n"
        "        return False, (out.strip()[-500:] or \"docker compose up failed\")\n"
        "    if already:\n"
        "        return True, \"Stack was already running -- reconciled.\"\n"
        "    return True, \"Stack started.\"\n"
        "\n"
        "def _down():\n"
        "    if not _running():\n"
        "        return True, \"Stack is already stopped -- nothing to do.\"\n"
        "    rc, out = _compose(\"down\", timeout=300)\n"
        "    if rc != 0:\n"
        "        return False, (out.strip()[-500:] or \"docker compose down failed\")\n"
        "    return True, \"Stack stopped. Your data and config are untouched.\"\n"
        "\n"
        "def _safe_next(raw, fallback):\n"
        "    raw = (raw or \"\").strip()\n"
        "    if raw.startswith(\"http://\") or raw.startswith(\"https://\"):\n"
        "        return raw\n"
        "    return fallback\n"
        "\n"
        "def _action_page(message, next_url):\n"
        "    msg = html.escape(message or \"\")\n"
        "    nxt = html.escape(next_url or \"\", quote=True)\n"
        "    return (\n"
        "        \"<!doctype html><html><head><meta charset='utf-8'><title>PirateFish Power Action</title></head>\"\n"
        "        \"<body><h1>PirateFish Power Action</h1>\"\n"
        "        f\"<p>{msg}</p><p><a href='{nxt}'>Continue</a></p>\"\n"
        "        \"</body></html>\"\n"
        "    )\n"
        "\n"
        "def _page(msg=\"\"):\n"
        "    status = \"running\" if _running() else \"stopped\"\n"
        "    message = f\"<p><strong>{html.escape(msg)}</strong></p>\" if msg else \"\"\n"
        "    dash = html.escape(_dashboard_url())\n"
        "    return (\n"
        "        \"<!doctype html><html><head><meta charset='utf-8'><title>PirateFish Control</title></head>\"\n"
        "        \"<body><h1>PirateFish Control</h1>\"\n"
        "        f\"{message}<p>Stack is <strong>{status}</strong>.</p>\"\n"
        "        \"<form method='post' action='/api/down'><button>Shut down stack</button></form>\"\n"
        "        \"<form method='post' action='/api/up'><button>Start stack</button></form>\"\n"
        "        f\"<p><a href='{dash}'>Open dashboard</a></p>\"\n"
        "        \"</body></html>\"\n"
        "    )\n"
        "\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "\n"
        "    def _send(self, code, body, ctype=\"text/html; charset=utf-8\"):\n"
        "        data = body.encode(\"utf-8\") if isinstance(body, str) else body\n"
        "        self.send_response(code)\n"
        "        self.send_header(\"Content-Type\", ctype)\n"
        "        self.send_header(\"Content-Length\", str(len(data)))\n"
        "        self.end_headers()\n"
        "        try:\n"
        "            self.wfile.write(data)\n"
        "        except OSError:\n"
        "            pass\n"
        "\n"
        "    def do_GET(self):\n"
        "        parsed = urlparse(self.path)\n"
        "        if parsed.path == \"/action/ping\":\n"
        "            self._send(200, json.dumps({\"ok\": True}), \"application/json\")\n"
        "            return\n"
        "        if parsed.path in (\"/action/down\", \"/action/up\"):\n"
        "            nxt = _safe_next(parse_qs(parsed.query).get(\"next\", [\"\"])[0], _dashboard_url())\n"
        "            if parsed.path.endswith(\"/down\"):\n"
        "                ok, message = _down()\n"
        "            else:\n"
        "                ok, message = _up()\n"
        "                if ok:\n"
        "                    message += f\"  Open the dashboard: {_dashboard_url()}\"\n"
        "            self._send(200, _action_page(message, nxt))\n"
        "            return\n"
        "        if parsed.path not in (\"/\", \"/index.html\"):\n"
        "            self._send(404, \"not found\")\n"
        "            return\n"
        "        msg = parse_qs(parsed.query).get(\"msg\", [\"\"])[0]\n"
        "        self._send(200, _page(msg))\n"
        "\n"
        "    def do_POST(self):\n"
        "        path = urlparse(self.path).path\n"
        "        if path == \"/api/down\":\n"
        "            ok, message = _down()\n"
        "        elif path == \"/api/up\":\n"
        "            ok, message = _up()\n"
        "            if ok:\n"
        "                message += f\"  Open the dashboard: {_dashboard_url()}\"\n"
        "        else:\n"
        "            self._send(404, json.dumps({\"ok\": False, \"message\": \"unknown action\"}), \"application/json\")\n"
        "            return\n"
        "        self._send(200, json.dumps({\"ok\": ok, \"message\": message}), \"application/json\")\n"
        "\n"
        "def main():\n"
        "    if not COMPOSE_FILE.exists():\n"
        "        return 1\n"
        "    try:\n"
        "        httpd = ThreadingHTTPServer((\"0.0.0.0\", CONTROL_PORT), Handler)\n"
        "    except OSError:\n"
        "        return 0\n"
        "    try:\n"
        "        httpd.serve_forever()\n"
        "    except KeyboardInterrupt:\n"
        "        pass\n"
        "    finally:\n"
        "        httpd.server_close()\n"
        "    return 0\n"
        "\n"
        "if __name__ == \"__main__\":\n"
        "    raise SystemExit(main())\n"
    )


def linux_launcher(project_dir: str, python: str, action: str) -> str:
    """Bash launcher that controls compose directly (standalone + idempotent)."""
    _ = python  # kept for backward-compatible signature in unit tests/callers
    compose = (
        "docker compose --project-name "
        f"{constants.COMPOSE_PROJECT} -f \"$COMPOSE_FILE\""
    )
    if action == "up":
        action_block = (
            "if [[ -n \"$RUNNING\" ]]; then\n"
            f"  echo \"{APP_NAME} stack is already running.\"\n"
            "else\n"
            f"  echo \"Starting {APP_NAME} stack...\"\n"
            f"  {compose} up -d --remove-orphans\n"
            f"  echo \"{APP_NAME} stack started.\"\n"
            "fi\n"
            "PORT=3000\n"
            "if [[ -f \"$ENV_FILE\" ]]; then\n"
            "  while IFS='=' read -r key value; do\n"
            "    if [[ \"$key\" == \"HOMEPAGE_PORT\" && -n \"$value\" ]]; then\n"
            "      PORT=\"$value\"\n"
            "      break\n"
            "    fi\n"
            "  done < \"$ENV_FILE\"\n"
            "fi\n"
            "URL=\"http://127.0.0.1:${PORT}\"\n"
            "if [[ -f \"$CONTROL_SCRIPT\" ]]; then\n"
            "  PY=\"\"\n"
            "  if command -v python3 >/dev/null 2>&1; then\n"
            "    PY=python3\n"
            "  elif command -v python >/dev/null 2>&1; then\n"
            "    PY=python\n"
            "  fi\n"
            "  if [[ -n \"$PY\" ]]; then\n"
            "    CTRL_OK=0\n"
            "    \"$PY\" - <<'PY' >/dev/null 2>&1\n"
            "import json\n"
            "import urllib.request\n"
            "try:\n"
            f"    with urllib.request.urlopen('http://127.0.0.1:{constants.CONTROL_PORT}/action/ping', timeout=2) as r:\n"
            "        data = json.loads((r.read() or b'{}').decode('utf-8', 'ignore'))\n"
            "        raise SystemExit(0 if (r.status == 200 and data.get('ok') is True) else 1)\n"
            "except Exception:\n"
            "    raise SystemExit(1)\n"
            "PY\n"
            "    if [[ $? -eq 0 ]]; then\n"
            "      CTRL_OK=1\n"
            "    fi\n"
            "    if [[ \"$CTRL_OK\" -eq 0 ]]; then\n"
            "      if command -v ss >/dev/null 2>&1; then\n"
            f"        OLD_PID=\"$(ss -ltnp '( sport = :{constants.CONTROL_PORT} )' 2>/dev/null | sed -n 's/.*pid=\\([0-9]\\+\\).*/\\1/p' | head -n1)\"\n"
            "        if [[ -n \"$OLD_PID\" ]]; then\n"
            "          OLD_CMD=\"$(ps -p \"$OLD_PID\" -o cmd= 2>/dev/null || true)\"\n"
            "          if [[ \"$OLD_CMD\" == *\"install.py control-serve\"* || \"$OLD_CMD\" == *\"PirateFish-control.py\"* ]]; then\n"
            "            kill \"$OLD_PID\" >/dev/null 2>&1 || true\n"
            "            sleep 1\n"
            "          fi\n"
            "        fi\n"
            "      fi\n"
            "      nohup \"$PY\" \"$CONTROL_SCRIPT\" >/dev/null 2>&1 &\n"
            "    fi\n"
            "  fi\n"
            "fi\n"
            "echo \"Opening dashboard: $URL\"\n"
            "if command -v xdg-open >/dev/null 2>&1; then\n"
            "  nohup xdg-open \"$URL\" >/dev/null 2>&1 &\n"
            "fi\n"
        )
    else:
        action_block = (
            "if [[ -z \"$RUNNING\" ]]; then\n"
            f"  echo \"{APP_NAME} stack is already stopped -- nothing to do.\"\n"
            "else\n"
            f"  echo \"Stopping {APP_NAME} stack...\"\n"
            f"  {compose} down\n"
            f"  echo \"{APP_NAME} stack stopped. Your data and config are untouched.\"\n"
            "fi\n"
        )
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"# {APP_NAME} -- {action} the whole stack (standalone + idempotent).\n"
        f'PROJECT_DIR="{project_dir}"\n'
        'COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"\n'
        'ENV_FILE="$PROJECT_DIR/.env"\n'
        f'CONTROL_SCRIPT="$PROJECT_DIR/{CONTROL_SCRIPT}"\n'
        'if [[ ! -f "$COMPOSE_FILE" ]]; then\n'
        '  echo "Missing docker-compose.yml at $COMPOSE_FILE"\n'
        "  exit 1\n"
        "fi\n"
        "if ! command -v docker >/dev/null 2>&1; then\n"
        '  echo "Docker CLI not found in PATH."\n'
        "  exit 1\n"
        "fi\n"
        f'RUNNING="$({compose} ps --services --filter status=running 2>/dev/null || true)"\n'
        + action_block
    )


def macos_launcher(project_dir: str, python: str, action: str) -> str:
    _ = python
    return (
        "#!/bin/bash\n"
        f"# {APP_NAME} -- {action} the whole stack (standalone + idempotent).\n"
        f'PROJECT_DIR="{project_dir}"\n'
        'COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"\n'
        f'CONTROL_SCRIPT="$PROJECT_DIR/{CONTROL_SCRIPT}"\n'
        'if [[ ! -f "$COMPOSE_FILE" ]]; then echo "Missing $COMPOSE_FILE"; exit 1; fi\n'
        "if [[ -f \"$CONTROL_SCRIPT\" ]]; then\n"
        "  if command -v python3 >/dev/null 2>&1; then\n"
        "    nohup python3 \"$CONTROL_SCRIPT\" >/dev/null 2>&1 &\n"
        "  elif command -v python >/dev/null 2>&1; then\n"
        "    nohup python \"$CONTROL_SCRIPT\" >/dev/null 2>&1 &\n"
        "  fi\n"
        "fi\n"
        f'docker compose --project-name {constants.COMPOSE_PROJECT} '
        '-f "$COMPOSE_FILE" '
        f'{"up -d --remove-orphans" if action == "up" else "down"}\n'
    )


def windows_launcher(project_dir: str, python: str, action: str) -> str:
    """Batch launcher (CRLF). On Windows the stack lives inside WSL2, so the
    launcher simply calls the installer entry point, which bootstraps WSL2 and
    delegates the compose action there."""
    _ = python
    verb = "down" if action == "down" else "up"
    lines = [
        "@echo off",
        "setlocal EnableExtensions",
        f"rem {APP_NAME} -- {action} the whole stack via WSL2.",
        f'set "PROJECT_DIR={project_dir}"',
        'set "INSTALLER=%PROJECT_DIR%\\install.py"',
        'if not exist "%INSTALLER%" (',
        '  echo Missing install.py at "%INSTALLER%".',
        "  exit /b 1",
        ")",
        'set "PYEXE=py -3"',
        "where py >nul 2>nul || set \"PYEXE=python\"",
        f'%PYEXE% "%INSTALLER%" {verb}',
        "if errorlevel 1 exit /b 1",
        "pause",
        "endlocal",
        "",
    ]
    return "\r\n".join(lines)


def desktop_entry(name: str, launcher_path: str, comment: str,
                  terminal: bool = True, icon: str = "network-server") -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={comment}\n"
        f'Exec=bash -c \'"{launcher_path}"; read -p "Press Enter to close..."\'\n'
        f"Terminal={'true' if terminal else 'false'}\n"
        f"Icon={icon}\n"
        "Categories=Network;\n"
    )


def windows_lnk_ps(lnk: str, target: str, workdir: str, icon: str,
                   description: str) -> str:
    """PowerShell one-liner that creates a .lnk via WScript.Shell."""
    return (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath='{target}';"
        "$s.WorkingDirectory='{wd}';"
        "$s.IconLocation='{icon}';"
        "$s.Description='{desc}';"
        "$s.Save()"
    ).format(lnk=lnk, target=target, wd=workdir, icon=icon, desc=description)


# ---------------------------------------------------------------------------
# Filesystem writers
# ---------------------------------------------------------------------------

def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass


def _create_linux(runtime_dir: Path) -> list:
    made = []
    desktop = _desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)

    launcher = runtime_dir / "PirateFish.sh"
    _write_exec(launcher, linux_launcher(str(runtime_dir), "", "up"))
    entry = desktop / f"{START_LABEL}.desktop"
    icon_path = (runtime_dir / ICON_FILE
                 if (runtime_dir / ICON_FILE).exists()
                 else Path("network-server"))
    _write_exec(entry, desktop_entry(START_LABEL, str(launcher),
                                     "Start the PirateFish media stack",
                                     icon=str(icon_path)))
    # Mark trusted for GNOME (best-effort).
    try:
        subprocess.run(["gio", "set", str(entry), "metadata::trusted", "true"],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    made.append(entry)
    return made


def _create_windows(runtime_dir: Path) -> list:  # pragma: no cover - windows only
    made = []
    desktop = _desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)

    launcher = runtime_dir / "PirateFish.bat"
    # Write bytes to preserve exact CRLF (text mode would translate \n and
    # turn our \r\n into \r\r\n on Windows).
    launcher.write_bytes(windows_launcher(str(runtime_dir), "", "up")
                         .encode("utf-8"))
    lnk = desktop / f"{START_LABEL}.lnk"
    icon_location = (runtime_dir / ICON_FILE
                     if (runtime_dir / ICON_FILE).exists()
                     else Path("shell32.dll,18"))
    ps = windows_lnk_ps(str(lnk), str(launcher), str(runtime_dir),
                        str(icon_location), "Start the PirateFish media stack")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        made.append(launcher)
        return made
    made.append(lnk if lnk.exists() else launcher)
    return made


def _create_macos(runtime_dir: Path) -> list:  # pragma: no cover - macos only
    made = []
    desktop = _desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)
    launcher = runtime_dir / "PirateFish.command"
    _write_exec(launcher, macos_launcher(str(runtime_dir), "", "up"))
    cmd = desktop / f"{START_LABEL}.command"
    _write_exec(cmd, f'#!/bin/bash\nexec "{launcher}"\n')
    made.append(cmd)
    return made


def create(environment) -> list:
    """Create the Start desktop launcher. Returns the list of paths made."""
    source_dir = _project_dir()
    try:
        runtime_dir = _prepare_runtime_bundle(source_dir)
        _cleanup_legacy_stop_shortcuts(runtime_dir)
        if environment.os_name == "windows":
            paths = _create_windows(runtime_dir)
        elif environment.os_name == "darwin":
            paths = _create_macos(runtime_dir)
        else:
            paths = _create_linux(runtime_dir)
    except Exception as e:  # noqa - shortcut creation must never abort the install
        ui.warn(f"Could not create desktop shortcuts: {e}")
        return []

    if paths:
        ui.ok(f"Desktop shortcut created: '{START_LABEL}'. "
              "Use the dashboard Power button to shut the stack down.")
    return paths
