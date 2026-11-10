# kit setup-repo REPO LEVEL: apply an autonomy level's Forgejo settings to one
# of the project's repositories, then check the files the level depends on
# and, if they differ, open a pull request (as the human) that makes them
# right, for the human to review and merge. Safe to re-run: a later run
# updates that pull request rather than opening another. A lower level falls
# back. Exits 3 while the files still need that pull request merged.
#
# Settings it applies:
#   - labels the protocol uses;
#   - squash merges only, branches deleted after merging, Actions on;
#   - main's branch protection: no pushes, one approval from the level's
#     approvals list, blocked by official review requests, rejected reviews
#     and an outdated branch, stale approvals dismissed, `checks*` required;
#   - protected tags: every tag, by the human only (and the gatekeeper at
#     level 3).
# Files it checks on main (they are code-owned by the human, so they change
# only through the human's pull request): .forgejo/workflows/checks.yml,
# .forgejo/CODEOWNERS, .forgejo/issue_template/request.md and
# scripts/check-pins.sh, against templates/repo/; and that nothing is left in
# .github/ from GitHub.

# conformance reviews every pull request: it is on every approvals list and a
# code owner of every path. CONFORMANCE_REVIEW=false leaves it out.
CONFORMANCE_REVIEW="${CONFORMANCE_REVIEW:-true}"

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
  ["milestone-review", "1d76db", "Start the end-of-milestone reviews"],
  ["milestone", "1d76db", "A milestone issue (scripts/milestone.sh)"],
  ["upstream:lasm", "bfdadc", "An upstream issue in Lasm"],
  ["recommendation", "5319e7", "A recommendation from a review, for the human to decide"],
  ["review:architecture", "c5def5", "From the architecture review"],
  ["review:process", "c5def5", "From the process review"],
  ["review:lasm", "c5def5", "From the Lasm review"],
  ["declined", "cccccc", "A recommendation the human rejected; not raised again without new evidence"]
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

# has_ci REPO: whether the repository is a Lean package with CI. The
# coordination repository holds documents only.
has_ci() {
  [ "$1" != "$COORDINATION" ]
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
  # The coordination repository has no CI: nothing to require.
  if ! has_ci "$repo"; then
    body="$(jq -c '.enable_status_check = false | .status_check_contexts = []' <<<"$body")"
  fi
  # While paused, only the human may merge (scripts/pause.sh); keep it so.
  if [ -e /control/paused ]; then
    body="$(jq -c --arg h "$HUMAN_USER" '.enable_merge_whitelist = true | .merge_whitelist_usernames = [$h]' <<<"$body")"
  fi
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
# expressions matched against whole paths; every matching line adds owners,
# and a line starting with ! adds them for every path it doesn't match.
codeowners() {
  local level="$1" repo="${2:-}" catchall api conf=""
  case "$level" in
    1) catchall="@$HUMAN_USER"; api="@$HUMAN_USER" ;;
    2) catchall="@gatekeeper"; api="@$HUMAN_USER" ;;
    3) catchall="@gatekeeper"; api="@coordinator" ;;
  esac
  $CONFORMANCE_REVIEW && conf=" @conformance"
  if [ "$repo" = "$COORDINATION" ]; then
    # The coordinator's NEEDS-HUMAN.md merges on conformance's approval
    # alone; everything else, ROADMAP.md and REQUIREMENTS.md included, is
    # the human's. (It stays at level 1: it has no CI.)
    cat <<EOF2
# Code owners for inkproof-coordination (scripts/setup-repo.sh checks this file).
# Each line is a Go regular expression matched against a whole path, without
# a leading slash; every matching line adds its owners, and a line starting
# with ! adds them for every path it doesn't match. Each owner's review
# request blocks the merge until they approve.
.*$conf
!NEEDS-HUMAN\.md @$HUMAN_USER
EOF2
    return
  fi
  cat <<EOF2
# Code owners for autonomy level $level (scripts/setup-repo.sh checks this file).
# Each line is a Go regular expression matched against a whole path, without
# a leading slash; every matching line adds its owners, and each owner's
# review request blocks the merge until they approve.
.* $catchall$conf
API\.lock $api
\.forgejo/.* @$HUMAN_USER
lean-toolchain @$HUMAN_USER
CLAUDE\.md @$HUMAN_USER
REQUIREMENTS\.md @$HUMAN_USER
EOF2
}

# file_ops REPO REF LEVEL: the file operations, as a JSON array for Forgejo's
# multi-file commit, that make REF's files right for LEVEL.
file_ops() {
  local repo="$1" ref="$2" level="$3" ops='[]' f want have op commit
  local files=".forgejo/issue_template/request.md .forgejo/CODEOWNERS"
  has_ci "$repo" && files=".forgejo/workflows/checks.yml scripts/check-pins.sh $files"
  for f in $files; do
    if [ "$f" = .forgejo/CODEOWNERS ]; then want="$(codeowners "$level" "$repo")"; else want="$(cat "/etc/kit/templates/repo/$f")"; fi
    if [ "$(api GET "/repos/$ORG/$repo/contents/$f?ref=$ref")" = 200 ]; then
      have="$(jq -r '.content // ""' "$RESP" | base64 -d)"
      FILE_SHA="$(jq -r .sha "$RESP")"
      [ "$have" = "$want" ] && continue
      op=update
    else
      op=create FILE_SHA=""
    fi
    ops="$(jq -c --arg o "$op" --arg p "$f" --arg s "$FILE_SHA" --arg c "$(printf '%s\n' "$want" | base64 -w0)" \
      '. + [{operation: $o, path: $p, content: $c} + (if $s == "" then {} else {sha: $s} end)]' <<<"$ops")"
  done
  # Everything left in .github/: Forgejo ignores its CODEOWNERS, and would run
  # its workflow if .forgejo/workflows/ were missing.
  commit="$(call 200 GET "/repos/$ORG/$repo/branches/$ref" | jq -r .commit.id)"
  call 200 GET "/repos/$ORG/$repo/git/trees/$commit?recursive=true&per_page=10000" \
    | jq -c --argjson ops "$ops" '$ops + [.tree[] | select(.type == "blob" and (.path | startswith(".github/")))
                                           | {operation: "delete", path: .path, sha: .sha}]'
}

# The human's pull request that makes the files right: opened if there's none,
# brought up to date if one from an earlier run is still open.
check_files() {
  local repo="$1" level="$2" branch="kit/setup-level-$2" ops pr n url body
  ops="$(file_ops "$repo" main "$level")"
  if [ "$(jq length <<<"$ops")" = 0 ]; then
    log "$repo: its files are right for level $level."
    return 0
  fi
  log "$repo's files need changing for level $level:"
  jq -r '.[] | "  \(.operation) \(.path)"' <<<"$ops" >&2
  pr="$(call 200 GET "/repos/$ORG/$repo/pulls?state=open&limit=50" \
    | jq -c --arg b "$branch" '[.[] | select(.head.ref == $b)][0] // empty')"
  if [ -n "$pr" ]; then
    n="$(jq -r .number <<<"$pr")"
    url="$(jq -r .html_url <<<"$pr")"
    ops="$(file_ops "$repo" "$branch" "$level")"
    if [ "$(jq length <<<"$ops")" != 0 ]; then
      call 201 POST "/repos/$ORG/$repo/contents" "$(jq -nc --argjson f "$ops" --arg b "$branch" \
        '{branch: $b, files: $f, message: "Bring the files up to date (scripts/setup-repo.sh)"}')" >/dev/null
      changed "$repo: updated pull request #$n"
    fi
  else
    # A branch left from a pull request that was closed without merging.
    api DELETE "/repos/$ORG/$repo/branches/$branch" >/dev/null
    call 201 POST "/repos/$ORG/$repo/contents" "$(jq -nc --argjson f "$ops" --arg b "$branch" --arg l "$level" \
      '{branch: "main", new_branch: $b, files: $f,
        message: "Set up for autonomy level \($l) (scripts/setup-repo.sh)"}')" >/dev/null
    body="$(printf '%s\n' \
      "(no reply needed)" "" \
      "Made by \`scripts/setup-repo.sh $repo $level\`: the files autonomy level $level needs, from the kit's \`templates/repo/\`." "" \
      "$(jq -r '.[] | "- \(.operation) `\(.path)`"' <<<"$ops")" "" \
      "These files are code-owned by you, so the kit proposes them rather than committing them. Review and merge; as an admin you can merge without an approval. Re-running the script updates this pull request.")"
    pr="$(call 201 POST "/repos/$ORG/$repo/pulls" "$(jq -nc --arg b "$branch" --arg l "$level" --arg body "$body" \
      '{head: $b, base: "main", title: "Set up for autonomy level \($l)", body: $body}')")"
    n="$(jq -r .number <<<"$pr")"
    url="$(jq -r .html_url <<<"$pr")"
    changed "$repo: opened pull request #$n"
  fi
  log ""
  log "Review and merge it, then run this again: $url"
  return 3
}

# A setup pull request for another level would set that level if merged: close it.
close_other_levels() {
  local repo="$1" level="$2" n b
  call 200 GET "/repos/$ORG/$repo/pulls?state=open&limit=50" \
    | jq -r --arg b "kit/setup-level-$level" '.[] | select((.head.ref | startswith("kit/setup-level-")) and .head.ref != $b) | "\(.number) \(.head.ref)"' \
    | while read -r n b; do
        call 201 POST "/repos/$ORG/$repo/issues/$n/comments" "$(jq -nc --arg l "$level" '{body: "(no reply needed)\n\nClosed: `scripts/setup-repo.sh` now sets this repository up for level \($l)."}')" >/dev/null
        call 201 PATCH "/repos/$ORG/$repo/pulls/$n" '{"state":"closed"}' >/dev/null
        api DELETE "/repos/$ORG/$repo/branches/$b" >/dev/null
        changed "$repo: closed #$n, the setup pull request for level ${b#kit/setup-level-}"
      done
}

cmd_setup_repo() {
  local repo="${1:-}" level="${2:-}"
  TOKEN="$(token_of human)"
  [ -n "$repo" ] && [ -n "$level" ] || die "usage: setup-repo <repository> <level 1-3>"
  case "$level" in
    1|2|3) ;;
    *) die "the level is 1, 2 or 3" ;;
  esac
  # From level 2 the gatekeeper approves on CI, which the coordination
  # repository doesn't have: its pull requests could never merge.
  if [ "$level" != 1 ] && ! has_ci "$repo"; then
    die "$repo has no CI, so it stays at level 1: from level 2 the gatekeeper approves only on CI"
  fi
  [ "$(api GET "/repos/$ORG/$repo")" = 200 ] || die "$ORG/$repo isn't in Forgejo"
  log "Setting up $ORG/$repo for autonomy level $level:"
  ensure_labels "$repo"
  ensure_repo_settings "$repo"
  ensure_branch_protection "$repo" "$level"
  ensure_tag_protection "$repo" "$level"
  log "Made $CHANGES change(s) to Forgejo's settings."
  if [ "$level" = 3 ] && [ "$(api GET "/repos/$ORG/$repo/contents/API.lock?ref=main")" != 200 ]; then
    log "Warning: $repo has no API.lock on main, so no release can be computed for it until it has one."
  fi
  close_other_levels "$repo" "$level"
  check_files "$repo" "$level"
}
