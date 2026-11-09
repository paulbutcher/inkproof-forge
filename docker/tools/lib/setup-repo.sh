# kit setup-repo REPO LEVEL: apply an autonomy level's Forgejo settings to one
# of the project's repositories, then check the files the level depends on
# and say what to change. Safe to re-run; a lower level falls back.
#
# Settings it applies:
#   - labels the protocol uses;
#   - squash merges only, branches deleted after merging, Actions on;
#   - main's branch protection: no pushes, one approval from the level's
#     approvals list, blocked by official review requests, rejected reviews
#     and an outdated branch, stale approvals dismissed, `checks*` required;
#   - protected tags: every tag, by the human only (and the gatekeeper at
#     level 3).
# Files it checks on main (they are code-owned by the human, so the kit
# never commits them): .forgejo/workflows/checks.yml, .forgejo/CODEOWNERS,
# .forgejo/issue_template/request.md and scripts/check-pins.sh, against
# templates/repo/; and that no .github/workflows/ is left from GitHub.

# The reviewers that exist so far. conformance joins at stage 5.
CONFORMANCE_REVIEW="${CONFORMANCE_REVIEW:-false}"

LABELS='[
  ["request", "0e8a16", "A capability another library or consumer needs"],
  ["new", "fbca04", "Not yet triaged by the owner"],
  ["accepted", "0e8a16", "The owner will do it"],
  ["counter-proposal", "c5def5", "The owner proposes something else"],
  ["needs-human", "d93f0b", "Waiting for the human"],
  ["blocked", "b60205", "Waiting on another issue (Blocked-by: line)"],
  ["ready-for-release", "5319e7", "Merged and in CHANGELOG.md; waiting for a release"],
  ["verified", "0e8a16", "The requester checked the release"]
]'
COORD_LABELS='[
  ["upstream", "bfdadc", "A bug or limitation in a dependency outside this project"],
  ["blocking", "b60205", "An upstream issue that stops an agent working"],
  ["milestone-review", "1d76db", "Start the end-of-milestone reviews"]
]'

ensure_labels() {
  local repo="$1" have spec name color desc
  have="$(call 200 GET "/repos/$ORG/$repo/labels?limit=100" | jq -r '.[].name')"
  spec="$LABELS"
  [ "$repo" = "$COORDINATION" ] && spec="$(jq -c -n --argjson a "$LABELS" --argjson b "$COORD_LABELS" '$a + $b')"
  while IFS=$'\t' read -r name color desc; do
    grep -qx -- "$name" <<<"$have" && continue
    call 201 POST "/repos/$ORG/$repo/labels" "$(jq -nc --arg n "$name" --arg c "#$color" --arg d "$desc" \
      '{name: $n, color: $c, description: $d}')" >/dev/null
    changed "$repo: created label $name"
  done < <(jq -r '.[] | @tsv' <<<"$spec")
}

ensure_repo_settings() {
  local repo="$1" want have
  want='{"has_actions": true, "has_wiki": false, "has_projects": false, "has_packages": false,
         "allow_squash_merge": true, "allow_merge_commits": false, "allow_rebase": false,
         "allow_rebase_explicit": false, "allow_fast_forward_only_merge": false,
         "default_merge_style": "squash", "default_delete_branch_after_merge": true}'
  have="$(call 200 GET "/repos/$ORG/$repo" | jq -c --argjson w "$want" '. as $r | $w | with_entries(.value = $r[.key])')"
  if [ "$(jq -cS . <<<"$have")" != "$(jq -cS . <<<"$want")" ]; then
    call 200 PATCH "/repos/$ORG/$repo" "$want" >/dev/null
    changed "$repo: squash merges only, branches deleted after merging, Actions on"
  fi
}

# approvals list for a level, as a JSON array
approvers() {
  local level="$1" list
  case "$level" in
    1) list="$HUMAN_USER" ;;
    2) list="$HUMAN_USER gatekeeper" ;;
    3) list="$HUMAN_USER gatekeeper coordinator" ;;
  esac
  $CONFORMANCE_REVIEW && list="$list conformance"
  printf '%s\n' $list | jq -R . | jq -sc .
}

ensure_branch_protection() {
  local repo="$1" level="$2" body have keys
  body="$(jq -nc --argjson a "$(approvers "$level")" '{
    rule_name: "main", enable_push: false, enable_merge_whitelist: false,
    required_approvals: 1, enable_approvals_whitelist: true, approvals_whitelist_username: $a,
    block_on_rejected_reviews: true, block_on_official_review_requests: true,
    block_on_outdated_branch: true, dismiss_stale_approvals: true, ignore_stale_approvals: false,
    enable_status_check: true, status_check_contexts: ["checks*"],
    require_signed_commits: false, apply_to_admins: false}')"
  keys="$(jq -c 'keys' <<<"$body")"
  if [ "$(api GET "/repos/$ORG/$repo/branch_protections/main")" = 404 ]; then
    call 201 POST "/repos/$ORG/$repo/branch_protections" "$body" >/dev/null
    changed "$repo: protected main for level $level"
    return
  fi
  have="$(jq -c --argjson k "$keys" '. as $r | reduce $k[] as $x ({}; .[$x] = $r[$x])
            | .approvals_whitelist_username |= sort' "$RESP")"
  if [ "$(jq -cS . <<<"$have")" != "$(jq -cS '.approvals_whitelist_username |= sort' <<<"$body")" ]; then
    call 200 PATCH "/repos/$ORG/$repo/branch_protections/main" "$(jq -c 'del(.rule_name)' <<<"$body")" >/dev/null
    changed "$repo: updated main's protection for level $level"
  fi
}

ensure_tag_protection() {
  local repo="$1" level="$2" who id have
  who="[\"$HUMAN_USER\"]"
  [ "$level" = 3 ] && who="[\"$HUMAN_USER\",\"gatekeeper\"]"
  have="$(call 200 GET "/repos/$ORG/$repo/tag_protections" | jq -c '[.[] | select(.name_pattern == "*")][0] // empty')"
  if [ -z "$have" ]; then
    call 201 POST "/repos/$ORG/$repo/tag_protections" \
      "$(jq -nc --argjson w "$who" '{name_pattern: "*", whitelist_usernames: $w}')" >/dev/null
    changed "$repo: only $(jq -r 'join(" and ")' <<<"$who") may create tags"
  elif [ "$(jq -c '.whitelist_usernames | sort' <<<"$have")" != "$(jq -c 'sort' <<<"$who")" ]; then
    id="$(jq -r .id <<<"$have")"
    call 200 PATCH "/repos/$ORG/$repo/tag_protections/$id" \
      "$(jq -nc --argjson w "$who" '{whitelist_usernames: $w}')" >/dev/null
    changed "$repo: only $(jq -r 'join(" and ")' <<<"$who") may create tags"
  fi
}

# The CODEOWNERS a level needs. Forgejo reads it from main, as Go regular
# expressions matched against whole paths; every matching line adds owners.
codeowners() {
  local level="$1" catchall api
  case "$level" in
    1) catchall="@$HUMAN_USER"; api="@$HUMAN_USER" ;;
    2) catchall="@gatekeeper"; api="@$HUMAN_USER" ;;
    3) catchall="@gatekeeper"; api="@coordinator" ;;
  esac
  $CONFORMANCE_REVIEW && catchall="$catchall @conformance"
  cat <<EOF
# Code owners for autonomy level $level (scripts/setup-repo.sh checks this file).
# Each line is a Go regular expression matched against a whole path, without
# a leading slash; every matching line adds its owners, and each owner's
# review request blocks the merge until they approve.
.* $catchall
API\.lock $api
\.forgejo/.* @$HUMAN_USER
lean-toolchain @$HUMAN_USER
CLAUDE\.md @$HUMAN_USER
REQUIREMENTS\.md @$HUMAN_USER
EOF
}

# file_on_main REPO PATH: its contents on main, or nothing.
file_on_main() {
  [ "$(api GET "/repos/$ORG/$1/contents/$2?ref=main")" = 200 ] || return 1
  jq -r '.content // ""' "$RESP" | base64 -d 2>/dev/null
}

check_files() {
  local repo="$1" level="$2" todo=() f want have
  for f in .forgejo/workflows/checks.yml .forgejo/issue_template/request.md scripts/check-pins.sh; do
    want="$(cat "/etc/kit/templates/repo/$f")"
    if ! have="$(file_on_main "$repo" "$f")"; then
      todo+=("add $f from templates/repo/$f")
    elif [ "$have" != "$want" ]; then
      todo+=("replace $f with templates/repo/$f")
    fi
  done
  want="$(codeowners "$level")"
  if ! have="$(file_on_main "$repo" .forgejo/CODEOWNERS)"; then
    todo+=("add .forgejo/CODEOWNERS, as below")
  elif [ "$have" != "$want" ]; then
    todo+=("replace .forgejo/CODEOWNERS with the version below")
  fi
  if [ "$(api GET "/repos/$ORG/$repo/contents/.github?ref=main")" = 200 ]; then
    todo+=("delete .github/ (GitHub's workflow, CODEOWNERS and issue template; Forgejo uses .forgejo/)")
  fi
  if [ "${#todo[@]}" = 0 ]; then
    log "$repo: its files are right for level $level."
    return 0
  fi
  log ""
  log "$repo needs a pull request from you (these files are yours, so the kit doesn't commit them):"
  local i=1 t
  for t in "${todo[@]}"; do log "  $i. $t"; i=$((i + 1)); done
  log ""
  log ".forgejo/CODEOWNERS for level $level:"
  codeowners "$level" | sed 's/^/    /' >&2
  return 3
}

cmd_setup_repo() {
  local repo="${1:-}" level="${2:-}"
  TOKEN="$(token_of human)"
  [ -n "$repo" ] && [ -n "$level" ] || die "usage: setup-repo <repository> <level 1-3>"
  case "$level" in
    1) ;;
    2|3) die "level $level arrives in a later stage of the kit" ;;
    *) die "the level is 1, 2 or 3" ;;
  esac
  [ "$(api GET "/repos/$ORG/$repo")" = 200 ] || die "$ORG/$repo isn't in Forgejo"
  log "Setting up $ORG/$repo for autonomy level $level:"
  ensure_labels "$repo"
  ensure_repo_settings "$repo"
  ensure_branch_protection "$repo" "$level"
  ensure_tag_protection "$repo" "$level"
  log "Made $CHANGES change(s) to Forgejo's settings."
  check_files "$repo" "$level"
}
