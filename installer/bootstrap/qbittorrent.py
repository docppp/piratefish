"""qBittorrent bootstrap.

Handles the LinuxServer image's first-run flow:
  1. Recover the temporary WebUI password from container logs.
  2. Log in, set a permanent username/password.
  3. Whitelist the LAN subnet for auth bypass (so devices/apps connect freely).
  4. Configure download paths + categories (tv/movies) and seeding defaults.

Uses qBittorrent's WebUI API (/api/v2). Cookie-based session, urllib only.
"""

from __future__ import annotations

import re
import subprocess
import time
import urllib.parse
import urllib.request
import urllib.error

from .. import ui, constants


class QbitError(Exception):
    pass


class QbitClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.sid = None

    def _post(self, path, fields: dict, timeout=30):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method="POST")
        req.add_header("Referer", self.base_url)
        req.add_header("Origin", self.base_url)
        if self.sid:
            req.add_header("Cookie", f"SID={self.sid}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                cookie = resp.headers.get("Set-Cookie", "")
                m = re.search(r"SID=([^;]+)", cookie)
                if m:
                    self.sid = m.group(1)
                return resp.status, body
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            raise QbitError(str(e))

    def _get(self, path, timeout=30):
        req = urllib.request.Request(self.base_url + path, method="GET")
        req.add_header("Referer", self.base_url)
        if self.sid:
            req.add_header("Cookie", f"SID={self.sid}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            raise QbitError(str(e))

    def login(self, username, password) -> bool:
        status, body = self._post("/api/v2/auth/login",
                                   {"username": username, "password": password})
        return status == 200 and body.strip() == "Ok." and self.sid is not None

    def set_preferences(self, prefs: dict):
        import json
        status, body = self._post("/api/v2/app/setPreferences",
                                  {"json": json.dumps(prefs)})
        if status != 200:
            raise QbitError(f"setPreferences failed ({status}): {body}")

    def create_category(self, name, save_path):
        status, body = self._post("/api/v2/torrents/createCategory",
                                  {"category": name, "savePath": save_path})
        # 409 == already exists; treat as fine and update path via editCategory.
        if status not in (200, 409):
            raise QbitError(f"createCategory {name} failed ({status}): {body}")
        if status == 409:
            self._post("/api/v2/torrents/editCategory",
                       {"category": name, "savePath": save_path})

    def version(self):
        try:
            _, body = self._get("/api/v2/app/version")
            return body.strip()
        except QbitError:
            return "?"


TEMP_PASSWORD_RE = re.compile(
    r"temporary password is provided for this session:\s*([^\s]+)", re.I)


def parse_temp_password(text: str) -> str | None:
    """Extract the one-time WebUI password from qBittorrent log output.

    The log line (qBittorrent 4.2+) reads:
      "... A temporary password is provided for this session: <PASSWORD>"
    Kept as a standalone, unit-testable function because this scraping has been
    fragile in the past (an earlier regex matched the word 'provided')."""
    if not text:
        return None
    m = TEMP_PASSWORD_RE.search(text)
    if not m:
        return None
    return m.group(1).strip().rstrip(".,;")


def _recover_temp_password(container="qbittorrent", attempts=30, delay=2):
    """Scrape the one-time WebUI password from container logs."""
    for i in range(attempts):
        try:
            out = subprocess.run(["docker", "logs", container],
                                 capture_output=True, text=True, timeout=20)
            text = (out.stdout or "") + (out.stderr or "")
            pw = parse_temp_password(text)
            if pw:
                return pw
        except (OSError, subprocess.SubprocessError):
            pass
        if i and i % 8 == 0:
            ui.info(f"  still waiting for qBittorrent's first-run password... "
                    f"({i * delay}s)")
        time.sleep(delay)
    return None


def bootstrap(base_url: str, username: str, password: str,
              lan_subnet: str, container="qbittorrent") -> dict:
    """Configure qBittorrent. Returns a small status dict."""
    client = QbitClient(base_url)

    # 1. Establish an authenticated session.
    logged_in = False
    # Try target creds first (idempotent re-runs).
    if client.login(username, password):
        logged_in = True
        ui.ok("qBittorrent: logged in with configured credentials (already set).")
    else:
        temp = _recover_temp_password(container)
        candidates = []
        if temp:
            candidates.append(("admin", temp))
        candidates.append(("admin", "adminadmin"))  # legacy default
        for u, p in candidates:
            if client.login(u, p):
                logged_in = True
                ui.ok(f"qBittorrent: logged in via first-run credentials.")
                break

    if not logged_in:
        raise QbitError(
            "Could not authenticate to qBittorrent. Check `docker logs qbittorrent` "
            "for the temporary password and re-run.")

    # 2. Apply preferences: permanent creds, LAN auth bypass, paths, seeding.
    prefs = {
        "web_ui_username": username,
        "web_ui_password": password,
        "bypass_local_auth": True,
        "bypass_auth_subnet_whitelist_enabled": True,
        "bypass_auth_subnet_whitelist": lan_subnet,
        "save_path": constants.TORR_DIR,
        "temp_path_enabled": True,
        "temp_path": constants.INCOMPLETE_DIR,
        "auto_tmm_enabled": False,
        # Seeding: pause (never delete) once ratio 3.0 is reached; no time limit.
        "max_ratio_enabled": True,
        "max_ratio": constants.SEED_RATIO_LIMIT,
        "max_seeding_time_enabled": constants.SEED_TIME_LIMIT_MIN >= 0,
        "max_seeding_time": (constants.SEED_TIME_LIMIT_MIN
                             if constants.SEED_TIME_LIMIT_MIN >= 0 else 0),
        "max_ratio_act": 0,  # 0 = pause torrent, 1 = remove
    }
    client.set_preferences(prefs)
    ui.ok("qBittorrent: credentials, LAN auth bypass, paths and seeding defaults set.")

    # Re-login in case the password/username just changed mid-session.
    client.login(username, password)

    # 3. Categories.
    for cat, path in constants.QBIT_CATEGORIES.items():
        client.create_category(cat, path)
    ui.ok(f"qBittorrent: categories created ({', '.join(constants.QBIT_CATEGORIES)}).")

    return {"version": client.version(), "categories": list(constants.QBIT_CATEGORIES)}
