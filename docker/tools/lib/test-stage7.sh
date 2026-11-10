# kit test-stage7 PHASE ...: the stage 7 checks that run against Forgejo's
# API. scripts/test-stage7.sh drives them, with the steps that act in
# containers (the coordinator's and conformance's forge commands). Phases
# print PASS/FAIL/INFO lines, and KEY value lines.
#
# They use a scratch repository at level 3, stage7-scratch, with stage 6's
# quick CI. Its name isn't inkproof-, so the dispatcher starts no runs for it.

S7_FAILS=0
s7_pass() { echo "PASS: $*"; }
s7_fail() { echo "FAIL: $*"; S7_FAILS=$((S7_FAILS + 1)); }
s7_info() { echo "INFO: $*"; }
s7_done() { echo "RESULT: $S7_FAILS failure(s)"; [ "$S7_FAILS" = 0 ]; }

S7=stage7-scratch

s7_head() { as human call 200 GET "/repos/$ORG/$S7/pulls/$1" | jq -r .head.sha; }
s7_merged() { as human call 200 GET "/repos/$ORG/$S7/pulls/$1" | jq -r .merged; }
s7_is_merged() { [ "$(s7_merged "$1")" = true ] && echo merged; }
s7_main() { as human call 200 GET "/repos/$ORG/$S7/branches/main" | jq -r .commit.id; }
s7_ci() { as human call 200 GET "/repos/$ORG/$S7/actions/runs?head_sha=$1" | jq -r --arg s "$1" '[.workflow_runs[]? | select(.commit_sha == $s) | .status][0] // ""'; }
s7_ci_done() { case "$(s7_ci "$1")" in success|failure) s7_ci "$1" ;; esac; }
s7_approval() {
  as human call 200 GET "/repos/$ORG/$S7/pulls/$2/reviews" | jq -c --arg w "$1" --arg s "$3" \
    '[.[] | select(.user.login == $w and .state == "APPROVED" and .commit_id == $s and (.dismissed | not) and (.stale | not))][0] // empty'
}
s7_dismissed() { [ -z "$(s7_approval "$@")" ] && echo dismissed; }

# s7_file_op WHO BRANCH FILE CONTENT MESSAGE: create or update FILE, on BRANCH
# made from main (or on BRANCH itself if it exists).
s7_file_op() {
  local who="$1" branch="$2" file="$3" content="$4" msg="$5" base=main from have method=POST ok=201
  [ "$(as human api GET "/repos/$ORG/$S7/branches/$branch")" = 200 ] && base="$branch"
  have="$(as "$who" call 200,404 GET "/repos/$ORG/$S7/contents/$file?ref=$base" | jq -r '.sha // ""')"
  [ -n "$have" ] && method=PUT ok=200
  from='{branch: $base} + (if $base == $b then {} else {new_branch: $b} end)'
  as "$who" call "$ok" "$method" "/repos/$ORG/$S7/contents/$file" "$(jq -nc --arg b "$branch" --arg base "$base" \
    --arg c "$(s6_b64 "$content")" --arg m "$msg" --arg s "$have" \
    "$from"' + {content: $c, message: $m} + (if $s == "" then {} else {sha: $s} end)')" >/dev/null
}

# s7_land BRANCH FILE CONTENT MESSAGE: the human changes main through a pull
# request, merged as an admin, and waits for main's CI.
s7_land() {
  local branch="$1" n sha
  as human api DELETE "/repos/$ORG/$S7/branches/$branch" >/dev/null
  s7_file_op human "$@"
  n="$(as human call 201 POST "/repos/$ORG/$S7/pulls" "$(jq -nc --arg b "$branch" --arg t "$4" \
    '{head: $b, base: "main", title: $t, body: "(no reply needed)"}')" | jq -r .number)"
  local i s
  for i in 1 2 3 4 5 6 7 8 9 10; do
    s="$(as human api POST "/repos/$ORG/$S7/pulls/$n/merge" '{"Do":"squash","force_merge":true,"delete_branch_after_merge":true}')"
    [ "$s" = 200 ] && break
    sleep 3
  done
  [ "$s" = 200 ] || { s7_fail "couldn't merge the human's change \"$4\" ($s)"; return 1; }
  sha="$(s7_main)"
  [ "$(s6_wait 600 s7_ci_done "$sha")" = success ] || { s7_fail "main's CI didn't pass at ${sha:0:12}"; return 1; }
}

# --- the scratch repository at level 3 ------------------------------------------
s7_scratch() {
  TOKEN="$(token_of human)"
  api DELETE "/repos/$ORG/$S7" >/dev/null
  call 201 POST "/orgs/$ORG/repos" "$(jq -nc --arg n "$S7" '{name: $n, private: true, auto_init: true, default_branch: "main"}')" >/dev/null
  ensure_repo_settings "$S7" >/dev/null 2>&1
  local ops
  ops="$(jq -nc --arg w "$(s6_b64 "$S6_WORKFLOW")" --arg o "$(s6_b64 "$(codeowners 3)
")" --arg a "$(s6_b64 'theorem one : 1 = 1
')" --arg c "$(s6_b64 '# Changelog

## [Unreleased]

## [0.1.0]

- First release.
')" '[{operation: "create", path: ".forgejo/workflows/checks.yml", content: $w},
      {operation: "create", path: ".forgejo/CODEOWNERS", content: $o},
      {operation: "create", path: "API.lock", content: $a},
      {operation: "create", path: "CHANGELOG.md", content: $c}]')"
  call 201 POST "/repos/$ORG/$S7/contents" "$(jq -nc --argjson f "$ops" '{branch: "main", files: $f, message: "Level 3, a quick CI, an API.lock and a changelog"}')" >/dev/null
  local who
  for who in core conformance coordinator; do
    call 204 PUT "/repos/$ORG/$S7/collaborators/$who" '{"permission":"write"}' >/dev/null
  done
  ensure_labels "$S7" >/dev/null 2>&1
  ensure_branch_protection "$S7" 3 >/dev/null 2>&1
  ensure_tag_protection "$S7" 3 >/dev/null 2>&1
  call 201 POST "/repos/$ORG/$S7/tags" "$(jq -nc --arg s "$(s7_main)" '{tag_name: "v0.1.0", target: $s, message: "v0.1.0"}')" >/dev/null
  s7_info "made $S7 at level 3, tagged v0.1.0 by $HUMAN_USER; code owners: $(codeowners 3 | grep -v '^#' | head -2 | tr '\n' ';')"
  call 200 GET "/repos/$ORG/$S7/tag_protections" | jq -e --arg h "$HUMAN_USER" \
    '[.[] | select(.name_pattern == "*")][0].whitelist_usernames | sort == ([$h, "gatekeeper"] | sort)' >/dev/null \
    && s7_pass "only $HUMAN_USER and the gatekeeper may create tags" || s7_fail "tag protection isn't level 3's"
  local s
  s="$(as core api POST "/repos/$ORG/$S7/tags" "$(jq -nc --arg s "$(s7_main)" '{tag_name: "v9.9.9", target: $s}')")"
  [ "$s" != 201 ] && s7_pass "core can't create a tag ($s)" || s7_fail "core created a tag"
  s="$(as core api POST "/repos/$ORG/$S7/releases" '{"tag_name":"v9.9.8","name":"v9.9.8"}')"
  [ "$s" != 201 ] && s7_pass "core can't publish a release ($s)" || s7_fail "core published a release"
  s6_wait 600 s7_ci_done "$(s7_main)" >/dev/null || s7_fail "main's first CI run didn't finish"
  s7_done
}

# --- an API.lock change needs the coordinator ----------------------------------------
s7_api_pr() {
  local n sha
  s7_file_op core agent/core/api API.lock 'theorem one : 1 = 1
theorem two : 2 = 2
' 'Add theorem two'
  n="$(as core call 201 POST "/repos/$ORG/$S7/pulls" '{"head":"agent/core/api","base":"main","title":"Stage 7 test: an API change","body":"(no reply needed)\n\nThe kit'"'"'s stage 7 test."}' | jq -r .number)"
  as core call 200,201 POST "/repos/$ORG/$S7/pulls/$n/merge" '{"Do":"squash","merge_when_checks_succeed":true,"delete_branch_after_merge":true}' >/dev/null
  echo "API $n"
  sleep 3
  local req
  req="$(as human call 200 GET "/repos/$ORG/$S7/pulls/$n" | jq -r '[.requested_reviewers[]?.login] | sort | join(" ")')"
  [ "$req" = "conformance coordinator gatekeeper" ] && s7_pass "the coordinator's review is requested, and not $HUMAN_USER's ($req)" \
    || s7_fail "requested reviewers: $req"
  sha="$(s7_head "$n")"
  s6_wait 600 s7_ci_done "$sha" >/dev/null
  s6_wait 180 s7_approval gatekeeper "$n" "$sha" >/dev/null && s7_pass "the gatekeeper approved it" || s7_fail "the gatekeeper didn't approve it"
  s7_done
}

s7_check_waits() {
  local n="$1"
  sleep 30
  [ "$(s7_merged "$n")" = false ] && s7_pass "with the gatekeeper's and conformance's approvals, it waits for the coordinator" \
    || s7_fail "an API.lock change merged without the coordinator"
  s7_done
}

s7_push() {
  local n="$1" old new
  old="$(s7_head "$n")"
  [ -n "$(s7_approval coordinator "$n" "$old")" ] && s7_pass "the coordinator approved the head" || s7_fail "no approval from the coordinator"
  s7_file_op core agent/core/api API.lock 'theorem one : 1 = 1
theorem two : 2 = 2
theorem three : 3 = 3
' 'Add theorem three'
  s6_wait 60 s7_dismissed coordinator "$n" "$old" >/dev/null && s7_pass "a new commit dismissed the coordinator's approval" \
    || s7_fail "the coordinator's approval of ${old:0:12} survived a new commit"
  new="$(s7_head "$n")"
  s6_wait 600 s7_ci_done "$new" >/dev/null
  sleep 20
  [ "$(s7_merged "$n")" = false ] && s7_pass "without the coordinator's approval of the new head, it doesn't merge" \
    || s7_fail "it merged on a dismissed approval"
  s6_wait 180 s7_approval gatekeeper "$n" "$new" >/dev/null || s7_fail "the gatekeeper didn't approve the new head"
  s7_done
}

s7_check_merged() {
  s6_wait 180 s7_is_merged "$1" >/dev/null && s7_pass "once the coordinator approved the new head, its scheduled merge went through" \
    || s7_fail "it didn't merge within 3 minutes of the coordinator's approval"
  [ "$(s6_wait 600 s7_ci_done "$(s7_main)")" = success ] || s7_fail "main's CI didn't pass after the merge"
  s7_done
}

# --- releases -------------------------------------------------------------------------
s7_issue() {
  local n
  n="$(as core call 201 POST "/repos/$ORG/$S7/issues" "$(jq -nc --argjson l "$(label_ids "$S7" request,ready-for-release)" \
    '{title: "Stage 7 test: theorems two and three", body: "(no reply needed)\n\nThe kit'"'"'s stage 7 test.", labels: $l}')" | jq -r .number)"
  echo "ISSUE $n"
}

s7_changelog() {
  # s7_changelog VERSION SENTENCE: the human puts an entry on main.
  s7_land "kit/changelog-$1" CHANGELOG.md "# Changelog

## [Unreleased]

## [$1]

- $2

## [0.1.0]

- First release.
" "Changelog for $1"
}

s7_check_release() {
  local tag="$1" issue="$2" by="$3" rel
  rel="$(as human call 200,404 GET "/repos/$ORG/$S7/releases/tags/$tag")"
  if jq -e '.tag_name' <<<"$rel" >/dev/null 2>&1; then
    s7_pass "$tag is released by $(jq -r .author.login <<<"$rel")"
    [ "$(jq -r .author.login <<<"$rel")" = "$by" ] || s7_fail "expected $by to publish it"
    [ "$(as human call 200 GET "/repos/$ORG/$S7/tags/$tag" | jq -r .commit.sha)" = "$(s7_main)" ] \
      && s7_pass "$tag tags main's head" || s7_fail "$tag isn't main's head"
  else
    s7_fail "no release $tag"
  fi
  if [ -n "$issue" ]; then
    as human call 200 GET "/repos/$ORG/$S7/issues/$issue/comments" | jq -e --arg t "$tag" 'any(.[]; .user.login == "gatekeeper" and (.body | contains("Released in " + $t)) and (.body | startswith("(no reply needed)")))' >/dev/null \
      && s7_pass "the gatekeeper commented $tag on #$issue, needing no reply" || s7_fail "no release comment on #$issue"
    as human call 200 GET "/repos/$ORG/$S7/issues/$issue" | jq -e '[.labels[].name] | index("ready-for-release") | not' >/dev/null \
      && s7_pass "#$issue is no longer labelled ready-for-release" || s7_fail "#$issue is still labelled ready-for-release"
  fi
  s7_done
}

s7_docs() { s7_land kit/docs docs/readme.md 'Documentation only.
' 'Documentation only'; }

s7_human_tag() {
  as human call 201 POST "/repos/$ORG/$S7/tags" "$(jq -nc --arg s "$(s7_main)" '{tag_name: "v1.0.0", target: $s, message: "v1.0.0"}')" >/dev/null
  s7_info "$HUMAN_USER tagged main v1.0.0"
}

s7_api_remove() {
  s7_land kit/remove API.lock 'theorem one : 1 = 1
theorem three : 3 = 3
' 'Remove theorem two'
}

s7_cleanup() {
  as human api DELETE "/repos/$ORG/$S7" >/dev/null
  s7_info "deleted $S7"
}

cmd_test_stage7() {
  local phase="${1:-}"
  TOKEN="$(token_of human)"
  shift || true
  case "$phase" in
    scratch) s7_scratch ;;
    api-pr) s7_api_pr ;;
    check-waits) s7_check_waits "$@" ;;
    push) s7_push "$@" ;;
    check-merged) s7_check_merged "$@" ;;
    issue) s7_issue ;;
    changelog) s7_changelog "$@"; s7_done ;;
    check-release) s7_check_release "$@" ;;
    docs) s7_docs; s7_done ;;
    human-tag) s7_human_tag ;;
    api-remove) s7_api_remove; s7_done ;;
    cleanup) s7_cleanup ;;
    *) die "usage: test-stage7 scratch|api-pr|check-waits N|push N|check-merged N|issue|changelog V S|check-release TAG [ISSUE] BY|docs|human-tag|api-remove|cleanup" ;;
  esac
}
