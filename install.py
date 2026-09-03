#!/usr/bin/env python3
"""ARR Stack Appliance -- single entrypoint launcher.

Run directly on Linux, WSL2, or native Windows:

    python install.py                # interactive install
    python install.py --path X       # provide the data path up front
    python install.py doctor PATH    # just test a path's filesystem
    python install.py verify         # re-check a running stack

The installer uses only the Python standard library, so no `pip install` is
required. Python 3.8+ is needed.
"""

import sys


def _check_python():
    if sys.version_info < (3, 8):
        sys.stderr.write("Python 3.8 or newer is required.\n")
        sys.exit(1)


def main():
    _check_python()
    # Ensure the package is importable when run as a loose script.
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from installer.cli import main as cli_main
    sys.exit(cli_main())


if __name__ == "__main__":
    main()
