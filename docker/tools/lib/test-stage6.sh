# kit test-stage6 PHASE ...: the stage 6 checks that run against Forgejo's
# API. scripts/test-stage6.sh drives them, with the steps that act in
# containers. Phases print PASS/FAIL/INFO lines, and KEY value lines.
#
# They use a scratch repository at level 2, stage6-scratch, whose CI is a
# few seconds of shell: it fails when the commit has a file named FAIL. Its
# name isn't inkproof-, so the dispatcher starts no runs for it; the test
# plays core and conformance with their own accounts, and the gatekeeper does
# its real work.

S6_FAILS=0
s6_pass() { echo "PASS: $*"; }
s6_fail() { echo "FAIL: $*"; S6_FAILS=$((S6_FAILS + 1)); }
s6_info() { echo "INFO: $*"; }
s6_done() { echo "RESULT: $S6_FAILS failure(s)"; [ "$S6_FAILS" = 0 ]; }

S6=stage6-scratch

s6_b64() { printf '%s' "$1" | base64 -w0; }
as() { local who="$1"; shift; TOKEN="$(token_of "$who")" "$@"; }

S6_WORKFLOW='# The stage 6 test'"'"'s CI: fails when the commit has a file named FAIL.
name: checks
on:
  pull_request:
  push:
    branches: [main]
jobs:
  build:
    runs-on: docker
    steps:
      - env:
          TOKEN: ${{ secrets.DEPS_READ_TOKEN }}
        run: |
          git init -q .
          git -c "http.extraHeader=Authorization: Basic $(printf "ci-reader:%s" "$TOKEN" | base64 -w0)" \
            fetch -q --depth 1 "${FORGE_URL}/${GITHUB_REPOSITORY}.git" "$GITHUB_SHA"
          git checkout -q FETCH_HEAD
          test ! -e FAIL || { echo "FAIL is present"; exit 1; }
'

# s6_pr BRANCH FILE CONTENT TITLE: core opens a pull request and schedules
# its merge, as an agent does; prints the number.
s6_pr() {
  local branch="$1" file="$2" content="$3" title="$4" n
  as human api DELETE "/repos/$ORG/$S6/branches/$branch" >/dev/null
  local have method=POST ok=201
  have="$(as core call 200,404 GET "/repos/$ORG/$S6/contents/$file?ref=main" | jq -r '.sha // ""')"
  [ -n "$have" ] && method=PUT ok=200
  as core call "$ok" "$method" "/repos/$ORG/$S6/contents/$file" "$(jq -nc --arg b "$branch" --arg c "$(s6_b64 "$content")" \
    --arg m "$title" --arg s "$have" '{branch: "main", new_branch: $b, content: $c, message: $m} + (if $s == "" then {} else {sha: $s} end)')" >/dev/null
  n="$(as core call 201 POST "/repos/$ORG/$S6/pulls" "$(jq -nc --arg b "$branch" --arg t "$title" \
    '{head: $b, base: "main", title: $t, body: "(no reply needed)\n\nThe kit'"'"'s stage 6 test."}')" | jq -r .number)"
  as core call 200,201 POST "/repos/$ORG/$S6/pulls/$n/merge" \
    '{"Do":"squash","merge_when_checks_succeed":true,"delete_branch_after_merge":true}' >/dev/null
  echo "$n"
}

s6_head() { as human call 200 GET "/repos/$ORG/$S6/pulls/$1" | jq -r .head.sha; }
s6_merged() { as human call 200 GET "/repos/$ORG/$S6/pulls/$1" | jq -r .merged; }
# s6_ci SHA: the status of the Actions run for SHA, or nothing.
s6_ci() { as human call 200 GET "/repos/$ORG/$S6/actions/runs?head_sha=$1" | jq -r --arg s "$1" '[.workflow_runs[]? | select(.commit_sha == $s) | .status][0] // ""'; }
# s6_approval WHO N SHA: WHO's live approval of N at SHA, as JSON, or nothing.
s6_approval() {
  as human call 200 GET "/repos/$ORG/$S6/pulls/$2/reviews" | jq -c --arg w "$1" --arg s "$3" \
    '[.[] | select(.user.login == $w and .state == "APPROVED" and .commit_id == $s and (.dismissed | not) and (.stale | not))][0] // empty'
}
# s6_wait SECONDS COMMAND...: until COMMAND prints something; prints it.
s6_wait() {
  local deadline=$(( $(date +%s) + $1 )) out
  shift
  while [ "$(date +%s)" -lt "$deadline" ]; do
    out="$("$@" 2>/dev/null || true)"
    [ -n "$out" ] && { printf '%s\n' "$out"; return 0; }
    sleep 5
  done
  return 1
}
s6_ci_done() { case "$(s6_ci "$1")" in success|failure) s6_ci "$1" ;; esac; }
s6_is_merged() { [ "$(s6_merged "$1")" = true ] && echo merged; }
s6_dismissed() { [ -z "$(s6_approval "$@")" ] && echo dismissed; }

# --- the scratch repository at level 2 ----------------------------------------
s6_scratch() {
  TOKEN="$(token_of human)"
  api DELETE "/repos/$ORG/$S6" >/dev/null
  call 201 POST "/orgs/$ORG/repos" "$(jq -nc --arg n "$S6" '{name: $n, private: true, auto_init: true, default_branch: "main"}')" >/dev/null
  ensure_repo_settings "$S6" >/dev/null 2>&1
  local ops
  ops="$(jq -nc --arg w "$(s6_b64 "$S6_WORKFLOW")" --arg o "$(s6_b64 "$(codeowners 2)
")" --arg a "$(s6_b64 'theorem one : 1 = 1
')" '[{operation: "create", path: ".forgejo/workflows/checks.yml", content: $w},
      {operation: "create", path: ".forgejo/CODEOWNERS", content: $o},
      {operation: "create", path: "API.lock", content: $a}]')"
  call 201 POST "/repos/$ORG/$S6/contents" "$(jq -nc --argjson f "$ops" '{branch: "main", files: $f, message: "Level 2, a quick CI and an API.lock"}')" >/dev/null
  local who
  for who in core conformance; do
    call 204 PUT "/repos/$ORG/$S6/collaborators/$who" '{"permission":"write"}' >/dev/null
  done
  ensure_labels "$S6" >/dev/null 2>&1
  ensure_branch_protection "$S6" 2 >/dev/null 2>&1
  call 200 GET "/repos/$ORG/$S6/branch_protections/main" \
    | jq -e '(.approvals_whitelist_username | sort) == (["gatekeeper", "conformance", env.HUMAN_USER] | sort)
             and .block_on_official_review_requests and .dismiss_stale_approvals' >/dev/null \
    && s6_pass "$S6 is protected as at level 2: approvals from $HUMAN_USER, gatekeeper and conformance" \
    || s6_fail "$S6's protection isn't level 2's"
  s6_info "code owners: $(codeowners 2 | grep -v '^#' | tr '\n' ';')"
}

# --- an ordinary pull request merges on the gatekeeper's and conformance's approvals
s6_good() {
  local n sha ci a
  n="$(s6_pr agent/core/good docs/good.md 'An ordinary change.
' 'Stage 6 test: an ordinary change')"
  echo "GOOD $n"
  s6_info "core's ordinary pull request: $S6#$n, its merge scheduled"
  sha="$(s6_head "$n")"
  ci="$(s6_wait 600 s6_ci_done "$sha")" || { s6_fail "no Actions run finished for $sha within 10 minutes"; s6_done; return; }
  [ "$ci" = success ] && s6_pass "its Actions run succeeded" || s6_fail "its Actions run: $ci"
  if a="$(s6_wait 180 s6_approval gatekeeper "$n" "$sha")"; then
    s6_pass "the gatekeeper approved the head commit (${sha:0:12}) after its run succeeded"
    jq -e '.official and (.body | startswith("(no reply needed)"))' <<<"$a" >/dev/null \
      && s6_pass "its approval is official and needs no reply" || s6_fail "its approval: $(jq -c '{official, body: .body[:60]}' <<<"$a")"
  else
    s6_fail "the gatekeeper didn't approve ${sha:0:12} within 3 minutes"
  fi
  sleep 20
  [ "$(s6_merged "$n")" = false ] && s6_pass "with the gatekeeper's approval alone, it waits for conformance" \
    || s6_fail "it merged without conformance's approval"
  # A new commit dismisses the approval; the gatekeeper approves the new head.
  local f
  f="$(as core call 200 GET "/repos/$ORG/$S6/contents/docs/good.md?ref=agent/core/good" | jq -r .sha)"
  as core call 200 PUT "/repos/$ORG/$S6/contents/docs/good.md" "$(jq -nc --arg c "$(s6_b64 'An ordinary change, revised.
')" --arg s "$f" '{branch: "agent/core/good", content: $c, sha: $s, message: "Revise it"}')" >/dev/null
  local new
  for _ in 1 2 3 4 5 6; do new="$(s6_head "$n")"; [ "$new" != "$sha" ] && break; sleep 2; done
  # Forgejo dismisses stale approvals shortly after it processes the push.
  s6_wait 60 s6_dismissed gatekeeper "$n" "$sha" >/dev/null && s6_pass "a new commit dismissed the approval of the old head" \
    || s6_fail "the approval of ${sha:0:12} still stands after a new commit"
  s6_wait 600 s6_ci_done "$new" >/dev/null
  s6_wait 180 s6_approval gatekeeper "$n" "$new" >/dev/null \
    && s6_pass "the gatekeeper approved the new head (${new:0:12}) once its run succeeded" \
    || s6_fail "the gatekeeper didn't approve the new head ${new:0:12}"
  s6_done
}

s6_check_merged() {
  local n="$1"
  s6_wait 180 s6_is_merged "$n" >/dev/null \
    && s6_pass "with conformance's approval too, its scheduled merge went through" \
    || s6_fail "$S6#$n didn't merge within 3 minutes of conformance's approval"
  s6_done
}

# --- a status posted under CI's name doesn't get a failing build approved --------
s6_spoof() {
  local n sha ci ctx s
  n="$(s6_pr agent/core/spoof FAIL 'This file makes CI fail.
' 'Stage 6 test: a failing build')"
  s6_info "core's pull request with a failing build: $S6#$n, its merge scheduled"
  sha="$(s6_head "$n")"
  ci="$(s6_wait 600 s6_ci_done "$sha")" || { s6_fail "no Actions run finished for $sha"; s6_done; return; }
  [ "$ci" = failure ] && s6_pass "its Actions run failed" || s6_fail "its Actions run: $ci"
  ctx="$(as human call 200 GET "/repos/$ORG/$S6/commits/$sha/statuses" | jq -r '[.[].context][0] // "checks / build (pull_request)"')"
  as core call 201 POST "/repos/$ORG/$S6/statuses/$sha" "$(jq -nc --arg c "$ctx" \
    '{state: "success", context: $c, description: "Successful (posted by core, not CI)"}')" >/dev/null
  s="$(as human call 200 GET "/repos/$ORG/$S6/commits/$sha/status" | jq -r .state)"
  s6_info "core posted \"$ctx\" as success; the commit's combined status is now $s"
  # The other approval it would need, as if conformance had missed the failure.
  as conformance call 200 POST "/repos/$ORG/$S6/pulls/$n/reviews" "$(jq -nc --arg s "$sha" \
    '{event: "APPROVED", body: "(no reply needed)\n\nThe stage 6 test: an approval conformance would not give.", commit_id: $s}')" >/dev/null
  sleep 120  # four of the gatekeeper's polls
  [ -z "$(s6_approval gatekeeper "$n" "$sha")" ] && s6_pass "the gatekeeper didn't approve it" \
    || s6_fail "the gatekeeper approved a failing build"
  [ "$(s6_merged "$n")" = false ] && s6_pass "it didn't merge" || s6_fail "a failing build merged"
  s="$(as core api POST "/repos/$ORG/$S6/pulls/$n/merge" '{"Do":"squash"}')"
  [ "$s" != 200 ] && s6_pass "core can't merge it either ($s)" || s6_fail "core merged a failing build"
  s6_done
}

# --- an API.lock change waits for the human --------------------------------------
s6_apilock() {
  local n sha
  n="$(s6_pr agent/core/api API.lock 'theorem one : 1 = 1
theorem two : 2 = 2
' 'Stage 6 test: an API change')"
  echo "API $n"
  s6_info "core's pull request changing API.lock: $S6#$n, its merge scheduled"
  sleep 3
  as human call 200 GET "/repos/$ORG/$S6/pulls/$n" | jq -e --arg h "$HUMAN_USER" '[.requested_reviewers[]?.login] | index($h)' >/dev/null \
    && s6_pass "$HUMAN_USER's review is requested" || s6_fail "$HUMAN_USER's review isn't requested"
  sha="$(s6_head "$n")"
  s6_wait 600 s6_ci_done "$sha" >/dev/null
  s6_wait 180 s6_approval gatekeeper "$n" "$sha" >/dev/null \
    && s6_pass "the gatekeeper approved it" || s6_fail "the gatekeeper didn't approve it"
  s6_done
}

s6_check_apilock() {
  local n="$1" s
  sleep 60
  [ "$(s6_merged "$n")" = false ] && s6_pass "with the gatekeeper's and conformance's approvals, it still waits for $HUMAN_USER" \
    || { s6_fail "an API.lock change merged without $HUMAN_USER"; s6_done; return; }
  s="$(as core api POST "/repos/$ORG/$S6/pulls/$n/merge" '{"Do":"squash"}')"
  [ "$s" != 200 ] && s6_pass "core can't merge it ($s)" || s6_fail "core merged it without $HUMAN_USER"
  as human call 200 POST "/repos/$ORG/$S6/pulls/$n/reviews" \
    '{"event":"APPROVED","body":"(no reply needed)\n\nApproved (stage 6 test)."}' >/dev/null
  s6_wait 180 s6_is_merged "$n" >/dev/null \
    && s6_pass "once $HUMAN_USER approved, its scheduled merge went through" \
    || s6_fail "it didn't merge within 3 minutes of $HUMAN_USER's approval"
  s6_done
}

# --- while paused, the gatekeeper approves nothing --------------------------------
s6_paused_pr() {
  local n sha
  n="$(as core call 201 POST "/repos/$ORG/$S6/contents/docs/paused.md" "$(jq -nc --arg c "$(s6_b64 'While paused.
')" '{branch: "main", new_branch: "agent/core/paused", content: $c, message: "While paused"}')" >/dev/null
    as core call 201 POST "/repos/$ORG/$S6/pulls" '{"head":"agent/core/paused","base":"main","title":"Stage 6 test: while paused","body":"(no reply needed)"}' | jq -r .number)"
  echo "PAUSED $n"
  sha="$(s6_head "$n")"
  s6_wait 600 s6_ci_done "$sha" >/dev/null
  sleep 90
  [ -z "$(s6_approval gatekeeper "$n" "$sha")" ] && s6_pass "while paused, the gatekeeper didn't approve $S6#$n, whose run succeeded" \
    || s6_fail "the gatekeeper approved while paused"
  s6_done
}

s6_resumed() {
  local n="$1"
  s6_wait 180 s6_approval gatekeeper "$n" "$(s6_head "$n")" >/dev/null \
    && s6_pass "after resuming, it approved $S6#$n" || s6_fail "after resuming, it didn't approve $S6#$n within 3 minutes"
  s6_done
}

s6_cleanup() {
  as human api DELETE "/repos/$ORG/$S6" >/dev/null
  s6_info "deleted $S6"
}

cmd_test_stage6() {
  local phase="${1:-}"
  shift || true
  case "$phase" in
    scratch) s6_scratch ;;
    good) s6_good ;;
    check-merged) s6_check_merged "$@" ;;
    spoof) s6_spoof ;;
    apilock) s6_apilock ;;
    check-apilock) s6_check_apilock "$@" ;;
    paused-pr) s6_paused_pr ;;
    resumed) s6_resumed "$@" ;;
    cleanup) s6_cleanup ;;
    *) die "usage: test-stage6 scratch|good|check-merged N|spoof|apilock|check-apilock N|paused-pr|resumed N|cleanup" ;;
  esac
}
