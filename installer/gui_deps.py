"""GUI dependency management + availability detection.

The GUI uses pywebview (native webview backend). This module decides whether the
GUI can run and installs what's needed:

- A display must be present (X11/Wayland on Linux; always on Windows/macOS).
- pywebview must be importable. On PEP-668 "externally managed" systems we can't
  `pip install` into the system interpreter, so we create a local virtualenv
  (`.venv`) with --system-site-packages (to reuse the system GTK/WebKit bindings)
  and install pywebview there, then re-exec the GUI under that interpreter.
- On Linux the WebKit2GTK backend needs system libs (python3-gi, gir1.2-webkit2).

If any of this can't be satisfied, callers fall back to the console installer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import ui
from .detect import Environment


PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = PROJECT_DIR / ".venv"


def has_display(env: Environment) -> bool:
    if env.os_name in ("windows", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def pywebview_importable(python: str | None = None) -> bool:
    """Check whether `import webview` works under the given interpreter."""
    if python is None:
        try:
            import webview  # noqa
            return True
        except Exception:
            return False
    try:
        r = subprocess.run([python, "-c", "import webview"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _gtk_bindings_present(python: str | None = None) -> bool:
    code = ("import gi;"
            "gi.require_version('WebKit2','4.1') if True else None;"
            "import gi.repository")
    # Try 4.1 then 4.0.
    probe = ("import gi\n"
             "ok=False\n"
             "for v in ('4.1','4.0'):\n"
             "    try:\n"
             "        gi.require_version('WebKit2', v); ok=True; break\n"
             "    except Exception: pass\n"
             "import sys; sys.exit(0 if ok else 1)\n")
    exe = python or sys.executable
    try:
        r = subprocess.run([exe, "-c", probe], capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _install_gtk_libs(env: Environment) -> bool:
    """Install WebKit2GTK python bindings via the system package manager."""
    pkgs_apt = ["python3-gi", "gir1.2-gtk-3.0",
                "gir1.2-webkit2-4.1", "python3-cairo"]
    pkgs_dnf = ["python3-gobject", "gtk3", "webkit2gtk4.1"]
    from .deps import _run_live
    if env.pkg_manager == "apt":
        _run_live(["apt-get", "update"], env, use_root=True)
        return _run_live(["apt-get", "install", "-y", *pkgs_apt],
                         env, use_root=True) == 0
    if env.pkg_manager in ("dnf", "yum"):
        return _run_live([env.pkg_manager, "install", "-y", *pkgs_dnf],
                         env, use_root=True) == 0
    return False


def _create_venv() -> Path | None:
    """Create .venv (with system site packages) and return its python path."""
    py = _venv_python()
    if py.exists():
        return py
    ui.info("Creating a local environment for the GUI (.venv)...")
    try:
        subprocess.run([sys.executable, "-m", "venv",
                        "--system-site-packages", str(VENV_DIR)],
                       check=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        ui.warn(f"Could not create venv: {e}")
        return None
    return py if py.exists() else None


def _pip_install(python: str, *pkgs: str) -> bool:
    try:
        r = subprocess.run([python, "-m", "pip", "install", "--quiet", *pkgs],
                           capture_output=True, text=True, timeout=600)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_gui(env: Environment) -> str | None:
    """Ensure the GUI can run. Returns the python interpreter to launch the GUI
    with (may be a venv python), or None if the GUI is not available."""
    if not has_display(env):
        ui.info("No graphical display detected -- using the console installer.")
        return None

    # Fast path: current interpreter already has everything.
    if pywebview_importable():
        if env.os_name != "linux" or _gtk_bindings_present():
            return sys.executable

    # Linux: make sure the WebKit2GTK bindings exist first.
    if env.os_name == "linux" and not _gtk_bindings_present():
        ui.info("Installing GTK/WebKit libraries for the GUI...")
        if not _install_gtk_libs(env) or not _gtk_bindings_present():
            ui.warn("GTK/WebKit libraries unavailable -- using the console installer.")
            return None

    # Prefer a local venv: on modern Debian/Ubuntu the system interpreter is
    # PEP-668 "externally managed", so `pip install` into it fails. Creating the
    # venv first (with --system-site-packages to reuse the system GTK bindings)
    # avoids that error entirely.
    py = _create_venv()
    if py is not None:
        if pywebview_importable(str(py)):
            return str(py)
        if _pip_install(str(py), "pywebview") and pywebview_importable(str(py)):
            return str(py)

    # Fallback for non-PEP-668 systems: install into the current interpreter.
    if _pip_install(sys.executable, "pywebview") and pywebview_importable():
        return sys.executable

    ui.warn("Could not set up the GUI environment -- using the console installer.")
    return None
