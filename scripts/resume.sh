#!/usr/bin/env bash
# Undo scripts/pause.sh or scripts/drain.sh: let agents start work again.
# After a pause, first restore every merge list and reschedule the merges
# pause cancelled (in your name).
set -euo pipefail
. "$(dirname "$0")/lib.sh"
confirm "This lets the agents start work again, and after a pause merges or reschedules, in your name, the merges it cancelled"
docker compose run --rm -T tools resume
