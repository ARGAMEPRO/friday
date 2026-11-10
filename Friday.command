#!/usr/bin/env bash
# Double-click this file in Finder to launch F.R.I.D.A.Y. on macOS.
# It opens Terminal and starts the local server; close the window to stop.
cd "$(dirname "$0")" || exit 1
exec ./run.sh "$@"
