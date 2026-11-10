# kit test-stage5 PHASE ...: the stage 5 checks that run against Forgejo's
# API. scripts/test-stage5.sh drives them, with the steps that act in
# containers. Phases print PASS/FAIL/INFO lines, and KEY value lines.

S5_FAILS=0
s5_pass() { echo "PASS: $*"; }
s5_fail() { echo "FAIL: $*"; S5_FAILS=$((S5_FAILS + 1)); }
s5_info() { echo "INFO: $*"; }
s5_done() { echo "RESULT: $S5_FAILS failure(s)"; [ "$S5_FAILS" = 0 ]; }

S5_SCRATCH=stage5-scratch   # not inkproof-: a project repository would wake the coordinator
S5_LIB=inkproof-core

b64() { printf '%s' "$1" | base64 -w0; }
# pr_as WHO REPO BRANCH FILE CONTENT TITLE -> number; branch from main.
pr_as() {
  local who="$1" repo="$2" branch="$3" file="$4" content="$5" title="$6"
  TOKEN="$(token_of human)" api DELETE "/repos/$ORG/$repo/branches/$branch" >/dev/null
  TOKEN="$(token_of "$who")" call 201 POST "/repos/$ORG/$repo/contents/$file" "$(jq -nc --arg b "$branch" --arg c "$(b64 "$content")" \
    --arg m "$title" '{branch: "main", new_branch: $b, content: $c, message: $m}')" >/dev/null
  TOKEN="$(token_of "$who")" call 201 POST "/repos/$ORG/$repo/pulls" "$(jq -nc --arg b "$branch" --arg t "$title" \
    '{head: $b, base: "main", title: $t, body: "(no reply needed)\n\nThe kit'"'"'s stage 5 test."}')" | jq -r .number
}
requested() { call 200 GET "/repos/$ORG/$1/pulls/$2" | jq -r '[.requested_reviewers[]?.login] | sort | join(" ")'; }
merge_as() { TOKEN="$(token_of "$1")" api POST "/repos/$ORG/$2/pulls/$3/merge" '{"Do":"squash"}'; }

# --- a scratch repository with two code owners -------------------------------
s5_scratch() {
  TOKEN="$(token_of human)"
  api DELETE "/repos/$ORG/$S5_SCRATCH" >/dev/null
  call 201 POST "/orgs/$ORG/repos" "$(jq -nc --arg n "$S5_SCRATCH" '{name: $n, private: true, auto_init: true, default_branch: "main"}')" >/dev/null
  call 200 PATCH "/repos/$ORG/$S5_SCRATCH" '{"has_actions": true}' >/dev/null
  call 201 POST "/repos/$ORG/$S5_SCRATCH/contents/.forgejo/CODEOWNERS" "$(jq -nc --arg c "$(b64 ".* @$HUMAN_USER @conformance
")" '{branch: "main", content: $c, message: "Two code owners"}')" >/dev/null
  local who
  for who in core conformance; do
    call 204 PUT "/repos/$ORG/$S5_SCRATCH/collaborators/$who" '{"permission":"write"}' >/dev/null
  done
  call 201 POST "/repos/$ORG/$S5_SCRATCH/labels" '{"name":"needs-human","color":"#d93f0b"}' >/dev/null
  call 201 POST "/repos/$ORG/$S5_SCRATCH/branch_protections" "$(jq -nc --arg h "$HUMAN_USER" '{
    rule_name: "main", enable_push: false, required_approvals: 1, enable_approvals_whitelist: true,
    approvals_whitelist_username: [$h, "conformance"], block_on_official_review_requests: true,
    block_on_rejected_reviews: true, dismiss_stale_approvals: true}')" >/dev/null
  s5_info "made $S5_SCRATCH: code owners $HUMAN_USER and conformance, both on the approvals list"
  echo "DISPUTE $(pr_as core "$S5_SCRATCH" agent/core/dispute docs/dispute.md 'A disputed change.
' 'Stage 5 test: a disputed finding')"
}

s5_two_owners() {
  local n
  n="$(pr_as core "$S5_SCRATCH" agent/core/owners docs/owners.md 'A change for two code owners.
' 'Stage 5 test: two code owners')"
  sleep 2
  TOKEN="$(token_of human)"
  [ "$(requested "$S5_SCRATCH" "$n")" = "conformance $HUMAN_USER" ] \
    && s5_pass "both code owners' reviews are requested" || s5_fail "requested: $(requested "$S5_SCRATCH" "$n")"
  TOKEN="$(token_of conformance)" call 200 POST "/repos/$ORG/$S5_SCRATCH/pulls/$n/reviews" '{"event":"APPROVED","body":"Approved (stage 5 test)."}' >/dev/null
  TOKEN="$(token_of human)"
  # Forgejo goes on listing a reviewer as requested after they review; an
  # official approval is what answers the request.
  call 200 GET "/repos/$ORG/$S5_SCRATCH/pulls/$n/reviews" \
    | jq -e 'any(.[]; .user.login == "conformance" and .state == "APPROVED" and .official)' >/dev/null \
    && s5_pass "conformance's approval is official: it answers its own review request" || s5_fail "conformance's approval isn't official"
  local s
  s="$(merge_as core "$S5_SCRATCH" "$n")"
  [ "$s" != 200 ] && s5_pass "with one approval but $HUMAN_USER's request pending, it doesn't merge ($s)" \
    || s5_fail "it merged with $HUMAN_USER's review request pending"
  TOKEN="$(token_of human)" call 200 POST "/repos/$ORG/$S5_SCRATCH/pulls/$n/reviews" '{"event":"APPROVED","body":"Approved (stage 5 test)."}' >/dev/null
  local i
  for i in 1 2 3 4 5 6; do  # the approval takes a moment to count
    s="$(merge_as core "$S5_SCRATCH" "$n")"
    [ "$s" = 200 ] && break
    sleep 2
  done
  [ "$s" = 200 ] && s5_pass "with both code owners' approvals, it merges" || s5_fail "with both approvals, merging returned $s"
  s5_done
}

s5_check_dispute() {
  TOKEN="$(token_of human)"
  local n="$1" asked
  asked="$(call 200 GET "/repos/$ORG/$S5_SCRATCH/pulls/$n/reviews" | jq '[.[] | select(.user.login == "conformance" and .state == "REQUEST_CHANGES")] | length')"
  [ "$asked" = 3 ] && s5_pass "conformance requested changes for 3 rounds" || s5_fail "conformance requested changes $asked time(s)"
  call 200 GET "/repos/$ORG/$S5_SCRATCH/issues/$n" | jq -e '[.labels[].name] | index("needs-human")' >/dev/null \
    && s5_pass "the 4th round labelled the pull request needs-human instead" || s5_fail "the pull request isn't labelled needs-human"
  call 200 GET "/repos/$ORG/$S5_SCRATCH/issues/$n/comments" \
    | jq -e '[.[] | select(.user.login == "conformance" and (.body | contains("\"escalated\": true")))] | length == 1' >/dev/null \
    && s5_pass "conformance recorded the findings as escalated" || s5_fail "no escalation comment with its findings record"
  s5_done
}

# --- the conformance loop on a real library ----------------------------------
s5_deviation() {
  echo "PR $(pr_as core "$S5_LIB" agent/core/stage5-markdown docs/stage5-test.md \
'# Stage 5 test

This file is part of the kit'"'"'s stage 5 test. Its paragraphs are hard-wrapped
at seventy characters, which the Markdown section of CLAUDE.md forbids,
so conformance should ask for each paragraph on a single line.
' 'Add docs/stage5-test.md')"
}

s5_check_loop() {
  TOKEN="$(token_of human)"
  local n="$1" reviews head first last
  reviews="$(call 200 GET "/repos/$ORG/$S5_LIB/pulls/$n/reviews" | jq -c '[.[] | select(.user.login == "conformance")]')"
  head="$(call 200 GET "/repos/$ORG/$S5_LIB/pulls/$n" | jq -r .head.sha)"
  first="$(jq -c '.[0] // {}' <<<"$reviews")"
  last="$(jq -c '.[-1] // {}' <<<"$reviews")"
  jq -e '.state == "REQUEST_CHANGES" and (.body | test("Markdown"))' <<<"$first" >/dev/null \
    && s5_pass "conformance requested changes, naming the Markdown section" || s5_fail "conformance's first review: $(jq -c '{state, body: (.body // "" | .[:200])}' <<<"$first")"
  [ "$(call 200 GET "/repos/$ORG/$S5_LIB/pulls/$n/commits" | jq length)" -gt 1 ] \
    && s5_pass "core pushed a fix" || s5_fail "core pushed no fix"
  # What the reviewers find beyond the planted deviation is their judgement;
  # the loop is right if the new head was reviewed and the Markdown finding
  # recorded as fixed. Approval follows when nothing else is open.
  jq -e --arg f "$(jq -r .commit_id <<<"$first")" '.commit_id != $f and (.body | capture("conformance-findings: (?<j>\\{.*\\}) -->").j | fromjson
        | any(.findings[]; (.section | test("Markdown")) and .status == "fixed"))' <<<"$last" >/dev/null \
    && s5_pass "conformance reviewed a later head ($(jq -r '.commit_id[:12]' <<<"$last")) and recorded the Markdown finding fixed" \
    || s5_fail "conformance's latest review: $(jq -c '{state, commit_id}' <<<"$last")"
  s5_info "its verdict on the new head: $(jq -r .state <<<"$last"); other findings: $(jq -r '.body | capture("conformance-findings: (?<j>\\{.*\\}) -->").j | fromjson | [.findings[] | select(.section | test("Markdown") | not) | "\(.section) (\(.status))"] | join(", ")' <<<"$last" 2>/dev/null)"
  [ "$(call 200 GET "/repos/$ORG/$S5_LIB/pulls/$n" | jq -r .merged)" = false ] \
    && s5_pass "it still waits for $HUMAN_USER at level 1" || s5_fail "it merged without $HUMAN_USER"
  s5_done
}

# --- milestone reviews -------------------------------------------------------
s5_declined() {
  TOKEN="$(token_of human)"
  local n
  n="$(call 201 POST "/repos/$ORG/$COORDINATION/issues" "$(jq -nc --argjson l "$(label_ids "$COORDINATION" recommendation,review:process,declined)" \
    '{title: "Agents should sign every comment with a From: line", labels: $l,
      body: "(no reply needed)\n\nA process recommendation, declined: authorship identifies the writer now that every agent has its own account (decided 2026-11-08)."}')" | jq -r .number)"
  call 201 POST "/repos/$ORG/$COORDINATION/issues/$n/comments" '{"body":"(no reply needed)\n\nDeclined: authorship identifies the writer."}' >/dev/null
  api PATCH "/repos/$ORG/$COORDINATION/issues/$n" '{"state":"closed"}' >/dev/null
  s5_info "a declined process recommendation (#$n) is on record"
}

s5_milestone_issue() {
  TOKEN="$(token_of human)"
  call 200 GET "/repos/$ORG/$COORDINATION/issues?state=open&type=issues&labels=milestone&limit=5" \
    | jq -r --arg t "$1" '[.[] | select(.title == "Milestone: \($t)")][0].number // empty'
}

s5_check_milestone() {
  TOKEN="$(token_of human)"
  local m="$1" since="$2" sessions="$3" r recs body
  call 200 GET "/repos/$ORG/$COORDINATION/issues/$m" | jq -e '.body | test("### inkproof-core")' >/dev/null \
    && s5_pass "the milestone issue carries the API.lock diff" || s5_fail "the milestone issue has no API.lock diff"
  local comments
  comments="$(call 200 GET "/repos/$ORG/$COORDINATION/issues/$m/comments?limit=50")"
  for r in architecture process lasm; do
    [ "$(jq --arg r "$r" '[.[] | select(.user.login == $r)] | length' <<<"$comments")" = 1 ] \
      && s5_pass "$r reported once on the milestone issue" || s5_fail "$r's reports on the milestone issue: $(jq --arg r "$r" '[.[] | select(.user.login == $r)] | length' <<<"$comments")"
  done
  recs="$(call 200 GET "/repos/$ORG/$COORDINATION/issues?state=all&type=issues&labels=recommendation&since=$since&limit=50" \
    | jq -c --arg since "$since" '[.[] | select(.created_at >= $since and (.user.login | IN("architecture", "process", "lasm")))]')"
  s5_info "recommendations: $(jq -r 'map("#\(.number) \(.user.login): \(.title)") | join("; ")' <<<"$recs")"
  jq -e 'all(.[]; ([.labels[].name] | index("recommendation") and index("needs-human") and any(.[]; startswith("review:"))))' <<<"$recs" >/dev/null \
    && s5_pass "every recommendation is its own issue, labelled recommendation, review:<kind> and needs-human" \
    || s5_fail "a recommendation is missing its labels"
  jq -e 'all(.[]; (.user.login) as $u | [.labels[].name] | index("review:\($u)"))' <<<"$recs" >/dev/null \
    && s5_pass "each recommendation is labelled with its own review" || s5_fail "a recommendation has another review's label"
  jq -e '[.[] | select(.user.login == "process" and (.title | test("From:")))] | length == 0' <<<"$recs" >/dev/null \
    && s5_pass "the declined recommendation wasn't raised again" || s5_fail "process raised the declined recommendation again"
  body="$(jq -r '[.[] | select(.user.login == "process") | .body] | join("\n")' <<<"$comments")$(jq -r '[.[] | select(.user.login == "process") | .body] | join("\n")' <<<"$recs")"
  if grep -qiE "run #?[0-9]+|$(printf '%s' "$sessions" | tr ' ' '|' | sed 's/^|//; s/|$//')" <<<"$body"; then
    s5_pass "the process review cites runs from the ledger or the archive"
  else
    s5_fail "the process review cites no run or session"
  fi
  s5_done
}

s5_lasm_issue() {
  TOKEN="$(token_of core)"
  call 201 POST "/repos/$ORG/$COORDINATION/issues" "$(jq -nc --argjson l "$(TOKEN="$(token_of human)" label_ids "$COORDINATION" upstream,upstream:lasm,blocking)" \
    '{title: "Stage 5 test: Lasm drops a test without saying why", labels: $l,
      body: "(no reply needed)\n\nThe kit'"'"'s stage 5 test: a blocking upstream issue in Lasm, made up for the test. Under Lasm one test in the test subproject reports success without running, and nothing in the output says it was skipped."}')" | jq -r .number
}

s5_check_lasm() {
  TOKEN="$(token_of human)"
  [ "$(call 200 GET "/repos/$ORG/$COORDINATION/issues/$1/comments" | jq '[.[] | select(.user.login == "lasm")] | length')" -ge 1 ] \
    && s5_pass "the Lasm review reported on the blocking Lasm issue straight away" || s5_fail "no Lasm report on #$1"
  s5_done
}

s5_cleanup() {
  TOKEN="$(token_of human)"
  local since="$1" repo n
  api DELETE "/repos/$ORG/$S5_SCRATCH" >/dev/null
  for repo in "$S5_LIB" "$COORDINATION"; do
    for n in $(call 200 GET "/repos/$ORG/$repo/issues?state=open&type=all&limit=50&since=$since" \
                | jq -r --arg since "$since" '.[] | select(.created_at >= $since) | select((.title | test("Stage 5 test|^Milestone: Stage 5 test|docs/stage5-test")) or ([.labels[].name] | index("recommendation"))) | .number'); do
      api PATCH "/repos/$ORG/$repo/issues/$n" '{"state":"closed"}' >/dev/null
    done
  done
  api DELETE "/repos/$ORG/$S5_LIB/branches/agent/core/stage5-markdown" >/dev/null
  s5_info "deleted $S5_SCRATCH and closed the test's issues, pull requests and recommendations"
}

cmd_test_stage5() {
  local phase="${1:-}"
  shift || true
  case "$phase" in
    scratch) s5_scratch ;;
    two-owners) s5_two_owners ;;
    check-dispute) s5_check_dispute "$@" ;;
    deviation) s5_deviation ;;
    check-loop) s5_check_loop "$@" ;;
    declined) s5_declined ;;
    milestone-issue) s5_milestone_issue "$@" ;;
    check-milestone) s5_check_milestone "$@" ;;
    lasm-issue) s5_lasm_issue ;;
    check-lasm) s5_check_lasm "$@" ;;
    cleanup) s5_cleanup "$@" ;;
    *) die "test-stage5: unknown phase $phase" ;;
  esac
}
