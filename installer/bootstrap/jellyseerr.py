"""Jellyseerr bootstrap: auto-link Jellyseerr to Jellyfin on first run."""

from __future__ import annotations

import re
import urllib.parse

from .. import ui
from ..api import HttpError, request, wait_for_http

# server/constants/server.ts -> MediaServerType.JELLYFIN = 2
_MEDIA_SERVER_TYPE_JELLYFIN = 2


def _extract_connect_sid(headers: dict) -> str | None:
    if not headers:
        return None
    for k, v in headers.items():
        if k.lower() != "set-cookie":
            continue
        m = re.search(r"(connect\.sid=[^;,\s]+)", str(v))
        if m:
            return m.group(1)
    return None


def _short_error(resp) -> str:
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        for k in ("message", "error"):
            if payload.get(k):
                return str(payload[k])
    text = (resp.text or "").strip()
    return text[:240] if text else f"HTTP {resp.status}"


def _is_media_library(library: dict) -> bool:
    name = str(library.get("name") or "").lower()
    ltype = str(library.get("type") or "").lower()
    terms = ("movie", "series", "show", "tv")
    return any(t in name for t in terms) or any(t in ltype for t in terms)


def _libraries_to_enable(libraries: list[dict]) -> list[dict]:
    picked = [lib for lib in libraries if _is_media_library(lib)]
    return picked or libraries


def bootstrap(base_url: str, jellyfin_user: str, jellyfin_pass: str,
              jellyfin_host: str = "jellyfin", jellyfin_port: int = 8096) -> dict:
    """Configure Jellyseerr <-> Jellyfin integration.

    Returns:
      {configured: bool, linked: bool, libraries_enabled: int, manual_note: str|None}
    """
    ui.info("Configuring Jellyseerr...")
    base_url = base_url.rstrip("/")
    result = {
        "configured": False,
        "linked": False,
        "libraries_enabled": 0,
        "manual_note": None,
    }

    if not wait_for_http(base_url, timeout=180, label="Jellyseerr"):
        ui.warn("Jellyseerr not reachable yet; complete setup manually in its UI.")
        result["manual_note"] = "Jellyseerr not reachable"
        return result

    try:
        pub = request("GET", f"{base_url}/api/v1/settings/public", timeout=20)
    except HttpError as e:
        ui.warn(f"Could not read Jellyseerr settings: {e}")
        result["manual_note"] = "Could not read Jellyseerr public settings"
        return result

    public = {}
    if pub.status == 200 and pub._body:
        try:
            public = pub.json() or {}
        except Exception:
            public = {}

    if public.get("initialized"):
        ui.ok("Jellyseerr is already configured.")
        result["configured"] = True
        result["linked"] = True
        return result

    payload = {
        "username": jellyfin_user,
        "password": jellyfin_pass,
        "hostname": jellyfin_host,
        "port": int(jellyfin_port),
        "useSsl": False,
        "urlBase": "",
        "email": f"{jellyfin_user}@example.com",
        "serverType": _MEDIA_SERVER_TYPE_JELLYFIN,
    }

    try:
        auth = request("POST", f"{base_url}/api/v1/auth/jellyfin",
                       data=payload, timeout=30)
    except HttpError as e:
        ui.warn(f"Jellyseerr login/config failed: {e}")
        result["manual_note"] = "Connect Jellyseerr to Jellyfin manually"
        return result

    if auth.status >= 400:
        err = _short_error(auth)
        # Idempotent re-run path: Jellyseerr is already configured, so re-login
        # without host fields and continue with library sync.
        if "already configured" in err.lower():
            auth = request(
                "POST",
                f"{base_url}/api/v1/auth/jellyfin",
                data={
                    "username": jellyfin_user,
                    "password": jellyfin_pass,
                    "email": f"{jellyfin_user}@example.com",
                },
                timeout=30,
            )
            if auth.status >= 400:
                ui.warn(f"Jellyseerr login/config failed: {_short_error(auth)}")
                result["manual_note"] = "Connect Jellyseerr to Jellyfin manually"
                return result
        else:
            ui.warn(f"Jellyseerr login/config failed: {err}")
            result["manual_note"] = "Connect Jellyseerr to Jellyfin manually"
            return result

    result["linked"] = True
    session_cookie = _extract_connect_sid(auth.headers)
    if not session_cookie:
        ui.warn("Jellyseerr linked to Jellyfin, but session was not established; "
                "enable libraries manually in Jellyseerr.")
        result["configured"] = True
        result["manual_note"] = "Enable Jellyseerr libraries manually"
        return result

    headers = {"Cookie": session_cookie}
    try:
        sync = request("GET", f"{base_url}/api/v1/settings/jellyfin/library?sync=1",
                       headers=headers, timeout=60)
    except HttpError as e:
        ui.warn(f"Jellyseerr library sync failed: {e}")
        result["configured"] = True
        result["manual_note"] = "Sync libraries manually in Jellyseerr"
        return result

    if sync.status >= 400:
        ui.warn(f"Jellyseerr library sync failed: {_short_error(sync)}")
        result["configured"] = True
        result["manual_note"] = "Sync libraries manually in Jellyseerr"
        return result

    libraries = []
    if sync._body:
        try:
            parsed = sync.json() or []
            if isinstance(parsed, list):
                libraries = parsed
        except Exception:
            libraries = []

    if not libraries:
        ui.warn("Jellyseerr synced no libraries; enable them manually in settings.")
        result["configured"] = True
        result["manual_note"] = "Enable Jellyseerr libraries manually"
        return result

    target_ids = []
    for lib in _libraries_to_enable(libraries):
        lib_id = lib.get("id")
        if lib_id:
            target_ids.append(str(lib_id))

    target_set = set(target_ids)
    enabled_ids = []
    for lib in libraries:
        lib_id = lib.get("id")
        if not lib_id:
            continue
        lib_id = str(lib_id)
        if lib.get("enabled") or lib_id in target_set:
            enabled_ids.append(lib_id)

    if enabled_ids:
        q_ids = ",".join(urllib.parse.quote(lib_id, safe="")
                         for lib_id in enabled_ids)
        try:
            enable = request(
                "GET",
                f"{base_url}/api/v1/settings/jellyfin/library?enable={q_ids}",
                headers=headers,
                timeout=30,
            )
        except HttpError as e:
            ui.warn(f"Jellyseerr library enable failed: {e}")
            result["configured"] = True
            result["manual_note"] = "Enable Jellyseerr libraries manually"
            return result

        if enable.status >= 400:
            ui.warn(f"Jellyseerr library enable failed: {_short_error(enable)}")
            result["configured"] = True
            result["manual_note"] = "Enable Jellyseerr libraries manually"
            return result

        if enable._body:
            try:
                parsed = enable.json() or []
                if isinstance(parsed, list):
                    libraries = parsed
            except Exception:
                pass

    enabled_count = sum(
        1 for lib in libraries
        if str(lib.get("id")) in target_set and lib.get("enabled")
    )

    if enabled_count:
        ui.ok(f"Jellyseerr linked to Jellyfin and enabled {enabled_count} librar"
              f"{'y' if enabled_count == 1 else 'ies'}.")
    else:
        ui.warn("Jellyseerr linked to Jellyfin, but no libraries were enabled.")
        result["manual_note"] = "Enable Jellyseerr libraries manually"
    result["libraries_enabled"] = enabled_count

    try:
        init = request("POST", f"{base_url}/api/v1/settings/initialize",
                       headers=headers, data={}, timeout=20)
    except HttpError as e:
        ui.warn(f"Jellyseerr finalize setup failed: {e}")
        result["manual_note"] = "Finish setup in Jellyseerr UI"
        return result

    if init.status >= 400:
        ui.warn(f"Jellyseerr finalize setup failed: {_short_error(init)}")
        result["manual_note"] = "Finish setup in Jellyseerr UI"
        return result

    result["configured"] = True
    return result
