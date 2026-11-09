# kit pause, kit resume: the human's brake.
#   pause   writes control/paused (the dispatcher grants no claims, and a
#           managed hook stops every run at its next tool call), cancels
#           every scheduled merge, recording it in control/paused-merges.json,
#           and narrows main's merge list in every repository to the human;
#   resume  restores the merge lists, reschedules the recorded merges in the
#           human's name, then removes control/paused.
# While paused, nothing changes in Forgejo except by the human.
#
# kit project-status: open requests, pull requests, recommendations and
# needs-human items across the project's repositories, for scripts/status.sh.

PAUSED_MERGES=/control/paused-merges.json

existing_repos() {
  local r
  for r in $(all_repos); do
    [ "$(api GET "/repos/$ORG/$r")" = 200 ] && echo "$r"
  done
}

# Whether pull request REPO#N has a merge scheduled, and by whom: the latest
# schedule or cancellation in its timeline decides.
scheduled_by() {
  call 200 GET "/repos/$ORG/$1/issues/$2/timeline?limit=200" \
    | jq -r '[.[] | select(.type == "pull_scheduled_merge" or .type == "pull_cancel_scheduled_merge")]
             | last | if . != null and .type == "pull_scheduled_merge" then .user.login else empty end'
}

set_merge_list() {
  local repo="$1" narrow="$2" body
  [ "$(api GET "/repos/$ORG/$repo/branch_protections/main")" = 200 ] || return 0
  if [ "$narrow" = true ]; then
    body="$(jq -nc --arg h "$HUMAN_USER" '{enable_merge_whitelist: true, merge_whitelist_usernames: [$h]}')"
  else
    body='{"enable_merge_whitelist": false, "merge_whitelist_usernames": []}'
  fi
  call 200 PATCH "/repos/$ORG/$repo/branch_protections/main" "$body" >/dev/null
}

cmd_pause() {
  TOKEN="$(token_of human)"
  : > /control/paused
  log "Paused: no run starts, and runs under way stop at their next step."
  local recorded='[]' r n by
  [ -s "$PAUSED_MERGES" ] && recorded="$(cat "$PAUSED_MERGES")"
  for r in $(existing_repos); do
    for n in $(call 200 GET "/repos/$ORG/$r/pulls?state=open&limit=50" | jq -r '.[].number'); do
      by="$(scheduled_by "$r" "$n")"
      [ -n "$by" ] || continue
      call 204,200 DELETE "/repos/$ORG/$r/pulls/$n/merge" >/dev/null
      recorded="$(jq -c --arg r "$r" --argjson n "$n" --arg by "$by" \
        '[.[] | select(.repo != $r or .number != $n)] + [{repo: $r, number: $n, scheduled_by: $by}]' <<<"$recorded")"
      log "  cancelled the scheduled merge of $r#$n (scheduled by $by)"
    done
    set_merge_list "$r" true
    log "  only $HUMAN_USER may merge into $r's main"
  done
  ( umask 077; printf '%s\n' "$recorded" > "$PAUSED_MERGES" )
  log "Recorded $(jq length <<<"$recorded") scheduled merge(s) in control/paused-merges.json for scripts/resume.sh."
}

cmd_resume() {
  TOKEN="$(token_of human)"
  local r repo n s
  for r in $(existing_repos); do
    set_merge_list "$r" false
  done
  log "Restored every repository's merge list."
  if [ -s "$PAUSED_MERGES" ]; then
    while read -r repo n; do
      [ -n "$repo" ] || continue
      if [ "$(call 200 GET "/repos/$ORG/$repo/pulls/$n" | jq -r .state)" != open ]; then
        log "  $repo#$n is no longer open; not rescheduling it"
        continue
      fi
      s="$(api POST "/repos/$ORG/$repo/pulls/$n/merge" '{"Do":"squash","merge_when_checks_succeed":true,"delete_branch_after_merge":true}')"
      case "$s" in
        2*) log "  rescheduled the merge of $repo#$n, in $HUMAN_USER's name" ;;
        *) log "  couldn't reschedule $repo#$n ($s): $(jq -r '.message // empty' "$RESP" 2>/dev/null)" ;;
      esac
    done < <(jq -r '.[] | "\(.repo) \(.number)"' "$PAUSED_MERGES")
    rm -f "$PAUSED_MERGES"
  fi
  rm -f /control/paused
  log "Resumed: agents start work again."
}

cmd_project_status() {
  TOKEN="$(token_of human)"
  local r
  echo "Open requests:"
  for r in $(existing_repos); do
    call 200 GET "/repos/$ORG/$r/issues?state=open&type=issues&labels=request&limit=50" \
      | jq -r --arg r "$r" '.[] | "  \($r)#\(.number) \(.title) [\([.labels[].name] | join(", "))] by \(.user.login)"'
  done
  echo
  echo "Open pull requests:"
  for r in $(existing_repos); do
    call 200 GET "/repos/$ORG/$r/pulls?state=open&limit=50" \
      | jq -r --arg r "$r" '.[] | "  \($r)#\(.number) \(.title) by \(.user.login)"
          + (if (.requested_reviewers // []) | length > 0 then ", waiting for review by \([.requested_reviewers[].login] | join(", "))" else "" end)'
  done
  echo
  echo "Needs you (needs-human):"
  for r in $(existing_repos); do
    call 200 GET "/repos/$ORG/$r/issues?state=open&type=all&labels=needs-human&limit=50" \
      | jq -r --arg r "$r" '.[] | "  \($r)#\(.number) \(.title)"'
  done
  echo
  echo "Recommendations:"
  call 200 GET "/repos/$ORG/$COORDINATION/issues?state=open&type=issues&labels=recommendation&limit=50" 2>/dev/null \
    | jq -r --arg r "$COORDINATION" '.[] | "  \($r)#\(.number) \(.title)"' || true
}

# kit dispatcher METHOD PATH [JSON]: the dispatcher's admin requests.
cmd_dispatcher() {
  curl -sS -X "$1" -H "Authorization: Bearer $(tr -d '[:space:]' < /secrets/dispatcher/admin-key)" \
    -H 'Content-Type: application/json' ${3:+--data-binary "$3"} "http://dispatcher:8080$2"
}
