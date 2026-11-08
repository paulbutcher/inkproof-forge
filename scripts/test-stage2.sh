#!/usr/bin/env bash
# Stage 2 acceptance: the repositories are imported with their history, issues
# and pull requests; migrated content keeps its GitHub authors; push mirrors
# work and go through forge-proxy; GitHub's API is closed again; and a consumer
# fetches this project's dependencies from Forgejo and others from GitHub.
#   bash scripts/test-stage2.sh                  checks, with no pushes to GitHub
#   MIRROR_TEST_REPO=inkproof-mirror-test bash scripts/test-stage2.sh
#       also pushes a commit, a tag and a branch to that scratch GitHub
#       repository through a push mirror, and checks what arrives there
set -euo pipefail
. "$(dirname "$0")/lib.sh"
fails=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }

start="$(date +%s)"
out="$(mktemp)"

echo "== Forgejo API: import, attribution, mirrors, consumer"
if docker compose run --rm tools test-stage2 ${MIRROR_TEST_REPO:-} | tee "$out"; then :; else fails=$((fails + 1)); fi

# git reads of GitHub, from Forgejo's container through its own proxy.
gh_refs() {
  docker compose exec -T forgejo git -c http.proxy=http://forge-proxy:3128 \
    ls-remote "https://github.com/${GH_OWNER}/$1" refs/heads/main 'refs/tags/*' | grep -v '\^{}$' | sort
}

echo "== Imported main and tags match GitHub"
for r in $(grep -v '^[[:space:]]*\(#\|$\)' agents.conf | awk '$3 != "-" { print $3 }' | sort -u); do
  fj="$(docker compose exec -T forgejo git --git-dir="/var/lib/gitea/git/repositories/${GH_OWNER}/$r.git" \
    for-each-ref --format='%(objectname)%09%(refname)' refs/heads/main refs/tags 2>/dev/null | sort)"
  gh="$(gh_refs "$r")"
  if [ -n "$fj" ] && [ "$fj" = "$gh" ]; then
    pass "$r: main and $(printf '%s\n' "$fj" | grep -c refs/tags/) tag(s) match GitHub"
  else
    fail "$r: Forgejo and GitHub differ"
    diff <(printf '%s\n' "$fj") <(printf '%s\n' "$gh") | sed 's/^/    /' || true
  fi
done

mt="$(grep '^MIRROR_TEST ' "$out" || true)"
if [ -n "$mt" ]; then
  echo "== What reached the scratch GitHub repository"
  set -- $mt
  repo="$2" sha="$3" tag="$4" branch="$5"
  refs="$(docker compose exec -T forgejo git -c http.proxy=http://forge-proxy:3128 ls-remote "https://github.com/${GH_OWNER}/$repo")"
  printf '%s\n' "$refs" | grep -q "^$sha	refs/heads/main$" && pass "main arrived on GitHub" || fail "main didn't arrive on GitHub"
  printf '%s\n' "$refs" | grep -q "	refs/tags/$tag$" && pass "tag $tag arrived on GitHub" || fail "tag $tag didn't arrive on GitHub"
  printf '%s\n' "$refs" | grep -q "	refs/heads/$branch$" && fail "branch $branch reached GitHub" || pass "branch $branch stayed in Forgejo"
fi

echo "== Forgejo's egress"
sleep 5   # squid logs a tunnel when it closes, and writes its log asynchronously
log="$(docker compose exec -T forge-proxy cat /var/log/squid/access.log 2>/dev/null \
  | awk -v s="$start" '$1 >= s { print int($1), $4, $6, $7 }')"
window="$(grep '^PROBE_WINDOW ' "$out" || true)"
if [ -n "$window" ]; then
  set -- $window
  if printf '%s\n' "$log" | awk -v a="$2" -v b="$3" '$1 >= a && $1 <= b + 1 && $3 == "CONNECT" && $4 == "github.com:443"' | grep -q .; then
    pass "the push mirror's traffic went through forge-proxy"
  else
    fail "forge-proxy saw no connection to github.com while the push mirror synced"
  fi
fi
others="$(printf '%s\n' "$log" | awk '$4 != "github.com:443" { print $2, $3, $4 }' | sort | uniq -c)"
if [ -z "$others" ]; then
  pass "since the test started, Forgejo connected to github.com and nothing else"
else
  fail "Forgejo's proxy saw other traffic since the test started:"
  printf '%s\n' "$others" | sed 's/^/    /'
fi
# Last, because the attempt itself appears in the proxy's log.
if docker compose exec -T forgejo curl -fsS -m 20 -o /dev/null https://api.github.com >/dev/null 2>&1; then
  fail "Forgejo can reach GitHub's API"
else
  pass "Forgejo can't reach GitHub's API"
fi

echo
[ "$fails" = 0 ] && echo "Stage 2: all checks passed." || { echo "Stage 2: $fails check(s) failed."; exit 1; }
