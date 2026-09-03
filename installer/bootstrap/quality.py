"""Quality profile setup for Sonarr and Radarr.

Creates/updates one canonical profile (`piratefish_default`) from user-selected:
  - resolution,
  - allowed release source types,
  - max bitrate cap.
Optionally removes all other quality profiles so only the canonical one remains.

Data model is built from each app's live `/qualityprofile/schema`, so quality ids
and groups stay version-safe.
"""

from __future__ import annotations

from typing import Optional

from .. import ui
from ..api import ArrClient, HttpError


PROFILE_NAME = "piratefish_default"

RESOLUTION_OPTIONS = (
    {"id": "720p", "label": "HD 720p"},
    {"id": "1080p", "label": "HD 1080p"},
    {"id": "2160p", "label": "Ultra-HD 4K 2160p"},
)

# Source labels come directly from Servarr Wiki "Qualities Defined" terminology.
RELEASE_TYPE_OPTIONS = (
    {"id": "bluray", "label": "BluRay"},
    {"id": "webdl", "label": "WEB-DL"},
    {"id": "webrip", "label": "WEBRip"},
    {"id": "hdtv", "label": "HDTV"},
    {"id": "remux", "label": "Remux"},
    {"id": "dvd", "label": "DVD"},
)

DEFAULT_SELECTION = {
    "resolution": "1080p",
    "release_types": ["bluray", "webdl", "webrip", "hdtv"],
    "max_bitrate_mbps": 8.0,
}

_RES_TOKENS = {r["id"]: r["id"].upper() for r in RESOLUTION_OPTIONS}
_RELEASE_PATTERNS = {
    "bluray": ("BLURAY",),
    "webdl": ("WEBDL", "WEB-DL"),
    "webrip": ("WEBRIP", "WEB-RIP"),
    "hdtv": ("HDTV",),
    "remux": ("REMUX",),
    "dvd": ("DVD",),
}
_RELEASE_ALIASES = {
    "bluray": "bluray",
    "blu-ray": "bluray",
    "webdl": "webdl",
    "web-dl": "webdl",
    "webrip": "webrip",
    "web-rip": "webrip",
    "hdtv": "hdtv",
    "remux": "remux",
    "dvd": "dvd",
}


def _item_name(item: dict) -> str:
    if item.get("quality"):
        return item["quality"].get("name", "")
    return item.get("name", "")


def _item_id(item: dict):
    if item.get("quality"):
        return item["quality"].get("id")
    return item.get("id")


def selection_options() -> dict:
    return {
        "profile_name": PROFILE_NAME,
        "resolutions": list(RESOLUTION_OPTIONS),
        "release_types": list(RELEASE_TYPE_OPTIONS),
        "defaults": dict(DEFAULT_SELECTION),
    }


def _normalize_release_type(value: str) -> Optional[str]:
    key = (value or "").strip().lower()
    return _RELEASE_ALIASES.get(key)


def normalize_selection(selection) -> dict:
    if isinstance(selection, dict):
        raw = dict(selection)
    elif selection is None:
        raw = {}
    else:
        raise ValueError("quality selection must be an object")

    resolution = str(raw.get("resolution") or
                     DEFAULT_SELECTION["resolution"]).strip().lower()
    if resolution not in _RES_TOKENS:
        valid = ", ".join(r["id"] for r in RESOLUTION_OPTIONS)
        raise ValueError(f"unknown resolution '{resolution}' (expected: {valid})")

    release_types = raw.get("release_types")
    if release_types is None:
        release_types = raw.get("sources")
    if release_types is None:
        release_types = list(DEFAULT_SELECTION["release_types"])
    if isinstance(release_types, str):
        release_types = [release_types]
    if not isinstance(release_types, (list, tuple)):
        raise ValueError("release_types must be a list")

    normalized_types = []
    for v in release_types:
        nv = _normalize_release_type(str(v))
        if nv and nv not in normalized_types:
            normalized_types.append(nv)
    if not normalized_types:
        raise ValueError("at least one release type must be selected")

    try:
        max_bitrate_mbps = float(
            raw.get("max_bitrate_mbps", DEFAULT_SELECTION["max_bitrate_mbps"])
        )
    except (TypeError, ValueError):
        raise ValueError("max_bitrate_mbps must be a number") from None
    if max_bitrate_mbps <= 0:
        raise ValueError("max_bitrate_mbps must be greater than 0")

    return {
        "resolution": resolution,
        "release_types": normalized_types,
        "max_bitrate_mbps": max_bitrate_mbps,
    }


def _matches_resolution(name: str, resolution: str) -> bool:
    return _RES_TOKENS[resolution] in name.upper()


def _matches_release_type(name: str, release_types: list[str]) -> bool:
    up = name.upper()
    for rt in release_types:
        pats = _RELEASE_PATTERNS.get(rt, ())
        if any(p in up for p in pats):
            return True
    return False


def _build_profile(schema: dict, selection) -> dict:
    cfg = normalize_selection(selection)
    cutoff_id = None

    def _mark(item: dict) -> bool:
        nonlocal cutoff_id
        children = item.get("items") or []
        if children:
            allowed = False
            for sub in children:
                allowed = _mark(sub) or allowed
            item["allowed"] = allowed
            if allowed:
                # Servarr validates cutoff against allowed quality *or group* ids.
                # Nested child quality ids can be rejected, while the group id is valid.
                gid = _item_id(item)
                if gid is not None:
                    cutoff_id = gid
            return allowed

        name = _item_name(item)
        allowed = (_matches_resolution(name, cfg["resolution"]) and
                   _matches_release_type(name, cfg["release_types"]))
        item["allowed"] = allowed
        if allowed:
            qid = _item_id(item)
            if qid is not None:
                cutoff_id = qid  # schema order is lowest -> highest
        return allowed

    for item in schema.get("items", []):
        _mark(item)

    if cutoff_id is None:
        raise RuntimeError(
            f"No qualities matched resolution={cfg['resolution']} "
            f"and release types={cfg['release_types']}"
        )

    schema["name"] = PROFILE_NAME
    schema["upgradeAllowed"] = True
    schema["cutoff"] = cutoff_id
    return schema


def _existing_profiles(client: ArrClient) -> list[dict]:
    try:
        profiles = client.get("qualityprofile")
        if isinstance(profiles, list):
            return profiles
    except HttpError:
        pass
    return []


def _select_target_profile(profiles: list[dict]) -> Optional[dict]:
    for p in profiles:
        if p.get("name") == PROFILE_NAME:
            return p
    return None


def ensure_profile(client: ArrClient, selection) -> Optional[int]:
    """Create/update the tuned profile; return its id."""
    cfg = normalize_selection(selection)
    try:
        schema = client.get("qualityprofile/schema")
    except HttpError as e:
        ui.warn(f"  could not read quality schema: {e}")
        return None

    profile = _build_profile(schema, cfg)
    profiles = _existing_profiles(client)
    existing = _select_target_profile(profiles)
    try:
        if existing:
            profile["id"] = existing["id"]
            client.put(f"qualityprofile/{existing['id']}", profile)
            pid = existing["id"]
        else:
            created = client.post("qualityprofile", profile)
            pid = created.get("id") if isinstance(created, dict) else None
            if pid is None:  # some versions return list/empty; re-fetch
                again = _select_target_profile(_existing_profiles(client))
                pid = again["id"] if again else None
        ui.ok(f"  quality profile '{PROFILE_NAME}' ready.")
        return pid
    except HttpError as e:
        ui.warn(f"  could not create quality profile: {e}")
        return None


def _remove_other_profiles(client: ArrClient, keep_profile_id: int) -> int:
    removed = 0
    for profile in _existing_profiles(client):
        pid = profile.get("id")
        if pid is None or pid == keep_profile_id:
            continue
        try:
            client.delete(f"qualityprofile/{pid}")
            removed += 1
        except HttpError as e:
            name = profile.get("name") or str(pid)
            ui.warn(f"  could not remove quality profile '{name}': {e}")
    return removed


def _apply_to_existing(client: ArrClient, kind: str, profile_id: int) -> int:
    """Point already-added items at the new profile (best-effort). kind is
    'movie' (Radarr) or 'series' (Sonarr)."""
    endpoint = "movie" if kind == "movie" else "series"
    changed = 0
    try:
        items = client.get(endpoint)
    except HttpError:
        return 0
    for it in items:
        if it.get("qualityProfileId") == profile_id:
            continue
        it["qualityProfileId"] = profile_id
        try:
            client.put(f"{endpoint}/{it['id']}", it)
            changed += 1
        except HttpError:
            pass
    return changed


def _quality_name_from_definition(defn: dict) -> str:
    q = defn.get("quality") or {}
    return (q.get("name") or defn.get("title") or "")


def _bitrate_cap_units(max_bitrate_mbps: float, kind: str) -> float:
    # 1 Mbps ~= 450 MB/hour.
    mb_per_hour = max_bitrate_mbps * 450.0
    # Sonarr definitions are shown as MB/hour; Radarr as MB/minute.
    return mb_per_hour if kind == "series" else (mb_per_hour / 60.0)


def _cap_definition(defn: dict, target_max: float) -> bool:
    prev = (defn.get("minSize"), defn.get("preferredSize"), defn.get("maxSize"))

    min_size = defn.get("minSize")
    pref_size = defn.get("preferredSize")
    max_size = target_max

    if min_size is not None:
        min_size = min(float(min_size), max_size)
    if pref_size is not None:
        pref_size = min(float(pref_size), max_size)
    if min_size is not None and pref_size is not None and pref_size < min_size:
        pref_size = min_size

    defn["minSize"] = round(min_size, 3) if min_size is not None else None
    defn["preferredSize"] = round(pref_size, 3) if pref_size is not None else None
    defn["maxSize"] = round(max_size, 3)

    now = (defn.get("minSize"), defn.get("preferredSize"), defn.get("maxSize"))
    return prev != now


def _apply_bitrate_cap(client: ArrClient, selection, kind: str) -> int:
    cfg = normalize_selection(selection)
    target_max = _bitrate_cap_units(cfg["max_bitrate_mbps"], kind)
    try:
        defs = client.get("qualitydefinition")
    except HttpError as e:
        ui.warn(f"  could not read quality definitions: {e}")
        return 0

    changed = []
    for d in defs:
        name = _quality_name_from_definition(d)
        if not name:
            continue
        if not _matches_resolution(name, cfg["resolution"]):
            continue
        if not _matches_release_type(name, cfg["release_types"]):
            continue
        if _cap_definition(d, target_max):
            changed.append(d)

    if not changed:
        return 0

    try:
        client.put("qualitydefinition/update", defs)
        return len(changed)
    except HttpError:
        applied = 0
        for d in changed:
            try:
                client.put(f"qualitydefinition/{d['id']}", d)
                applied += 1
            except HttpError:
                pass
        return applied


def apply(client: ArrClient, selection, kind: str,
          update_existing: bool = True,
          prune_other_profiles: bool = False) -> Optional[int]:
    """Configure the profile in one app. kind = 'movie' | 'series'."""
    cfg = normalize_selection(selection)
    label = "Radarr" if kind == "movie" else "Sonarr"
    ui.info(
        f"Configuring {label} quality profile "
        f"({cfg['resolution']}, {', '.join(cfg['release_types'])}, "
        f"{cfg['max_bitrate_mbps']} Mbps)..."
    )
    pid = ensure_profile(client, cfg)
    if pid is None:
        return None
    changed_defs = _apply_bitrate_cap(client, cfg, kind)
    if changed_defs:
        ui.ok(f"  updated bitrate cap for {changed_defs} quality definition(s).")
    if update_existing:
        n = _apply_to_existing(client, kind, pid)
        if n:
            ui.ok(f"  updated {n} existing {label} item(s) to '{PROFILE_NAME}'.")
    if prune_other_profiles:
        removed = _remove_other_profiles(client, pid)
        if removed:
            ui.ok(f"  removed {removed} extra {label} quality profile(s).")
    return pid
