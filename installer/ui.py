"""Tiny TUI helpers: colored output, section headers, prompts.

Kept dependency-free (no rich/colorama) so the installer runs anywhere. Colors
degrade gracefully when stdout is not a TTY or on legacy Windows terminals.
"""

from __future__ import annotations

import getpass
import os
import sys

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# Enable ANSI on Windows 10+ terminals.
if os.name == "nt":  # pragma: no cover - windows only
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        _USE_COLOR = False


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t):   return _c("1", t)
def green(t):  return _c("32", t)
def red(t):    return _c("31", t)
def yellow(t): return _c("33", t)
def cyan(t):   return _c("36", t)
def dim(t):    return _c("2", t)


# --- status lines -----------------------------------------------------------

# Optional listeners receive (level, message) for every status line, so a GUI
# can mirror the console output live. Console printing always happens too.
_listeners = []


def add_listener(fn):
    _listeners.append(fn)


def remove_listener(fn):
    if fn in _listeners:
        _listeners.remove(fn)


def _emit(level, msg):
    for fn in list(_listeners):
        try:
            fn(level, msg)
        except Exception:
            pass


def ok(msg):    print(f"  {green('[OK]')} {msg}");  _emit("ok", msg)
def info(msg):  print(f"  {cyan('[..]')} {msg}");   _emit("info", msg)
def warn(msg):  print(f"  {yellow('[!]')} {msg}");  _emit("warn", msg)
def fail(msg):  print(f"  {red('[X]')} {msg}");     _emit("fail", msg)
def step(msg):  print(f"  {cyan('->')} {msg}");     _emit("step", msg)


def header(title: str) -> None:
    line = "=" * 60
    print()
    print(cyan(line))
    print(cyan(f"  {title}"))
    print(cyan(line))
    _emit("header", title)


def banner() -> None:
    print(cyan("+" + "-" * 48 + "+"))
    print(cyan("|") + bold("        ARR Stack Appliance -- Installer        ") + cyan("|"))
    print(cyan("+" + "-" * 48 + "+"))


# --- prompts ----------------------------------------------------------------

def ask(prompt: str, default: str | None = None,
        non_interactive: bool = False) -> str:
    if non_interactive:
        if default is None:
            raise RuntimeError(f"Missing required value in non-interactive mode: {prompt}")
        return default
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            resp = input(f"  {bold('?')} {prompt}{suffix}: ").strip()
        except EOFError:
            resp = ""
        if resp:
            return resp
        if default is not None:
            return default


# --- filesystem path completion --------------------------------------------

def _make_path_completer():
    """Return a readline completer function that completes filesystem paths."""
    import glob

    def completer(text, state):
        expanded = os.path.expanduser(os.path.expandvars(text))
        matches = glob.glob(expanded + "*")
        results = []
        for m in matches:
            results.append(m + os.sep if os.path.isdir(m) else m)
        results.sort()
        try:
            return results[state]
        except IndexError:
            return None

    return completer


class _path_completion:
    """Context manager: enable readline path completion, restore afterwards."""

    def __enter__(self):
        self.readline = None
        try:
            import readline
        except ImportError:
            return self  # not available (e.g. bare Windows) -> plain input
        self.readline = readline
        self._prev_completer = readline.get_completer()
        self._prev_delims = readline.get_completer_delims()
        readline.set_completer(_make_path_completer())
        # Only whitespace breaks words, so '/' and '.' stay in the token.
        readline.set_completer_delims(" \t\n")
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")   # macOS libedit
        else:
            readline.parse_and_bind("tab: complete")         # GNU readline
        return self

    def __exit__(self, *exc):
        if self.readline is not None:
            self.readline.set_completer(self._prev_completer)
            self.readline.set_completer_delims(self._prev_delims)
        return False


def ask_path(prompt: str, default: str | None = None,
             non_interactive: bool = False) -> str:
    """Like `ask`, but with Tab filesystem auto-completion (when available)."""
    if non_interactive:
        if default is None:
            raise RuntimeError(f"Missing required path in non-interactive mode: {prompt}")
        return default
    suffix = f" [{default}]" if default else ""
    print(dim("  (Tab to autocomplete paths)"))
    with _path_completion():
        while True:
            try:
                resp = input(f"  {bold('?')} {prompt}{suffix}: ").strip()
            except EOFError:
                resp = ""
            if resp:
                return os.path.expanduser(os.path.expandvars(resp))
            if default is not None:
                return default


def ask_yes_no(prompt: str, default: bool = True,
               non_interactive: bool = False) -> bool:
    if non_interactive:
        return default
    d = "Y/n" if default else "y/N"
    while True:
        try:
            resp = input(f"  {bold('?')} {prompt} [{d}]: ").strip().lower()
        except EOFError:
            resp = ""
        if not resp:
            return default
        if resp in ("y", "yes"):
            return True
        if resp in ("n", "no"):
            return False


def ask_secret(prompt: str, non_interactive: bool = False,
               default: str | None = None) -> str:
    if non_interactive:
        if default is None:
            raise RuntimeError(f"Missing required secret in non-interactive mode: {prompt}")
        return default
    while True:
        val = getpass.getpass(f"  ? {prompt}: ").strip()
        if val:
            return val
