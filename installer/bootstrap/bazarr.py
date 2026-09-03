"""Bazarr bootstrap: link Bazarr to Sonarr and Radarr automatically.

Uses Bazarr's REST API (`/api/system/settings`) rather than patching config
files on the host. Reading/writing the host-mounted config.yaml proved fragile
(container vs host inode/mount inconsistencies caused long hangs), whereas the
API always reflects what the container sees and applies changes live -- no
restart required.

No Path Mappings are configured: every container shares the same /data tree.
"""

from __future__ import annotations

import re
import subprocess
import time
import urllib.parse
import urllib.request

from .. import ui


def _bazarr_api_key(container: str = "bazarr") -> str | None:
    try:
        out = subprocess.run(
            ["docker", "exec", container, "cat", "/config/config/config.yaml"],
            capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        m = re.search(r"apikey:\s*([A-Za-z0-9]+)", out.stdout)
        return m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def _wait_for_bazarr_api(base_url: str, timeout: int = 120) -> str | None:
    """Wait until Bazarr has generated its API key and is answering."""
    deadline = time.time() + timeout
    start = time.time()
    next_tick = start + 15
    while time.time() < deadline:
        key = _bazarr_api_key()
        if key:
            try:
                req = urllib.request.Request(
                    base_url.rstrip("/") + "/api/system/status")
                req.add_header("X-API-KEY", key)
                with urllib.request.urlopen(req, timeout=10):
                    return key
            except Exception:
                pass
        now = time.time()
        if now >= next_tick:
            ui.info(f"  still waiting for Bazarr to come online... "
                    f"({int(now - start)}s / {timeout}s)")
            next_tick = now + 15
        time.sleep(3)
    return None


def _post_settings(base_url: str, api_key: str, pairs) -> int:
    body = urllib.parse.urlencode(pairs).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/api/system/settings",
                                 data=body, method="POST")
    req.add_header("X-API-KEY", api_key)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def bootstrap(base_url: str, sonarr_key: str, radarr_key: str,
              container: str = "bazarr") -> bool:
    """Link Bazarr to Sonarr + Radarr via the Bazarr settings API.

    base_url is Bazarr's URL (e.g. http://127.0.0.1:6767). Sonarr/Radarr are
    reached over the shared docker network as sonarr:8989 / radarr:7878.
    """
    ui.info("Configuring Bazarr (linking Sonarr + Radarr)...")

    api_key = _wait_for_bazarr_api(base_url)
    if not api_key:
        ui.warn("Bazarr API not ready; connect Sonarr/Radarr manually in "
                "Bazarr -> Settings (sonarr:8989 / radarr:7878).")
        return False

    pairs = []
    if sonarr_key:
        pairs += [
            ("settings-general-use_sonarr", "true"),
            ("settings-sonarr-ip", "sonarr"),
            ("settings-sonarr-port", "8989"),
            ("settings-sonarr-base_url", ""),
            ("settings-sonarr-ssl", "false"),
            ("settings-sonarr-apikey", sonarr_key),
        ]
    if radarr_key:
        pairs += [
            ("settings-general-use_radarr", "true"),
            ("settings-radarr-ip", "radarr"),
            ("settings-radarr-port", "7878"),
            ("settings-radarr-base_url", ""),
            ("settings-radarr-ssl", "false"),
            ("settings-radarr-apikey", radarr_key),
        ]

    if not pairs:
        ui.warn("No Sonarr/Radarr API keys available; skipped Bazarr linking.")
        return False

    try:
        status = _post_settings(base_url, api_key, pairs)
    except Exception as e:  # noqa
        ui.warn(f"Bazarr settings update failed: {e}")
        return False

    if status not in (200, 204):
        ui.warn(f"Bazarr settings POST returned {status}.")
        return False

    linked = []
    if sonarr_key:
        linked.append("Sonarr")
    if radarr_key:
        linked.append("Radarr")
    ui.ok(f"Bazarr linked to {' + '.join(linked)}.")
    return True
