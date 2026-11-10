# kit milestone NAME: open the milestone issue on inkproof-coordination that
# ends a milestone: it records each library's main, carries every API.lock
# change since the last milestone issue, and is labelled milestone-review,
# which starts the architecture, process and Lasm reviews.
# kit milestone-diff: just the API.lock changes since the last milestone.

# The refs the last milestone issue recorded, as JSON {repo: sha}, or {}.
last_milestone_refs() {
  call 200 GET "/repos/$ORG/$COORDINATION/issues?state=all&type=issues&labels=milestone&limit=50" \
    | jq -r '[.[] | .body | capture("<!-- milestone-refs: (?<r>\\{.*?\\}) -->").r][0] // "{}"'
}

library_repos() {
  local r
  for r in $(existing_repos); do
    [ "$r" = "$COORDINATION" ] || echo "$r"
  done
}

# api_lock REPO REF: API.lock's text at REF, or nothing.
api_lock() {
  [ -n "$2" ] || return 0
  [ "$(api GET "/repos/$ORG/$1/contents/API.lock?ref=$2")" = 200 ] || return 0
  jq -r '.content // ""' "$RESP" | base64 -d
}

# Prints the diff in Markdown, and leaves the current refs in MILESTONE_REFS.
milestone_diff() {
  local prev r old new d
  prev="$(last_milestone_refs)"
  MILESTONE_REFS='{}'
  for r in $(library_repos); do
    new="$(call 200 GET "/repos/$ORG/$r/branches/main" | jq -r .commit.id)"
    MILESTONE_REFS="$(jq -c --arg r "$r" --arg s "$new" '. + {($r): $s}' <<<"$MILESTONE_REFS")"
    old="$(jq -r --arg r "$r" '.[$r] // ""' <<<"$prev")"
    d="$(diff -u --label "API.lock at ${old:0:12}" --label "API.lock at ${new:0:12}" \
          <(api_lock "$r" "$old") <(api_lock "$r" "$new") || true)"
    echo "### $r"
    echo
    if [ -z "$old" ]; then
      echo "No earlier milestone recorded $r; this is all of \`API.lock\` at \`${new:0:12}\`."
      echo
    fi
    if [ -z "$d" ]; then
      echo "No change to \`API.lock\` since \`${old:0:12}\`."
    else
      echo '```diff'
      printf '%s\n' "$d"
      echo '```'
    fi
    echo
  done
}

cmd_milestone_diff() {
  TOKEN="$(token_of human)"
  milestone_diff
}

cmd_milestone() {
  local name="${1:-}" diff body n
  [ -n "$name" ] || die "usage: milestone <name>"
  TOKEN="$(token_of human)"
  diff="$(milestone_diff)"
  milestone_diff >/dev/null   # again, in this shell, for MILESTONE_REFS
  body="$(cat <<EOF
(no reply needed)

The end of milestone **$name**. The architecture, process and Lasm reviews report here, each with its recommendations as issues labelled \`recommendation\`. The milestone is accepted once you've decided on them.

## Every API.lock change since the last milestone

$diff

<!-- milestone-refs: $MILESTONE_REFS -->
EOF
)"
  n="$(call 201 POST "/repos/$ORG/$COORDINATION/issues" "$(jq -nc --arg t "Milestone: $name" --arg b "$body" \
    --argjson l "$(label_ids "$COORDINATION" milestone,milestone-review)" '{title: $t, body: $b, labels: $l}')" | jq -r .html_url)"
  log "Opened $n; the reviews start shortly."
}
