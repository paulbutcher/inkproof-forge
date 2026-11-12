# kit reviews DAYS: which reviews have taken place and their results, for
# scripts/reviews.sh.
#   Pull request reviews (conformance, the coordinator at level 3, the human)
#   on pull requests updated in the last DAYS days: who, when, the verdict,
#   and conformance's round and open findings.
#   Milestone reviews (architecture, process, Lasm): for each milestone issue,
#   whether each has reported, and what became of its recommendations.
# Times are shown at KIT_TZ_OFFSET seconds from UTC.

# The first line of a body worth showing: not "(no reply needed)", a heading
# or a comment, cut to fit a line.
REVIEW_JQ_DEFS='
  def epoch: (.[19:] | sub("^\\.[0-9]+"; "")) as $z
    | (.[0:19] + "Z" | fromdateiso8601)
      - (if $z == "Z" or $z == "" then 0
         else ($z[0:1] + "1" | tonumber) * (($z[1:3] | tonumber) * 3600 + ($z[4:6] | tonumber) * 60) end);
  def when: epoch + ($tz | tonumber) | strftime("%Y-%m-%d %H:%M");
  def gist: [(. // "") | splits("\n") | gsub("^\\s+|\\s+$"; "")
             | select(. != "" and (startswith("(no reply needed)") | not) and (startswith("#") | not)
                      and (startswith("<!--") | not))][0] // ""
    | if length > 100 then .[0:99] + "…" else . end;
'

cmd_reviews() {
  TOKEN="$(token_of human)"
  local days="${1:-7}" changes="${2:-}" tz="${KIT_TZ_OFFSET:-0}" web="${KIT_WEB_URL:-$FORGE_URL}/$ORG" since r prs n
  since=$(( $(date +%s) - days * 86400 ))

  if [ -n "$changes" ]; then
    echo "Reviews that asked for changes, on pull requests updated in the last $days day(s):"
  else
    echo "Pull request reviews, on pull requests updated in the last $days day(s):"
  fi
  for r in $(existing_repos); do
    prs="$(call 200 GET "/repos/$ORG/$r/pulls?state=all&sort=recentupdate&limit=50" \
      | jq -c --argjson s "$since" --arg tz "$tz" "$REVIEW_JQ_DEFS"'[.[] | select((.updated_at | epoch) >= $s)
                                 | {number, title, state: (if .merged then "merged" else .state end),
                                    head: .head.sha}]')"
    for n in $(jq -r '.[].number' <<<"$prs"); do
      call 200 GET "/repos/$ORG/$r/pulls/$n/reviews" | jq -r --arg r "$r" --arg web "$web" --arg tz "$tz" --arg changes "$changes" \
        --argjson pr "$(jq -c --argjson n "$n" '.[] | select(.number == $n)' <<<"$prs")" "$REVIEW_JQ_DEFS"'
        [.[] | select(.state != "REQUEST_REVIEW" and .state != "PENDING" and (.submitted_at // "") != ""
                    and ($changes == "" or .state == "REQUEST_CHANGES"))]
        | select(length > 0)
        | "  \($web)/\($r)/pulls/\($pr.number)  \($pr.title) (\($pr.state))",
          (.[] | (.body // "") as $b
            | ([$b | capture("<!-- conformance-findings: (?<j>\\{.*\\}) -->") | .j | fromjson?][0]) as $f
            | "    \(.submitted_at | when)  \(.user.login): "
              + ({"APPROVED": "approved", "REQUEST_CHANGES": "changes requested", "COMMENT": "commented"}[.state] // .state)
              + (if $f then ", round \($f.round)"
                   + ([$f.findings[] | select(.status == "new" or .status == "kept")] | length
                      | if . > 0 then ", \(.) open finding(s)" else "" end)
                   + (if $f.escalated then ", escalated to you" else "" end)
                 else "" end)
              + (if .dismissed then " [dismissed]" elif .stale or (.commit_id != "" and .commit_id != $pr.head)
                 then " [an earlier commit]" else "" end)
              + ($b | gist | if . != "" then "\n        \(.)" else "" end))'
    done
  done

  echo
  echo "Milestone reviews${changes:+ that made recommendations}:"
  local ms recs i num reviewer comments head out shown=""
  ms="$(call 200 GET "/repos/$ORG/$COORDINATION/issues?state=all&type=issues&labels=milestone-review&limit=50" \
    | jq -c 'sort_by(.created_at) | [.[] | select(any(.labels[]; .name == "milestone-review"))]')"
  recs="$(call 200 GET "/repos/$ORG/$COORDINATION/issues?state=all&type=issues&labels=recommendation&limit=50" \
    | jq -c '[.[] | select(any(.labels[]; .name == "recommendation"))]')"
  for i in $(seq 0 $(( $(jq length <<<"$ms") - 1 ))); do
    num="$(jq -r ".[$i].number" <<<"$ms")"
    head="$(jq -r --argjson i "$i" --arg r "$COORDINATION" --arg web "$web" --arg tz "$tz" "$REVIEW_JQ_DEFS"'
      .[$i] | "  \($web)/\($r)/issues/\(.number)  \(.title) (\(.state), opened \(.created_at | when))"' <<<"$ms")" out=""
    comments="$(call 200 GET "/repos/$ORG/$COORDINATION/issues/$num/comments?limit=50")"
    for reviewer in architecture process lasm; do
      out+="$(jq -nr --arg changes "$changes" --arg web "$web" --arg r "$COORDINATION" --argjson ms "$ms" --argjson i "$i" --argjson recs "$recs" --argjson c "$comments" \
        --arg who "$reviewer" --arg tz "$tz" "$REVIEW_JQ_DEFS"'
        ($ms[$i].created_at | epoch) as $from | ($ms[$i + 1].created_at // "9999-12-31T00:00:00Z" | epoch) as $to
        | [$c[] | select(.user.login == $who)][0] as $report
        | [$recs[] | select(any(.labels[]; .name == "review:\($who)")
                             and (.created_at | epoch) >= $from and (.created_at | epoch) < $to)
           | . + {status: (if .state == "open" then "waiting on you"
                           elif any(.labels[]; .name == "declined") then "declined" else "accepted" end)}] as $mine
        | select($changes == "" or ($mine | length) > 0)
        | "    \($who): "
          + (if $report == null then (if ($mine | length) > 0 then "recommended" else "not reported yet" end)
             else "reported \($report.created_at | when)" end)
          + (if ($mine | length) == 0 then (if $report then ", no recommendations" else "" end)
             else ", \($mine | length) recommendation(s): "
               + ([("accepted", "declined", "waiting on you") as $s
                   | ($mine | map(select(.status == $s)) | length) as $k | select($k > 0) | "\($k) \($s)"]
                  | join(", "))
             end),
          ($mine[] | "        \($web)/\($r)/issues/\(.number)  \(.title) (\(.status))")')"$'\n'
    done
    if [ -n "${out//[$'\n']/}" ]; then printf '%s\n' "$head"; grep -v '^$' <<<"$out"; shown=1; fi
  done
  [ -n "$shown" ] || echo "  none yet"
}
