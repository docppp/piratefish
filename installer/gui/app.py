"""pywebview GUI shell + JS bridge.

Hosts the HTML frontend in a native webview window and exposes a Python `Api`
object to JavaScript (`window.pywebview.api.*`). The bridge reuses the existing
engine: environment detection, the shared `execute_install`, and the bootstrap
modules for the post-install setup wizard.

All long-running work runs on worker threads; progress is streamed to the UI via
the `ui` listener hook (every ui.ok/info/warn line is forwarded to `appLog`).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .. import ui, constants, detect
from ..api import ArrClient, read_api_key_from_container
from . import browser


PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
FRONTEND = Path(__file__).resolve().parent / "frontend" / "index.html"


class Api:
    """Methods here are callable from JS as window.pywebview.api.<name>(...)."""

    def __init__(self, state_path: Path | None = None, initial_path: str = ""):
        self._window = None
        self.environment = detect.detect()
        self.state_path = Path(state_path or (Path.home() / ".arrstack_state.json"))
        self.initial_path = initial_path or ""
        self.ctx = None            # populated after install
        self.cfg = None

    # -- infra ----------------------------------------------------------------

    def set_window(self, window):
        self._window = window
        ui.add_listener(self._on_log)

    def _on_log(self, level, msg):
        if not self._window:
            return
        try:
            payload = json.dumps({"level": level, "msg": msg})
            self._window.evaluate_js(f"window.appLog && window.appLog({payload})")
        except Exception:
            pass

    def _emit(self, event, data):
        if not self._window:
            return
        try:
            self._window.evaluate_js(
                f"window.appEvent && window.appEvent({json.dumps(event)}, {json.dumps(data)})")
        except Exception:
            pass

    # -- environment / config -------------------------------------------------

    def get_environment(self):
        env = self.environment
        return {
            "os": env.os_pretty,
            "arch": env.arch,
            "is_admin": env.is_admin,
            "lan_ip": env.lan_ip,
            "docker_ok": env.docker_ok,
            "compose_ok": env.compose_ok,
            "is_wsl": env.is_wsl,
        }

    def pick_folder(self):
        import webview
        try:
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            result = None
        if result:
            return result[0] if isinstance(result, (list, tuple)) else result
        return ""

    def _path_locked(self) -> bool:
        """The data path is fixed (not user-editable) when it was chosen on the
        Windows host and we are running delegated inside WSL2 -- so it always
        points at the selected Windows drive, never the WSL2 filesystem."""
        import os
        return bool(self.initial_path) and bool(os.environ.get("PIRATEFISH_DELEGATED"))

    def guess_defaults(self):
        from .. import cli
        return {
            "tz": cli._guess_tz(),
            "lan_subnet": cli._guess_subnet(self.environment.lan_ip),
            "qbit_user": constants.DEFAULT_QBIT_USER,
            "data_path": self.initial_path,
            "data_path_locked": self._path_locked(),
        }

    def check_path(self, path):
        """Run the fs doctor on a candidate path (non-destructive preview)."""
        from .. import fs_checks
        rep = fs_checks.run_doctor(path, preview=True)
        return {
            "safe": rep.safe_to_proceed,
            "fs_type": rep.fs_type,
            "hardlink_ok": rep.hardlink_ok,
            "caveats": rep.caveats,
            "fatal": rep.fatal,
        }

    # -- install --------------------------------------------------------------

    def start_install(self, form):
        """Kick off the install on a worker thread. Returns immediately."""
        t = threading.Thread(target=self._run_install, args=(form,), daemon=True)
        t.start()
        return {"started": True}

    def _run_install(self, form):
        from .. import cli, deps
        try:
            def _progress(payload):
                self._emit("install_progress", payload)

            env = self.environment

            # Windows: the data path is chosen with the native Windows folder
            # picker on the host and locked. Enforce it here too, so a bypassed
            # form field can never redirect media onto the WSL2 Linux filesystem.
            if self._path_locked():
                form = dict(form)
                form["data_path"] = self.initial_path

            # Ensure docker + compose (Linux/WSL2 install path). On Windows the
            # GUI is never reached: `cli.main` bootstraps WSL2 and re-runs this
            # installer inside the distro, where it executes as native Linux.
            ui.header("Ensuring Docker + Compose")
            try:
                deps.ensure_docker(env, non_interactive=True)
            except deps.DependencyError as e:
                ui.fail(str(e))
                self._emit("install_done", {"ok": False, "error": str(e)})
                return

            cfg = cli.build_config_from_dict(form, env)
            ctx = cli.Context()
            ctx.env = env
            ctx.data = cfg["data_path_obj"]
            ctx.lan_ip = env.lan_ip
            ctx.qbit_user = cfg["qbit_user"]
            ctx.qbit_pass = cfg["qbit_pass"]

            code = cli.execute_install(cfg, env, ctx,
                                       non_interactive=True,
                                       run_console_wizard=False,
                                       progress=_progress)
            self.ctx = ctx
            self.cfg = cfg
            if code == 0:
                self._emit("install_done", {"ok": True, "report": self.get_report()})
            else:
                self._emit("install_done", {"ok": False, "error": f"exit {code}"})
        except Exception as e:  # noqa
            ui.fail(f"Install failed: {e}")
            self._emit("install_done", {"ok": False, "error": str(e)})

    def get_report(self):
        if not self.ctx:
            return {}
        ip = self.ctx.lan_ip
        services = []
        for name in constants.SERVICE_ORDER:
            services.append({
                "name": constants.SERVICES[name].label,
                "url": f"http://{ip}:{self.ctx.ports[name]}",
            })
        return {
            "lan_ip": ip,
            "services": services,
            "dashboard_url": f"http://{ip}:{self.ctx.ports['homepage']}",
            "qbit_user": self.ctx.qbit_user,
            "qbit_pass": self.ctx.qbit_pass,
            "data_path": self.cfg["data_path_obj"].display if self.cfg else "",
        }

    # -- tracker wizard -------------------------------------------------------

    def _prowlarr(self):
        key = read_api_key_from_container("prowlarr")
        port = self.ctx.ports["prowlarr"] if self.ctx else 9696
        return ArrClient(f"http://127.0.0.1:{port}", key, "v1")

    def list_all_trackers(self):
        """Return the FULL Prowlarr indexer catalog (uncapped) so the frontend
        can filter/autocomplete client-side. Also returns names already added."""
        from ..bootstrap import prowlarr as bp
        pc = self._prowlarr()
        try:
            schemas = pc.get("indexer/schema")
        except Exception as e:  # noqa
            return {"error": str(e), "trackers": [], "added": []}
        added = sorted(bp.existing_indexer_names(pc))
        out = []
        for s in schemas:
            out.append({
                "name": s.get("name"),
                "privacy": s.get("privacy", "public"),
                "protocol": s.get("protocol", "torrent"),
                "language": s.get("language", ""),
                "urls": s.get("indexerUrls", []),
            })
        out.sort(key=lambda t: (t["name"] or "").lower())
        return {"trackers": out, "added": added}

    def search_trackers(self, query):
        from ..bootstrap import prowlarr as bp
        pc = self._prowlarr()
        try:
            matches = bp.search_schema(pc, query) if query else pc.get("indexer/schema")
        except Exception as e:  # noqa
            return {"error": str(e), "trackers": []}
        out = []
        for s in matches[:60]:
            out.append({
                "name": s.get("name"),
                "privacy": s.get("privacy", "public"),
                "protocol": s.get("protocol", "torrent"),
                "language": s.get("language", ""),
                "urls": s.get("indexerUrls", []),
            })
        return {"trackers": out}

    def get_tracker_form(self, name):
        """Return the user-facing fields for a tracker + whether it needs cookie."""
        from ..bootstrap import prowlarr as bp
        pc = self._prowlarr()
        schema = None
        for s in pc.get("indexer/schema"):
            if (s.get("name") or "").lower() == name.lower():
                schema = s
                break
        if schema is None:
            return {"error": "tracker not found"}
        fields = []
        needs_cookie = False
        for f in schema.get("fields", []):
            if not bp._should_prompt(f):
                continue
            fname = f.get("name", "")
            if fname == "cookie":
                needs_cookie = True
            fields.append({
                "name": fname,
                "label": f.get("label") or fname,
                "secret": bp._is_secret_field(f),
            })
        return {
            "name": schema.get("name"),
            "privacy": schema.get("privacy", "public"),
            "urls": schema.get("indexerUrls", []),
            "fields": fields,
            "needs_cookie": needs_cookie,
        }

    def login_capture(self, url, title="Log in to tracker"):
        """Open the embedded login browser; return the captured cookie + UA."""
        try:
            res = browser.capture_login_cookie(url, title)
            cookie = res.get("cookie", "")
            return {"cookie": cookie, "user_agent": res.get("user_agent", ""),
                    "ok": bool(cookie)}
        except Exception as e:  # noqa
            return {"cookie": "", "user_agent": "", "ok": False, "error": str(e)}

    def add_tracker(self, name, field_values):
        from ..bootstrap import prowlarr as bp
        pc = self._prowlarr()
        schema = None
        for s in pc.get("indexer/schema"):
            if (s.get("name") or "").lower() == name.lower():
                schema = s
                break
        if schema is None:
            return {"ok": False, "error": "tracker not found"}
        ok = bp.add_single_tracker(pc, schema, non_interactive=True,
                                   provided_fields=field_values or {})
        return {"ok": ok}

    # -- bazarr providers -----------------------------------------------------

    def get_bazarr_providers(self):
        from ..bootstrap import bazarr_providers as bpv
        return {"providers": bpv.PROVIDER_CATALOG,
                "languages": bpv.COMMON_LANGUAGES}

    def apply_bazarr(self, providers, primary_language):
        from ..bootstrap import bazarr_providers as bpv
        port = self.ctx.ports["bazarr"] if self.ctx else 6767
        try:
            bpv.apply(f"http://127.0.0.1:{port}",
                      providers=providers,
                      primary_language=primary_language)
            return {"ok": True}
        except Exception as e:  # noqa
            return {"ok": False, "error": str(e)}

    # -- quality --------------------------------------------------------------

    def get_quality_options(self):
        from ..bootstrap import quality as ql
        return ql.selection_options()

    def set_quality(self, selection):
        """Create/apply the PirateFish quality profile in Sonarr + Radarr."""
        from ..bootstrap import quality as ql
        try:
            normalized = ql.normalize_selection(selection)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        results = {}
        try:
            for kind, name, port in (("series", "sonarr", "sonarr"),
                                     ("movie", "radarr", "radarr")):
                key = read_api_key_from_container(name)
                if not key:
                    continue
                p = self.ctx.ports[name] if self.ctx else \
                    (8989 if name == "sonarr" else 7878)
                c = ArrClient(f"http://127.0.0.1:{p}", key, "v3")
                results[name] = ql.apply(c, normalized, kind,
                                         prune_other_profiles=True)
            ok = any(v is not None for v in results.values())
            return {"ok": ok, "results": results, "selection": normalized}
        except Exception as e:  # noqa
            return {"ok": False, "error": str(e)}

    def open_url(self, url):
        import os
        import shutil
        import subprocess
        import webbrowser
        # Running inside WSL2 (delegated from Windows): open the *Windows*
        # browser -- a Linux browser normally isn't installed in the distro, so
        # webbrowser.open() would silently do nothing.
        if os.environ.get("PIRATEFISH_DELEGATED") or getattr(self.environment, "is_wsl", False):
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
        return {"ok": True}


def _apply_webkit_workarounds():
    """WebKitGTK on many systems renders a blank/grey, unclickable surface when
    hardware compositing/DMABUF fails (common on VMs, some GPUs/drivers, X11
    forwarding). Disabling those makes it fall back to software rendering. These
    env vars must be set BEFORE the webview's web process starts."""
    import os
    os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")


def run_gui(args) -> int:
    _apply_webkit_workarounds()
    import webview

    state_path = getattr(args, "state", None)
    initial = getattr(args, "path", None) or ""
    api = Api(state_path=Path(state_path).expanduser() if state_path else None,
              initial_path=initial)

    window = webview.create_window(
        "PirateFish", str(FRONTEND),
        js_api=api, width=980, height=720, min_size=(820, 600))
    api.set_window(window)

    storage = str(PROJECT_DIR / ".webview")
    # private_mode=False + storage_path so tracker login cookies are retrievable.
    webview.start(private_mode=False, storage_path=storage)
    return 0
