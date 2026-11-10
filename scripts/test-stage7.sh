#!/usr/bin/env bash
# Stage 7 acceptance: level 3 and releases.
#   - a pull request changing API.lock merges only with the coordinator's
#     approval, and a new commit dismisses it;
#   - the coordinator releases through the gatekeeper, which computes the
#     version from API.lock's changes since the last tag and refuses without
#     the matching CHANGELOG.md entry; versions before and after 1.0;
#   - only the coordinator can ask: other agents can't reach the gatekeeper,
#     and a wrong key is refused; nothing is released while paused, or for a
#     library below level 3, whose releases are yours (scripts/release.sh);
#   - no agent can create a tag or release.
# No Claude runs: it uses a scratch repository at level 3, stage7-scratch,
# with a CI of a few seconds, plays core with core's account, and has the
# coordinator and conformance act through their own forge commands. It
# pauses and resumes the kit for a minute, and deletes the scratch repository
# at the end. About 20 minutes.
# Before it: scripts/bootstrap.sh and scripts/up.sh.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
fails=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; fails=$((fails + 1)); }
info() { echo "INFO: $*"; }
S7=stage7-scratch

kit() { docker compose run --rm -T tools "$@"; }
phase() {
  local out
  out="$(kit test-stage7 "$@" || true)"
  printf '%s\n' "$out" | grep -E '^(PASS|FAIL|INFO)' || true
  printf '%s\n' "$out" | grep -q '^FAIL' && fails=$((fails + 1))
  printf '%s\n' "$out" | grep -Ev '^(PASS|FAIL|INFO|RESULT)' >&3 || true
}
keys="$(mktemp)"
exec 3>>"$keys"
key() { awk -v k="$1" '$1 == k { print $2 }' "$keys"; }
in_() { docker compose exec -T "$@"; }
conformance_approves() {
  in_ conformance sh -c 'cat > /tmp/stage7.json' <<'JSON'
{"summary": "The kit's stage 7 test: nothing to find.", "findings": []}
JSON
  in_ conformance forge review "$S7" "$1" /tmp/stage7.json
}
# expect_refusal TEXT DESCRIPTION COMMAND...: COMMAND fails, saying TEXT.
expect_refusal() {
  local text="$1" what="$2" out
  shift 2
  if out="$("$@" 2>&1)"; then
    fail "$what: it succeeded: $out"
  elif grep -q -- "$text" <<<"$out"; then
    pass "$what: $(tail -1 <<<"$out")"
  else
    fail "$what: $out"
  fi
}
coord_release() { in_ coordinator forge release "$@"; }

echo "== Preconditions"
for a in gatekeeper coordinator conformance core; do
  [ -n "$(docker compose ps -q --status running "$a")" ] || { fail "$a isn't running (scripts/up.sh)"; exit 1; }
done
[ ! -e "$STATE_DIR/control/paused" ] || { fail "the kit is paused; run scripts/resume.sh first"; exit 1; }
pass "the gatekeeper, coordinator, conformance and core are running"

echo "== The version rules"
if out="$(in_ gatekeeper python3 - 2>&1 <<'PY'
import importlib.util, importlib.machinery
loader = importlib.machinery.SourceFileLoader("g", "/usr/local/bin/gatekeeper")
spec = importlib.util.spec_from_loader("g", loader); g = importlib.util.module_from_spec(spec); loader.exec_module(g)
bad = []
for prev, old, new, want in [
    (None, None, "a\n", (0, 1, 0)),
    ((0, 1, 0), "a\n", "a\nb\n", (0, 2, 0)), ((0, 1, 0), "a\nb\n", "a\n", (0, 2, 0)), ((0, 2, 0), "a\n", "a\n", (0, 2, 1)),
    ((1, 0, 0), "a\n", "a\nb\n", (1, 1, 0)), ((1, 0, 0), "a\nb\n", "a\n", (2, 0, 0)),
    ((1, 2, 3), "a\n", "a2\n", (2, 0, 0)), ((1, 2, 3), "a\n", "a\n\n", (1, 2, 4))]:
    got = g.next_version(prev, old, new)[0]
    if got != want: bad.append(f"after {prev}: {got}, expected {want}")
for text, v, want in [("## [0.2.0]\n\n- x\n## [0.1.0]\n- y", (0, 2, 0), "- x"), ("## v1.1.0 - 2026-11-10\n- x", (1, 1, 0), "- x"),
                      ("## [0.2.0]\n- x", (0, 2, 1), None), ("## [0.2.10]\n- x", (0, 2, 1), None)]:
    got = g.changelog_entry(text, v)
    if got != want: bad.append(f"changelog {v}: {got!r}, expected {want!r}")
print("; ".join(bad) or "ok")
PY
)" && [ "$out" = ok ]; then
  pass "before 1.0: minor when API.lock changed, else patch; from 1.0: major when a line went, minor when lines were only added, else patch; both changelog heading forms"
else
  fail "the version rules: $out"
fi

echo "== Who can ask for a release"
in_ core python3 -c 'import urllib.request; urllib.request.urlopen("http://gatekeeper:8080/healthz", timeout=5)' >/dev/null 2>&1 \
  && fail "core can reach the gatekeeper" || pass "core can't reach the gatekeeper"
in_ coordinator python3 -c 'import urllib.request; urllib.request.urlopen("http://gatekeeper:8080/healthz", timeout=5)' >/dev/null 2>&1 \
  && pass "the coordinator can reach the gatekeeper" || fail "the coordinator can't reach the gatekeeper"
s="$(in_ coordinator python3 -c '
import urllib.request, urllib.error
r = urllib.request.Request("http://gatekeeper:8080/release", data=b"{\"repo\": \"x\", \"note\": \"x\"}", method="POST",
    headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})
try: urllib.request.urlopen(r, timeout=10); print(200)
except urllib.error.HTTPError as e: print(e.code)' 2>&1)"
[ "$s" = 403 ] && pass "a request without the coordinator's key is refused (403)" || fail "a wrong key got $s"
expect_refusal "only the coordinator" "core's forge release" in_ core forge release inkproof-core x

echo "== A level-3 scratch repository"
phase scratch

echo "== An API.lock change needs the coordinator"
phase api-pr
pr="$(key API)"
out="$(conformance_approves "$pr" 2>&1)" && pass "conformance approved it" || fail "conformance: $out"
phase check-waits "$pr"
out="$(in_ coordinator forge pr approve "$S7" "$pr" 'Theorem two meets the stage 7 test requirement.' 2>&1)" \
  && pass "the coordinator approved through forge pr approve" || fail "the coordinator's approval: $out"
phase push "$pr"
out="$(conformance_approves "$pr" 2>&1)" || fail "conformance on the new head: $out"
out="$(in_ coordinator forge pr approve "$S7" "$pr" 'Theorem three too.' 2>&1)" || fail "the coordinator on the new head: $out"
phase check-merged "$pr"

echo "== Releases at level 3"
phase issue
issue="$(key ISSUE)"
expect_refusal "no entry for v0.2.0" "without a changelog entry, it's refused" coord_release "$S7" 'Theorems two and three.' "$issue"
phase changelog 0.1.1 'Adds theorems two and three.'
expect_refusal "no entry for v0.2.0" "with an entry for the wrong version, it's refused" coord_release "$S7" 'Theorems two and three.' "$issue"
phase changelog 0.2.0 'Adds theorems two and three.'
out="$(coord_release "$S7" 'Theorems two and three.' "$issue" 2>&1)" && pass "the coordinator released: $out" || fail "the coordinator's release: $out"
phase check-release v0.2.0 "$issue" gatekeeper
expect_refusal "nothing to release" "asking again releases nothing" coord_release "$S7" 'Again.'
expect_refusal "isn't at autonomy level 3" "a library below level 3 isn't released" coord_release inkproof-core 'Not mine.'

echo "== Paused, then your own release"
phase changelog 0.2.1 'Documents the changelog.'
"$(dirname "$0")/pause.sh" >/dev/null 2>&1
expect_refusal "paused" "while paused, it's refused" coord_release "$S7" 'While paused.'
"$(dirname "$0")/resume.sh" >/dev/null 2>&1
out="$("$(dirname "$0")/release.sh" "$S7" 2>&1)"
grep -q '"version": "v0.2.1"' <<<"$out" && pass "scripts/release.sh shows the next release: v0.2.1, $(grep -o '"reason": "[^"]*"' <<<"$out")" || fail "scripts/release.sh's plan: $out"
out="$("$(dirname "$0")/release.sh" "$S7" 'Documentation.' 2>&1)" && pass "scripts/release.sh released it" || fail "scripts/release.sh: $out"
phase check-release v0.2.1 "" "$HUMAN_USER"

echo "== After 1.0"
phase human-tag
phase api-remove
phase changelog 2.0.0 'Removes theorem two.'
out="$(coord_release "$S7" 'Theorem two is gone.' 2>&1)" && pass "after removing a line from API.lock at 1.0: $out" || fail "the major release: $out"
phase check-release v2.0.0 "" gatekeeper

echo "== Cleanup"
[ "${KEEP:-}" = 1 ] || phase cleanup
rm -f "$keys"

echo
[ "$fails" = 0 ] && echo "Stage 7: all checks passed." || { echo "Stage 7: $fails check(s) failed."; exit 1; }
