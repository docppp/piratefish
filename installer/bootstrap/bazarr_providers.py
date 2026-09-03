"""Bazarr subtitle providers + language profile setup.

Configures Bazarr via its REST API (`/api/system/settings`):
  - enables selected subtitle providers + their credentials,
  - enables the chosen subtitle language(s),
  - creates a `piratefish_default` language profile and assigns it to Series + Movies.

Language rule (per product spec): the user picks a primary language; **English is
always added as a fallback** unless English *is* the primary. The profile is
ordered [primary, English] so a subtitle is still fetched when the preferred
language is unavailable.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request

from .. import ui

PROFILE_NAME = "piratefish_default"


# Curated set of popular providers with the credential fields Bazarr expects.
# `fields` map to settings-<id>-<name>. `needs_cookie` providers can use the
# embedded login browser to fill the `cookies` field.
PROVIDER_CATALOG = [
    {"id": "opensubtitlescom", "name": "OpenSubtitles.com",
     "fields": [{"name": "username", "label": "Username", "secret": False},
                {"name": "password", "label": "Password", "secret": True}],
     "needs_cookie": False},
    {"id": "podnapisi", "name": "Podnapisi", "fields": [], "needs_cookie": False},
    {"id": "gestdown", "name": "Gestdown (Addic7ed)", "fields": [],
     "needs_cookie": False},
    {"id": "tvsubtitles", "name": "TVSubtitles", "fields": [],
     "needs_cookie": False},
    {"id": "subf2m", "name": "Subf2m (Subscene)", "fields": [],
     "needs_cookie": False},
    {"id": "yifysubtitles", "name": "YIFY Subtitles", "fields": [],
     "needs_cookie": False},
    {"id": "addic7ed", "name": "Addic7ed",
     "fields": [{"name": "username", "label": "Username", "secret": False},
                {"name": "password", "label": "Password", "secret": True},
                {"name": "cookies", "label": "Cookies (auto)", "secret": False}],
     "needs_cookie": True, "login_url": "https://www.addic7ed.com/login.php"},
    {"id": "napiprojekt", "name": "NapiProjekt", "fields": [],
     "needs_cookie": False},
    {"id": "subdl", "name": "SUBDL",
     "fields": [{"name": "api_key", "label": "API key", "secret": True}],
     "needs_cookie": False},
]


COMMON_LANGUAGES = [
    {"code": "en", "name": "English"},
    {"code": "pl", "name": "Polish"},
    {"code": "es", "name": "Spanish"},
    {"code": "fr", "name": "French"},
    {"code": "de", "name": "German"},
    {"code": "it", "name": "Italian"},
    {"code": "pt", "name": "Portuguese"},
    {"code": "nl", "name": "Dutch"},
    {"code": "ru", "name": "Russian"},
    {"code": "cs", "name": "Czech"},
    {"code": "sv", "name": "Swedish"},
    {"code": "da", "name": "Danish"},
    {"code": "fi", "name": "Finnish"},
    {"code": "no", "name": "Norwegian"},
    {"code": "tr", "name": "Turkish"},
    {"code": "ar", "name": "Arabic"},
    {"code": "zh", "name": "Chinese"},
    {"code": "ja", "name": "Japanese"},
    {"code": "ko", "name": "Korean"},
]


def read_api_key(container="bazarr") -> str | None:
    try:
        out = subprocess.run(
            ["docker", "exec", container, "cat", "/config/config/config.yaml"],
            capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        import re
        m = re.search(r"apikey:\s*([A-Za-z0-9]+)", out.stdout)
        return m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def _post_form(base_url: str, api_key: str, pairs: list[tuple[str, str]]) -> int:
    body = urllib.parse.urlencode(pairs).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/api/system/settings",
                                 data=body, method="POST")
    req.add_header("X-API-KEY", api_key)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def build_language_profile(primary_language: str) -> tuple[list, list]:
    """Return (enabled_languages, profile_items) with English fallback."""
    primary = (primary_language or "en").lower()
    langs = [primary]
    if primary != "en":
        langs.append("en")
    items = []
    for i, code in enumerate(langs, start=1):
        items.append({"id": i, "language": code,
                      "audio_exclude": "False", "hi": "False", "forced": "False"})
    profile = [{
        "profileId": 1, "name": PROFILE_NAME, "items": items,
        "cutoff": None, "mustContain": [], "mustNotContain": [],
        "originalFormat": False, "tag": None,
    }]
    return langs, profile


def apply(base_url: str, providers: list, primary_language: str,
          container="bazarr") -> None:
    """providers: list of {id, fields:{name:value}}; primary_language: code2."""
    api_key = read_api_key(container)
    if not api_key:
        raise RuntimeError("Could not read Bazarr API key.")

    langs, profile = build_language_profile(primary_language)

    pairs: list[tuple[str, str]] = []
    for code in langs:
        pairs.append(("languages-enabled", code))
    pairs.append(("languages-profiles", json.dumps(profile)))

    # Providers + credentials.
    for p in providers or []:
        pid = p.get("id")
        if not pid:
            continue
        pairs.append(("settings-general-enabled_providers", pid))
        for fname, fval in (p.get("fields") or {}).items():
            if fval is None:
                continue
            pairs.append((f"settings-{pid}-{fname}", str(fval)))

    # Assign the profile to Series + Movies.
    pairs += [
        ("settings-general-serie_default_enabled", "true"),
        ("settings-general-serie_default_profile", "1"),
        ("settings-general-movie_default_enabled", "true"),
        ("settings-general-movie_default_profile", "1"),
    ]

    status = _post_form(base_url, api_key, pairs)
    if status not in (200, 204):
        raise RuntimeError(f"Bazarr settings POST returned {status}")

    lang_str = " + ".join(langs)
    prov_str = ", ".join(p.get("id") for p in (providers or [])) or "none"
    ui.ok(f"Bazarr: languages [{lang_str}], providers [{prov_str}], profile "
          f"'{PROFILE_NAME}' assigned to Series & Movies.")
