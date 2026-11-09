# kit test-stage4 PHASE ...: the stage 4 checks that run against Forgejo's
# API and the dispatcher. scripts/test-stage4.sh drives them, with the steps
# that act on containers. Phases print PASS/FAIL/INFO lines, and KEY value
# lines for the host script.

S4_FAILS=0
s4_pass() { echo "PASS: $*"; }
s4_fail() { echo "FAIL: $*"; S4_FAILS=$((S4_FAILS + 1)); }
s4_info() { echo "INFO: $*"; }
s4_done() { echo "RESULT: $S4_FAILS failure(s)"; [ "$S4_FAILS" = 0 ]; }

S4_LIB=inkproof-core
S4_DEP=inkproof-scale
S4_STRANGER=stage4-stranger

dstatus() { curl -fsS http://dispatcher:8080/status; }
pending() { dstatus | jq -r --arg a "$1" '.agents[$a].pending[].trigger'; }
as() { local who="$1"; shift; TOKEN="$(token_of "$who")" "$@"; }

# A signed webhook, as Forgejo would send it: for events a test can't cause
# without changing a repository's main or tags.
fake_hook() {
  local event="$1" body="$2" sig
  sig="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$(tr -d '[:space:]' < /secrets/dispatcher/webhook-secret)" | awk '{ print $NF }')"
  curl -fsS -o /dev/null -X POST -H "X-Forgejo-Event: $event" -H "X-Forgejo-Signature: $sig" \
    -H "X-Forgejo-Delivery: stage4-$(date +%s%N)" -H 'Content-Type: application/json' \
    --data-binary "$body" http://dispatcher:8080/hook
}
repo_json() { jq -nc --arg r "$1" --arg o "$ORG" '{name: $r, full_name: "\($o)/\($r)", owner: {login: $o}}'; }

drop_all() {
  local a
  for a in $(all_agents); do cmd_dispatcher POST /admin/drop "{\"agent\":\"$a\"}" >/dev/null; done
}
all_agents() { grep -v '^[[:space:]]*\(#\|$\)' /etc/kit/agents.conf | awk '{ print $1 }'; }

new_issue() {  # new_issue AS REPO TITLE LABELS BODY -> number
  local ids
  ids="$(TOKEN="$(token_of human)" label_ids "$2" "$4")"
  as "$1" call 201 POST "/repos/$ORG/$2/issues" "$(jq -nc --arg t "$3" --arg b "$5" --argjson l "$ids" \
    '{title: $t, body: $b, labels: $l}')" | jq -r .number
}
say() { as "$1" call 201 POST "/repos/$ORG/$2/issues/$3/comments" "$(jq -nc --arg b "$4" '{body: $b}')" >/dev/null; }

# --- setup: a clean queue, an untrusted account, and core's pull request ----
s4_setup() {
  TOKEN="$(token_of human)"
  drop_all
  if [ "$(api GET "/users/$S4_STRANGER")" != 200 ]; then
    call 201 POST /admin/users "$(jq -nc --arg u "$S4_STRANGER" --arg p "$(random_hex 24)" \
      '{username: $u, email: "\($u)@noreply.localhost", password: $p, must_change_password: false}')" >/dev/null
  fi
  call 204 PUT "/repos/$ORG/$S4_DEP/collaborators/$S4_STRANGER" '{"permission":"write"}' >/dev/null
  local pw tok
  pw="$(random_hex 24)"
  call 200 PATCH "/admin/users/$S4_STRANGER" "$(jq -nc --arg u "$S4_STRANGER" --arg p "$pw" \
    '{login_name: $u, source_id: 0, password: $p, must_change_password: false}')" >/dev/null
  BASIC="$S4_STRANGER:$pw" api DELETE "/users/$S4_STRANGER/tokens/stage4" >/dev/null
  tok="$(BASIC="$S4_STRANGER:$pw" call 201 POST "/users/$S4_STRANGER/tokens" \
    '{"name":"stage4","scopes":["write:issue","read:repository","read:user"]}' | jq -r .sha1)"
  mkdir -p "/secrets/$S4_STRANGER"; ( umask 077; echo "$tok" > "/secrets/$S4_STRANGER/token" )
  s4_info "created $S4_STRANGER, a Forgejo account the kit doesn't trust, with write access to $S4_DEP"

  # core's pull request, branched from main's parent so it is behind main.
  local branch="agent/core/stage4-test" parent n
  api DELETE "/repos/$ORG/$S4_LIB/branches/$branch" >/dev/null
  parent="$(call 200 GET "/repos/$ORG/$S4_LIB/git/commits/main" | jq -r '.parents[0].sha')"
  as core call 201 POST "/repos/$ORG/$S4_LIB/branches" "$(jq -nc --arg b "$branch" --arg p "$parent" \
    '{new_branch_name: $b, old_ref_name: $p}')" >/dev/null
  as core call 201 POST "/repos/$ORG/$S4_LIB/contents/docs/stage4-test.md" "$(jq -nc --arg b "$branch" \
    --arg c "$(printf 'The kit'"'"'s stage 4 test.\n' | base64 -w0)" '{branch: $b, content: $c, message: "Stage 4 test"}')" >/dev/null
  n="$(as core call 201 POST "/repos/$ORG/$S4_LIB/pulls" "$(jq -nc --arg b "$branch" \
    '{head: $b, base: "main", title: "Stage 4 test: a pull request by core", body: "(no reply needed)\n\nThe kit'"'"'s stage 4 test."}')" | jq -r .number)"
  as core call 200,201 POST "/repos/$ORG/$S4_LIB/pulls/$n/merge" \
    '{"Do":"squash","merge_when_checks_succeed":true,"delete_branch_after_merge":true}' >/dev/null
  [ "$(scheduled_by "$S4_LIB" "$n")" = core ] && s4_pass "core scheduled the merge of $S4_LIB#$n" \
    || s4_fail "core couldn't schedule the merge of $S4_LIB#$n"
  echo "PR $n"
  s4_done
}

# --- pause: the scheduled merge cancelled and recorded; merges narrowed -----
s4_check_paused() {
  TOKEN="$(token_of human)"
  local n="$1"
  [ -e /control/paused ] && s4_pass "control/paused exists" || s4_fail "control/paused is missing"
  [ -z "$(scheduled_by "$S4_LIB" "$n")" ] && s4_pass "pause cancelled the scheduled merge of $S4_LIB#$n" \
    || s4_fail "$S4_LIB#$n still has a scheduled merge"
  jq -e --arg r "$S4_LIB" --argjson n "$n" 'any(.[]; .repo == $r and .number == $n and .scheduled_by == "core")' \
    "$PAUSED_MERGES" >/dev/null && s4_pass "pause recorded it for resume" || s4_fail "pause didn't record $S4_LIB#$n"
  call 200 GET "/repos/$ORG/$S4_LIB/branch_protections/main" >/dev/null
  jq -e --arg h "$HUMAN_USER" '.enable_merge_whitelist and .merge_whitelist_usernames == [$h]' "$RESP" >/dev/null \
    && s4_pass "only $HUMAN_USER may merge into $S4_LIB's main" || s4_fail "$S4_LIB's merge list wasn't narrowed"
  dstatus | jq -e '.paused and ([.agents[].state] | all(. == "paused" or . == "running"))' >/dev/null \
    && s4_pass "the dispatcher reports every agent paused" || s4_fail "the dispatcher doesn't report the pause"
  s4_done
}

# --- events, while paused: each queues for the right agent, or for none -----
s4_events() {
  local pr="$1" req up blocked
  req="$(new_issue human "$S4_DEP" "Stage 4 test: a request" request,new "Building against: main

The kit's stage 4 test: a request from $HUMAN_USER.")"
  echo "REQ $req"
  say consumer-metrics "$S4_DEP" "$req" "A question from consumer-metrics: does this need a reply? (stage 4 test)"
  say consumer-metrics "$S4_DEP" "$req" "(no reply needed)

consumer-metrics acknowledges (stage 4 test)."
  say scale "$S4_DEP" "$req" "scale's own comment (stage 4 test)."
  say "$S4_STRANGER" "$S4_DEP" "$req" "An untrusted comment (stage 4 test)."
  new_issue "$S4_STRANGER" "$S4_DEP" "Stage 4 test: an untrusted request" request,new "From an untrusted account." >/dev/null
  # needs-human on a core issue: a notice for the human, work for the coordinator.
  local nh
  nh="$(new_issue core "$S4_LIB" "Stage 4 test: needs the human" needs-human "(no reply needed)

The kit's stage 4 test.")"
  echo "NH $nh"
  # A review request for the coordinator, and a review of core's pull request.
  as human call 201 POST "/repos/$ORG/$S4_LIB/pulls/$pr/requested_reviewers" '{"reviewers":["coordinator"]}' >/dev/null
  as human call 200 POST "/repos/$ORG/$S4_LIB/pulls/$pr/reviews" \
    '{"event":"COMMENT","body":"A review comment from the human (stage 4 test)."}' >/dev/null
  # Blocked work: core waits on an upstream issue, which the human then comments on.
  up="$(new_issue core "$COORDINATION" "Stage 4 test: an upstream issue" upstream "(no reply needed)

The kit's stage 4 test.")"
  blocked="$(new_issue core "$S4_LIB" "Stage 4 test: blocked work" blocked "(no reply needed)

Blocked-by: $ORG/$COORDINATION#$up")"
  echo "UP $up"
  echo "BLOCKED $blocked"
  say human "$COORDINATION" "$up" "The human comments on the upstream issue (stage 4 test)."
  # Events a test can't cause without changing main or publishing a release.
  fake_hook release "$(jq -nc --argjson r "$(repo_json "$S4_DEP")" --arg h "$HUMAN_USER" --arg t "$S4_TAG" \
    '{action: "published", release: {tag_name: $t}, repository: $r, sender: {login: $h}}')"
  fake_hook action_run_failure "$(jq -nc --argjson r "$(repo_json "$S4_LIB")" --argjson n "$pr" \
    --arg p "$(jq -nc --argjson n "$pr" '{pull_request: {number: $n, user: {login: "core"}}}')" \
    '{action: "failure", run: {event: "pull_request", commit_sha: "stage4fake000", event_payload: $p},
      repository: $r, sender: {login: "ci"}}')"
  fake_hook push "$(jq -nc --argjson r "$(repo_json "$S4_LIB")" --arg h "$HUMAN_USER" \
    '{ref: "refs/heads/main", after: "", repository: $r, sender: {login: $h}}')"
  fake_hook issues "$(jq -nc --argjson r "$(repo_json "$S4_DEP")" \
    '{action: "opened", issue: {number: 999, state: "open", user: {login: "Ghost"}, labels: [{name: "request"}, {name: "new"}]},
      repository: $r, sender: {login: "Ghost"}}')"
  sleep 5
  s4_info "made the events"
}

expect() {  # expect AGENT PATTERN WHAT
  local p; p="$(pending "$1")"   # captured: grep -q would end the pipe early
  if grep -qE -- "$2" <<<"$p"; then s4_pass "$1 has work: $3"; else s4_fail "$1 has no work for: $3"; fi
}
expect_not() {  # expect_not AGENT PATTERN WHAT
  local p; p="$(pending "$1")"
  if grep -qE -- "$2" <<<"$p"; then s4_fail "$1 has work it shouldn't: $3"; else s4_pass "$1 has no work for: $3"; fi
}

s4_check_triggers() {
  local pr="$1" req="$2" nh="$3" up="$4" blocked="$5" n
  expect scale "^untriaged request $S4_DEP#$req$" "a request from the human"
  expect coordinator "^request filed: $S4_DEP#$req$" "a request filed"
  n="$(pending scale | grep -c "^comment on issue $S4_DEP#$req$" || true)"
  [ "$n" = 1 ] && s4_pass "scale has one comment trigger, consumer-metrics's question: its own, the no-reply and the untrusted comments start nothing" \
    || s4_fail "scale has $n comment trigger(s) on #$req; expected 1 (consumer-metrics's question only)"
  expect_not scale "untrusted|$S4_DEP#999" "an untrusted or migrated request"
  [ "$(pending scale | grep -c "^untriaged request")" = 1 ] && s4_pass "the untrusted account's request started nothing" \
    || s4_fail "scale has work from an untrusted request"
  expect coordinator "^needs-human added to issue $S4_LIB#$nh$" "needs-human added"
  expect coordinator "^pull request opened: $S4_LIB#$pr$" "a pull request opened"
  expect coordinator "^review requested from you on pull request $S4_LIB#$pr$" "a review requested from it"
  expect core "^review on pull request $S4_LIB#$pr$" "a review of its pull request"
  expect core "^CI failed on pull request $S4_LIB#$pr$" "a failed Actions run on its pull request"
  expect core "^pull request $S4_LIB#$pr is behind main$" "its pull request behind main"
  expect core "^$TEST_HUMAN_RE commented on upstream issue $COORDINATION#$up; $S4_LIB#$blocked is blocked on it$" \
    "the human's comment on the upstream issue it's blocked on"
  expect coordinator "^$S4_DEP released $S4_TAG$" "a release"
  expect_not core "^untriaged request|^request filed" "events on other repositories"
  local notices
  notices="$(dstatus | jq -r --arg since "$S4_SINCE" '[.notices[] | select(.at >= $since) | .kind] | join(" ")')"
  [ "$notices" = needs-human ] && s4_pass "the only notice is the needs-human one (nothing routine is a notice)" \
    || s4_fail "notices since the test began: ${notices:-none}"
  s4_done
}

# --- reconciliation: a request filed while the dispatcher was down ----------
s4_missed_request() {
  new_issue human inkproof-consumer-metrics "Stage 4 test: a request while the dispatcher was down" request,new \
    "The kit's stage 4 test."
}
s4_check_reconciled() {
  expect consumer-metrics "^untriaged request inkproof-consumer-metrics#$1$" "a request whose webhook was missed, found by reconciliation"
  s4_done
}

# --- resume: lists restored, merge rescheduled in the human's name -----------
s4_check_resumed() {
  TOKEN="$(token_of human)"
  local n="$1"
  [ ! -e /control/paused ] && s4_pass "control/paused is gone" || s4_fail "control/paused is still there"
  call 200 GET "/repos/$ORG/$S4_LIB/branch_protections/main" >/dev/null
  jq -e '.enable_merge_whitelist == false' "$RESP" >/dev/null && s4_pass "$S4_LIB's merge list is restored" \
    || s4_fail "$S4_LIB's merge list is still narrowed"
  [ "$(scheduled_by "$S4_LIB" "$n")" = "$HUMAN_USER" ] && s4_pass "resume rescheduled $S4_LIB#$n in $HUMAN_USER's name" \
    || s4_fail "$S4_LIB#$n isn't rescheduled (by: $(scheduled_by "$S4_LIB" "$n"))"
  s4_done
}

# --- caps and bursts ---------------------------------------------------------
s4_burst() {  # three quick comments from consumer-metrics on scale's request: one run
  local req="$1" i
  for i in 1 2 3; do say consumer-metrics "$S4_DEP" "$req" "Burst comment $i from consumer-metrics (stage 4 test): please reply briefly, once, to all of these."; done
}
s4_poke() {  # more work for scale after the burst: a fresh request, so it surely queues
  new_issue human "$S4_DEP" "Stage 4 test: a request after the cap" request,new "The kit's stage 4 test." >/dev/null
}

s4_cleanup() {
  TOKEN="$(token_of human)"
  local repo n
  for repo in $(existing_repos); do
    for n in $(call 200 GET "/repos/$ORG/$repo/issues?state=open&type=all&limit=50" \
                | jq -r '.[] | select(.title | startswith("Stage 4 test")) | .number'); do
      api PATCH "/repos/$ORG/$repo/issues/$n" '{"state":"closed"}' >/dev/null
    done
  done
  api DELETE "/repos/$ORG/$S4_LIB/branches/agent/core/stage4-test" >/dev/null
  api DELETE "/admin/users/$S4_STRANGER?purge=true" >/dev/null
  rm -rf "/secrets/$S4_STRANGER"
  drop_all
  s4_info "closed the test's issues and pull requests, removed $S4_STRANGER, and dropped queued work"
}

cmd_test_stage4() {
  local phase="${1:-}"
  shift || true
  S4_SINCE="${S4_SINCE:-1970}"
  TEST_HUMAN_RE="$HUMAN_USER"
  S4_TAG="v0.0.0-stage4-$(printf %s "$S4_SINCE" | tr -dc 0-9)"
  case "$phase" in
    setup) s4_setup ;;
    check-paused) s4_check_paused "$@" ;;
    events) s4_events "$@" ;;
    check-triggers) s4_check_triggers "$@" ;;
    missed-request) s4_missed_request ;;
    check-reconciled) s4_check_reconciled "$@" ;;
    check-resumed) s4_check_resumed "$@" ;;
    burst) s4_burst "$@" ;;
    poke) s4_poke "$@" ;;
    drop) drop_all ;;
    cleanup) s4_cleanup ;;
    *) die "test-stage4: unknown phase $phase" ;;
  esac
}
