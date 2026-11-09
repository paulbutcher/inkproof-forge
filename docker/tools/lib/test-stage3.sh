# kit test-stage3 PHASE ...: the stage 3 checks that run against Forgejo's API.
# scripts/test-stage3.sh drives them, with the steps that act inside the
# agent's container. Each phase prints PASS/FAIL/INFO lines, plus KEY value
# lines for the host script.

S3_FAILS=0
s3_pass() { echo "PASS: $*"; }
s3_fail() { echo "FAIL: $*"; S3_FAILS=$((S3_FAILS + 1)); }
s3_info() { echo "INFO: $*"; }
s3_done() { echo "RESULT: $S3_FAILS failure(s)"; [ "$S3_FAILS" = 0 ]; }

S3_REPO=inkproof-core

label_ids() {
  call 200 GET "/repos/$ORG/$1/labels?limit=100" \
    | jq -c --arg want "$2" '[.[] | select(.name as $n | $want | split(",") | index($n)) | .id]'
}

# A trusted request on core's repository, small enough for one short run.
s3_request() {
  TOKEN="$(token_of human)"
  local body n
  body="$(cat <<'EOF'
Building against: main

Priority: low

## Use case

This is the kit's stage 3 test (inkproof-forge). It checks that a request starts a run, and that the run opens a pull request and schedules its merge.

## Current limitation

The repository has no `docs/kit-test.md`.

## Acceptance criteria

A pull request adds `docs/kit-test.md` containing the single line `Written by the core agent for the kit's stage 3 test.`, and nothing else. Nothing else in the repository changes, and no `CHANGELOG.md` entry is needed.

## Impact if not done

The kit's stage 3 test fails. Once the pull request is open with its merge scheduled, this run's work is done: stop rather than taking other tasks.
EOF
)"
  n="$(call 201 POST "/repos/$ORG/$S3_REPO/issues" "$(jq -nc --arg b "$body" --argjson l "$(label_ids "$S3_REPO" request,new)" \
    '{title: "Stage 3 test: add docs/kit-test.md", body: $b, labels: $l}')" | jq -r .number)"
  s3_info "opened request $S3_REPO#$n as $HUMAN_USER"
  echo "ISSUE $n"
}

# The run's outcome in Forgejo: a pull request by core, its merge scheduled,
# waiting for the human.
s3_check_pr() {
  TOKEN="$(token_of human)"
  local issue="$1" since="$2" pr prs n timeline merged
  prs="$(call 200 GET "/repos/$ORG/$S3_REPO/pulls?state=all&limit=50" \
    | jq -c --arg since "$since" '[.[] | select(.user.login == "core" and .created_at >= $since)]')"
  n="$(jq -r '.[0].number // empty' <<<"$prs")"
  if [ -z "$n" ]; then
    s3_fail "core opened no pull request on $S3_REPO since $since"
    s3_done; return
  fi
  echo "PR $n"
  pr="$(jq -c '.[0]' <<<"$prs")"
  s3_pass "core opened $S3_REPO#$n, as core, from $(jq -r .head.ref <<<"$pr")"
  [ "$(api GET "/repos/$ORG/$S3_REPO/issues/$issue")" = 200 ] \
    && ! jq -e '[.labels[].name] | index("new")' "$RESP" >/dev/null \
    && s3_pass "core triaged request #$issue (removed new; labels: $(jq -r '[.labels[].name] | join(", ")' "$RESP"))" \
    || s3_fail "request #$issue is still untriaged"
  timeline="$(call 200 GET "/repos/$ORG/$S3_REPO/issues/$n/timeline?limit=100")"
  if jq -e '[.[] | select(.type == "pull_scheduled_merge" and .user.login == "core")] | length > 0' <<<"$timeline" >/dev/null; then
    s3_pass "core scheduled $S3_REPO#$n to merge when its checks succeed"
  else
    s3_fail "no scheduled merge on $S3_REPO#$n (timeline types: $(jq -r '[.[].type] | unique | join(", ")' <<<"$timeline"))"
  fi
  merged="$(call 200 GET "/repos/$ORG/$S3_REPO/pulls/$n" | jq -r .merged)"
  [ "$merged" = false ] && s3_pass "$S3_REPO#$n hasn't merged without the human's approval" \
    || s3_fail "$S3_REPO#$n merged without the human's approval"
  jq -e --arg h "$HUMAN_USER" '[.requested_reviewers[]?.login] | index($h)' "$RESP" >/dev/null \
    && s3_pass "$S3_REPO#$n requests $HUMAN_USER's review (code owner)" \
    || s3_fail "$S3_REPO#$n doesn't request $HUMAN_USER's review (requested: $(jq -r '[.requested_reviewers[]?.login] | join(", ")' "$RESP"))"
  s3_done
}

# A trusted comment on the request, which needs a reply: work for core.
s3_comment() {
  TOKEN="$(token_of human)"
  call 201 POST "/repos/$ORG/$S3_REPO/issues/$1/comments" \
    '{"body":"Thanks. Before this closes, please run `lake build` in your repository and reply here with the last line of its output."}' >/dev/null
  s3_info "commented on $S3_REPO#$1 as $HUMAN_USER"
}

# A pull request by core that changes CLAUDE.md waits for the human.
s3_claude_md() {
  TOKEN="$(token_of core)"
  local branch="agent/core/stage3-claude-md-test" sha content n
  api DELETE "/repos/$ORG/$S3_REPO/branches/$branch" >/dev/null
  sha="$(call 200 GET "/repos/$ORG/$S3_REPO/contents/CLAUDE.md?ref=main" | jq -r .sha)"
  content="$( { jq -r .content "$RESP" | base64 -d; printf '\n<!-- stage 3 test -->\n'; } | base64 | tr -d '\n')"
  call 200,201 PUT "/repos/$ORG/$S3_REPO/contents/CLAUDE.md" "$(jq -nc --arg c "$content" --arg s "$sha" --arg b "$branch" \
    '{content: $c, sha: $s, branch: "main", new_branch: $b, message: "Stage 3 test: change CLAUDE.md"}')" >/dev/null
  n="$(call 201 POST "/repos/$ORG/$S3_REPO/pulls" "$(jq -nc --arg b "$branch" \
    '{head: $b, base: "main", title: "Stage 3 test: a CLAUDE.md change", body: "(no reply needed)\n\nThe kit'"'"'s stage 3 test."}')" | jq -r .number)"
  sleep 3
  call 200 GET "/repos/$ORG/$S3_REPO/pulls/$n" >/dev/null
  jq -e --arg h "$HUMAN_USER" '[.requested_reviewers[]?.login] | index($h)' "$RESP" >/dev/null \
    && s3_pass "a CLAUDE.md change by core requests $HUMAN_USER's review" \
    || s3_fail "a CLAUDE.md change by core doesn't request $HUMAN_USER's review"
  local s
  s="$(api POST "/repos/$ORG/$S3_REPO/pulls/$n/merge" '{"Do":"squash"}')"
  [ "$s" != 200 ] && s3_pass "core can't merge it itself ($s)" || s3_fail "core merged its own CLAUDE.md change"
  TOKEN="$(token_of human)"
  [ "${KEEP:-}" = 1 ] || { api PATCH "/repos/$ORG/$S3_REPO/pulls/$n" '{"state":"closed"}' >/dev/null; api DELETE "/repos/$ORG/$S3_REPO/branches/$branch" >/dev/null; }
  s3_done
}

# An upstream issue marked blocking reaches the human, as a mention.
s3_blocking() {
  TOKEN="$(token_of core)"
  local n i found=""
  n="$(call 201 POST "/repos/$ORG/$COORDINATION/issues" "$(jq -nc --argjson l "$(TOKEN="$(token_of human)" label_ids "$COORDINATION" upstream,blocking)" \
    '{title: "Stage 3 test: an upstream blocker", body: "(no reply needed)\n\nThe kit'"'"'s stage 3 test.", labels: $l}')" | jq -r .number)"
  for i in $(seq 1 15); do
    sleep 2
    found="$(call 200 GET "/repos/$ORG/$COORDINATION/issues/$n/comments" \
      | jq -r --arg h "@$HUMAN_USER" '[.[] | select(.user.login == "dispatcher" and (.body | contains($h)))] | length')"
    [ "$found" -gt 0 ] && break
  done
  [ "$found" -gt 0 ] && s3_pass "an upstream issue marked blocking (#$n) brought a dispatcher comment mentioning $HUMAN_USER" \
    || s3_fail "no dispatcher comment mentioning $HUMAN_USER on blocking upstream issue #$n"
  TOKEN="$(token_of human)"
  [ "${KEEP:-}" = 1 ] || api PATCH "/repos/$ORG/$COORDINATION/issues/$n" '{"state":"closed"}' >/dev/null
  s3_done
}

s3_cleanup() {
  TOKEN="$(token_of human)"
  local issue="$1" pr="${2:-}" branch
  api PATCH "/repos/$ORG/$S3_REPO/issues/$issue" '{"state":"closed"}' >/dev/null
  if [ -n "$pr" ]; then
    branch="$(call 200 GET "/repos/$ORG/$S3_REPO/pulls/$pr" | jq -r .head.ref)"
    api PATCH "/repos/$ORG/$S3_REPO/pulls/$pr" '{"state":"closed"}' >/dev/null
    api DELETE "/repos/$ORG/$S3_REPO/branches/$branch" >/dev/null
  fi
  s3_info "closed $S3_REPO#$issue${pr:+ and #$pr}"
}

cmd_test_stage3() {
  local phase="${1:-}"
  shift || true
  case "$phase" in
    request) s3_request ;;
    check-pr) s3_check_pr "$@" ;;
    comment) s3_comment "$@" ;;
    claude-md) s3_claude_md ;;
    blocking) s3_blocking ;;
    cleanup) s3_cleanup "$@" ;;
    *) die "test-stage3: unknown phase $phase" ;;
  esac
}
