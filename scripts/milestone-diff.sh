#!/usr/bin/env bash
# Every API.lock change since the last milestone issue, from Forgejo.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
docker compose run --rm -T tools milestone-diff
