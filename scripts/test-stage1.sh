#!/usr/bin/env bash
# Stage 1 acceptance: bootstrap is idempotent, the networks allow only what
# they should, CI builds in its own engine, agents can't tag or release, and
# whether Forgejo sends webhooks for Actions runs.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
fails=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
# allows DESCRIPTION COMMAND...: the command must succeed.
allows() { d="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$d"; else fail "$d"; fi; }
# refuses DESCRIPTION COMMAND...: the command must fail.
refuses() { d="$1"; shift; if "$@" >/dev/null 2>&1; then fail "$d"; else pass "$d"; fi; }

echo "== Bootstrap"
docker compose run --rm tools bootstrap >/dev/null 2>&1
n="$(docker compose run --rm tools bootstrap 2>/dev/null | tail -1)"
[ "$n" = 0 ] && pass "re-running bootstrap changes nothing" || fail "re-running bootstrap made $n change(s)"
bound="$(docker compose port ports 3000)"
case "$bound" in
  127.0.0.1:*) pass "Forgejo is published on $bound only" ;;
  *) fail "Forgejo is published on $bound" ;;
esac

echo "== Agents' network (probe container)"
docker compose --profile tools up -d probe >/dev/null 2>&1
p() { docker compose exec -T probe "$@"; }
allows  "an agent reaches github.com through the proxy" p curl -fsS -m 20 -o /dev/null https://github.com
allows  "an agent reaches Anthropic through the proxy" p curl -sS -m 20 -o /dev/null https://api.anthropic.com
refuses "an agent can't reach api.github.com" p curl -fsS -m 20 -o /dev/null https://api.github.com
refuses "an agent can't reach an unlisted domain" p curl -fsS -m 20 -o /dev/null https://example.com
refuses "an agent has no direct route out" p curl -fsS -m 10 --noproxy '*' -o /dev/null https://github.com
allows  "an agent reaches Forgejo directly" p curl -fsS -m 10 -o /dev/null http://forgejo:3000/api/healthz
allows  "an agent reaches the dispatcher directly" p curl -fsS -m 10 -o /dev/null http://dispatcher:8080/healthz
docker compose --profile tools rm -sf probe >/dev/null 2>&1

echo "== Forgejo's egress"
f() { docker compose exec -T forgejo "$@"; }
refuses "Forgejo has no direct route out" f curl -fsS -m 10 --noproxy '*' -o /dev/null https://github.com
allows  "Forgejo reaches github.com through forge-proxy" f curl -fsS -m 20 -x http://forge-proxy:3128 -o /dev/null https://github.com
refuses "Forgejo can't reach api.github.com" f curl -fsS -m 20 -x http://forge-proxy:3128 -o /dev/null https://api.github.com

echo "== CI jobs' network (as a job runs: host network of the CI engine)"
j() { docker compose exec -T ci-docker docker run --rm --network host inkproof-ci:1 "$@"; }
refuses "a job has no direct route out" j curl -fsS -m 10 -o /dev/null https://github.com
allows  "a job reaches github.com through ci-proxy" j curl -fsS -m 20 -x http://ci-proxy:3128 -o /dev/null https://github.com
refuses "a job can't reach an unlisted domain" j curl -fsS -m 20 -x http://ci-proxy:3128 -o /dev/null https://example.com
allows  "a job reaches Forgejo" j curl -fsS -m 10 -o /dev/null http://forgejo:3000/api/healthz
refuses "a job can't reach the agents' network" j curl -fsS -m 10 -o /dev/null http://dispatcher:8080/healthz
refuses "a job can't use the CI engine's API (plain)" j curl -fsS -m 5 http://127.0.0.1:2375/version
refuses "a job can't use the CI engine's API (TLS, no client certificate)" j curl -fsSk -m 5 https://127.0.0.1:2376/version

echo "== Forgejo API: registration, CI, permissions"
start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
out="$(mktemp)"
if docker compose run --rm tools test-stage1 | tee "$out"; then :; else fails=$((fails + 1)); fi

echo "== Webhooks for Actions runs (dispatcher's record since $start)"
events="$(docker compose exec -T dispatcher cat /state/events.jsonl 2>/dev/null \
  | awk -v s="$start" -F'"at": "' '{ split($2, a, "\""); if (a[1] >= s) print }' || true)"
if [ -z "$events" ]; then
  fail "the dispatcher received no webhooks during the test"
else
  pass "the dispatcher received $(printf '%s\n' "$events" | grep -c .) signed webhooks"
  echo "INFO: event types: $(printf '%s\n' "$events" | sed -n 's/.*"type": "\([^"]*\)".*/\1/p' | sort | uniq -c | tr -s ' ' | tr '\n' ';')"
  if printf '%s\n' "$events" | grep -q '"type": "action_run_failure"'; then
    pass "Forgejo sends a webhook when a CI run fails"
  else
    echo "INFO: no webhook for a failed CI run; the dispatcher must read Actions runs"
  fi
fi

echo
[ "$fails" = 0 ] && echo "Stage 1: all checks passed." || { echo "Stage 1: $fails check(s) failed."; exit 1; }
