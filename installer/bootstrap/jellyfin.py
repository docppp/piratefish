"""Jellyfin bootstrap: complete the first-run wizard and add media libraries.

Jellyfin exposes a headless startup API (/Startup/*). We use it to create the
admin user, finish configuration, then authenticate and add the Series/Movies
libraries pointing at /data/Media/*.
"""

from __future__ import annotations

import json

from .. import ui, constants
from ..api import request, HttpError


def _is_setup_complete(base_url: str) -> bool | None:
    try:
        r = request("GET", f"{base_url}/System/Info/Public", timeout=15)
        if r.status == 200 and r._body:
            return bool(r.json().get("StartupWizardCompleted"))
    except HttpError:
        return None
    return None


def _startup(base_url: str, admin_user: str, admin_pass: str) -> bool:
    # 1. Initial config (locale).
    request("POST", f"{base_url}/Startup/Configuration",
            headers={"Content-Type": "application/json"},
            data={"UICulture": "en-US", "MetadataCountryCode": "US",
                  "PreferredMetadataLanguage": "en",
                  "ServerName": constants.DEFAULT_JELLYFIN_SERVER_NAME}, timeout=20)
    # 2. Touch the default user endpoint (required by the wizard sequence).
    request("GET", f"{base_url}/Startup/User", timeout=20)
    # 3. Create the admin account.
    r = request("POST", f"{base_url}/Startup/User",
                headers={"Content-Type": "application/json"},
                data={"Name": admin_user, "Password": admin_pass}, timeout=20)
    if r.status >= 400:
        return False
    # 4. Remote access + finish.
    request("POST", f"{base_url}/Startup/RemoteAccess",
            headers={"Content-Type": "application/json"},
            data={"EnableRemoteAccess": True,
                  "EnableAutomaticPortMapping": False}, timeout=20)
    r = request("POST", f"{base_url}/Startup/Complete", timeout=20)
    return r.status < 400


def _auth_token(base_url: str, user: str, password: str) -> str | None:
    auth_header = ('MediaBrowser Client="ARR-Installer", Device="installer", '
                   'DeviceId="arr-installer", Version="1.0.0"')
    r = request("POST", f"{base_url}/Users/AuthenticateByName",
                headers={"Content-Type": "application/json",
                         "X-Emby-Authorization": auth_header},
                data={"Username": user, "Pw": password}, timeout=20)
    if r.status == 200 and r._body:
        return r.json().get("AccessToken")
    return None


def _public_server_name(base_url: str) -> str | None:
    try:
        r = request("GET", f"{base_url}/System/Info/Public", timeout=15)
    except HttpError:
        return None
    if r.status == 200 and r._body:
        try:
            return (r.json() or {}).get("ServerName")
        except Exception:
            return None
    return None


def _ensure_server_name(base_url: str, token: str, desired_name: str) -> bool:
    try:
        r = request("GET", f"{base_url}/System/Configuration",
                    headers={"X-Emby-Token": token}, timeout=20)
    except HttpError:
        return False
    if r.status != 200 or not r._body:
        return False
    try:
        cfg = r.json() or {}
    except Exception:
        return False
    if cfg.get("ServerName") == desired_name:
        return True
    cfg["ServerName"] = desired_name
    try:
        u = request("POST", f"{base_url}/System/Configuration",
                    headers={"X-Emby-Token": token,
                             "Content-Type": "application/json"},
                    data=cfg, timeout=30)
    except HttpError:
        return False
    if u.status >= 400:
        return False
    return _public_server_name(base_url) == desired_name


def _existing_libraries(base_url: str, token: str):
    r = request("GET", f"{base_url}/Library/VirtualFolders",
                headers={"X-Emby-Token": token}, timeout=20)
    if r.status == 200 and r._body:
        return [vf.get("Name") for vf in r.json()]
    return []


def _add_library(base_url: str, token: str, name: str,
                 collection_type: str, path: str) -> bool:
    url = (f"{base_url}/Library/VirtualFolders"
           f"?name={name}&collectionType={collection_type}&refreshLibrary=true")
    body = {"LibraryOptions": {"PathInfos": [{"Path": path}]}}
    r = request("POST", url,
                headers={"Content-Type": "application/json",
                         "X-Emby-Token": token},
                data=body, timeout=30)
    return r.status < 400


def bootstrap(base_url: str, admin_user: str, admin_pass: str) -> dict:
    ui.info("Configuring Jellyfin...")
    base_url = base_url.rstrip("/")
    result = {"configured": False, "libraries": [], "manual_note": None}

    complete = _is_setup_complete(base_url)
    if complete is None:
        ui.warn("Jellyfin not reachable yet; finish setup manually at the URL below.")
        result["manual_note"] = "Jellyfin setup wizard not reachable"
        return result

    if not complete:
        if _startup(base_url, admin_user, admin_pass):
            ui.ok("Jellyfin: first-run wizard completed (admin account created).")
        else:
            ui.warn("Jellyfin wizard could not be completed headlessly; open the UI "
                    "and create the admin account manually.")
            result["manual_note"] = "Complete Jellyfin wizard in the UI"
            return result
    else:
        ui.ok("Jellyfin already set up.")

    token = _auth_token(base_url, admin_user, admin_pass)
    if not token:
        ui.warn("Could not authenticate to Jellyfin to add libraries; add Series "
                f"({constants.SERIES_DIR}) and Movies ({constants.MOVIES_DIR}) manually.")
        result["manual_note"] = "Add Jellyfin libraries manually"
        return result

    if _ensure_server_name(base_url, token, constants.DEFAULT_JELLYFIN_SERVER_NAME):
        ui.ok(f"Jellyfin server name set to '{constants.DEFAULT_JELLYFIN_SERVER_NAME}'.")
    else:
        ui.warn("Could not enforce Jellyfin server name automatically.")

    existing = _existing_libraries(base_url, token)
    for name, ctype, path in (("Series", "tvshows", constants.SERIES_DIR),
                              ("Movies", "movies", constants.MOVIES_DIR)):
        if name in existing:
            ui.ok(f"  library '{name}' already present.")
            result["libraries"].append(name)
            continue
        if _add_library(base_url, token, name, ctype, path):
            ui.ok(f"  library '{name}' added ({path}).")
            result["libraries"].append(name)
        else:
            ui.warn(f"  could not add library '{name}'; add it manually ({path}).")

    result["configured"] = True
    return result
