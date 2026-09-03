"""Embedded login browser + cookie capture (pywebview).

Opens a real browser window at a tracker/provider login page so the user can log
in normally -- solving Cloudflare, CAPTCHAs and 2FA that block scripted logins.

A floating "I'm logged in -- save & close" button is injected into every page in
the window. When the user clicks it (after logging in), we read the webview's
native cookie store via `get_cookies()` (captures HttpOnly cookies incl.
Cloudflare `cf_clearance`), format them Prowlarr-style, and close the window.
Closing the window manually also captures whatever cookies exist (fallback).

Requires the app to run with `private_mode=False` and a `storage_path` (set in
gui/app.py) so cookies are actually retained.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse


def base_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def cookies_to_string(cookies) -> str:
    """pywebview get_cookies() -> list[http.cookies.SimpleCookie]."""
    pairs = []
    seen = set()
    for c in cookies or []:
        try:
            for name, morsel in c.items():
                if name in seen:
                    continue
                seen.add(name)
                pairs.append(f"{name}={morsel.value}")
        except AttributeError:
            try:
                name, value = c
                if name not in seen:
                    seen.add(name)
                    pairs.append(f"{name}={value}")
            except (ValueError, TypeError):
                continue
    return "; ".join(pairs)


# JS injected into every page: a fixed "save & close" banner button.
_INJECT_JS = r"""
(function(){
  if (document.getElementById('__arr_done_bar')) return;
  var bar = document.createElement('div');
  bar.id = '__arr_done_bar';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
    + 'background:#12324a;color:#fff;padding:8px 12px;font:14px sans-serif;'
    + 'display:flex;align-items:center;gap:12px;box-shadow:0 2px 8px rgba(0,0,0,.4)';
  var txt = document.createElement('span');
  txt.style.flex = '1';
  txt.textContent = 'Log in to the site, then click \u2192';
  var btn = document.createElement('button');
  btn.textContent = '\u2713 I\u2019m logged in \u2014 save & close';
  btn.style.cssText = 'padding:8px 14px;background:#3ddc84;color:#04121a;'
    + 'border:none;border-radius:6px;font-weight:700;cursor:pointer';
  btn.onclick = function(){
    btn.textContent = 'Saving\u2026';
    try { window.pywebview.api.finish(); } catch(e) {}
  };
  bar.appendChild(txt); bar.appendChild(btn);
  document.body.appendChild(bar);
  document.body.style.marginTop =
    (parseInt(getComputedStyle(document.body).marginTop) + 44) + 'px';
})();
"""


def capture_login_cookie(url: str, title: str = "Log in", timeout: int = 1800) -> dict:
    """Open a login window with an injected save&close button.

    Returns a dict {"cookie": <str>, "user_agent": <str>}. The user agent is the
    browser's own UA -- cookie-based trackers require the cookie AND the matching
    User-Agent (Cloudflare validates that they came from the same browser).
    Call from a worker thread (blocks until closed).
    """
    import webview

    holder = {"cookie": "", "user_agent": "", "window": None}
    done = threading.Event()

    def _harvest(win):
        try:
            holder["cookie"] = cookies_to_string(win.get_cookies())
        except Exception:
            holder["cookie"] = ""
        try:
            ua = win.evaluate_js("navigator.userAgent")
            if ua:
                holder["user_agent"] = ua
        except Exception:
            pass

    class LoginApi:
        def finish(self):
            win = holder["window"]
            _harvest(win)
            done.set()
            try:
                win.destroy()
            except Exception:
                pass

    window = webview.create_window(title, url, js_api=LoginApi(),
                                   width=1100, height=850)
    holder["window"] = window

    def inject():
        try:
            window.evaluate_js(_INJECT_JS)
        except Exception:
            pass

    # Inject on every navigation/load.
    try:
        window.events.loaded += inject
    except Exception:
        pass

    # Fallback: capture on manual window close too.
    def on_closing():
        if not done.is_set():
            _harvest(window)
            done.set()
    try:
        window.events.closing += on_closing
    except Exception:
        pass

    # Belt-and-suspenders: keep re-injecting for a while in case a navigation
    # doesn't fire the 'loaded' event on some backends.
    def reinjector():
        for _ in range(60):
            if done.is_set():
                return
            inject()
            time.sleep(2)
    threading.Thread(target=reinjector, daemon=True).start()

    done.wait(timeout=timeout)
    return {"cookie": holder["cookie"], "user_agent": holder["user_agent"]}
