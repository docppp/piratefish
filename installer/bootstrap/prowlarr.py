"""Prowlarr bootstrap.

Two responsibilities:
  1. Register Sonarr + Radarr as *applications* so indexers auto-sync to them.
  2. Run the guided indexer/tracker wizard (plan §4a): schema-driven, supports
     public and private/account-based trackers, tests before saving.

All Prowlarr API is v1.
"""

from __future__ import annotations

from .. import ui
from ..api import ArrClient, HttpError


# ---------------------------------------------------------------------------
# Applications (Sonarr / Radarr registration)
# ---------------------------------------------------------------------------

def _find_app_schema(client: ArrClient, implementation: str):
    for s in client.get("applications/schema"):
        if s.get("implementation") == implementation:
            return s
    return None


def add_application(client: ArrClient, implementation: str, name: str,
                    prowlarr_url: str, app_base_url: str, app_api_key: str) -> None:
    existing = client.get("applications")
    if any(a.get("implementation") == implementation for a in existing):
        ui.ok(f"  {name} already registered in Prowlarr.")
        return

    schema = _find_app_schema(client, implementation)
    if schema is None:
        raise RuntimeError(f"Prowlarr application schema for {implementation} not found.")

    field_values = {
        "prowlarrUrl": prowlarr_url,
        "baseUrl": app_base_url,
        "apiKey": app_api_key,
    }
    for f in schema.get("fields", []):
        if f.get("name") in field_values:
            f["value"] = field_values[f["name"]]

    payload = {
        "name": name,
        "syncLevel": "fullSync",
        "implementation": schema["implementation"],
        "implementationName": schema.get("implementationName", name),
        "configContract": schema["configContract"],
        "fields": schema["fields"],
        "tags": [],
    }
    client.post("applications", payload)
    ui.ok(f"  {name} registered in Prowlarr (full sync).")


def register_apps(client: ArrClient, prowlarr_url: str,
                  sonarr_url: str, sonarr_key: str,
                  radarr_url: str, radarr_key: str) -> None:
    ui.info("Registering Sonarr & Radarr as Prowlarr applications...")
    add_application(client, "Sonarr", "Sonarr", prowlarr_url, sonarr_url, sonarr_key)
    add_application(client, "Radarr", "Radarr", prowlarr_url, radarr_url, radarr_key)


# ---------------------------------------------------------------------------
# Guided indexer / tracker wizard
# ---------------------------------------------------------------------------

_CREDENTIAL_HINTS = ("password", "passkey", "apikey", "api_key", "cookie",
                     "rsskey", "rss_key", "key", "token")


def _is_secret_field(field: dict) -> bool:
    name = (field.get("name") or "").lower()
    ftype = (field.get("type") or "").lower()
    if ftype == "password":
        return True
    return any(h in name for h in _CREDENTIAL_HINTS)


def _should_prompt(field: dict) -> bool:
    """Prompt for user-supplied settings fields (login/credentials/options)."""
    ftype = (field.get("type") or "").lower()
    if ftype in ("info", "hidden"):
        return False
    name = (field.get("name") or "").lower()
    # Skip advanced/rarely-needed fields that already have a value.
    has_value = field.get("value") not in (None, "", [])
    # Always prompt for obvious credential fields even if they have a blank value.
    if _is_secret_field(field):
        return True
    if ftype in ("textbox", "select") and not has_value:
        # Only prompt for things that look like required inputs.
        return name in ("username", "email", "user", "cookie", "site")
    return False


def search_schema(client: ArrClient, query: str):
    query = query.lower().strip()
    results = []
    for s in client.get("indexer/schema"):
        name = (s.get("name") or "").lower()
        if query in name:
            results.append(s)
    return results


def _fill_definition(schema: dict, non_interactive=False,
                     provided_fields: dict | None = None,
                     app_profile_id: int = 1) -> dict:
    provided_fields = provided_fields or {}
    privacy = schema.get("privacy", "public")
    ui.info(f"Configuring '{schema.get('name')}' (privacy: {privacy})")

    # Case-insensitive lookup so caller-provided values (e.g. 'cookie',
    # 'useragent'/'user_agent') match schema fields regardless of exact casing
    # (trackers use 'cookie', 'userAgent', 'useragent', ...).
    def _norm(s):
        return (s or "").lower().replace("_", "").replace(" ", "")
    norm_provided = {_norm(k): v for k, v in provided_fields.items()}

    for f in schema.get("fields", []):
        name = f.get("name")
        if name in provided_fields:
            f["value"] = provided_fields[name]
            continue
        if _norm(name) in norm_provided:
            f["value"] = norm_provided[_norm(name)]
            continue
        if not _should_prompt(f):
            continue
        label = f.get("label") or name
        if _is_secret_field(f):
            val = ui.ask_secret(f"{label}", non_interactive=non_interactive,
                                default=provided_fields.get(name))
        else:
            default = f.get("value") if f.get("value") not in (None, "") else None
            val = ui.ask(f"{label}", default=default,
                         non_interactive=non_interactive)
        f["value"] = val

    # appProfileId must reference a real sync profile (> 0). The schema ships
    # with 0, so force a valid one (caller supplies it).
    if not schema.get("appProfileId"):
        schema["appProfileId"] = app_profile_id
    schema["enable"] = True
    return schema


def _default_app_profile_id(client: ArrClient) -> int:
    try:
        profiles = client.get("appprofile")
        if profiles:
            return profiles[0]["id"]
    except HttpError:
        pass
    return 1


def test_indexer(client: ArrClient, definition: dict) -> tuple[bool, str]:
    try:
        client.post("indexer/test", definition)
        return True, ""
    except HttpError as e:
        # Prowlarr returns validation details in the body.
        return False, e.body


def save_indexer(client: ArrClient, definition: dict) -> None:
    client.post("indexer", definition)


def existing_indexer_names(client: ArrClient) -> set:
    try:
        return {(i.get("name") or "") for i in client.get("indexer")}
    except HttpError:
        return set()


def add_single_tracker(client: ArrClient, schema: dict,
                       non_interactive=False, provided_fields=None) -> bool:
    # Skip trackers already configured in Prowlarr (name must be unique).
    if schema.get("name") in existing_indexer_names(client):
        ui.ok(f"'{schema.get('name')}' is already added -- skipping.")
        return True

    app_profile_id = _default_app_profile_id(client)
    while True:
        definition = _fill_definition(dict_deepcopy(schema),
                                      non_interactive=non_interactive,
                                      provided_fields=provided_fields,
                                      app_profile_id=app_profile_id)
        ui.step("Testing indexer via Prowlarr...")
        ok, err = test_indexer(client, definition)
        if ok:
            save_indexer(client, definition)
            ui.ok(f"'{schema.get('name')}' tested OK and saved (synced to Sonarr/Radarr).")
            return True
        ui.fail(f"Test failed: {_short_err(err)}")
        if non_interactive:
            return False
        choice = ui.ask("Retry / Skip? [retry/skip]", default="skip")
        if choice.lower().startswith("s"):
            ui.warn(f"Skipped '{schema.get('name')}'.")
            return False
        provided_fields = None  # re-prompt fresh


def _short_err(body: str) -> str:
    import json
    try:
        data = json.loads(body)
        if isinstance(data, list) and data:
            return "; ".join(d.get("errorMessage", str(d)) for d in data)[:200]
        if isinstance(data, dict):
            return data.get("errorMessage") or data.get("message") or body[:200]
    except (ValueError, TypeError):
        pass
    return (body or "unknown error")[:200]


def dict_deepcopy(d):
    import copy
    return copy.deepcopy(d)


def indexer_wizard(client: ArrClient, non_interactive=False,
                   tracker_configs=None) -> int:
    """Interactive (or config-driven) tracker setup. Returns count added."""
    added = 0

    # Non-interactive: trackers come from the config file.
    if non_interactive:
        for tc in (tracker_configs or []):
            name = tc.get("name", "")
            matches = search_schema(client, name)
            exact = next((s for s in matches
                          if (s.get("name") or "").lower() == name.lower()), None)
            schema = exact or (matches[0] if matches else None)
            if not schema:
                ui.warn(f"Tracker '{name}' not found in Prowlarr schema; skipping.")
                continue
            if add_single_tracker(client, schema, non_interactive=True,
                                  provided_fields=tc.get("fields", {})):
                added += 1
        return added

    # Interactive wizard.
    ui.header("Guided Indexer / Tracker Setup")
    ui.info("Add torrent trackers now (public or private). You can also skip and "
            "add them later in the Prowlarr UI.")
    while True:
        if not ui.ask_yes_no("Add a torrent tracker now?", default=(added == 0)):
            break
        query = ui.ask("Search tracker by name (e.g. 'thepiratebay')")
        matches = search_schema(client, query)
        if not matches:
            ui.warn("No trackers matched that name. Try another search term.")
            continue
        # Show up to 15 matches.
        shown = matches[:15]
        for i, s in enumerate(shown, 1):
            print(f"       {i:>2}. {s.get('name')}  "
                  f"({s.get('privacy', 'public')}, {s.get('protocol', 'torrent')})")
        sel = ui.ask("Pick a number (or 'c' to cancel)", default="1")
        if sel.lower().startswith("c"):
            continue
        try:
            idx = int(sel) - 1
            schema = shown[idx]
        except (ValueError, IndexError):
            ui.warn("Invalid selection.")
            continue
        if add_single_tracker(client, schema, non_interactive=False):
            added += 1

    if added == 0:
        ui.warn("No trackers added. Add them anytime in Prowlarr -> Indexers.")
    else:
        ui.ok(f"{added} tracker(s) configured and synced.")
    return added
