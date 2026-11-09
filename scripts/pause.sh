#!/usr/bin/env bash
# Pause every agent: no run starts, a run under way stops at its next tool
# call, every scheduled merge is cancelled (and recorded), and only you may
# merge into main. scripts/resume.sh undoes all of it.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
mkdir -p "$STATE_DIR/control"
docker compose run --rm -T tools pause
