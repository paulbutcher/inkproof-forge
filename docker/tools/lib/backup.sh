# kit snapshot: what a backup must preserve, as one JSON object of
# key -> value, for scripts/backup.sh to record and scripts/restore-test.sh to
# compare against a restore:
#   repo:<repo>                     true
#   ref:<repo>:heads/<branch>       commit
#   ref:<repo>:tags/<tag>           commit
#   item:<repo>#<n>                 {title, state, pr, comments}: issues and
#                                   pull requests, with their comment counts

# pages PATH: every item of a paged list.
pages() {
  local path="$1" sep="?" page=1 n
  case "$path" in *\?*) sep="&" ;; esac
  while :; do
    call 200 GET "$path${sep}limit=50&page=$page" > "$RESP.page"
    jq -c '.[]' "$RESP.page"
    n="$(jq length "$RESP.page")"
    [ "$n" -lt 50 ] && break
    page=$((page + 1))
  done
}

cmd_snapshot() {
  TOKEN="$(token_of human)"
  local r
  {
    for r in $(pages "/orgs/$ORG/repos" | jq -r .name); do
      jq -nc --arg r "$r" '{("repo:" + $r): true}'
      pages "/repos/$ORG/$r/branches" | jq -c --arg r "$r" '{("ref:" + $r + ":heads/" + .name): .commit.id}'
      pages "/repos/$ORG/$r/tags" | jq -c --arg r "$r" '{("ref:" + $r + ":tags/" + .name): .commit.sha}'
      pages "/repos/$ORG/$r/issues?state=all&type=all" | jq -c --arg r "$r" \
        '{("item:" + $r + "#" + (.number | tostring)): {title, state, pr: (.pull_request != null), comments}}'
    done
  } | jq -s 'add // {}'
}

# kit snapshot-compare BEFORE AFTER RESTORED: whether RESTORED (a snapshot of
# a restored backup) has everything that stood still while the backup was
# made (BEFORE and AFTER, the live snapshots on either side of it). Prints a
# summary, and each difference.
cmd_snapshot_compare() {
  jq -n -r --slurpfile b "$1" --slurpfile a "$2" --slurpfile x "$3" '
    ($b[0]) as $b | ($a[0]) as $a | ($x[0]) as $x
    | [$b | to_entries[] | select($a[.key] == .value)] as $stable
    | [$stable[] | select($x[.key] != .value) | "\(.key): live \(.value | tojson), restored \($x[.key] | tojson)"] as $bad
    | def n(p): [$stable[] | select(.key | startswith(p))] | length;
      "Compared \(n("repo:")) repositories, \(n("ref:")) refs, \(n("item:")) issues and pull requests with \([$stable[] | select(.key | startswith("item:")) | .value.comments] | add // 0) comments; \(($b | length) - ($stable | length)) item(s) changed while the backup was made and were skipped.",
      (if ($bad | length) == 0 then "MATCH" else ("MISMATCH \($bad | length)", $bad[:20][]) end)'
}
