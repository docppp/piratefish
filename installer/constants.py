"""Central configuration: services, ports, container paths, defaults.

Everything that other modules need to know about "what the stack looks like"
lives here so there is a single source of truth.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Container-internal paths (identical in every service — the single /data model)
# ---------------------------------------------------------------------------
DATA_MOUNT = "/data"
TORR_DIR = "/data/Torr"
INCOMPLETE_DIR = "/data/Torr/incomplete"
TORR_TV_DIR = "/data/Torr/tv"
TORR_MOVIES_DIR = "/data/Torr/movies"
SERIES_DIR = "/data/Media/Series"
MOVIES_DIR = "/data/Media/Movies"

# Host-side sub-directories (relative to the user's data path X)
HOST_SUBDIRS = [
    "Torr/incomplete",
    "Torr/tv",
    "Torr/movies",
    "Media/Series",
    "Media/Movies",
    "Arr/prowlarr",
    "Arr/sonarr",
    "Arr/radarr",
    "Arr/bazarr",
    "Arr/qbittorrent",
    "Arr/jellyseerr",
    "Arr/jellyfin",
    "Arr/homepage",
]

# ---------------------------------------------------------------------------
# Service catalogue
# ---------------------------------------------------------------------------
# Each service: internal container port, default host port, image, config subdir.
# `api` marks the *Arr-family services that expose a config.xml + v3/v1 REST API.


class Service:
    def __init__(self, name, image, port, config_subdir=None, api=False,
                 api_version=None, category=None, label=None):
        self.name = name
        self.image = image
        self.port = port
        self.config_subdir = config_subdir
        self.api = api
        self.api_version = api_version
        self.category = category
        self.label = label or name.capitalize()

    def __repr__(self):
        return f"<Service {self.name}:{self.port}>"


SERVICES = {
    "prowlarr": Service(
        "prowlarr", "lscr.io/linuxserver/prowlarr:1.30.2", 9696,
        config_subdir="Arr/prowlarr", api=True, api_version="v1",
        label="Prowlarr"),
    "sonarr": Service(
        "sonarr", "lscr.io/linuxserver/sonarr:4.0.10", 8989,
        config_subdir="Arr/sonarr", api=True, api_version="v3",
        category="tv", label="Sonarr"),
    "radarr": Service(
        "radarr", "lscr.io/linuxserver/radarr:5.14.0", 7878,
        config_subdir="Arr/radarr", api=True, api_version="v3",
        category="movies", label="Radarr"),
    "bazarr": Service(
        "bazarr", "lscr.io/linuxserver/bazarr:1.4.5", 6767,
        config_subdir="Arr/bazarr", api=False, label="Bazarr"),
    "qbittorrent": Service(
        "qbittorrent", "lscr.io/linuxserver/qbittorrent:5.0.2", 8080,
        config_subdir="Arr/qbittorrent", api=False, label="qBittorrent"),
    "jellyseerr": Service(
        "jellyseerr", "seerr/seerr:v3.4.1", 5055,
        config_subdir="Arr/jellyseerr", api=False, label="Jellyseerr"),
    "jellyfin": Service(
        "jellyfin", "lscr.io/linuxserver/jellyfin:10.11.11", 8096,
        config_subdir="Arr/jellyfin", api=False, label="Jellyfin"),
    "homepage": Service(
        "homepage", "ghcr.io/gethomepage/homepage:v0.9.13", 3000,
        config_subdir="Arr/homepage", api=False, label="Homepage"),
}

# Order matters for start-up / reporting.
SERVICE_ORDER = ["prowlarr", "sonarr", "radarr", "bazarr",
                 "qbittorrent", "jellyseerr", "jellyfin", "homepage"]

# ---------------------------------------------------------------------------
# qBittorrent categories -> download sub-paths
# ---------------------------------------------------------------------------
QBIT_CATEGORIES = {
    "tv": TORR_TV_DIR,
    "movies": TORR_MOVIES_DIR,
}

# ---------------------------------------------------------------------------
# Seeding defaults: ratio 3.0, INFINITE time, then pause (never auto-delete).
# Not user-configurable in the installer -- change later in qBittorrent options.
# ---------------------------------------------------------------------------
SEED_RATIO_LIMIT = 3.0
SEED_TIME_LIMIT_MIN = -1  # -1 = infinite (no seeding-time limit)

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
DEFAULT_QBIT_USER = "admin"
DEFAULT_JELLYFIN_SERVER_NAME = "PirateFish"
STATE_FILENAME = "state.json"
REPORT_FILENAME = "install-report.txt"
COMPOSE_PROJECT = "arrstack"

# Host-side control panel (the dashboard "Shut down stack" button + Start desktop
# shortcut talk to this). It runs on the HOST, not in a container, so it can stop
# the whole stack (including Homepage itself) and bring it back up.
CONTROL_PORT = 8787
