#!/usr/bin/env python3
"""One poll of the dispatcher's activity stream, for scripts/activity.sh.

  activity STATE TZ LIMIT [FILTER]

STATE is what the last poll printed on its final line (empty the first
time): the newest time shown, the day last shown, and the entries already
shown near that time. Entries are printed one per line, in the reader's
time zone (TZ, an offset in seconds); FILTER, if given, keeps the lines that
contain it, ignoring case. The last line is the new STATE, after a tab.
"""
import json
import sys
import time
import urllib.request

state, tz, limit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
needle = (sys.argv[4] if len(sys.argv) > 4 else "").lower()
cursor, day, seen = 0.0, "", set()
if state:
    s = json.loads(state)
    cursor, day, seen = s["cursor"], s["day"], set(s["seen"])

# Look back a little before the newest entry shown, in case one was written
# with an earlier time just after the last poll; `seen` stops it repeating.
after = cursor - 30 if cursor else 0
url = f"http://127.0.0.1:8080/activity?after={after}&limit={limit if not cursor else 1000}&tz={tz}"
entries = json.load(urllib.request.urlopen(url, timeout=10))

for e in entries:
    if e["id"] in seen:
        continue
    local = time.gmtime(e["at"] + tz)
    line = f"{time.strftime('%H:%M:%S', local)}  {e['who']:<16} {e['text']}"
    if needle and needle not in f"{e['who']} {e['text']}".lower():
        continue
    d = time.strftime("%a %d %b", local)
    if d != day:
        print(f"-- {d} --")
        day = d
    print(line)

if entries:
    cursor = max(cursor, max(e["at"] for e in entries))
seen = {e["id"] for e in entries if e["at"] > cursor - 30} if entries else seen
print("\t" + json.dumps({"cursor": cursor, "day": day, "seen": sorted(seen)}))
