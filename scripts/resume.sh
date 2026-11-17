#!/usr/bin/env bash
# Undo scripts/pause.sh: restore every merge list, reschedule the merges pause
# cancelled (in your name), and let agents start work again.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
confirm "This lets the agents start work again, and merges or reschedules, in your name, the merges pause cancelled"
docker compose run --rm -T tools resume
