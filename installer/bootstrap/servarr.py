"""Shared Sonarr/Radarr bootstrap logic (both are *Arr v3 apps).

Configures: root folder, qBittorrent download client (correct category),
hardlink-based imports, and renaming. All operations are idempotent -- they
check for existing config before creating.
"""

from __future__ import annotations

from .. import ui, constants
from ..api import ArrClient, HttpError


def configure_authentication(client: ArrClient, username: str, password: str) -> None:
    """Enable Forms authentication (same credentials), disabled for LAN.

    Prowlarr/Sonarr/Radarr require authentication to be enabled. We set method
    'forms' with the provided username/password and 'disabledForLocalAddresses'
    so devices on the local network are not prompted, while remote access is
    protected. API-key access (used by this installer) is unaffected.
    """
    try:
        cfg = client.get("config/host")
    except HttpError as e:
        ui.warn(f"  could not read host config for auth: {e}")
        return

    if (cfg.get("authenticationMethod") not in (None, "", "none")
            and cfg.get("username") == username
            and cfg.get("authenticationRequired") == "disabledForLocalAddresses"):
        ui.ok("  authentication already configured.")
        return

    cfg["authenticationMethod"] = "forms"
    cfg["authenticationRequired"] = "disabledForLocalAddresses"
    cfg["username"] = username
    cfg["password"] = password
    cfg["passwordConfirmation"] = password  # required by newer versions
    try:
        client.put(f"config/host/{cfg['id']}", cfg)
        ui.ok("  authentication enabled (Forms; disabled for local addresses).")
    except HttpError as e:
        ui.warn(f"  could not set authentication: {e}")


def _find_qbit_schema(client: ArrClient):
    schemas = client.get("downloadclient/schema")
    for s in schemas:
        if s.get("implementation") == "QBittorrent":
            return s
    return None


def add_root_folder(client: ArrClient, path: str) -> None:
    existing = client.get("rootfolder")
    if any(rf.get("path", "").rstrip("/") == path.rstrip("/") for rf in existing):
        ui.ok(f"  root folder already present: {path}")
        return
    client.post("rootfolder", {"path": path})
    ui.ok(f"  root folder added: {path}")


def add_qbit_download_client(client: ArrClient, category: str,
                             qbit_host: str, qbit_port: int,
                             qbit_user: str, qbit_pass: str) -> None:
    existing = client.get("downloadclient")
    if any(dc.get("implementation") == "QBittorrent" for dc in existing):
        ui.ok("  qBittorrent download client already configured.")
        return

    schema = _find_qbit_schema(client)
    if schema is None:
        raise RuntimeError("qBittorrent download client schema not found.")

    field_values = {
        "host": qbit_host,
        "port": qbit_port,
        "username": qbit_user,
        "password": qbit_pass,
        "category": category,
        "useSsl": False,
    }
    for f in schema.get("fields", []):
        name = f.get("name")
        if name in field_values:
            f["value"] = field_values[name]

    payload = {
        "enable": True,
        "protocol": "torrent",
        "priority": 1,
        "name": "qBittorrent",
        "implementation": schema["implementation"],
        "implementationName": schema.get("implementationName", "qBittorrent"),
        "configContract": schema["configContract"],
        "fields": schema["fields"],
        "tags": [],
    }
    client.post("downloadclient", payload)
    ui.ok(f"  qBittorrent download client added (category '{category}').")


def configure_media_management(client: ArrClient) -> None:
    cfg = client.get("config/mediamanagement")
    cfg["copyUsingHardlinks"] = True          # hardlink instead of copy
    cfg["importExtraFiles"] = True
    cfg["setPermissionsLinux"] = False        # avoid chmod on Windows mounts
    client.put(f"config/mediamanagement/{cfg['id']}", cfg)
    ui.ok("  media management: hardlinks enabled.")


def configure_naming(client: ArrClient, rename_key: str) -> None:
    cfg = client.get("config/naming")
    cfg["renameEpisodes" if rename_key == "episodes" else "renameMovies"] = True
    client.put(f"config/naming/{cfg['id']}", cfg)
    ui.ok("  renaming enabled.")


def bootstrap_sonarr(client: ArrClient, qbit_host, qbit_port,
                     qbit_user, qbit_pass) -> None:
    ui.info("Configuring Sonarr...")
    add_root_folder(client, constants.SERIES_DIR)
    add_qbit_download_client(client, "tv", qbit_host, qbit_port, qbit_user, qbit_pass)
    configure_media_management(client)
    configure_naming(client, "episodes")


def bootstrap_radarr(client: ArrClient, qbit_host, qbit_port,
                     qbit_user, qbit_pass) -> None:
    ui.info("Configuring Radarr...")
    add_root_folder(client, constants.MOVIES_DIR)
    add_qbit_download_client(client, "movies", qbit_host, qbit_port, qbit_user, qbit_pass)
    configure_media_management(client)
    configure_naming(client, "movies")
