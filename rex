#!/bin/sh
# rextools entry point for Linux (and any POSIX shell): runs py/rex.py with
# Python 3.11+. The commands themselves are Python; see py/rex.py. On Windows
# use rex.cmd (PowerShell or cmd).
dir="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec python3 -I -B "$dir/py/rex.py" "$@"
