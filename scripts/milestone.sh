#!/usr/bin/env bash
# End a milestone: open its issue on inkproof-coordination, with every
# API.lock change since the last milestone, labelled milestone-review, which
# starts the architecture, process and Lasm reviews.
#   scripts/milestone.sh "Milestone 2"
set -euo pipefail
. "$(dirname "$0")/lib.sh"
[ $# = 1 ] || { echo "usage: scripts/milestone.sh <milestone name>" >&2; exit 2; }
docker compose run --rm -T tools milestone "$1"
