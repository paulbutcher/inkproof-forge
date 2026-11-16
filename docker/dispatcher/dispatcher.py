#!/usr/bin/env python3
"""The dispatcher: turns Forgejo's webhooks into work for agents, hands that
work to each agent's runner, keeps the run ledger, and tells the human what
needs them.

  POST /hook                 Forgejo's webhooks, signed with the webhook secret
  POST /claim                a runner asks for work (Bearer: its runner key)
  POST /heartbeat            a runner reports the human active in its container
  POST /runs/<id>/start      the runner started the claimed run
  POST /runs/<id>/end        the run ended: outcome, cost, turns, denials...
  POST /runs/abandon         the runner restarted; unfinished runs are closed
  POST /sessions             the human's session in an agent's container
  POST /admin/drop           drop an agent's pending triggers (Bearer: admin key)
  POST /admin/reconcile      reconcile now (Bearer: admin key)
  GET  /status               agents, queues, recent runs, sessions, notices
  GET  /activity?after=T     events, queued work, runs, sessions and notices
                             after epoch T, for activity.sh
  GET  /notices.txt?after=N  notices after id N, one per line, for notify.sh
  GET  /healthz

Only trusted authors (the human and the kit's accounts) start work, never an
agent's own actions, and never a comment whose first line is
`(no reply needed)`. Trigger descriptions carry only numbers and names, never
issue text, because they become a run's prompt.

What an agent owes comes from rules over each issue and pull request as it
is now (see Rules), never from the events themselves: a webhook only says
which items to evaluate again, and every RECONCILE_SECONDS, and on start, a
sweep evaluates them all. Each rule queues a trigger once per version of the
state it looks at, so nothing is lost to a missed webhook and nothing wakes
an agent twice for the same state.

A claim is granted only when the agent has no run, is under its daily caps,
the kit isn't paused, and its newest trigger has settled for the debounce
period (or its oldest has waited the maximum delay), so a burst of events
becomes one run; and only for triggers whose items still owe them. A run
that fails, times out or exits early has its work queued again, up to
MAX_RUN_RETRIES times; then it's the human's.

The rules can't foresee everything, so once the kit has been quiet for
IDLE_SECONDS (no run, no queued work, no CI), every open issue and pull
request must be waiting for something the kit can see (see Stalls); whoever
should move on one that isn't is woken, and after STALL_ROUNDS quiet spells
it is the human's. With nothing stalled, it wakes the coordinator to tell the
human what it needs from them. Once per quiet spell.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(os.environ.get("STATE_DIR", "/state"))
SECRETS = Path(os.environ.get("DISPATCHER_SECRETS", "/run/secrets/dispatcher"))
KEYS_DIR = Path(os.environ.get("RUNNER_KEYS_DIR", "/run/secrets/runner-keys"))
AGENTS_CONF = Path(os.environ.get("AGENTS_CONF", "/etc/kit/agents.conf"))
CONTROL = Path(os.environ.get("CONTROL_DIR", "/control"))
FORGE = os.environ.get("FORGE_URL", "http://forgejo:3000").rstrip("/")
ORG = os.environ.get("FORGE_ORG", "")
HUMAN = os.environ.get("HUMAN_USER", "")
PREFIX = os.environ.get("REPO_PREFIX", "inkproof-")
CAP = int(os.environ.get("MAX_RUNS_PER_DAY", "20"))
# The coordinator is woken by every other agent's work, so it has its own cap.
COORDINATOR_CAP = int(os.environ.get("MAX_COORDINATOR_RUNS_PER_DAY", "40"))
# How long the coordinator's "pull request opened" waits, so that a review
# requested from it soon after shares the run.
OPENED_HOLD = int(os.environ.get("PR_OPENED_HOLD_SECONDS", "600"))
SPEND_CAP = float(os.environ.get("MAX_SPEND_PER_DAY", "0") or 0)  # dollars per agent; 0: none
DEBOUNCE = int(os.environ.get("DEBOUNCE_SECONDS", "60"))
MAX_DELAY = int(os.environ.get("MAX_DELAY_SECONDS", "900"))
RECONCILE = int(os.environ.get("RECONCILE_SECONDS", "900"))
SETTLE = int(os.environ.get("CONFORMANCE_SETTLE_SECONDS", "600"))
# How long a scheduled merge may sit once nothing stands in its way before
# its owner is told to merge it: Forgejo acts on a schedule only when a review
# or CI status arrives after it, so one made once everything passed never fires.
STUCK = int(os.environ.get("MERGE_STUCK_SECONDS", "600"))
# How long a pull request in a repository with CI may go with no run at all
# for its head before its owner is woken: Forgejo creates a run (and a pending
# status) as soon as a push arrives, so none means its branch has no workflow.
NO_CI = int(os.environ.get("CI_MISSING_SECONDS", "600"))
# A run that fails, times out or exits early is tried again this many times,
# after RETRY_DELAY_SECONDS times the attempt; then it is the human's.
RETRIES = int(os.environ.get("MAX_RUN_RETRIES", "2"))
RETRY_DELAY = int(os.environ.get("RETRY_DELAY_SECONDS", "300"))
REVIEWER = "conformance"
MILESTONE_REVIEWERS = ("architecture", "process", "lasm")
KEEP_PAYLOADS = 500
MAX_BODY = 25 * 1024 * 1024
NO_REPLY = "(no reply needed)"
BLOCKED_BY = re.compile(r"Blocked-by:[ \t]*([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)")


def log(msg):
    print(f"dispatcher: {msg}", file=sys.stderr, flush=True)


def now():
    return time.time()


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


# --- Configuration --------------------------------------------------------------

def load_agents():
    """name -> {kind, own}, from agents.conf."""
    agents = {}
    for line in AGENTS_CONF.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, kind, own = line.split()[:3]
        agents[name] = {"kind": kind, "own": None if own == "-" else own}
    return agents


AGENTS = load_agents()
TRUSTED = set(AGENTS) | {"gatekeeper", "dispatcher", HUMAN}
OWNER_OF = {a["own"]: name for name, a in AGENTS.items()
            if a["own"] and a["kind"] in ("library", "coordinator")}
COORDINATOR = next((n for n, a in AGENTS.items() if a["kind"] == "coordinator"), None)
COORDINATION = AGENTS[COORDINATOR]["own"] if COORDINATOR else f"{PREFIX}coordination"


def secret(name):
    return (SECRETS / name).read_text().strip()


def agent_for_key(key):
    """Runner keys are read on every request, so a new one needs no restart."""
    if not key:
        return None
    for f in KEYS_DIR.glob("*"):
        try:
            if hmac.compare_digest(f.read_text().strip(), key):
                return f.name
        except OSError:
            continue
    return None


def paused():
    return (CONTROL / "paused").exists()


# --- The ledger -----------------------------------------------------------------

STATE.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(STATE / "ledger.db", check_same_thread=False, isolation_level=None)
DB.row_factory = sqlite3.Row
LOCK = threading.RLock()
DB.executescript("""
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS triggers (
  id INTEGER PRIMARY KEY, agent TEXT, key TEXT, description TEXT,
  created REAL, run_id INTEGER);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, agent TEXT, triggers TEXT, claimed REAL, started REAL,
  ended REAL, outcome TEXT, cost REAL, turns INTEGER, denied TEXT, session_id TEXT,
  log TEXT, transcript TEXT, in_extension INTEGER, forge_changes INTEGER,
  archived_log TEXT, archived_transcript TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, agent TEXT, session_id TEXT, started REAL, ended REAL,
  transcript TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at REAL, event TEXT, type TEXT, action TEXT, repo TEXT,
  sender TEXT, number INTEGER, delivery TEXT);
CREATE TABLE IF NOT EXISTS labels (
  repo TEXT, number INTEGER, labels TEXT, PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS notices (
  id INTEGER PRIMARY KEY, at REAL, key TEXT UNIQUE, kind TEXT, repo TEXT,
  number INTEGER, text TEXT, posted INTEGER);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS triggers_pending ON triggers (agent, run_id);
CREATE INDEX IF NOT EXISTS triggers_key ON triggers (agent, key);
CREATE INDEX IF NOT EXISTS events_sender ON events (sender, at);
CREATE TABLE IF NOT EXISTS blocked_by (repo TEXT, number INTEGER, on_repo TEXT, on_number INTEGER);
CREATE INDEX IF NOT EXISTS blocked_by_on ON blocked_by (on_repo, on_number);
""")
for column in ("triggers ADD COLUMN not_before REAL",  # ledgers from before stage 5
               "events ADD COLUMN detail TEXT",      # ledgers from before the activity stream
               "triggers ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"):  # before failed runs were retried
    try:
        DB.execute(f"ALTER TABLE {column}")
    except sqlite3.OperationalError:
        pass


def q(sql, *args):
    with LOCK:
        return DB.execute(sql, args).fetchall()



def kv_get(key, default=None):
    row = q("SELECT value FROM kv WHERE key=?", key)
    return row[0]["value"] if row else default


def kv_set(key, value):
    q("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", key, str(value))


def add_trigger(agent, key, description, once=False, not_before=None):
    """Queue work for an agent, unless the same key is already waiting; with
    once, unless it was ever queued. With not_before, it waits until then to
    be claimed. Queued again without one while it waits, it waits no longer
    (a review whose author's run has ended, a pull request whose CI has
    finished), unless it waits to be retried."""
    if agent not in AGENTS:
        return
    with LOCK:
        dup = DB.execute("SELECT id, run_id, not_before, attempts FROM triggers WHERE agent=? AND key=? "
                         + ("" if once else "AND run_id IS NULL ") + "ORDER BY id DESC", (agent, key)).fetchone()
        if dup:
            if (dup["run_id"] is None and dup["not_before"] and not_before is None and not dup["attempts"]):
                DB.execute("UPDATE triggers SET not_before=NULL WHERE id=?", (dup["id"],))
            return
        DB.execute("INSERT INTO triggers (agent, key, description, created, not_before) VALUES (?,?,?,?,?)",
                   (agent, key, description, now(), not_before))
    log(f"queued for {agent}: {description}")


# --- Forgejo --------------------------------------------------------------------

def forge(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(FORGE + "/api/v1" + path, method=method, data=data, headers={
        "Authorization": f"token {secret('token')}", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else None


def forge_list(path, limit=50):
    sep = "&" if "?" in path else "?"
    return forge("GET", f"{path}{sep}limit={limit}") or []


# --- Notices --------------------------------------------------------------------

def add_notice(key, kind, repo, number, text):
    """Record a notice once per key; notify.sh shows each on the Mac."""
    with LOCK:
        if DB.execute("SELECT 1 FROM notices WHERE key=?", (key,)).fetchone():
            return False
        DB.execute("INSERT INTO notices (at, key, kind, repo, number, text, posted) VALUES (?,?,?,?,?,?,0)",
                   (now(), key, kind, repo, number, text))
    return True


def comment(repo, number, text):
    forge("POST", f"/repos/{ORG}/{repo}/issues/{number}/comments", {"body": f"{NO_REPLY}\n\n@{HUMAN} {text}"})


def notify(key, kind, repo, number, text):
    """Once per key: a notice, and a comment on the issue as dispatcher that
    mentions the human, so it shows in their Forgejo notifications."""
    if not add_notice(key, kind, repo, number, text):
        return
    try:
        comment(repo, number, text)
        q("UPDATE notices SET posted=1 WHERE key=?", key)
        log(f"notified {HUMAN}: {repo}#{number}: {kind}")
    except Exception as e:
        log(f"couldn't post the notice for {repo}#{number}: {e}")


def notify_agent(agent, key, kind, title, sections):
    """A notice about an agent rather than an issue (caps, failed transcript
    rewrites): a decision of its own for the human, on the agent's repository
    (a reviewer's on the coordination repository)."""
    decision_issue(AGENTS[agent]["own"] or COORDINATION, key, kind, title, sections)


# --- Reading Forgejo ------------------------------------------------------------

def first_line(text):
    return ((text or "").strip().splitlines() or [""])[0].strip()


def wants_reply(body):
    return not first_line(body).startswith(NO_REPLY)


def login(x):
    return (x.get("user") or {}).get("login")


def labels_changed(repo, number, labels):
    """Labels added and removed since this issue was last seen: for the
    notices and the activity stream, not for waking anyone."""
    with LOCK:
        row = DB.execute("SELECT labels FROM labels WHERE repo=? AND number=?", (repo, number)).fetchone()
        DB.execute("INSERT OR REPLACE INTO labels (repo, number, labels) VALUES (?,?,?)",
                   (repo, number, json.dumps(sorted(labels))))
    before = set(json.loads(row["labels"])) if row else set()
    return set(labels) - before, before - set(labels)


def project_repo(repo):
    return repo.startswith(PREFIX)


def wip(title):
    t = (title or "").strip().lower()
    return t.startswith(("wip", "[wip]", "draft"))


def has_ci(repo):
    return repo != COORDINATION


def when(stamp):
    """A Forgejo timestamp, in seconds."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def forge_all(path):
    """Every page of a list."""
    sep = "&" if "?" in path else "?"
    out, page = [], 1
    while True:
        batch = forge("GET", f"{path}{sep}limit=50&page={page}") or []
        out += batch
        if len(batch) < 50:
            return out
        page += 1


_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and now() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now(), value)
    return value


def main_of(repo):
    """main's head commit: id and timestamp."""
    return cached(("main", repo), 20, lambda: forge("GET", f"/repos/{ORG}/{repo}/branches/main")["commit"])


def ci_state(repo, sha):
    """success, failure, running or none: the commit statuses branch
    protection requires (contexts starting "checks"), as it sees them."""
    return ci_settled(repo, sha)[0]


def ci_settled(repo, sha):
    """ci_state, and when its statuses last changed (0 if there are none)."""
    st = forge("GET", f"/repos/{ORG}/{repo}/commits/{sha}/status") or {}
    checks = [s for s in st.get("statuses") or [] if (s.get("context") or "").startswith("checks")]
    states = {s.get("status") for s in checks}
    at = max([when(s[k]) for s in checks for k in ("updated_at", "created_at") if s.get(k)] or [0])
    if not states:
        return "none", at
    if states & {"failure", "error"}:
        return "failure", at
    return ("success" if states <= {"success", "warning"} else "running"), at


def at_level_3(repo):
    """Whether repo's .forgejo/CODEOWNERS on main gives API.lock to the
    coordinator: the coordinator's approval is then the one its API changes
    need. Read from main each time (cached briefly), never from a clone."""
    def read():
        try:
            f = forge("GET", f"/repos/{ORG}/{repo}/contents/.forgejo/CODEOWNERS?ref=main") or {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise
        text = base64.b64decode(f.get("content") or "").decode(errors="replace")
        return any(l.split()[:1] == ["API\\.lock"] and f"@{COORDINATOR}" in l.split()[1:]
                   for l in text.splitlines())
    return cached(("level3", repo), 300, read)


def label_id(repo, name):
    labels = cached(("labels", repo), 3600, lambda: forge_all(f"/repos/{ORG}/{repo}/labels"))
    return next((l["id"] for l in labels if l["name"] == name), None)


REFS = re.compile(r"(?im)^\s*refs:?\s+(.+)$")
REF = re.compile(r"(?:([\w.-]+)/([\w.-]+))?#(\d+)")


def pr_refs(repo, pr):
    """The issues on repo a pull request refers to with `Refs`, in its
    description or its commits."""
    def read():
        texts = [pr.get("body") or ""]
        texts += [(c.get("commit") or {}).get("message") or ""
                  for c in forge_all(f"/repos/{ORG}/{repo}/pulls/{pr['number']}/commits")]
        found = set()
        for text in texts:
            for line in REFS.findall(text):
                for org, r, k in REF.findall(line):
                    if (not org or (org.lower() == ORG.lower() and r == repo)) and int(k) != pr["number"]:
                        found.add(int(k))
        return found
    return cached(("refs", repo, pr["number"], pr["head"]["sha"], pr.get("body")), 3600, read)


def owner_prs(repo, owner, state):
    """The owner's pull requests on repo, open or (the last 20) merged, each
    with the issues it refers to."""
    def read():
        if state == "open":
            prs = forge_all(f"/repos/{ORG}/{repo}/pulls?state=open")
        else:
            prs = [p for p in forge("GET", f"/repos/{ORG}/{repo}/pulls?state=closed&sort=recentupdate&limit=20") or []
                   if p.get("merged")]
        return [(p, pr_refs(repo, p)) for p in prs if login(p) == owner]
    return cached(("prs", repo, state), 30, read)


def may_push(agent):
    """Whether an agent could still push: a run under way, or the human
    active in its container."""
    return bool(q("SELECT 1 FROM runs WHERE agent=? AND ended IS NULL", agent)) or human_active(agent)


def busy(agent):
    """Running, with work queued, or with the human in its container: what it
    leaves undone may yet be done."""
    return bool(may_push(agent) or q("SELECT 1 FROM triggers WHERE agent=? AND run_id IS NULL", agent))


def release_reviews(agent):
    """The agent's run has ended: the reviews of its pull requests that wait
    to settle can start now."""
    if may_push(agent):
        return
    for t in q("SELECT id, key FROM triggers WHERE agent=? AND run_id IS NULL AND key LIKE 'conf:%' "
               "AND not_before>?", REVIEWER, now()):
        m = re.match(r"conf:([^#]+)#(\d+):", t["key"])
        if not m:
            continue
        try:
            pr = forge("GET", f"/repos/{ORG}/{m[1]}/pulls/{m[2]}")
        except Exception as e:
            log(f"couldn't read {m[1]}#{m[2]} to release its review: {e!r}")
            continue
        if login(pr) == agent:
            q("UPDATE triggers SET not_before=NULL WHERE id=?", t["id"])
            log(f"{agent}'s run ended: conformance may review {m[1]}#{m[2]} now")


# --- What waits on the human ------------------------------------------------------
#
# Everything waiting on the human is an open issue or pull request assigned to
# them (labelled needs-human, or a blocking upstream issue), or a pull request
# requesting their review: Forgejo's own dashboards are the list. A
# needs-human item must put its decision so it can be answered as it stands.

DECISION = ("Question", "Options", "Recommendation", "If you say go ahead", "Blocked on this")
RELEASED = re.compile(r"^Released in (v\d+\.\d+\.\d+)\b", re.M)
NUDGE = int(os.environ.get("UNVERIFIED_NUDGE_SECONDS", "86400"))
STALL = int(os.environ.get("UNVERIFIED_STALL_SECONDS", "172800"))


def missing_sections(text):
    return [s for s in DECISION if not re.search(rf"(?im)^#+\s*{re.escape(s)}\s*:?\s*$", text or "")]


def waits_on_human(labels):
    return "needs-human" in labels or {"upstream", "blocking"} <= labels


def set_assigned(repo, n, item, assign):
    have = [a["login"] for a in item.get("assignees") or []]
    if assign == (HUMAN in have):
        return
    want = have + [HUMAN] if assign else [a for a in have if a != HUMAN]
    forge("PATCH", f"/repos/{ORG}/{repo}/issues/{n}", {"assignees": want})
    log(f"{'assigned' if assign else 'unassigned'} {HUMAN} {'to' if assign else 'from'} {repo}#{n}")


def refuse_needs_human(repo, n, item, sender):
    """needs-human added without the decision written out: take it off, say
    why, and wake the agent that added it. Returns whether it was refused."""
    if sender in (HUMAN, "dispatcher"):
        return False
    texts = [item.get("body") or ""] + [c.get("body") or ""
                                        for c in forge_list(f"/repos/{ORG}/{repo}/issues/{n}/comments")]
    best = min((missing_sections(t) for t in texts), key=len)
    if not best:
        return False
    label = next((l["id"] for l in item.get("labels") or [] if l["name"] == "needs-human"), None)
    if label is not None:
        forge("DELETE", f"/repos/{ORG}/{repo}/issues/{n}/labels/{label}")
    forge("POST", f"/repos/{ORG}/{repo}/issues/{n}/comments", {"body": (
        f"{NO_REPLY}\n\n`needs-human` is off again: a decision for the human states it so they can answer "
        f"as it stands, under the headings {', '.join(f'`## {s}`' for s in DECISION)}, in the description "
        f"or one comment (Protocol: Escalate). Missing here: {', '.join(best)}. Write them, then add the label.")})
    if sender in AGENTS:
        add_trigger(sender, f"nh-refused:{repo}#{n}:{int(now())}",
                    f"needs-human taken off {repo}#{n}: the decision lacks the sections your protocol asks for")
    log(f"took needs-human off {repo}#{n}, added by {sender}: missing {', '.join(best)}")
    return True


def human_labels_changed(repo, n, item, labels, added, removed, sender):
    """Keep the human's assignment in step with needs-human and blocking
    upstream issues. Returns the labels, and the labels added, that stand."""
    if "needs-human" in added and refuse_needs_human(repo, n, item, sender):
        added = added - {"needs-human"}
        labels = labels - {"needs-human"}
    if (added | removed) & {"needs-human", "blocking", "upstream"} and item.get("state") == "open":
        set_assigned(repo, n, item, waits_on_human(labels))
    return labels, added


def decision_issue(repo, key, kind, title, sections):
    """A decision for the human from the kit itself: once per key, an issue
    labelled needs-human and assigned to them, with the decision's sections."""
    if not add_notice(key, kind, repo, None, title):
        return
    try:
        labels = [l["id"] for l in forge_list(f"/repos/{ORG}/{repo}/labels", 100) if l["name"] == "needs-human"]
        body = f"{NO_REPLY}\n\n" + "\n\n".join(f"## {h}\n\n{sections[h].strip()}" for h in DECISION)
        issue = forge("POST", f"/repos/{ORG}/{repo}/issues",
                      {"title": title, "body": body, "labels": labels, "assignees": [HUMAN]})
        q("UPDATE notices SET posted=1, number=? WHERE key=?", issue["number"], key)
        log(f"asked {HUMAN} to decide {repo}#{issue['number']}: {title}")
    except Exception as e:
        log(f"couldn't open the decision {title!r} on {repo}: {e}")


def waiting_on_human():
    """How many open issues and pull requests wait on the human: assigned to
    them, or requesting their review."""
    found = set()
    for repo in OWNER_OF:
        for i in forge_list(f"/repos/{ORG}/{repo}/issues?state=open&type=all&assigned_by={HUMAN}"):
            found.add((repo, i["number"]))
        for p in forge_list(f"/repos/{ORG}/{repo}/pulls?state=open"):
            if HUMAN in {u["login"] for u in p.get("requested_reviewers") or []}:
                found.add((repo, p["number"]))
    return len(found)


def check_unverified_stalls(repo):
    """Open requests released but never verified: the requester was woken
    again after NUDGE seconds (rule 10); after STALL, the stall is the human's."""
    for issue in forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=issues&labels=request"):
        labels = {l["name"] for l in issue.get("labels") or []}
        if "request" not in labels or "verified" in labels:
            continue
        n = issue["number"]
        released = [(c, RELEASED.search(c.get("body") or "")) for c in
                    forge_all(f"/repos/{ORG}/{repo}/issues/{n}/comments")
                    if login(c) in ("gatekeeper", HUMAN, OWNER_OF.get(repo))]
        released = [(c, m) for c, m in released if m]
        if not released:
            continue
        c, m = released[-1]
        tag = m[1]
        age = now() - when(c["created_at"])
        requester = login(issue) or "?"
        if age > STALL:
            decision_issue(COORDINATION, f"stalled:{repo}#{n}:{tag}", "stalled",
                           f"Released but never verified: {repo}#{n}", {
                "Question": f"[{repo}#{n}]({issue.get('html_url', '')}) was released in {tag} "
                            f"{int(age // 86400)} days ago, and its requester, `{requester}`, hasn't verified it, "
                            "even after being woken again a day later. Something has stalled: what should happen?",
                "Options": f"- Find out why: `scripts/activity.sh {repo}#{n}` and `scripts/activity.sh {requester}` "
                           "show what happened to it, and fix the kit if a step is missing.\n"
                           "- Verify it yourself against its acceptance criteria, add `verified` and close it.\n"
                           "- Close it as no longer needed, saying why.",
                "Recommendation": "Find out why first: a release nobody verifies is usually a missing wake-up, "
                                  "an upgrade the requester can't make, or a requester that isn't an agent.",
                "If you say go ahead": "Nothing happens on its own: the kit has already done what it can. "
                                       "Close this issue once you've dealt with it.",
                "Blocked on this": f"{repo}#{n}, and anything whose `Blocked-by:` line names it."})


def later(fn, *args, delay=30):
    def run():
        try:
            fn(*args)
        except Exception as e:
            log(f"evaluating {args} again failed: {e!r}")
    t = threading.Timer(delay, run)
    t.daemon = True
    t.start()


def handle(event, payload):
    """A webhook: the kit's own side effects (notices, the human's
    assignments, refusals), then the items it touched evaluated again. What
    anyone owes comes from the items as they are now (see Rules), never from
    the event itself, so a missed or late event loses nothing."""
    repo = (payload.get("repository") or {}).get("name") or ""
    sender = (payload.get("sender") or {}).get("login") or ""
    if ((payload.get("repository") or {}).get("owner") or {}).get("login", ORG).lower() != ORG.lower():
        return
    if not project_repo(repo):
        return
    trusted = sender in TRUSTED
    action = payload.get("action")
    item = payload.get("issue") or payload.get("pull_request") or {}
    n = item.get("number")

    if event == "issues" or (event == "pull_request" and action == "label_updated"):
        labels = {l["name"] for l in item.get("labels") or []}
        added, removed = labels_changed(repo, n, labels)
        if trusted:
            labels, added = human_labels_changed(repo, n, item, labels, added, removed, sender)
            if "blocking" in added and "upstream" in labels:
                notify(f"blocking:{repo}#{n}", "blocking", repo, n,
                       "this upstream issue is marked `blocking`: an agent can't continue until it's resolved "
                       "upstream. Close it or comment on it once a fix is available.")
            # A recommendation's needs-human is grouped into its review's report notice.
            if "needs-human" in added and sender != "dispatcher" and "recommendation" not in labels:
                notify(f"needs-human:{repo}#{n}:{int(now())}", "needs-human", repo, n,
                       "this needs your decision (`needs-human`).")
    elif event == "issue_comment" and action == "created" and trusted:
        labels = {l["name"] for l in item.get("labels") or []}
        # A milestone reviewer's report: one notice per report, not per recommendation.
        if sender in MILESTONE_REVIEWERS and ({"milestone-review", "upstream:lasm"} & labels):
            notify(f"report:{sender}:{repo}#{n}", "report", repo, n,
                   f"the `{sender}` review has reported here. Its recommendations are issues labelled "
                   f"`recommendation` and `review:{sender}`: close each as accepted, or label it `declined`, "
                   "say why and close it.")
    elif event == "release" and action == "published":
        # The one wake-up that is an event, not a state: the coordinator
        # hears of a release it didn't ask the gatekeeper for.
        tag = (payload.get("release") or {}).get("tag_name", "")
        if COORDINATOR and trusted and sender not in ("gatekeeper", COORDINATOR):
            add_trigger(COORDINATOR, f"released:{repo}:{tag}", f"{repo} released {tag}", once=True)
        return

    if not kv_get("rules-seeded"):
        return  # the first sweep hasn't run: it would see this item anyway
    if event == "push":
        if payload.get("ref") == "refs/heads/main":
            _cache.pop(("main", repo), None)
            evaluate_repo(repo, items=True)
        return
    if event.startswith("action_run"):
        # The rules read CI from commit statuses, which may lag the run's own
        # event: look again a little later too.
        run = payload.get("run") or {}
        if run.get("event") == "push":
            # CI on main: and the requests waiting on it to be released.
            evaluate_repo(repo)
            later(evaluate_repo, repo)
            later(evaluate_labelled, repo, "ready-for-release")
            return
        try:
            n = (json.loads(run.get("event_payload") or "{}").get("pull_request") or {}).get("number")
        except ValueError:
            return
        if n:
            later(evaluate, repo, n)
    if n:
        evaluate(repo, n)
    if event == "issues" and action == "closed":
        evaluate_repo(repo)  # its milestone may be done


# --- Rules ------------------------------------------------------------------------
#
# Every wake-up is a rule over an issue or pull request as it is now, or over
# a repository (its main, its milestones). A rule names the agent that owes
# something and a version: the state that makes it new, such as a head
# commit, a comment or a time. A trigger is queued once per version, so an
# agent that looks and leaves things as they are isn't woken again until
# something changes, and a run that doesn't finish gives its triggers back
# (retry). Webhooks only say which items to look at again; the sweep looks at
# all of them every RECONCILE seconds; and a claim looks again at the items
# behind its triggers, dropping what is no longer owed. The numbers are the
# rules' in DESIGN.md (Triggers).

Owed = namedtuple("Owed", "agent key description not_before quiet", defaults=(None, False))

# Kinds of trigger a rule queues, which a claim checks are still owed.
RULE_KINDS = {"request", "comment", "review", "ci-failed", "conflicts", "unscheduled", "stuck-merge", "no-ci", "merged",
              "idle-request",
              "unblocked", "upstream-comment", "released", "unverified", "answered", "filed", "pr-opened",
              "review-requested", "rfr", "nh", "reviewed", "conf", "conf-reply", "milestone", "lasm-blocking", "mention"}
# Kinds whose state, when the rules first run on an existing ledger, was
# already acted on under the old event-driven triggers, or only reports what
# happened: recorded as handled rather than queued. The others are work still
# to do, which the old triggers may have missed.
SEED_KINDS = {"comment", "review", "merged", "unblocked", "upstream-comment", "answered", "filed", "pr-opened",
              "nh", "milestone", "lasm-blocking", "milestone-done"}
RULE_KEY = re.compile(r"^([a-z-]+):([\w.-]+)#(\d+):")


class Item:
    """An issue or pull request, read once for all the rules."""

    def __init__(self, repo, n):
        self.repo, self.n = repo, n
        self.issue = forge("GET", f"/repos/{ORG}/{repo}/issues/{n}")
        self.is_pr = self.issue.get("pull_request") is not None
        self.pr = forge("GET", f"/repos/{ORG}/{repo}/pulls/{n}") if self.is_pr else None
        self.events = forge_all(f"/repos/{ORG}/{repo}/issues/{n}/timeline")
        self.reviews = sorted(forge_all(f"/repos/{ORG}/{repo}/pulls/{n}/reviews"), key=lambda r: r["id"]) \
            if self.is_pr else []
        self.labels = {l["name"] for l in self.issue.get("labels") or []}
        self.author = login(self.issue)
        self.owner = OWNER_OF.get(repo)
        self.open = self.issue.get("state") == "open"
        self.kind = "pull request" if self.is_pr else "issue"
        self.at = f"{repo}#{n}"
        self._ci = None

    @property
    def head(self):
        return self.pr["head"]["sha"]

    def ci(self):
        if self._ci is None:
            self._ci = ci_state(self.repo, self.head) if has_ci(self.repo) else "none"
        return self._ci

    def comments(self):
        return [e for e in self.events if e.get("type") == "comment"]

    def by(self, who, types=None):
        """When `who` last did anything here (of the given timeline types),
        reviews included; 0 if never."""
        times = [when(e["created_at"]) for e in self.events
                 if login(e) == who and (types is None or e.get("type") in types)]
        times += [when(r["submitted_at"]) for r in self.reviews if login(r) == who and r.get("submitted_at")]
        return max(times or [0])

    def label_event(self, label, added=True):
        """When label was last added (or removed): Forgejo marks an addition with the body "1"."""
        found = [e["created_at"] for e in self.events if e.get("type") == "label"
                 and (e.get("label") or {}).get("name") == label and ((e.get("body") or "") == "1") == added]
        return found[-1] if found else None

    def released(self):
        """The `Released in vX.Y.Z` comments (from the gatekeeper, the human
        or the repository's owner), oldest first, with their tags."""
        return [(c, m[1]) for c in self.comments() if login(c) in ("gatekeeper", HUMAN, self.owner)
                for m in [RELEASED.search(c.get("body") or "")] if m]

    def merge_scheduled(self):
        """The latest schedule or cancellation in the timeline decides."""
        found = [e["type"] for e in self.events if e.get("type") in ("pull_scheduled_merge", "pull_cancel_scheduled_merge")]
        return bool(found) and found[-1] == "pull_scheduled_merge"

    def requested(self):
        """Everyone a review was requested from (code owners included) and
        not since unrequested."""
        found = set()
        for e in self.events:
            if e.get("type") == "review_request" and (e.get("assignee") or {}).get("login"):
                (found.discard if e.get("removed_assignee") else found.add)(e["assignee"]["login"])
        return found

    def verdicts(self):
        """Each reviewer's latest approval or request for changes that
        stands (not dismissed)."""
        found = {}
        for r in self.reviews:  # oldest first
            if r.get("state") in ("APPROVED", "REQUEST_CHANGES") and not r.get("dismissed"):
                found[login(r)] = r
        return found

    def ready_since(self):
        """When nothing last stood in the way of merging it, or None while
        something does: CI passed on the head (where the repository has CI),
        no conflict, no request for changes standing, and everyone a review
        was requested from (code owners included) approved the head; at
        least one approval. Ready since the latest of those approvals, the
        last push and the merge's scheduling."""
        if self.pr.get("mergeable") is not True or (has_ci(self.repo) and self.ci() != "success"):
            return None
        verdicts = self.verdicts()
        if any(r["state"] == "REQUEST_CHANGES" for r in verdicts.values()):
            return None
        approved = {who: r for who, r in verdicts.items() if r.get("commit_id") == self.head}
        if not approved or self.requested() - set(approved):
            return None
        times = [when(r["submitted_at"]) for r in approved.values()]
        times += [when(e["created_at"]) for e in self.events
                  if e.get("type") in ("pull_push", "pull_scheduled_merge")]
        return max(times)


def blockers(it):
    """The issues its Blocked-by: lines name, from trusted authors, each with
    when its line was first written."""
    texts = [(it.issue["created_at"], it.issue.get("body") or "")] if it.author in TRUSTED else []
    texts += [(c["created_at"], c.get("body") or "") for c in it.comments() if login(c) in TRUSTED]
    found = {}
    for at, text in texts:
        for org, r, k in BLOCKED_BY.findall(text):
            if org.lower() == ORG.lower() and project_repo(r) and (r, int(k)) != (it.repo, it.n):
                found.setdefault((r, int(k)), at)
    return found


def blocker_state(repo, n):
    """When an issue stopped blocking (closed, or released) and why, and for
    an open upstream issue, the human's latest comment on it."""
    issue = forge("GET", f"/repos/{ORG}/{repo}/issues/{n}")
    comments = forge_all(f"/repos/{ORG}/{repo}/issues/{n}/comments")
    labels = {l["name"] for l in issue.get("labels") or []}
    s = {"done": None, "what": None, "human": None}
    if issue.get("state") == "closed":
        s["done"] = issue.get("closed_at") or issue.get("updated_at")
        s["what"] = f"{'upstream issue' if 'upstream' in labels else 'issue'} {repo}#{n} was closed"
        return s
    for c in comments:
        m = RELEASED.search(c.get("body") or "")
        if m and login(c) in ("gatekeeper", HUMAN, OWNER_OF.get(repo)):
            s["done"], s["what"] = c["created_at"], f"{repo}#{n} was released in {m[1]}"
            return s
    if "upstream" in labels:
        mine = [c for c in comments if login(c) == HUMAN]
        s["human"] = mine[-1] if mine else None
    return s


def blocked_rules(it):
    """Rule 8: an issue whose Blocked-by: lines name only issues now closed or
    released wakes its owner, once; and the human's comment on an upstream
    issue it waits on, once per comment. Returns those, whether it is blocked
    (None if it names nothing), when it last stopped being blocked, and what
    it names."""
    found = blockers(it)
    if not found or not it.open or not it.owner:
        return [], None, None, found
    states = {b: blocker_state(*b) for b in found}
    done = sorted((when(s["done"]), s["what"]) for s in states.values() if s["done"])
    blocked = len(done) < len(found)
    owed = []
    if not blocked:
        t, what = done[-1]
        owed.append(Owed(it.owner, f"unblocked:{it.at}:{iso(t)}", f"{what}; {it.at} is blocked on it"))
    for (r, k), at in found.items():
        h = states[(r, k)]["human"]
        if h and when(h["created_at"]) >= when(at):  # to the second, on another issue
            owed.append(Owed(it.owner, f"upstream-comment:{it.at}:{h['id']}",
                             f"{HUMAN} commented on upstream issue {r}#{k}; {it.at} is blocked on it"))
    return owed, blocked, (done[-1][0] if done and not blocked else None), found


def owner_rules(it, blocked, unblocked_at):
    """What the repository's owner owes on an item in it."""
    o, owner, at = [], it.owner, it.at
    if not owner or not it.open:
        return o
    # 1. An untriaged request from someone else.
    if (not it.is_pr and {"request", "new"} <= it.labels and it.author in TRUSTED and it.author != owner):
        since = it.label_event("new") or it.issue["created_at"]
        o.append(Owed(owner, f"request:{at}:{since}", f"untriaged request {at}"))
    #    Or an issue from someone else with no labels at all that the owner
    #    hasn't touched: Forgejo drops the labels of anyone without write
    #    access to issues here, and a request can be filed without them.
    elif (not it.is_pr and not it.labels and it.author in TRUSTED and it.author != owner
          and not it.by(owner)):
        o.append(Owed(owner, f"request:{at}:{it.issue['created_at']}",
                      f"untriaged issue {at}, filed without labels: if it's a request, add `request` and `new` "
                      "and handle it as one; otherwise answer it or close it"))
    # 2. A comment or review from someone else that wants a reply, newer than
    #    anything the owner has done here. An approval asks nothing, whatever
    #    it says; a request for changes always asks.
    #    Timestamps are to the second, so ties go by the timeline's order.
    order = lambda e, i: (when(e["created_at"]), i)
    placed = lambda r: (when(r["submitted_at"]), next(
        (i for i, e in enumerate(it.events) if e.get("type") == "review" and login(e) == login(r)
         and e["created_at"] == r["submitted_at"]), -1))
    asks = [(order(c, i), f"comment:{at}:{c['id']}", f"comment on {it.kind} {at}")
            for i, c in enumerate(it.events) if c.get("type") == "comment"
            and login(c) in TRUSTED and login(c) != owner and wants_reply(c.get("body"))]
    asks += [(placed(r), f"review:{at}:{r['id']}",
              f"{'changes requested on' if r['state'] == 'REQUEST_CHANGES' else 'review on'} pull request {at}")
             for r in it.reviews if login(r) in TRUSTED and login(r) != owner and r.get("submitted_at")
             and (r.get("state") == "REQUEST_CHANGES" or (r.get("state") == "COMMENT" and wants_reply(r.get("body"))))]
    if asks:
        t, key, what = max(asks)
        acted = max([order(e, i) for i, e in enumerate(it.events) if login(e) == owner]
                    + [placed(r) for r in it.reviews if login(r) == owner and r.get("submitted_at")]
                    or [(0, 0)])
        if t > acted:
            o.append(Owed(owner, key, what))
    if it.is_pr and it.author == owner:
        head = it.head[:12]
        # 3. CI failed on its head.
        if it.ci() == "failure":
            o.append(Owed(owner, f"ci-failed:{at}:{head}", f"CI failed on pull request {at}"))
        # 25. No CI run at all for its head NO_CI after it was pushed: the
        #     branch has no workflow, such as one started from a library's
        #     seed before CI reached main, and nothing reviews it until CI passes.
        if has_ci(it.repo) and it.ci() == "none":
            pushes = [e["created_at"] for e in it.events if e.get("type") == "pull_push"]
            pushed = when(pushes[-1] if pushes else it.pr["created_at"])
            o.append(Owed(owner, f"no-ci:{at}:{head}",
                          f"CI has never run on your pull request {at} at {head}, so nothing reviews it: its branch "
                          "probably lacks `.forgejo/workflows/`. Merge `origin/main` into it and push", pushed + NO_CI))
        # 4. It conflicts with main. One merely behind merges as it is.
        if it.pr.get("mergeable") is False:
            o.append(Owed(owner, f"conflicts:{at}:{head}:{main_of(it.repo)['id'][:12]}",
                          f"pull request {at} conflicts with main"))
        # 5. No merge scheduled, so it would never merge, once the owner can
        #    no longer schedule it (looked at again when its run ends); or one
        #    scheduled that hasn't gone through STUCK after nothing stood in
        #    its way.
        if not wip(it.pr.get("title")) and not it.merge_scheduled():
            if not may_push(owner):
                o.append(Owed(owner, f"unscheduled:{at}:{head}",
                              f"your pull request {at} has no merge scheduled, so it won't merge: schedule it "
                              "(`forge pr automerge`), or close it if it's no longer wanted"))
        elif (not wip(it.pr.get("title")) and it.merge_scheduled()
              and (ready := it.ready_since()) and now() >= ready + STUCK):
            o.append(Owed(owner, f"stuck-merge:{at}:{head}:{iso(ready)}",
                          f"your pull request {at} has every approval it needs and CI passed, but its scheduled "
                          f"merge hasn't gone through: merge it now (`forge pr merge {it.n}`)"))
    request = not it.is_pr and "request" in it.labels and not it.labels & {"ready-for-release", "verified"}
    # Blocked by its Blocked-by: lines, or (naming none) by the label.
    held = blocked if blocked is not None else "blocked" in it.labels
    released = it.released()
    if request and not released:
        # Since it was last unblocked (or accepted): earlier work was only part of it.
        since = (unblocked_at and iso(unblocked_at)) or it.label_event("blocked", False) \
            or it.label_event("accepted") or it.issue["created_at"]
        merged = [(p, refs) for p, refs in owner_prs(it.repo, owner, "merged")
                  if it.n in refs and when(p["merged_at"]) > when(since)]
        # 6. Merged work it still has to finish: CHANGELOG.md and ready-for-release.
        if merged and not held:
            p = max(merged, key=lambda m: m[0]["merged_at"])[0]
            o.append(Owed(owner, f"merged:{at}:{p['number']}",
                          f"your pull request {it.repo}#{p['number']} merged; it answers {at}, "
                          "not yet labelled ready-for-release"))
        # 7. Accepted, nothing in its way and nothing under way, while the
        #    owner has nothing to do: once each time it's unblocked.
        if ("accepted" in it.labels and not held
                and not it.labels & {"new", "needs-human", "counter-proposal"}
                and not any(it.n in refs for _, refs in owner_prs(it.repo, owner, "open"))
                and not merged):
            o.append(Owed(owner, f"idle-request:{at}:{since}",
                          f"your accepted request {at} isn't blocked and has no pull request under way: work on "
                          "it, or say on it what it waits for (a `Blocked-by:` line)", quiet=True))
    return o


def requester_rules(it):
    """What an agent that filed a request on someone else's repository owes,
    or should hear."""
    o, req, at = [], it.author, it.at
    if (it.is_pr or "request" not in it.labels or req not in AGENTS or not AGENTS[req]["own"]
            or "verified" in it.labels):
        return o
    if it.open:
        released = it.released()
        # 10. Released: upgrade, check, verify; and again a day later. The
        #     coordinator verifies its own requests in the run that released them.
        if released:
            c, tag = released[-1]
            if not (req == COORDINATOR and login(c) == "gatekeeper"):
                o.append(Owed(req, f"released:{at}:{tag}",
                              f"your request {at} was released in {tag}: upgrade to it, check its acceptance "
                              "criteria, and verify it"))
            age = now() - when(c["created_at"])
            if age > NUDGE:
                o.append(Owed(req, f"unverified:{at}:{tag}",
                              f"your request {at} was released in {tag} {int(age // 3600)} hours ago and you "
                              "haven't verified it: upgrade, check its acceptance criteria and verify it, or say "
                              "on the issue what stops you"))
        # 11. Counter-proposed.
        t = it.label_event("counter-proposal")
        if t and "counter-proposal" in it.labels and req != it.owner:
            o.append(Owed(req, f"answered:{at}:{t}", f"your request {at} has a counter-proposal"))
    elif req != it.owner:
        # 11. Closed without being verified, by someone else: rejected.
        closes = [e for e in it.events if e.get("type") == "close"]
        if closes and login(closes[-1]) != req:
            o.append(Owed(req, f"answered:{at}:{closes[-1]['created_at']}",
                          f"your request {at} was closed without being verified"))
    return o


def coordinator_rules(it):
    """What the coordinator owes on any item."""
    o, c, at = [], COORDINATOR, it.at
    if not c:
        return o
    # 12. A request filed by someone else: check it's well formed and placed.
    if it.open and not it.is_pr and "request" in it.labels and it.author in TRUSTED and it.author != c:
        o.append(Owed(c, f"filed:{at}:", f"request filed: {at}"))
    if it.is_pr and it.author in TRUSTED and it.author != c and (it.open or it.pr.get("merged")):
        # 13. A pull request opened by someone else: check its scope. Held a
        #     while, or until CI has finished, so that a review requested of
        #     the coordinator (14) shares the run.
        settled = not it.open or not has_ci(it.repo) or it.ci() in ("success", "failure")
        o.append(Owed(c, f"pr-opened:{at}:", f"pull request opened: {at}",
                      None if settled else when(it.pr["created_at"]) + OPENED_HOLD))
    # Forgejo lists a reviewer as requested only until it reviews, and a push
    # that dismisses its approval doesn't request it again: the timeline's
    # requests still stand (web#8).
    requested = it.requested() | {u["login"] for u in it.pr.get("requested_reviewers") or []} if it.is_pr else set()
    if it.is_pr and it.open and c in requested:
        # 14. Its review is requested (at level 3, API.lock), CI passed on the
        #     head, and it hasn't approved that head.
        approved = any(login(r) == c and r.get("state") == "APPROVED" and r.get("commit_id") == it.head
                       and not r.get("dismissed") for r in it.reviews)
        if not approved and (not has_ci(it.repo) or it.ci() == "success"):
            # Said outright, from CODEOWNERS on main, since a stale clone
            # (from before the setup pull request merged) suggests otherwise.
            whose = (f"{it.repo} is at level 3: CODEOWNERS on main gives `API.lock` to you, so this pull request "
                     "waits for your review. Approve it, request changes, or put a trade-off to the human as a "
                     "decision; a comment alone leaves it waiting" if at_level_3(it.repo) else
                     f"{it.repo} is below level 3, so the human approves its `API.lock`; your review is advisory")
            o.append(Owed(c, f"review-requested:{at}:{it.head[:12]}",
                          f"review requested from you on pull request {at}, whose CI passed at {it.head[:12]}. "
                          f"{whose}"))
    if it.open and "ready-for-release" in it.labels:
        # 15. Ready for release, CI on main finished and passed (the
        #     gatekeeper releases nothing else), and it hasn't looked since
        #     the label was added or main last changed, its CI included (an
        #     owner may fix what a refused release asked for and leave the
        #     label on).
        labelled = it.label_event("ready-for-release") or it.issue["created_at"]
        main = main_of(it.repo)
        ci, ci_at = ci_settled(it.repo, main["id"]) if has_ci(it.repo) else ("none", 0)
        changed = max(when(labelled), when(main["timestamp"]), ci_at)
        looked = it.by(c, ("comment", "label"))
        if ci in ("success", "none") and looked < changed:
            what = (f"ready-for-release added to {it.kind} {at}" if looked < when(labelled) else
                    f"{at} is still labelled ready-for-release, and main has changed or its CI has finished "
                    "since you last looked at it")
            o.append(Owed(c, f"rfr:{at}:{labelled}:{main['id'][:12]}:{ci}", what))
    # 16. needs-human added or removed by someone else (not the kit), and it
    #     hasn't looked since. Not on a process or Lasm recommendation: those
    #     change the kit or go upstream, which is the human's, never its own
    #     remit (coordination#128).
    nh = [e for e in it.events if e.get("type") == "label" and (e.get("label") or {}).get("name") == "needs-human"]
    theirs = "recommendation" in it.labels and it.labels & {"review:process", "review:lasm"}
    if nh and not theirs and login(nh[-1]) not in (c, "dispatcher") and it.by(c, ("comment", "label")) < when(nh[-1]["created_at"]):
        added = (nh[-1].get("body") or "") == "1"
        o.append(Owed(c, f"nh:{at}:{nh[-1]['created_at']}",
                      f"needs-human {'added to' if added else 'removed from'} {it.kind} {at}"))
    # 24. An open milestone issue on which every milestone review has reported
    #     since it was labelled, and the coordinator hasn't commented since the
    #     last report: their reports want no reply, so nothing else wakes it
    #     to accept the milestone.
    if it.repo == COORDINATION and it.open and not it.is_pr and "milestone-review" in it.labels:
        labelled = when(it.label_event("milestone-review") or it.issue["created_at"])
        reviewers = [r for r in MILESTONE_REVIEWERS if r in AGENTS]
        reported = [it.by(r, ("comment",)) for r in reviewers]
        if reviewers and min(reported) > labelled and it.by(c, ("comment",)) < max(reported):
            o.append(Owed(c, f"reviewed:{at}:{iso(max(reported))}",
                          f"every milestone review has reported on {at}: accept the milestone (say what you "
                          "checked, close the issue and its Forgejo milestones, record it in ROADMAP.md), or say "
                          "on it what it waits for"))
    return o


def reviewer_rules(it):
    """What conformance and the milestone reviewers owe."""
    o, at = [], it.at
    if REVIEWER in AGENTS and it.is_pr and it.open and not wip(it.pr.get("title")):
        head = it.head
        # 19. A head that passed CI (or a repository without it) and that it
        #     hasn't reviewed, once the head has settled: at once if the author
        #     is an agent that can't push any more, else SETTLE after the last push.
        if (not has_ci(it.repo) or it.ci() == "success") and \
                not any(login(r) == REVIEWER and r.get("commit_id") == head for r in it.reviews):
            settled = it.author in AGENTS and not may_push(it.author)
            pushes = [e["created_at"] for e in it.events if e.get("type") == "pull_push"]
            pushed = when(pushes[-1] if pushes else it.pr["created_at"])
            o.append(Owed(REVIEWER, f"conf:{at}:{head[:12]}", f"review pull request {at} at {head[:12]}",
                          None if settled else pushed + SETTLE))
        # 20. The author answered its request for changes at this head in a
        #     comment, without pushing.
        mine = [r for r in it.reviews if login(r) == REVIEWER]
        if mine and mine[-1].get("state") == "REQUEST_CHANGES" and mine[-1].get("commit_id") == head:
            replies = [c for c in it.comments() if login(c) == it.author and login(c) != REVIEWER
                       and wants_reply(c.get("body")) and when(c["created_at"]) > when(mine[-1]["submitted_at"])]
            if replies:
                o.append(Owed(REVIEWER, f"conf-reply:{at}:{replies[-1]['id']}",
                              f"the author replied on pull request {at}, which you asked to change"))
    if it.repo == COORDINATION and not it.is_pr:
        # 21. A milestone review requested that a reviewer hasn't reported on
        #     since, open or closed: the coordinator accepts a milestone and
        #     closes its issue without waiting for the reviews.
        if "milestone-review" in it.labels:
            labelled = it.label_event("milestone-review") or it.issue["created_at"]
            o += [Owed(r, f"milestone:{at}:{labelled}", f"milestone review requested on {at}")
                  for r in MILESTONE_REVIEWERS if r in AGENTS and it.by(r, ("comment",)) < when(labelled)]
        # 22. A Lasm issue that blocks an agent.
        if it.open and {"upstream", "upstream:lasm", "blocking"} <= it.labels and "lasm" in AGENTS:
            o.append(Owed("lasm", f"lasm-blocking:{at}:", f"blocking Lasm issue {at}"))
    return o


MENTION = re.compile(r"(?<![\w@/.-])@([\w.-]+)")


def mention_rules(it):
    """What the human's @-mentions ask of agents other than the owner."""
    o, at = [], it.at
    # 26. A comment by the human that @-mentions an agent, which that agent
    #     hasn't commented on or acted on the item since: the human's way to
    #     reach an agent other than the owner, whom any comment wakes (2).
    #     An agent's mentions wake no one.
    for c in it.comments():
        if login(c) != HUMAN:
            continue
        for name in dict.fromkeys(MENTION.findall(c.get("body") or "")):
            if name in AGENTS and name != it.owner and it.by(name) < when(c["created_at"]):
                o.append(Owed(name, f"mention:{at}:{c['id']}", f"the human mentioned you on {it.kind} {at}"))
    return o


def owed_on(it):
    """Everything owed on an item, and whether it is blocked (None if it
    names no blocker), with the issues it names."""
    owed, blocked, unblocked_at, found = blocked_rules(it)
    owed += owner_rules(it, blocked, unblocked_at)
    owed += requester_rules(it)
    owed += coordinator_rules(it)
    owed += reviewer_rules(it)
    owed += mention_rules(it)
    return [o for o in owed if o.agent in AGENTS], blocked, found


def repo_rules(repo):
    """What is owed on a repository itself."""
    o, owner = [], OWNER_OF.get(repo)
    # 9. CI failed on main: two pull requests that passed apart may fail
    #    together, since one behind main may merge, and main can't be released.
    if owner and has_ci(repo):
        main = main_of(repo)
        if ci_state(repo, main["id"]) == "failure":
            o.append(Owed(owner, f"main-failed:{repo}:{main['id'][:12]}",
                          f"CI failed on main of {repo} at {main['id'][:12]}: fix it before anything else"))
    # 17. A milestone with nothing left open on any repository it spans:
    #     once per milestone, not once per namesake, since until the last
    #     namesake's work is done there is nothing to close out.
    if COORDINATOR:
        for ms in forge_all(f"/repos/{ORG}/{repo}/milestones?state=open"):
            if ms.get("open_issues") == 0 and ms.get("closed_issues", 0) > 0:
                spans = finished_namesakes(ms["title"])
                if spans:
                    newest = max(m["id"] for _, m in spans)
                    o.append(Owed(COORDINATOR, f"milestone-done:{ms['title']}:{newest}",
                                  f"the last open issue in milestone {ms['title']} was closed, on every repository it "
                                  f"spans ({', '.join(sorted({r for r, _ in spans}))})"))
    # 23. No milestone on any repository with an open issue: nothing in the
    #     kit says what comes next, so the coordinator must, once per
    #     milestone finished. A milestone left open with its work done
    #     doesn't hold it back. Not while it runs, since it may be drafting
    #     the next one.
    if COORDINATOR and repo == COORDINATION:
        last = next_milestone_due()
        if last:
            o.append(Owed(COORDINATOR, f"next-milestone:{last[0]}:{last[1]['id']}",
                          f"no milestone has open work, the newest {last[1].get('title')} on {last[0]}: close "
                          "out any milestone left open (its milestone issue and reviews, or close it if that's "
                          "done), then draft the next one, or put to the human what it needs first (a repository, "
                          "an agent, a decision), or record in ROADMAP.md that the project is finished", quiet=True))
    return o


def finished_namesakes(title):
    """The (repository, milestone) pairs named title that had work, if none
    of its namesakes on any repository has an open issue; None otherwise."""
    found = []
    for repo in OWNER_OF:
        try:
            ms = forge_all(f"/repos/{ORG}/{repo}/milestones?state=all")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        for m in ms:
            if m.get("title") != title:
                continue
            if m.get("open_issues", 0) > 0:
                return None
            if m.get("closed_issues", 0) > 0:
                found.append((repo, m))
    return found or None


def next_milestone_due():
    """The newest (repository, milestone) that had work, if no milestone
    anywhere has an open issue; None otherwise, or if none ever had work.
    Newest by creation, so that closing milestones left open, or empty ones,
    doesn't make it due again."""
    done = []
    for repo in OWNER_OF:
        try:
            ms = forge_all(f"/repos/{ORG}/{repo}/milestones?state=all")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        if any(m.get("open_issues", 0) > 0 for m in ms):
            return None
        done += [(when(m["created_at"]), m["id"], repo, m) for m in ms if m.get("closed_issues", 0) > 0]
    if not done:
        return None
    _, _, repo, m = max(done, key=lambda c: c[:2])
    return repo, m


# --- Evaluation -------------------------------------------------------------------

def queue(o, seed=False):
    if seed and o.key.split(":")[0] in SEED_KINDS:
        with LOCK:
            if not DB.execute("SELECT 1 FROM triggers WHERE agent=? AND key=?", (o.agent, o.key)).fetchone():
                DB.execute("INSERT INTO triggers (agent, key, description, created, run_id) VALUES (?,?,?,?,-1)",
                           (o.agent, o.key, o.description, now()))
        return
    if o.quiet and busy(o.agent):
        return
    add_trigger(o.agent, o.key, o.description, once=True, not_before=o.not_before)


def set_blocked(it, blocked):
    """Keep the `blocked` label in step with the Blocked-by: lines."""
    if blocked is None or not it.open or blocked == ("blocked" in it.labels):
        return
    lid = label_id(it.repo, "blocked")
    if lid is None:
        return
    if blocked:
        forge("POST", f"/repos/{ORG}/{it.repo}/issues/{it.n}/labels", {"labels": [lid]})
    else:
        forge("DELETE", f"/repos/{ORG}/{it.repo}/issues/{it.n}/labels/{lid}")
    log(f"{'labelled' if blocked else 'unlabelled'} {it.at} blocked")


def keep_blocking(repo, n):
    """Keep `blocking` on an open upstream issue in step with the open items
    whose Blocked-by: lines name it: an agent waits on it, so it waits on the
    human. The label assigns it to them and tells them (and, for Lasm, wakes
    the lasm review)."""
    issue = forge("GET", f"/repos/{ORG}/{repo}/issues/{n}")
    labels = {l["name"] for l in issue.get("labels") or []}
    if issue.get("state") != "open" or "upstream" not in labels:
        return
    want = False
    for d in q("SELECT repo, number FROM blocked_by WHERE on_repo=? AND on_number=?", repo, n):
        try:
            if forge("GET", f"/repos/{ORG}/{d['repo']}/issues/{d['number']}").get("state") == "open":
                want = True
                break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    if want == ("blocking" in labels):
        return
    lid = label_id(repo, "blocking")
    if lid is None:
        return
    if want:
        forge("POST", f"/repos/{ORG}/{repo}/issues/{n}/labels", {"labels": [lid]})
    else:
        forge("DELETE", f"/repos/{ORG}/{repo}/issues/{n}/labels/{lid}")
    log(f"{'labelled' if want else 'unlabelled'} {repo}#{n} blocking")


def evaluate(repo, n, dependents=True, seed=False):
    """Queue what is owed on an item now, keep its `blocked` label and the
    index of what it waits on, then do the same for the items waiting on it."""
    try:
        it = Item(repo, n)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise
    owed, blocked, found = owed_on(it)
    # Only an open item waits: a closed one's lines block nothing.
    now_on = set(found) if it.open else set()
    with LOCK:
        was_on = {(d["on_repo"], d["on_number"]) for d in
                  DB.execute("SELECT on_repo, on_number FROM blocked_by WHERE repo=? AND number=?", (repo, n))}
        DB.execute("DELETE FROM blocked_by WHERE repo=? AND number=?", (repo, n))
        DB.executemany("INSERT INTO blocked_by (repo, number, on_repo, on_number) VALUES (?,?,?,?)",
                       [(repo, n, r, k) for r, k in now_on])
    set_blocked(it, blocked)
    for r, k in was_on ^ now_on:
        try:
            keep_blocking(r, k)
        except Exception as e:
            log(f"keeping blocking on {r}#{k} failed: {e!r}")
    for o in owed:
        queue(o, seed)
    if not dependents:
        return
    if it.is_pr:
        # A pull request opened or merged changes what is owed on the
        # requests it refers to (6, 7).
        for state in ("open", "merged"):
            _cache.pop(("prs", repo, state), None)
        for k in pr_refs(repo, it.pr):
            try:
                evaluate(repo, k, dependents=False)
            except Exception as e:
                log(f"evaluating {repo}#{k} failed: {e!r}")
    for d in q("SELECT repo, number FROM blocked_by WHERE on_repo=? AND on_number=?", repo, n):
        try:
            evaluate(d["repo"], d["number"], dependents=False)
        except Exception as e:
            log(f"evaluating {d['repo']}#{d['number']} failed: {e!r}")


def evaluate_repo(repo, items=False, seed=False):
    for o in repo_rules(repo):
        queue(o, seed)
    if items:
        for i in forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=all"):
            try:
                evaluate(repo, i["number"], dependents=False, seed=seed)
            except Exception as e:
                log(f"evaluating {repo}#{i['number']} failed: {e!r}")


def evaluate_own_prs(agent):
    """An agent's run has ended: what it left its open pull requests owing
    (5) was held while it could still act on them."""
    repo = AGENTS.get(agent, {}).get("own")
    if repo not in OWNER_OF or may_push(agent):
        return
    _cache.pop(("prs", repo, "open"), None)
    for p, _ in owner_prs(repo, agent, "open"):
        try:
            evaluate(repo, p["number"], dependents=False)
        except Exception as e:
            log(f"evaluating {repo}#{p['number']} failed: {e!r}")


def evaluate_labelled(repo, name):
    for i in forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=all&labels={name}"):
        try:
            evaluate(repo, i["number"], dependents=False)
        except Exception as e:
            log(f"evaluating {repo}#{i['number']} failed: {e!r}")


def no_longer_owed(rows):
    """The ids of queued triggers, among those a rule queued, that their
    item no longer owes: the pull request merged meanwhile, the comment was
    answered, the head moved on."""
    items, stale = {}, []
    for r in rows:
        m = RULE_KEY.match(r["key"])
        if m and m[1] in RULE_KINDS:
            items.setdefault((m[2], int(m[3])), []).append(r)
    for (repo, n), rs in items.items():
        try:
            keys = {o.key for o in owed_on(Item(repo, n))[0]}
        except urllib.error.HTTPError as e:
            if e.code != 404:
                continue
            keys = set()
        except Exception as e:
            log(f"couldn't check {repo}#{n} is still owed: {e!r}")
            continue
        stale += [r["id"] for r in rs if r["key"] not in keys]
    return stale


def forge_missing(repo):
    """Whether a repository, or its main, doesn't exist (yet)."""
    for path in (f"/repos/{ORG}/{repo}", f"/repos/{ORG}/{repo}/branches/main"):
        try:
            forge("GET", path)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True
            raise
    return False


def sweep(seed=False):
    """Every repository and every open issue and pull request evaluated, and
    issues closed lately (a rejected request): what a missed webhook would
    have lost, and what no webhook brings."""
    since = iso(max(float(kv_get("reconciled", 0)) - RECONCILE, now() - 86400))
    ok = True
    for repo in OWNER_OF:
        try:
            evaluate_repo(repo, items=True, seed=seed)
            for i in forge_all(f"/repos/{ORG}/{repo}/issues?state=closed&type=issues&since={since}"):
                evaluate(repo, i["number"], dependents=False, seed=seed)
            check_unverified_stalls(repo)
            # Upstream issues, once every item's Blocked-by: lines are read.
            for item in forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=issues&labels=upstream"):
                if "upstream" in {l["name"] for l in item.get("labels") or []}:
                    keep_blocking(repo, item["number"])
            # What waits on the human but isn't assigned to them: from before
            # assignment, or a missed webhook. Forgejo ignores a label filter
            # naming a label the repository lacks, so check each item's own.
            for labels in ("needs-human", "upstream,blocking"):
                for item in forge_list(f"/repos/{ORG}/{repo}/issues?state=open&type=all&labels={labels}"):
                    if waits_on_human({l["name"] for l in item.get("labels") or []}):
                        set_assigned(repo, item["number"], item, True)
        except urllib.error.HTTPError as e:
            if e.code == 404 and forge_missing(repo):
                log(f"{repo} is in agents.conf but not in Forgejo (or has no main): skipped")
                continue
            ok = False
            log(f"sweeping {repo} failed: {e!r}")
        except Exception as e:
            ok = False
            log(f"sweeping {repo} failed: {e!r}")
    kv_set("reconciled", now())
    return ok


def reconcile():
    """The first sweep on a ledger from before the rules records what they
    find as handled where the old triggers covered it (SEED_KINDS)."""
    if not kv_get("rules-seeded"):
        if sweep(seed=True):
            kv_set("rules-seeded", now())
            log("the rules' first sweep is done: from now on they queue work")
    else:
        sweep()


# --- Stalls ---------------------------------------------------------------------
#
# The rules name what starts work, and anything they miss would leave the kit
# quiet with work open. So once the kit has gone quiet, every open issue and
# pull request must be waiting for something the kit can see: the human, an
# open issue its Blocked-by: lines name, a reviewer, the world outside (an
# upstream issue), a released request's requester (rule 10 nudges them, then
# it's the human's), or its own pull request. One that isn't wakes whoever
# should move it on, once per quiet spell; after STALL_ROUNDS spells it is
# the human's.

STALL_ROUNDS = int(os.environ.get("STALL_ROUNDS", "2"))


def open_refs(repo):
    """The issues on repo that open pull requests, anyone's, refer to."""
    return cached(("refs", repo), 30, lambda: {n for p in forge_all(f"/repos/{ORG}/{repo}/pulls?state=open")
                                                for n in pr_refs(repo, p)})


def stall_owners(it, blocked):
    """Who should move an open item on, each with why; none when it waits
    for something the kit can see."""
    labels = it.labels
    if not it.open or "needs-human" in labels or HUMAN in {u.get("login") for u in it.issue.get("assignees") or []}:
        return []
    if "upstream" in labels or blocked:
        return []
    if it.is_pr:
        author = it.author if it.author in AGENTS else None
        if wip(it.pr.get("title")):
            return [(author, "it is marked work in progress")] if author else []
        verdicts = it.verdicts()
        if any(r["state"] == "REQUEST_CHANGES" and r.get("commit_id") == it.head for r in verdicts.values()):
            return [(author, "changes were requested on its head")] if author else []
        # Reviewers wait for CI to pass on the head, so until it does only the
        # author can move it.
        if has_ci(it.repo) and it.ci() in ("none", "failure"):
            why = "CI has never run on its head" if it.ci() == "none" else "CI failed on its head"
            return [(author, why)] if author else []
        pending = {who for who in it.requested() if not (who in verdicts and verdicts[who].get("commit_id") == it.head)}
        if HUMAN in pending or (author is None and it.author == HUMAN):
            return []
        reviewers = sorted(p for p in pending if p in AGENTS)
        if reviewers:
            return [(r, "your review of its head is requested and not given") for r in reviewers]
        return [(author, "it isn't merging, and no review or check is pending")] if author else []
    if "request" in labels and it.released() and "verified" not in labels:
        return []
    if "ready-for-release" in labels:
        return [(COORDINATOR, "it is labelled ready-for-release but hasn't been released")] if COORDINATOR else []
    if "counter-proposal" in labels and it.author != it.owner:
        return [(it.author, "its counter-proposal waits for your answer")] if it.author in AGENTS else []
    if it.n in open_refs(it.repo):
        return []  # its pull request is checked in its own right
    if not it.owner:
        return []
    if "blocked" in labels:
        return [(it.owner, "it is labelled blocked, but no Blocked-by: line names an open, unreleased issue")]
    return [(it.owner, "nothing is moving it on")]


def check_stalls():
    """Wake whoever should move on each open item that waits for nothing the
    kit can see; after STALL_ROUNDS quiet spells, ask the human instead.
    Returns how many triggers it queued."""
    spell = int(now())
    stalled = {}
    for repo in OWNER_OF:
        if forge_missing(repo):
            continue
        for i in forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=all"):
            try:
                it = Item(repo, i["number"])
                owners = [(a, why) for a, why in stall_owners(it, blocked_rules(it)[1]) if a in AGENTS]
                if owners:
                    stalled[(repo, it.n)] = (it, owners)
            except Exception as e:
                log(f"checking {repo}#{i['number']} for a stall failed: {e!r}")
    # An item no longer stalled starts again from nothing.
    for row in q("SELECT key FROM kv WHERE key LIKE 'stall:%'"):
        repo, _, n = row["key"][len("stall:"):].partition("#")
        if (repo, int(n)) not in stalled:
            q("DELETE FROM kv WHERE key=?", row["key"])
    queued = 0
    for (repo, n), (it, owners) in stalled.items():
        key = f"stall:{repo}#{n}"
        rounds, first = json.loads(kv_get(key, "[0, 0]"))
        if rounds >= STALL_ROUNDS:
            names = ", ".join(sorted({a for a, _ in owners}))
            decision_issue(COORDINATION, f"stalled-item:{repo}#{n}:{first}", "stalled",
                           f"Stalled: {repo}#{n}", {
                "Question": f"Nothing is moving {it.kind} [{repo}#{n}]({it.issue.get('html_url', '')}) on, and it "
                            f"waits for nothing the kit can see: {'; '.join(why for _, why in owners)}. "
                            f"{names} {'was' if len(owners) == 1 else 'were'} woken for it in {rounds} quiet "
                            "spells without moving it on. What should happen?",
                "Options": f"- Find out why: `scripts/activity.sh {repo}#{n}` shows what happened to it; if the "
                           "kit lacks a step, fix the kit.\n"
                           f"- Tell {names} on the item what to do.\n"
                           "- Close it, saying why.",
                "Recommendation": "Find out why first: a stall the kit can't see is a gap in its rules.",
                "If you say go ahead": "Nothing changes by itself: say on the item what should happen, then "
                                       "close this issue.",
                "Blocked on this": f"{repo}#{n}."})
            continue
        woke = False
        for agent, why in owners:
            if capped(agent):
                continue
            add_trigger(agent, f"stalled:{repo}#{n}:{spell}",
                        f"the kit is quiet and nothing is moving {it.kind} {repo}#{n} on ({why}): move it on, "
                        "or record what it waits for (a `Blocked-by:` line, or `needs-human` with a decision)")
            queued += 1
            woke = True
        if woke:
            kv_set(key, json.dumps([rounds + 1, first or spell]))
            # No rule woke anyone for it, so the rules have a gap: the human
            # hears of each one, once, and the process review looks into it.
            names = ", ".join(sorted({a for a, _ in owners if not capped(a)}))
            notify(f"stall-caught:{repo}#{n}:{first or spell}", "stall", repo, n,
                   f"the stall check woke {names} for this {it.kind}: it waited for nothing the kit can see "
                   f"({'; '.join(why for _, why in owners)}), and no rule had woken anyone for it. That is a gap "
                   "in the kit's rules; the process review looks into each one at the milestone's end.")
    return queued


# --- Idle summaries -------------------------------------------------------------

IDLE = int(os.environ.get("IDLE_SECONDS", "1200"))
IDLE_TRIGGER = "the kit is idle"  # each idle trigger begins so


def last_activity():
    """When anything last happened: an event, work queued or a run ending,
    leaving out the idle summaries themselves (and the human's sessions, so
    reading one doesn't call for another)."""
    found = [q("SELECT MAX(at) AS m FROM events")[0]["m"],
             q("SELECT MAX(created) AS m FROM triggers WHERE key NOT LIKE 'idle:%'")[0]["m"],
             q("SELECT MAX(ended) AS m FROM runs WHERE triggers NOT LIKE ?", f"%{IDLE_TRIGGER}%")[0]["m"]]
    return max([t for t in found if t] or [0])


def ci_running():
    """Whether an Actions run is under way. A run left waiting for over an
    hour (its pull request closed before a runner took it) doesn't count."""
    for repo in OWNER_OF:
        if not has_ci(repo):
            continue
        for status in ("running", "waiting"):
            runs = forge("GET", f"/repos/{ORG}/{repo}/actions/runs?status={status}") or {}
            runs = runs.get("workflow_runs", []) if isinstance(runs, dict) else runs
            for r in runs or []:
                if r.get("status") != status:
                    continue
                if status == "running":
                    return True
                created = datetime.fromisoformat((r.get("created") or "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
                if now() - created.timestamp() < 3600:
                    return True
    return False


def check_idle():
    """Once the kit has gone quiet (no run, no queued work except an agent's
    held by its cap, which is the human's to lift, the human active nowhere,
    no CI, and nothing new for IDLE seconds): wake whoever should move on
    any stalled item, or if none, the coordinator to tell the human what it
    needs from them. Once per quiet spell."""
    if not COORDINATOR or IDLE <= 0 or paused():
        return
    if q("SELECT 1 FROM runs WHERE ended IS NULL"):
        return
    if any(not capped(r["agent"]) for r in q("SELECT DISTINCT agent FROM triggers WHERE run_id IS NULL")):
        return
    if any(human_active(a) for a in AGENTS):
        return
    last = last_activity()
    if now() - last < IDLE or float(kv_get("idle-summary", 0)) >= last:
        return
    if ci_running():
        return
    kv_set("idle-summary", now())
    # Stalled work first: the summary waits for a quiet spell with none.
    if check_stalls():
        return
    n = waiting_on_human()
    waiting = f"{n} item{'' if n == 1 else 's'} wait{'s' if n == 1 else ''} on you" if n else "nothing waits on you"
    add_trigger(COORDINATOR, f"idle:{int(now())}", f"{IDLE_TRIGGER}; {waiting}")


def idler():
    while True:
        time.sleep(60)
        try:
            check_idle()
        except Exception as e:
            log(f"checking for idleness failed: {e!r}")


def reconciler():
    while True:
        try:
            reconcile()
        except Exception as e:
            log(f"reconciliation failed: {e!r}")
        time.sleep(RECONCILE)


# --- Claims and runs ------------------------------------------------------------

def day_start():
    """Midnight UTC today: caps are per UTC day."""
    return now() // 86400 * 86400


def runs_today(agent):
    return q("SELECT COUNT(*) AS n FROM runs WHERE agent=? AND claimed>=?", agent, day_start())[0]["n"]


def spend_today(agent):
    return q("SELECT COALESCE(SUM(cost), 0) AS s FROM runs WHERE agent=? AND claimed>=?", agent, day_start())[0]["s"]


def cap_of(agent):
    return COORDINATOR_CAP if agent == COORDINATOR else CAP


def cap_setting(agent):
    return "MAX_COORDINATOR_RUNS_PER_DAY" if agent == COORDINATOR else "MAX_RUNS_PER_DAY"


def capped(agent):
    """Why the agent can't start another run today, or None."""
    if runs_today(agent) >= cap_of(agent):
        return f"its daily cap of {cap_of(agent)} runs"
    if SPEND_CAP and spend_today(agent) >= SPEND_CAP:
        return f"its daily spending cap of ${SPEND_CAP:.2f}"
    return None


def human_active(agent):
    return now() - float(kv_get(f"human:{agent}", 0)) < 90


def agent_state(agent):
    if q("SELECT 1 FROM runs WHERE agent=? AND ended IS NULL", agent):
        return "running"
    if paused():
        return "paused"
    if capped(agent):
        return "capped"
    if human_active(agent):
        return "human active"
    return "idle"


def claim(agent):
    """A run for the agent if its work is due, after checking that each
    item behind it still owes that work: dropped (and renamed, so the same
    state can queue it again should it come back) if not."""
    if not due(agent):
        return None
    rows = q("SELECT id, key FROM triggers WHERE agent=? AND run_id IS NULL", agent)
    stale = no_longer_owed(rows)
    if stale:
        q(f"UPDATE triggers SET run_id=-1, key = key || ':stale:' || CAST(? AS INTEGER) "
          f"WHERE run_id IS NULL AND id IN ({','.join('?' * len(stale))})", now(), *stale)
        log(f"dropped {len(stale)} trigger(s) for {agent} that are no longer owed")
    return due(agent, grant=True)


def due(agent, grant=False):
    """Whether the agent's work is due: no run, not paused, under its caps,
    and its newest trigger settled for DEBOUNCE (or its oldest waited
    MAX_DELAY). With grant, the run is started and returned."""
    with LOCK:
        if DB.execute("SELECT 1 FROM runs WHERE agent=? AND ended IS NULL", (agent,)).fetchone():
            return None
        if paused():
            return None
        rows = DB.execute("SELECT id, description, created FROM triggers WHERE agent=? AND run_id IS NULL "
                          "AND (not_before IS NULL OR not_before<=?) ORDER BY created", (agent, now())).fetchall()
        if not rows:
            return None
        why = capped(agent)
        if why:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            waiting = "\n".join(f"- {d}" for d in dict.fromkeys(r["description"] for r in rows))
            notify_agent(agent, f"cap:{agent}:{day}", "cap", f"{agent} reached {why} ({day})", {
                "Question": f"`{agent}` reached {why} at {iso(now())}, with {len(rows)} trigger(s) waiting. "
                            "Should it carry on today?",
                "Options": f"- Raise `{cap_setting(agent)}` (or `MAX_SPEND_PER_DAY`) in `.env` and rerun "
                           "`scripts/up.sh`: the waiting work starts within a minute.\n"
                           "- Leave it: the waiting work starts after midnight UTC.",
                "Recommendation": "Leave it unless the waiting work is urgent. If caps are reached most days, "
                                  "raise them.",
                "If you say go ahead": "Nothing changes: the waiting work starts after midnight UTC. "
                                       "Close this issue.",
                "Blocked on this": f"`{agent}`'s waiting work:\n{waiting}"})
            return None
        t = now()
        if t - rows[-1]["created"] < DEBOUNCE and t - rows[0]["created"] < MAX_DELAY:
            return None
        if not grant:
            return True
        descs = list(dict.fromkeys(r["description"] for r in rows))
        cur = DB.execute("INSERT INTO runs (agent, triggers, claimed) VALUES (?,?,?)",
                         (agent, json.dumps(descs), t))
        run_id = cur.lastrowid
        DB.execute(f"UPDATE triggers SET run_id=? WHERE id IN ({','.join('?' * len(rows))})",
                   (run_id, *[r["id"] for r in rows]))
    log(f"{agent} claimed run {run_id}: {', '.join(descs)}")
    return {"run": run_id, "triggers": descs}


def requeue(run_id):
    """The run's work is still to do, through no fault of the run (the runner
    restarted, or the kit was paused under it): queue it again as it was."""
    q("UPDATE triggers SET run_id=NULL WHERE run_id=?", run_id)


def retry(agent, run_id, outcome):
    """The run failed, timed out or exited early, so its work may be undone:
    most triggers are queued once, and nothing else would queue them again.
    Queue them again after a delay, up to RETRIES times; then put it to the
    human, since another try would most likely fail the same way. Returns
    whether they were queued again."""
    rows = q("SELECT description, attempts FROM triggers WHERE run_id=?", run_id)
    if not rows:
        return False
    attempt = max(r["attempts"] for r in rows) + 1
    if attempt <= RETRIES:
        q("UPDATE triggers SET run_id=NULL, attempts=?, not_before=? WHERE run_id=?",
          attempt, now() + RETRY_DELAY * attempt, run_id)
        log(f"{agent} run {run_id} {outcome}: its work is queued again (retry {attempt} of {RETRIES})")
        return True
    work = "\n".join(f"- {d}" for d in dict.fromkeys(r["description"] for r in rows))
    notify_agent(agent, f"retries:{agent}:{run_id}", "failed-run",
                 f"{agent}'s work failed {attempt} times (run {run_id})", {
        "Question": f"Run {run_id} of `{agent}` ended `{outcome}`, the last of {attempt} tries at the same work. "
                    "It won't be tried again by itself. What should happen to it?",
        "Options": f"- Find out why from the run's log (`scripts/activity.sh {agent}`), fix the cause, and "
                   f"continue the run yourself: `scripts/open.sh {agent}`, then resume it in the Claude "
                   "extension.\n"
                   "- Leave it: some of it (pull requests, untriaged requests, stalled work) is found again "
                   "by the dispatcher's reconciliation; the rest waits for the next event on it.",
        "Recommendation": "Find out why first: a run that fails every time is usually a cause outside it, such "
                          "as a credential, the run length (`MAX_RUN_MINUTES`) or the turn limit.",
        "If you say go ahead": "Nothing happens on its own. Close this issue once you've dealt with it.",
        "Blocked on this": f"`{agent}`'s work from that run:\n{work}"})
    return False


def end_run(agent, run_id, r):
    row = q("SELECT claimed FROM runs WHERE id=? AND agent=?", run_id, agent)
    if not row:
        return False
    t = now()
    changes = q("SELECT COUNT(*) AS n FROM events WHERE sender=? AND at>=? AND at<=?",
                agent, row[0]["claimed"], t + 5)[0]["n"]
    q("""UPDATE runs SET ended=?, outcome=?, cost=?, turns=?, denied=?, session_id=?, log=?,
         transcript=?, in_extension=?, forge_changes=?, archived_log=?, archived_transcript=?
         WHERE id=?""",
      t, r.get("outcome"), r.get("cost"), r.get("turns"), json.dumps(r.get("denied") or []),
      r.get("session_id"), r.get("log"), r.get("transcript"), int(bool(r.get("in_extension"))),
      changes, r.get("archived_log"), r.get("archived_transcript"), run_id)
    outcome = r.get("outcome") or ""
    if paused() and outcome != "interrupted":
        requeue(run_id)  # the pause refused its tool calls: it did what it could, not its work
    elif outcome in ("completed", "interrupted") or not retry(agent, run_id, outcome):
        # Done; or stopped by the human, who may continue it; or given up on.
        # A retried run may still push, so its reviews wait for that.
        release_reviews(agent)
        later(evaluate_own_prs, agent)
    if not r.get("in_extension") and r.get("session_id"):
        sid = r.get("session_id")
        notify_agent(agent, f"not-listed:{agent}:{run_id}", "transcript-rewrite",
                     f"Run {run_id} of {agent} isn't listed in the Claude extension", {
            "Question": f"Run {run_id} of `{agent}` isn't listed in the Claude extension in its container, because "
                        "its transcript couldn't be rewritten. Do you need to continue that conversation?",
            "Options": f"- Resume it by its session ID: `claude --resume {sid}` in `{agent}`'s container.\n"
                       "- Leave it: its log and transcript are in the run archive.",
            "Recommendation": "Leave it unless you need that conversation. If this keeps happening, the "
                              "runner's transcript rewrite needs fixing in the kit.",
            "If you say go ahead": "Nothing happens. Close this issue.",
            "Blocked on this": "Nothing."})
    log(f"{agent} run {run_id} ended: {r.get('outcome')}")
    return True


def status():
    agents = {}
    for name in AGENTS:
        pending = q("SELECT description, created, not_before FROM triggers WHERE agent=? AND run_id IS NULL ORDER BY created", name)
        agents[name] = {"state": agent_state(name), "runs_today": runs_today(name), "cap": cap_of(name),
                        "spend_today": round(spend_today(name), 2), "spend_cap": SPEND_CAP or None,
                        "pending": [{"trigger": p["description"], "since": iso(p["created"]),
                                     "after": iso(p["not_before"])} for p in pending]}
    runs = [dict(r, claimed=iso(r["claimed"]), started=iso(r["started"]), ended=iso(r["ended"]),
                 triggers=json.loads(r["triggers"] or "[]"), denied=json.loads(r["denied"] or "[]"))
            for r in q("SELECT * FROM runs ORDER BY id DESC LIMIT 50")]
    sessions = [dict(s, started=iso(s["started"]), ended=iso(s["ended"]))
                for s in q("SELECT * FROM sessions ORDER BY id DESC LIMIT 20")]
    notices = [dict(n, at=iso(n["at"])) for n in q("SELECT * FROM notices ORDER BY id DESC LIMIT 50")]
    reconciled = kv_get("reconciled")
    return {"paused": paused(), "reconciled": iso(float(reconciled)) if reconciled else None,
            "agents": agents, "runs": runs, "sessions": sessions, "notices": notices,
            "summary": kit_summary()}


def kit_summary():
    """One line: whether the kit will make progress by itself, or nothing
    happens until the human acts. The same test as check_idle, without its
    wait for quiet."""
    try:
        waiting = waiting_on_human()
        ci = ci_running()
    except Exception as e:
        return f"Kit: can't tell ({e!r} from Forgejo)"
    wait = (f"{waiting} item{'' if waiting == 1 else 's'} wait{'s' if waiting == 1 else ''} on you"
            if waiting else "nothing waits on you")
    if paused():
        return f"Kit: paused, nothing starts until scripts/resume.sh; {wait}"
    running = [r["agent"] for r in q("SELECT agent FROM runs WHERE ended IS NULL ORDER BY id")]
    queued = q("SELECT agent, not_before FROM triggers WHERE run_id IS NULL")
    capped_agents = sorted({t["agent"] for t in queued if capped(t["agent"])})
    queued = [t for t in queued if t["agent"] not in capped_agents]
    later = [t for t in queued if t["not_before"] and t["not_before"] > now()]
    yours = [a for a in AGENTS if human_active(a)]
    work = []
    if running:
        work.append(f"{len(running)} running ({', '.join(running)})")
    if queued:
        held = f", {len(later)} of them not yet due" if later else ""
        work.append(f"{len(queued)} queued for {', '.join(sorted({t['agent'] for t in queued}))}{held}")
    if ci:
        work.append("CI running")
    extra = []
    if capped_agents:
        extra.append(f"capped with work waiting: {', '.join(capped_agents)} (raise its cap, or it waits for midnight UTC)")
    if yours:
        extra.append(f"you're active in {', '.join(yours)}, whose runs wait for you")
    tail = "".join(f"; {x}" for x in extra)
    if work:
        return f"Kit: working: {', '.join(work)}; {wait}{tail}"
    if waiting or capped_agents:
        return f"Kit: quiet, nothing happens until you act; {wait}{tail}"
    return (f"Kit: quiet, and nothing waits on you: once it has been quiet for {IDLE // 60} minutes, whoever "
            f"should move on any open item that is waiting for nothing is woken, or if there is none, the "
            f"coordinator summarises{tail}")


def notices_text(after):
    """One notice per line: id, kind, where, text; tabs and newlines removed."""
    out = []
    for n in q("SELECT * FROM notices WHERE id>? ORDER BY id", after):
        where = f"{n['repo']}#{n['number']}" if n["number"] else (n["repo"] or "")
        clean = lambda s: " ".join(str(s or "").split())
        out.append(f"{n['id']}\t{n['kind']}\t{where}\t{clean(n['text'])}")
    return "\n".join(out) + ("\n" if out else "")


# --- Activity -------------------------------------------------------------------

def event_detail(event_type, payload, repo, number):
    """What the activity stream says about an event beyond its type: read
    from the payload when it arrives, since payloads aren't all kept."""
    d = {}
    item = payload.get("pull_request") or payload.get("issue") or {}
    if item.get("title"):
        d["title"] = " ".join(item["title"].split())[:80]
    if event_type in ("issue_label", "pull_request_label") and number:
        labels = {l["name"] for l in item.get("labels") or []}
        with LOCK:
            row = DB.execute("SELECT labels FROM labels WHERE repo=? AND number=?", (repo, number)).fetchone()
        if row:
            before = set(json.loads(row["labels"]))
            d["added"], d["removed"] = sorted(labels - before), sorted(before - labels)
        else:
            d["labels"] = sorted(labels)
    if payload.get("pull_request") and payload.get("action") == "closed":
        d["merged"] = bool(payload["pull_request"].get("merged"))
    if payload.get("requested_reviewer"):
        d["reviewer"] = payload["requested_reviewer"].get("login")
    if payload.get("comment"):
        d["no_reply"] = not wants_reply(payload["comment"].get("body"))
    if event_type.startswith("pull_request_review") and isinstance(payload.get("review"), dict):
        d["no_reply"] = not wants_reply(payload["review"].get("content"))
    if payload.get("ref"):
        d["ref"] = payload["ref"].removeprefix("refs/heads/").removeprefix("refs/tags/")
        if payload.get("ref_type"):
            d["ref_type"] = payload["ref_type"]
        if "total_commits" in payload or "commits" in payload:
            d["commits"] = payload.get("total_commits") or len(payload.get("commits") or [])
    if (payload.get("release") or {}).get("tag_name"):
        d["tag"] = payload["release"]["tag_name"]
    run = payload.get("run")
    if isinstance(run, dict):
        d["sha"] = (run.get("commit_sha") or "")[:12]
        d["workflow"] = run.get("workflow_id")
        try:
            d["pr"] = (json.loads(run.get("event_payload") or "{}").get("pull_request") or {}).get("number")
        except ValueError:
            pass
        if not d.get("pr"):
            d["ref"] = run.get("prettyref") or ""
    return d


def describe_event(e):
    d = json.loads(e["detail"] or "{}")
    t, a, repo, n = e["type"], e["action"], e["repo"] or "?", e["number"]
    where = f"{repo}#{n}" if n else repo
    title = f" “{d['title']}”" if d.get("title") else ""
    quiet = " (no reply needed)" if d.get("no_reply") else ""
    if t == "push":
        c = d.get("commits")
        return f"pushed {c} commit{'' if c == 1 else 's'} to {repo} {d.get('ref', '')}" if c is not None else f"pushed to {repo}"
    if t in ("create", "delete"):
        if not d.get("ref"):
            return f"{t}d a branch or tag in {repo}"
        return f"{t}d {d.get('ref_type', 'ref')} {repo} {d['ref']}"
    if t == "issues":
        return f"{a} issue {where}{title if a == 'opened' else ''}"
    if t in ("issue_label", "pull_request_label"):
        if "labels" in d:
            return f"labels on {where}: {', '.join(d['labels']) or 'none'}"
        changes = [f"+{l}" for l in d.get("added", [])] + [f"-{l}" for l in d.get("removed", [])]
        return f"labelled {where}: {' '.join(changes)}" if changes else f"labelled {where}"
    if t == "pull_request_review_request":
        if not d.get("reviewer"):
            return f"requested a review on {where}"
        return f"asked {'you' if d['reviewer'] == HUMAN else d['reviewer']} to review {where}"
    if t == "pull_request_sync":
        return f"pushed to pull request {where}"
    if t == "pull_request":
        if a == "closed":
            if "merged" not in d:
                return f"closed pull request {where}"
            return f"merged pull request {where}{title}" if d["merged"] else f"closed pull request {where} unmerged"
        return f"{a} pull request {where}{title if a == 'opened' else ''}"
    if t == "pull_request_review_approved":
        return f"approved {where}{quiet}"
    if t == "pull_request_review_rejected":
        return f"requested changes on {where}"
    if t in ("pull_request_review_comment", "pull_request_comment", "issue_comment"):
        return f"commented on {where}{quiet}"
    if t in ("action_run_success", "action_run_failure"):
        on = f"{repo}#{d['pr']}" if d.get("pr") else f"{repo} {d.get('ref', '')}".strip()
        on = f" on {on}" if e["repo"] else ""  # runs recorded before their repository was
        sha = f" at {d['sha']}" if d.get("sha") else ""
        return f"CI {'passed' if t.endswith('success') else 'failed'}{on}{sha}"
    if t == "release":
        return f"released {repo} {d['tag']}" if d.get("tag") else f"published a release of {repo}"
    if t in ("issue_assign", "pull_request_assign"):
        return f"{a} {where}"
    return f"{t} {a or ''} {where}".replace("  ", " ")


def activity(after=0.0, limit=40, tz=0):
    """The kit's activity, oldest first: Forgejo events, queued work, runs,
    the human's sessions and notices. Entries after `after` (epoch seconds),
    at most `limit`, the newest kept; tz is the reader's offset in seconds,
    for the times inside entries."""
    def hm(t):
        return time.strftime("%H:%M", time.gmtime(t + tz))
    you = lambda who: "you" if who == HUMAN else (who or "?")
    out = []
    for e in q("SELECT * FROM events WHERE at>? ORDER BY at DESC LIMIT ?", after, limit):
        who = "ci" if e["type"].startswith("action_run") else you(e["sender"])
        out.append({"id": f"e{e['id']}", "at": e["at"], "who": who, "text": describe_event(e)})
    for t in q("SELECT * FROM triggers WHERE created>? ORDER BY created DESC LIMIT ?", after, limit):
        wait = f" (from {hm(t['not_before'])})" if t["not_before"] and t["not_before"] > t["created"] + 1 else ""
        out.append({"id": f"t{t['id']}", "at": t["created"], "who": t["agent"], "text": f"queued: {t['description']}{wait}"})
    for r in q("SELECT * FROM runs WHERE claimed>? OR ended>? ORDER BY id DESC LIMIT ?", after, after, limit):
        if r["claimed"] and r["claimed"] > after:
            out.append({"id": f"r{r['id']}s", "at": r["claimed"], "who": r["agent"],
                        "text": f"run {r['id']} started: {'; '.join(json.loads(r['triggers'] or '[]'))}"})
        if r["ended"] and r["ended"] > after:
            bits = [r["outcome"] or "ended"]
            if r["cost"] is not None:
                bits.append(f"${r['cost']:.2f}")
            if r["forge_changes"] is not None:
                bits.append(f"{r['forge_changes']} Forgejo change{'' if r['forge_changes'] == 1 else 's'}")
            denied = len(json.loads(r["denied"] or "[]"))
            if denied:
                bits.append(f"{denied} denied")
            out.append({"id": f"r{r['id']}e", "at": r["ended"], "who": r["agent"],
                        "text": f"run {r['id']} ended: {', '.join(bits)}"})
    for s in q("SELECT * FROM sessions WHERE started>? OR ended>? ORDER BY id DESC LIMIT ?", after, after, limit):
        if s["started"] and s["started"] > after:
            out.append({"id": f"s{s['id']}s", "at": s["started"], "who": "you",
                        "text": f"session started in {s['agent']}'s container: {s['agent']} starts no run while you're active"})
        if s["ended"] and s["ended"] > after:
            out.append({"id": f"s{s['id']}e", "at": s["ended"], "who": "you", "text": f"session ended in {s['agent']}'s container"})
    for n in q("SELECT * FROM notices WHERE at>? ORDER BY id DESC LIMIT ?", after, limit):
        where = f"{n['repo']}#{n['number']}" if n["number"] else (n["repo"] or "")
        out.append({"id": f"n{n['id']}", "at": n["at"], "who": "kit", "text": f"notice for you ({n['kind']}): {where}"})
    out.sort(key=lambda x: x["at"])
    return out[-limit:]


# --- HTTP -----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("too large")
        return self.rfile.read(length)

    def json_body(self):
        raw = self.body()
        return json.loads(raw) if raw.strip() else {}

    def bearer(self):
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.startswith("Bearer ") else ""

    def do_POST(self):
        try:
            if self.path == "/hook":
                return self.hook()
            if self.path.startswith("/admin/"):
                return self.admin()
            agent = agent_for_key(self.bearer())
            if not agent or agent not in AGENTS:
                return self.reply(403, {"error": "unknown runner key"})
            data = self.json_body()
            parts = self.path.strip("/").split("/")
            if self.path == "/claim":
                c = claim(agent)
                return self.reply(200, c) if c else self.reply(204, None)
            if self.path == "/heartbeat":
                if data.get("human_active"):
                    kv_set(f"human:{agent}", now())
                return self.reply(200, {})
            if self.path == "/runs/abandon":
                for r in q("SELECT id FROM runs WHERE agent=? AND ended IS NULL", agent):
                    q("UPDATE runs SET ended=?, outcome=? WHERE id=?", now(),
                      "abandoned: the runner restarted", r["id"])
                    requeue(r["id"])
                return self.reply(200, {})
            if len(parts) == 3 and parts[0] == "runs" and parts[1].isdigit() and parts[2] == "start":
                q("UPDATE runs SET started=?, session_id=?, log=? WHERE id=? AND agent=?",
                  now(), data.get("session_id"), data.get("log"), int(parts[1]), agent)
                return self.reply(200, {})
            if len(parts) == 3 and parts[0] == "runs" and parts[1].isdigit() and parts[2] == "end":
                ok = end_run(agent, int(parts[1]), data)
                return self.reply(200 if ok else 404, {})
            if self.path == "/sessions":
                at = data.get("at") or now()
                if data.get("event") == "start":
                    q("INSERT INTO sessions (agent, session_id, started, transcript) VALUES (?,?,?,?)",
                      agent, data.get("session"), at, data.get("transcript"))
                else:
                    if not q("SELECT 1 FROM sessions WHERE agent=? AND session_id=? AND ended IS NULL",
                             agent, data.get("session")):
                        q("INSERT INTO sessions (agent, session_id, transcript) VALUES (?,?,?)",
                          agent, data.get("session"), data.get("transcript"))
                    q("UPDATE sessions SET ended=?, transcript=? WHERE agent=? AND session_id=? AND ended IS NULL",
                      at, data.get("transcript"), agent, data.get("session"))
                return self.reply(200, {})
            self.reply(404, {"error": "not found"})
        except Exception as e:
            log(f"error handling {self.path}: {e!r}")
            self.reply(500, {"error": "internal error"})

    def admin(self):
        try:
            key = secret("admin-key")
        except OSError:
            return self.reply(403, {"error": "no admin key"})
        if not hmac.compare_digest(self.bearer(), key):
            return self.reply(403, {"error": "bad admin key"})
        data = self.json_body()
        if self.path == "/admin/drop":
            agent = data.get("agent")
            n = q("SELECT COUNT(*) AS n FROM triggers WHERE agent=? AND run_id IS NULL", agent)[0]["n"]
            q("UPDATE triggers SET run_id=-1 WHERE agent=? AND run_id IS NULL", agent)
            log(f"dropped {n} pending trigger(s) for {agent}")
            return self.reply(200, {"dropped": n})
        if self.path == "/admin/reconcile":
            reconcile()
            return self.reply(200, {})
        self.reply(404, {"error": "not found"})

    def hook(self):
        body = self.body()
        given = self.headers.get("X-Forgejo-Signature") or ""
        expected = hmac.new(secret("webhook-secret").encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(given, expected):
            log("refused a delivery with a bad signature")
            return self.reply(403, {"error": "bad signature"})
        try:
            payload = json.loads(body)
        except ValueError:
            return self.reply(400, {"error": "not JSON"})
        event = self.headers.get("X-Forgejo-Event") or "?"
        event_type = self.headers.get("X-Forgejo-Event-Type") or event
        run = payload.get("run")
        if isinstance(run, dict):
            # An Actions run carries its repository inside the run, and has no sender.
            payload.setdefault("repository", run.get("repository") or {})
            payload.setdefault("sender", {"login": "ci"})
        delivery = "".join(c for c in (self.headers.get("X-Forgejo-Delivery") or str(time.time_ns()))
                           if c.isalnum() or c == "-")
        repo = (payload.get("repository") or {}).get("name")
        sender = (payload.get("sender") or {}).get("login")
        number = (payload.get("issue") or payload.get("pull_request") or {}).get("number")
        t = now()
        try:
            detail = json.dumps(event_detail(event_type, payload, repo, number))
        except Exception as e:
            detail = None
            log(f"couldn't summarise a {event_type} event: {e!r}")
        q("INSERT INTO events (at, event, type, action, repo, sender, number, delivery, detail) "
          "VALUES (?,?,?,?,?,?,?,?,?)",
          t, event, event_type, payload.get("action"), repo, sender, number, delivery, detail)
        self.save(delivery, body, {"at": iso(t), "event": event, "type": event_type, "delivery": delivery,
                                   "repo": repo, "action": payload.get("action"), "sender": sender, "number": number})
        payload["_delivery"] = delivery
        # Answer at once; handling may call Forgejo, which is waiting on us.
        self.reply(200, {})
        try:
            # Reviews arrive as pull_request events typed by verdict; a review
            # request is a pull_request event of its own.
            review = event_type.startswith("pull_request_review") and event_type != "pull_request_review_request"
            handle(event_type if review else event, payload)
        except Exception as e:
            log(f"error handling a {event_type} event: {e!r}")

    def save(self, delivery, body, entry):
        payloads = STATE / "payloads"
        payloads.mkdir(exist_ok=True)
        (payloads / f"{delivery}.json").write_bytes(body)
        for p in sorted(payloads.iterdir(), key=lambda p: p.stat().st_mtime)[:-KEEP_PAYLOADS]:
            p.unlink(missing_ok=True)
        with open(STATE / "events.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/healthz":
            return self.reply(200, {"ok": True})
        if url.path == "/status":
            return self.reply(200, status())
        if url.path == "/ledger":
            # The run ledger, for the process review only.
            if agent_for_key(self.bearer()) != "process":
                return self.reply(403, {"error": "only the process review reads the ledger"})
            since = float((urllib.parse.parse_qs(url.query).get("since") or ["0"])[0] or 0)
            runs = [dict(r, claimed=iso(r["claimed"]), started=iso(r["started"]), ended=iso(r["ended"]),
                         triggers=json.loads(r["triggers"] or "[]"), denied=json.loads(r["denied"] or "[]"))
                    for r in q("SELECT * FROM runs WHERE claimed>=? ORDER BY id", since)]
            sessions = [dict(s, started=iso(s["started"]), ended=iso(s["ended"]))
                        for s in q("SELECT * FROM sessions WHERE COALESCE(started, ended)>=? ORDER BY id", since)]
            notices = [dict(n, at=iso(n["at"])) for n in q("SELECT * FROM notices WHERE at>=? ORDER BY id", since)]
            return self.reply(200, {"runs": runs, "sessions": sessions, "notices": notices})
        if url.path == "/activity":
            qs = urllib.parse.parse_qs(url.query)
            arg = lambda k, d: (qs.get(k) or [d])[0] or d
            return self.reply(200, activity(float(arg("after", "0")), int(arg("limit", "40")), int(arg("tz", "0"))))
        if url.path == "/notices.txt":
            after = int((urllib.parse.parse_qs(url.query).get("after") or ["0"])[0] or 0)
            return self.reply_text(200, notices_text(after))
        self.reply(404, {"error": "not found"})

    def reply(self, code, obj):
        data = b"" if obj is None else (json.dumps(obj, indent=1) + "\n").encode()
        self.send_response(code)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def reply_text(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    log(f"listening on :8080; agents: {', '.join(AGENTS)}")
    threading.Thread(target=reconciler, daemon=True).start()
    threading.Thread(target=idler, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
