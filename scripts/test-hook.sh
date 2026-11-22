#!/usr/bin/env bash
# Unit tests for the agents' own commands: the managed hook
# (docker/agent/bin/inkproof-hook), the guard that keeps the human's sessions
# from changing the checkouts runs use; and forge's version, held to the
# gatekeeper's rule. Quick, and nothing outside a throwaway container.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
image="$(sed -n 's/^FROM //p' docker/dispatcher/Dockerfile)"
docker run --rm --network none -v "$PWD/docker/agent:/src:ro" -v "$PWD/docker/gatekeeper:/gatekeeper:ro" \
  -w /src -e PYTHONDONTWRITEBYTECODE=1 -e GATEKEEPER_PY=/gatekeeper/gatekeeper.py \
  "$image" python3 -m unittest test_inkproof_hook test_forge
