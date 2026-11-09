#!/usr/bin/env bash
# Apply an autonomy level's Forgejo settings to one of the project's
# repositories, and check the files that level needs (which are yours, so it
# says what to change rather than committing them). Safe to re-run; run it
# with a lower level to fall back.
#   scripts/setup-repo.sh inkproof-core 1
set -euo pipefail
. "$(dirname "$0")/lib.sh"
[ $# = 2 ] || { echo "usage: scripts/setup-repo.sh <repository> <level 1-3>" >&2; exit 2; }
docker compose run --rm tools setup-repo "$1" "$2"
