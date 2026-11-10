#!/usr/bin/env bash
# Build and start the forge, CI, proxies, dispatcher and agents. Safe to re-run.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
# Mounted into containers; must exist before Docker creates them as root.
mkdir -p "$STATE_DIR/secrets/ci" "$STATE_DIR/secrets/dispatcher" "$STATE_DIR/secrets/github" "$STATE_DIR/secrets/runner-keys" "$STATE_DIR/secrets/claude" "$STATE_DIR/control" "$STATE_DIR/backups" "$STATE_DIR/proxy"
# Extra domains for Forgejo's proxy: empty except during scripts/migrate.sh.
[ -e "$STATE_DIR/proxy/domains.txt" ] || : > "$STATE_DIR/proxy/domains.txt"

docker compose --profile build --profile tools build
docker compose run --rm --no-deps tools init-secrets
docker compose up -d --remove-orphans forgejo ports proxy ci-proxy forge-proxy dispatcher ci-docker

printf 'Waiting for Forgejo'
i=0
until docker compose exec -T forgejo curl -fsS http://127.0.0.1:3000/api/healthz >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -lt 90 ] || { echo; echo "Forgejo didn't start; see: docker compose logs forgejo" >&2; exit 1; }
  printf '.'
  sleep 2
done
echo

# CI: Forgejo learns the runner's secret, the job image goes into the CI
# engine (which never pulls from a registry), then the runner starts.
docker compose exec -T forgejo forgejo forgejo-cli actions register \
  --name ci --secret "$(tr -d '[:space:]' < "$STATE_DIR/secrets/ci/runner-secret")" --labels docker >/dev/null
i=0
until docker compose exec -T ci-docker docker info >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -lt 60 ] || { echo "The CI engine didn't start; see: docker compose logs ci-docker" >&2; exit 1; }
  sleep 2
done
want="$(docker image inspect -f '{{.Id}}' inkproof-ci:1)"
have="$(docker compose exec -T ci-docker docker image inspect -f '{{.Id}}' inkproof-ci:1 2>/dev/null || true)"
if [ "$want" != "$have" ]; then
  echo "Loading the CI image into the CI engine..."
  docker save inkproof-ci:1 | docker compose exec -T ci-docker docker load >/dev/null
fi
docker compose up -d ci-runner

# Agents. Each needs its Forgejo token (scripts/bootstrap.sh), its runner key
# (made above) and the Claude token; without them it isn't started, rather
# than started into a run that can only fail.
AGENTS="coordinator core scale consumer-metrics conformance architecture process lasm"
claude_token="$STATE_DIR/secrets/claude/oauth-token"
if [ ! -s "$claude_token" ]; then
  echo "Not starting agents: put a token from \`claude setup-token\` in ${claude_token#./} first (see README)."
else
  ( umask 077; printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$(tr -d '[:space:]' < "$claude_token")" \
      > "$STATE_DIR/secrets/claude/agent.env" )
  ready=""
  for a in $AGENTS; do
    if [ -s "$STATE_DIR/secrets/$a/token" ] && [ -s "$STATE_DIR/secrets/runner-keys/$a" ]; then
      ready="$ready $a"
    else
      echo "Not starting $a: run scripts/bootstrap.sh, then this script again."
    fi
  done
  [ -z "$ready" ] || docker compose up -d $ready
fi

docker compose ps
echo "Forgejo: http://localhost:${FORGE_PORT:-3000}/"
