#!/bin/sh
# Container entrypoint: make sure a Stockfish build that runs on THIS CPU is
# installed, then exec the real command.
#
# fetch_stockfish.py re-probes a binary that is already in the volume and
# re-fetches only if it cannot search here, so a volume carried over from a
# different machine heals itself instead of crashing with SIGILL.
#
# Several workers start at once and share the volume, so the installer runs
# under an exclusive lock: the first one downloads, the rest wait and then
# find a verified binary already present.
set -eu

if [ "${FETCH_STOCKFISH:-1}" = "1" ]; then
    flock /app/vendor/.install.lock python /app/tools/fetch_stockfish.py
fi

exec "$@"
