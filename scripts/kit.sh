#!/usr/bin/env bash
# Run a kit command in the tools container with this environment's settings,
# for example: bash scripts/kit.sh api GET /repos/tentenths/inkproof-core
# (docker compose run by hand would read .env even in the dev container).
set -euo pipefail
. "$(dirname "$0")/lib.sh"
docker compose run --rm tools "$@"
