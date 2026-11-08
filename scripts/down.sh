#!/usr/bin/env bash
# Stop everything and keep the volumes; scripts/up.sh starts it again.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
docker compose --profile tools --profile build down --remove-orphans
