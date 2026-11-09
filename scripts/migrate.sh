#!/usr/bin/env bash
# Import the repositories in agents.conf from GitHub into Forgejo, once each,
# with history, issues, labels, milestones, pull requests and releases, using
# secrets/github/import-token. Repositories already in Forgejo are skipped.
# Forgejo's proxy allows GitHub's API only while this runs.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
extra="$STATE_DIR/proxy/domains.txt"

reconfigure() { docker compose exec -T forge-proxy squid -k reconfigure; }
close_window() {
  : > "$extra"
  if reconfigure; then
    echo "Forgejo's proxy no longer allows GitHub's API."
  else
    echo "Couldn't reload forge-proxy; run: docker compose restart forge-proxy" >&2
  fi
}

trap close_window EXIT
printf '%s\n' api.github.com objects.githubusercontent.com release-assets.githubusercontent.com > "$extra"
reconfigure
sleep 2   # squid applies a reconfigure asynchronously
docker compose run --rm tools migrate
# Teams get the repositories that now exist.
docker compose run --rm tools bootstrap >/dev/null
