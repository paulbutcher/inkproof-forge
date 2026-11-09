#!/usr/bin/env bash
# Interrupt an agent's current run. It is recorded and rewritten like any
# other, so you can open it in the extension and continue it.
#   bash scripts/stop-run.sh core
set -euo pipefail
. "$(dirname "$0")/lib.sh"
[ $# = 1 ] || { echo "usage: bash scripts/stop-run.sh <agent>" >&2; exit 2; }
docker compose exec -T "$1" agent-runner stop
