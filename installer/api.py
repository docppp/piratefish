"""Shared HTTP + API helpers used by every bootstrap module.

Uses only the standard library (urllib) so the installer needs no third-party
packages, but transparently prefers `requests` if it happens to be installed.
Also provides Servarr helpers: read API key from config.xml, wait for readiness,
and a thin typed client for the v3/v1 REST APIs.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path



class HttpError(Exception):
    def __init__(self, status, body, url):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")


class Response:
    def __init__(self, status: int, body: bytes, headers: dict):
        self.status = status
        self._body = body
        self.headers = headers

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self._body.decode("utf-8", "replace")) if self._body else None


def request(method: str, url: str, headers: dict = None, data=None,
            timeout: int = 30) -> Response:
    """Perform an HTTP request. `data` may be dict (json), str/bytes, or None."""
    headers = dict(headers or {})
    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(data, str):
            body = data.encode()
        else:
            body = data

    req = urllib.request.Request(url, data=body, method=method.upper())
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return Response(resp.status, raw, dict(resp.headers))
    except urllib.error.HTTPError as e:
        raw = e.read()
        return Response(e.code, raw, dict(e.headers or {}))
    except (urllib.error.URLError, OSError) as e:
        raise HttpError(0, str(e), url)


def _progress_tick(label, waited, timeout):
    """Emit a periodic 'still waiting' line so long polls don't look frozen."""
    if not label:
        return
    try:
        from . import ui
        ui.info(f"  still waiting for {label}... ({waited}s / {timeout}s)")
    except Exception:
        pass


def wait_for_http(url: str, timeout: int = 180, interval: float = 2.0,
                  accept_status=(200, 401, 403), label: str | None = None) -> bool:
    """Poll a URL until it responds (any of accept_status) or timeout.

    Emits a progress line every ~15s when `label` is given so a long wait is
    visibly making progress instead of appearing hung.
    """
    deadline = time.time() + timeout
    start = time.time()
    next_tick = start + 15
    while time.time() < deadline:
        try:
            r = request("GET", url, timeout=10)
            if r.status in accept_status or 200 <= r.status < 500:
                return True
        except HttpError:
            pass
        now = time.time()
        if now >= next_tick:
            _progress_tick(label or url, int(now - start), timeout)
            next_tick = now + 15
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Servarr config.xml helpers
# ---------------------------------------------------------------------------

def read_config_xml(config_path: Path):
    """Return dict of top-level config.xml elements (ApiKey, Port, UrlBase...)."""
    config_path = Path(config_path)
    if not config_path.exists():
        return None
    try:
        root = ET.parse(config_path).getroot()
        return {child.tag: (child.text or "") for child in root}
    except (ET.ParseError, OSError):
        return None


def read_api_key_from_container(container: str) -> str | None:
    """Read the ApiKey via `docker exec` (robust against host bind-mount/inode
    inconsistencies -- always reflects what the container itself sees)."""
    try:
        out = subprocess.run(
            ["docker", "exec", container, "cat", "/config/config.xml"],
            capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        m = re.search(r"<ApiKey>([^<]+)</ApiKey>", out.stdout)
        return m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def wait_for_api_key(config_path: Path, timeout: int = 180,
                     interval: float = 2.0, container: str | None = None,
                     label: str | None = None) -> str | None:
    """Wait until an ApiKey is available.

    Primary source is `docker exec <container>` (reliable regardless of host
    mount state); the host config.xml is used as a fallback. Either yields the
    same key, but the container read avoids hangs when the host path is stale.
    Emits a progress line every ~15s so a long wait doesn't look frozen.
    """
    deadline = time.time() + timeout
    start = time.time()
    next_tick = start + 15
    config_path = Path(config_path)
    while time.time() < deadline:
        if container:
            key = read_api_key_from_container(container)
            if key:
                return key
        cfg = read_config_xml(config_path)
        if cfg and cfg.get("ApiKey"):
            return cfg["ApiKey"]
        now = time.time()
        if now >= next_tick:
            _progress_tick(label or (container or "API key"),
                           int(now - start), timeout)
            next_tick = now + 15
        time.sleep(interval)
    return None


# ---------------------------------------------------------------------------
# Thin Servarr REST client (Prowlarr v1, Sonarr/Radarr v3)
# ---------------------------------------------------------------------------

class ArrClient:
    def __init__(self, base_url: str, api_key: str, api_version: str = "v3"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_version = api_version

    def _url(self, path: str) -> str:
        path = path.lstrip("/")
        if path.startswith("api/"):
            return f"{self.base_url}/{path}"
        return f"{self.base_url}/api/{self.api_version}/{path}"

    def _headers(self, extra=None):
        h = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def get(self, path: str, timeout=30):
        r = request("GET", self._url(path), headers=self._headers(), timeout=timeout)
        if r.status >= 400:
            raise HttpError(r.status, r.text, self._url(path))
        return r.json()

    def post(self, path: str, payload, timeout=60):
        r = request("POST", self._url(path), headers=self._headers(),
                    data=payload, timeout=timeout)
        if r.status >= 400:
            raise HttpError(r.status, r.text, self._url(path))
        return r.json() if r._body else None

    def put(self, path: str, payload, timeout=60):
        r = request("PUT", self._url(path), headers=self._headers(),
                    data=payload, timeout=timeout)
        if r.status >= 400:
            raise HttpError(r.status, r.text, self._url(path))
        return r.json() if r._body else None

    def delete(self, path: str, timeout=60):
        r = request("DELETE", self._url(path), headers=self._headers(),
                    timeout=timeout)
        if r.status >= 400:
            raise HttpError(r.status, r.text, self._url(path))
        return r.json() if r._body else None

    def ping(self) -> bool:
        try:
            self.get("system/status", timeout=10)
            return True
        except HttpError:
            return False
