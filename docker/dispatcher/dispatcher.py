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

A claim is granted only when the agent has no run, is under its daily caps,
the kit isn't paused, and its newest trigger has settled for the debounce
period (or its oldest has waited the maximum delay), so a burst of events
becomes one run.

Every RECONCILE_SECONDS, and on start, it checks Forgejo for work a missed
webhook would have lost: untriaged requests, failed Actions runs on agents'
pull requests, and agents' pull requests behind main.

Once the kit has been quiet for IDLE_SECONDS (no run, no queued work, no CI),
it wakes the coordinator to tell the human what it needs from them, once per
quiet spell.
"""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
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
""")
for column in ("triggers ADD COLUMN not_before REAL",  # ledgers from before stage 5
               "events ADD COLUMN detail TEXT"):     # ledgers from before the activity stream
    try:
        DB.execute(f"ALTER TABLE {column}")
    except sqlite3.OperationalError:
        pass


def q(sql, *args):
    with LOCK:
        return DB.execute(sql, args).fetchall()


# Once: conformance reviews that the reconciler dropped by mistake (before
# this was fixed, it dropped a review still settling, which `once` then never
# queued again). Renamed rather than deleted, so the history stays, they no
# longer stop the reconciler queueing the review again where it's still due.
if not q("SELECT 1 FROM kv WHERE key='repaired-conformance-drops'"):
    with LOCK:
        DB.execute("UPDATE triggers SET key = key || ':dropped' WHERE key LIKE 'conf:%' AND run_id=-1 "
                   "AND key NOT LIKE '%:dropped'")
        DB.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('repaired-conformance-drops', '1')")


def kv_get(key, default=None):
    row = q("SELECT value FROM kv WHERE key=?", key)
    return row[0]["value"] if row else default


def kv_set(key, value):
    q("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", key, str(value))


def add_trigger(agent, key, description, once=False, recent=0, not_before=None):
    """Queue work for an agent, unless the same key is already waiting; with
    once, unless it was ever queued; with recent, unless it was queued in the
    last `recent` seconds. With not_before, it waits until then to be claimed."""
    if agent not in AGENTS:
        return
    with LOCK:
        if once:
            dup = DB.execute("SELECT 1 FROM triggers WHERE agent=? AND key=?", (agent, key)).fetchone()
        elif recent:
            dup = DB.execute("SELECT 1 FROM triggers WHERE agent=? AND key=? AND (run_id IS NULL OR created>?)",
                             (agent, key, now() - recent)).fetchone()
        else:
            dup = DB.execute("SELECT 1 FROM triggers WHERE agent=? AND key=? AND run_id IS NULL",
                             (agent, key)).fetchone()
        if dup:
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


# --- Webhooks -> triggers -------------------------------------------------------

def first_line(text):
    return ((text or "").strip().splitlines() or [""])[0].strip()


def wants_reply(body):
    return not first_line(body).startswith(NO_REPLY)


def labels_changed(repo, number, labels):
    """Labels added and removed since this issue was last seen."""
    with LOCK:
        row = DB.execute("SELECT labels FROM labels WHERE repo=? AND number=?", (repo, number)).fetchone()
        DB.execute("INSERT OR REPLACE INTO labels (repo, number, labels) VALUES (?,?,?)",
                   (repo, number, json.dumps(sorted(labels))))
    before = set(json.loads(row["labels"])) if row else set()
    return set(labels) - before, before - set(labels)


def project_repo(repo):
    return repo.startswith(PREFIX)


def pr_behind(repo, pr):
    """Whether main has commits the pull request's branch lacks."""
    cmp = forge("GET", f"/repos/{ORG}/{repo}/compare/{pr['head']['sha']}...main")
    return (cmp or {}).get("total_commits", 0) > 0


def check_behind(repo, main_sha=""):
    owner = OWNER_OF.get(repo)
    if not owner:
        return
    main_sha = main_sha or forge("GET", f"/repos/{ORG}/{repo}/branches/main")["commit"]["id"]
    for pr in forge_list(f"/repos/{ORG}/{repo}/pulls?state=open"):
        if pr["user"]["login"] == owner and pr_behind(repo, pr):
            add_trigger(owner, f"behind:{repo}#{pr['number']}:{main_sha[:12]}",
                        f"pull request {repo}#{pr['number']} is behind main", once=True)


def wake_blocked(match, what):
    """Wake every agent with an open `blocked` issue whose Blocked-by: line
    matches (repo, number or None)."""
    dep_repo, dep_n = match
    for repo, owner in OWNER_OF.items():
        try:
            issues = forge_list(f"/repos/{ORG}/{repo}/issues?state=open&type=issues&labels=blocked")
        except Exception as e:
            log(f"couldn't read {repo}'s blocked issues: {e}")
            continue
        for issue in issues:
            texts = [issue.get("body") or ""] if issue["user"]["login"] in TRUSTED else []
            texts += [c.get("body") or "" for c in forge_list(f"/repos/{ORG}/{repo}/issues/{issue['number']}/comments", 100)
                      if c["user"]["login"] in TRUSTED]
            for org, r, n in BLOCKED_BY.findall("\n".join(texts)):
                if org.lower() == ORG.lower() and r == dep_repo and (dep_n is None or int(n) == dep_n):
                    add_trigger(owner, f"unblocked:{repo}#{issue['number']}:{what}",
                                f"{what}; {repo}#{issue['number']} is blocked on it", once=True)
                    break


def wip(title):
    t = (title or "").strip().lower()
    return t.startswith(("wip", "[wip]", "draft"))


def drop_pending(agent, prefix):
    q("UPDATE triggers SET run_id=-1 WHERE agent=? AND run_id IS NULL AND key LIKE ?", agent, prefix + "%")


def drop_reviews_of_closed(repo, n):
    """A pull request merged or closed before its reviews ran: drop them.
    Renamed as well, since each is queued once per head, so that reopening
    the pull request can queue them again."""
    for agent, prefix in ((REVIEWER, f"conf:{repo}#{n}:"), (COORDINATOR, f"review-requested:{repo}#{n}:")):
        q("UPDATE triggers SET run_id=-1, key = key || ':closed:' || CAST(? AS INTEGER) "
          "WHERE agent=? AND run_id IS NULL AND key LIKE ?", now(), agent, prefix + "%")


def has_ci(repo):
    return repo != COORDINATION


def queue_conformance(repo, n, sha=None):
    """Work for conformance: review pull request n at its head, once that head
    has settled and if it hasn't already reviewed it. Pull requests marked as
    work in progress are skipped.

    An agent pushes only during a run, so a head whose author is an agent with
    no run under way, and whose container the human isn't using, has settled
    already. Otherwise the review waits for the author's run to end
    (release_reviews), or SETTLE seconds at most: the human's own pull
    requests, and runs that go on, still get a review."""
    if REVIEWER not in AGENTS:
        return
    pr = forge("GET", f"/repos/{ORG}/{repo}/pulls/{n}")
    head = pr["head"]["sha"]
    if pr["state"] != "open" or wip(pr.get("title")) or (sha and sha != head):
        return
    key = f"conf:{repo}#{n}:{head[:12]}"
    # Reviews waiting for an older head are superseded. The one for this head
    # stays: dropping it would end it for good, since it's queued only once,
    # and the reconciler calls this for every open pull request.
    q("UPDATE triggers SET run_id=-1 WHERE agent=? AND run_id IS NULL AND key LIKE ? AND key<>?",
      REVIEWER, f"conf:{repo}#{n}:%", key)
    if any(r["user"]["login"] == REVIEWER and r.get("commit_id") == head
           for r in forge_list(f"/repos/{ORG}/{repo}/pulls/{n}/reviews")):
        drop_pending(REVIEWER, key)
        return
    author = (pr.get("user") or {}).get("login")
    settled = author in AGENTS and not may_push(author)
    add_trigger(REVIEWER, key, f"review pull request {repo}#{n} at {head[:12]}",
                once=True, not_before=None if settled else now() + SETTLE)
    if settled:  # queued while its author could still push
        q("UPDATE triggers SET not_before=NULL WHERE agent=? AND key=? AND run_id IS NULL", REVIEWER, key)


def may_push(agent):
    """Whether an agent could still push: a run under way, or the human
    active in its container."""
    return bool(q("SELECT 1 FROM runs WHERE agent=? AND ended IS NULL", agent)) or human_active(agent)


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
        if (pr.get("user") or {}).get("login") == agent:
            q("UPDATE triggers SET not_before=NULL WHERE id=?", t["id"])
            log(f"{agent}'s run ended: conformance may review {m[1]}#{m[2]} now")


def ci_state(repo, sha):
    """success, failure, running or none: the Actions runs for sha."""
    runs = (forge("GET", f"/repos/{ORG}/{repo}/actions/runs?head_sha={sha}") or {}).get("workflow_runs") or []
    states = {r.get("status") for r in runs if r.get("commit_sha") == sha}
    if not states:
        return "none"
    if states == {"success"}:
        return "success"
    return "failure" if states & {"failure", "cancelled", "skipped", "blocked"} else "running"


def queue_coordinator_review(repo, n, sha=None):
    """Work for the coordinator: a pull request that requests its review (at
    level 3, a change to API.lock), once CI has passed on the head and if it
    hasn't approved that head. A new commit dismisses its approval, so the
    next head that passes CI wakes it again."""
    if not COORDINATOR:
        return
    pr = forge("GET", f"/repos/{ORG}/{repo}/pulls/{n}")
    head = pr["head"]["sha"]
    if pr["state"] != "open" or (sha and sha != head):
        return
    if COORDINATOR not in {u["login"] for u in pr.get("requested_reviewers") or []}:
        return
    if any(r["user"]["login"] == COORDINATOR and r.get("state") == "APPROVED" and r.get("commit_id") == head
           and not r.get("dismissed") for r in forge_list(f"/repos/{ORG}/{repo}/pulls/{n}/reviews")):
        return
    if has_ci(repo) and ci_state(repo, head) != "success":
        return
    add_trigger(COORDINATOR, f"review-requested:{repo}#{n}:{head[:12]}",
                f"review requested from you on pull request {repo}#{n}, whose CI passed at {head[:12]}", once=True)
    # A pull request opened lately shares this run rather than having its own.
    q("UPDATE triggers SET not_before=NULL WHERE agent=? AND key=? AND run_id IS NULL",
      COORDINATOR, f"pr-opened:{repo}#{n}")


def release_opened(repo, n):
    """CI has finished on pull request n, so whether it asks for the
    coordinator's review is known: if it does, that review was queued and
    shares the run; if not, none is coming. Either way, "pull request opened"
    need hold no longer."""
    if COORDINATOR:
        q("UPDATE triggers SET not_before=NULL WHERE agent=? AND key=? AND run_id IS NULL",
          COORDINATOR, f"pr-opened:{repo}#{n}")


REFS = re.compile(r"(?im)^\s*refs:?\s+(.+)$")
REF = re.compile(r"(?:([\w.-]+)/([\w.-]+))?#(\d+)")


def requests_awaiting_release(repo, pr):
    """The open requests on repo that a merged pull request refers to with
    `Refs` (in its description or its commits) and that aren't yet labelled
    ready-for-release: their owner has that step still to do."""
    texts = [pr.get("body") or ""]
    try:
        texts += [(c.get("commit") or {}).get("message") or ""
                  for c in forge_list(f"/repos/{ORG}/{repo}/pulls/{pr['number']}/commits")]
    except Exception as e:
        log(f"reading {repo}#{pr['number']}'s commits failed: {e!r}")
    numbers = set()
    for text in texts:
        for line in REFS.findall(text):
            for org, r, k in REF.findall(line):
                if (not org or (org.lower() == ORG.lower() and r == repo)) and int(k) != pr["number"]:
                    numbers.add(int(k))
    found = []
    for k in sorted(numbers):
        try:
            issue = forge("GET", f"/repos/{ORG}/{repo}/issues/{k}") or {}
        except Exception:
            continue
        labels = {l["name"] for l in issue.get("labels") or []}
        if (issue.get("state") == "open" and not issue.get("pull_request")
                and "request" in labels and "ready-for-release" not in labels):
            found.append(k)
    return found


def wake_after_merge(repo, owner, pr):
    if not owner or (pr.get("user") or {}).get("login") != owner:
        return
    waiting = requests_awaiting_release(repo, pr)
    if waiting:
        n = pr["number"]
        refs = ", ".join(f"{repo}#{k}" for k in waiting)
        add_trigger(owner, f"merged:{repo}#{n}",
                    f"your pull request {repo}#{n} merged; it answers {refs}, not yet labelled ready-for-release",
                    once=True)


def conformance_awaits_reply(repo, n):
    """conformance's last review asked for changes at the pull request's
    current head, so an author's reply is its to read now. After a push the
    head has moved on: the review queued for the new head (once CI passes)
    reads the reply, and waking conformance before then is a run that can't
    review."""
    mine = [r for r in forge_list(f"/repos/{ORG}/{repo}/pulls/{n}/reviews") if r["user"]["login"] == REVIEWER]
    if not mine or mine[-1].get("state") != "REQUEST_CHANGES":
        return False
    pr = forge("GET", f"/repos/{ORG}/{repo}/pulls/{n}") or {}
    return mine[-1].get("commit_id") == (pr.get("head") or {}).get("sha")


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


def check_released_requests(repo):
    """Open requests released but not verified: the requester is woken again
    after NUDGE seconds; after STALL, the stall is the human's."""
    for issue in forge_list(f"/repos/{ORG}/{repo}/issues?state=open&type=issues&labels=request"):
        labels = {l["name"] for l in issue.get("labels") or []}
        if "verified" in labels:
            continue
        n = issue["number"]
        released = [(c, RELEASED.search(c.get("body") or "")) for c in
                    forge_list(f"/repos/{ORG}/{repo}/issues/{n}/comments")
                    if (c.get("user") or {}).get("login") in ("gatekeeper", HUMAN, OWNER_OF.get(repo))]
        released = [(c, m) for c, m in released if m]
        if not released:
            continue
        c, m = released[-1]
        tag = m[1]
        age = now() - datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp()
        requester = (issue.get("user") or {}).get("login") or "?"
        if age > NUDGE and requester in AGENTS and AGENTS[requester]["own"]:
            add_trigger(requester, f"unverified:{repo}#{n}:{tag}",
                        f"your request {repo}#{n} was released in {tag} {int(age // 3600)} hours ago and you "
                        "haven't verified it: upgrade, check its acceptance criteria and verify it, or say on "
                        "the issue what stops you", once=True)
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


def handle(event, payload):
    repo = (payload.get("repository") or {}).get("name") or ""
    sender = (payload.get("sender") or {}).get("login") or ""
    if ((payload.get("repository") or {}).get("owner") or {}).get("login", ORG).lower() != ORG.lower():
        return
    if not project_repo(repo):
        return
    owner = OWNER_OF.get(repo)
    trusted = sender in TRUSTED
    action = payload.get("action")
    coord = COORDINATOR if COORDINATOR and sender != COORDINATOR else None

    if event == "issues":
        issue = payload.get("issue") or {}
        n = issue.get("number")
        labels = {l["name"] for l in issue.get("labels") or []}
        added, removed = labels_changed(repo, n, labels)
        author = (issue.get("user") or {}).get("login")
        if not trusted:
            return
        is_pr = bool(issue.get("pull_request"))
        kind = "pull request" if is_pr else "issue"
        labels, added = human_labels_changed(repo, n, issue, labels, added, removed, sender)
        if "blocking" in added and "upstream" in labels:
            notify(f"blocking:{repo}#{n}", "blocking", repo, n,
                   "this upstream issue is marked `blocking`: an agent can't continue until it's resolved upstream. "
                   "Close it or comment on it once a fix is available.")
        # A recommendation's needs-human is grouped into its review's report notice.
        if "needs-human" in added and sender != "dispatcher" and "recommendation" not in labels:
            notify(f"needs-human:{repo}#{n}:{int(now())}", "needs-human", repo, n,
                   "this needs your decision (`needs-human`).")
        if repo == COORDINATION and "milestone-review" in added:
            for r in MILESTONE_REVIEWERS:
                add_trigger(r, f"milestone:{repo}#{n}", f"milestone review requested on {repo}#{n}", once=True)
        if (repo == COORDINATION and {"upstream", "upstream:lasm", "blocking"} <= labels
                and added & {"upstream:lasm", "blocking"}):
            add_trigger("lasm", f"lasm-blocking:{repo}#{n}", f"blocking Lasm issue {repo}#{n}", once=True)
        if (owner and issue.get("state") == "open" and not is_pr
                and {"request", "new"} <= labels and author in TRUSTED and author != owner
                and sender != owner and action in ("opened", "reopened", "label_updated")):
            add_trigger(owner, f"request:{repo}#{n}", f"untriaged request {repo}#{n}")
        if coord:
            if action == "opened" and "request" in labels and not is_pr:
                add_trigger(coord, f"filed:{repo}#{n}", f"request filed: {repo}#{n}", once=True)
            if "ready-for-release" in added:
                add_trigger(coord, f"rfr:{repo}#{n}:{int(now())}", f"ready-for-release added to {kind} {repo}#{n}")
            # The kit's own decisions (caps, stalls) and its refusals aren't the coordinator's.
            if "needs-human" in added and sender != "dispatcher":
                add_trigger(coord, f"nh+:{repo}#{n}:{int(now())}", f"needs-human added to {kind} {repo}#{n}")
            if "needs-human" in removed and sender != "dispatcher":
                add_trigger(coord, f"nh-:{repo}#{n}:{int(now())}", f"needs-human removed from {kind} {repo}#{n}")
            if action == "closed" and (issue.get("milestone") or {}).get("id"):
                ms = forge("GET", f"/repos/{ORG}/{repo}/milestones/{issue['milestone']['id']}") or {}
                if ms.get("open_issues") == 0:
                    add_trigger(coord, f"milestone-done:{repo}:{ms.get('id')}",
                                f"the last open issue in milestone {ms.get('title')} on {repo} was closed", once=True)
        if repo == COORDINATION and "upstream" in labels and action == "closed":
            wake_blocked((repo, n), f"upstream issue {repo}#{n} was closed")

    elif event == "issue_comment" and action == "created":
        issue, c = payload.get("issue") or {}, payload.get("comment") or {}
        n = issue.get("number")
        if not trusted:
            return
        is_pr = bool(payload.get("is_pull") or issue.get("pull_request"))
        if owner and sender != owner and issue.get("state") == "open" and wants_reply(c.get("body")):
            add_trigger(owner, f"comment:{c.get('id')}", f"comment on {'pull request' if is_pr else 'issue'} {repo}#{n}")
        labels = {l["name"] for l in issue.get("labels") or []}
        # A release comment on a request needs no reply, but its requester has
        # work: upgrade, check the acceptance criteria and verify.
        released = RELEASED.search(c.get("body") or "")
        requester = (issue.get("user") or {}).get("login")
        if (released and sender in ("gatekeeper", HUMAN, owner) and not is_pr and issue.get("state") == "open"
                and "request" in labels and requester in AGENTS and AGENTS[requester]["own"]):
            add_trigger(requester, f"released:{repo}#{n}:{released[1]}",
                        f"your request {repo}#{n} was released in {released[1]}: upgrade to it, check its "
                        "acceptance criteria, and verify it", once=True)
        # The author answers conformance's findings in a comment without pushing:
        # conformance looks again. A reply after a push waits for the new head's review.
        if (is_pr and issue.get("state") == "open" and sender == (issue.get("user") or {}).get("login")
                and sender != REVIEWER and wants_reply(c.get("body")) and conformance_awaits_reply(repo, n)):
            add_trigger(REVIEWER, f"conf-reply:{c.get('id')}",
                        f"the author replied on pull request {repo}#{n}, which you asked to change")
        # A milestone reviewer's report: one notice per report, not per recommendation.
        if sender in MILESTONE_REVIEWERS and ({"milestone-review", "upstream:lasm"} & labels):
            notify(f"report:{sender}:{repo}#{n}", "report", repo, n,
                   f"the `{sender}` review has reported here. Its recommendations are issues labelled "
                   f"`recommendation` and `review:{sender}`: close each as accepted, or label it `declined`, "
                   "say why and close it.")
        if repo == COORDINATION and "upstream" in labels and sender == HUMAN:
            wake_blocked((repo, n), f"{HUMAN} commented on upstream issue {repo}#{n}")

    elif event.startswith("pull_request_review") or event == "pull_request_comment":
        pr = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        n = pr.get("number")
        rejected = "rejected" in event or review.get("type") == "pull_request_review_rejected"
        approved = "approved" in event or review.get("type") == "pull_request_review_approved"
        content = review.get("content") or ""
        # An approval asks nothing of the author, whatever it says (the
        # coordinator's say what it checked): its scheduled merge goes ahead.
        # A reviewer who wants more requests changes or comments.
        asks = wants_reply(content) and not approved
        if (owner and trusted and sender != owner and (pr.get("user") or {}).get("login") == owner
                and pr.get("state") == "open" and (rejected or asks)):
            what = "changes requested on" if rejected else "review on"
            add_trigger(owner, f"review:{repo}#{n}:{payload.get('_delivery')}", f"{what} pull request {repo}#{n}")

    elif event == "pull_request":
        pr = payload.get("pull_request") or {}
        n = pr.get("number")
        if not trusted:
            return
        if action == "label_updated":
            labels = {l["name"] for l in pr.get("labels") or []}
            labels, added = human_labels_changed(repo, n, pr, labels, *labels_changed(repo, n, labels), sender)
            if "needs-human" in added and sender != "dispatcher":
                notify(f"needs-human:{repo}#{n}:{int(now())}", "needs-human", repo, n,
                       "this needs your decision (`needs-human`).")
                if coord:
                    add_trigger(coord, f"nh+:{repo}#{n}:{int(now())}", f"needs-human added to pull request {repo}#{n}")
        if action == "synchronized":
            drop_pending(REVIEWER, f"conf:{repo}#{n}:")  # a newer head supersedes the waiting review
        if action == "closed":
            drop_reviews_of_closed(repo, n)
        if not has_ci(repo) and action in ("opened", "reopened", "synchronized"):
            queue_conformance(repo, n)  # nothing to wait for: no CI here
        # Auto-merge lands after the owner's run has ended: wake it to finish
        # the requests the pull request answers (CHANGELOG.md, ready-for-release).
        if action == "closed" and pr.get("merged"):
            wake_after_merge(repo, owner, pr)
        if not coord:
            return
        if action == "opened":
            add_trigger(coord, f"pr-opened:{repo}#{n}", f"pull request opened: {repo}#{n}", once=True,
                        not_before=now() + OPENED_HOLD)
        elif action == "review_requested" and (payload.get("requested_reviewer") or {}).get("login") == COORDINATOR:
            queue_coordinator_review(repo, n)  # once CI has passed on the head

    elif event == "release" and action == "published":
        tag = (payload.get("release") or {}).get("tag_name", "")
        # A release the coordinator asked the gatekeeper for wakes it no further.
        if coord and trusted and sender != "gatekeeper":
            add_trigger(coord, f"released:{repo}:{tag}", f"{repo} released {tag}", once=True)
        wake_blocked((repo, None), f"{repo} released {tag}")

    elif event == "push" and payload.get("ref") == "refs/heads/main":
        check_behind(repo, payload.get("after") or "")

    elif event == "action_run_success":
        run = payload.get("run") or {}
        if run.get("event") != "pull_request":
            return
        try:
            pr = json.loads(run.get("event_payload") or "{}").get("pull_request") or {}
        except ValueError:
            return
        if pr.get("number"):
            queue_conformance(repo, pr["number"], run.get("commit_sha"))
            queue_coordinator_review(repo, pr["number"], run.get("commit_sha"))
            release_opened(repo, pr["number"])

    elif event == "action_run_failure":
        run = payload.get("run") or {}
        if run.get("event") != "pull_request":
            return
        try:
            pr = json.loads(run.get("event_payload") or "{}").get("pull_request") or {}
        except ValueError:
            return
        if pr.get("number"):
            release_opened(repo, pr["number"])
        if owner and (pr.get("user") or {}).get("login") == owner:
            n = pr.get("number")
            add_trigger(owner, f"ci-failed:{repo}#{n}:{run.get('commit_sha', '')[:12]}",
                        f"CI failed on pull request {repo}#{n}", once=True)


# --- Reconciliation -------------------------------------------------------------

def reconcile():
    """Catch what a missed webhook would have lost."""
    for repo, owner in OWNER_OF.items():
        try:
            for issue in forge_list(f"/repos/{ORG}/{repo}/issues?state=open&type=issues&labels=request,new"):
                labels = {l["name"] for l in issue.get("labels") or []}
                author = issue["user"]["login"]
                if {"request", "new"} <= labels and author in TRUSTED and author != owner:
                    add_trigger(owner, f"request:{repo}#{issue['number']}",
                                f"untriaged request {repo}#{issue['number']}", recent=RECONCILE * 2)
            runs = forge("GET", f"/repos/{ORG}/{repo}/actions/runs?limit=50") or {}
            runs = runs.get("workflow_runs", []) if isinstance(runs, dict) else runs
            for pr in forge_list(f"/repos/{ORG}/{repo}/pulls?state=open"):
                if pr["user"]["login"] != owner:
                    continue
                sha = pr["head"]["sha"]
                mine = [r for r in runs if (r.get("commit_sha") or r.get("head_sha")) == sha]
                if mine and mine[0].get("status") == "failure":
                    add_trigger(owner, f"ci-failed:{repo}#{pr['number']}:{sha[:12]}",
                                f"CI failed on pull request {repo}#{pr['number']}", once=True)
            # Merges a missed webhook would have lost.
            for pr in forge_list(f"/repos/{ORG}/{repo}/pulls?state=closed&sort=recentupdate", limit=20):
                merged = pr.get("merged_at")
                if (pr.get("merged") and merged and pr["user"]["login"] == owner
                        and now() - datetime.fromisoformat(merged.replace("Z", "+00:00")).timestamp() < RECONCILE * 2):
                    wake_after_merge(repo, owner, pr)
            check_behind(repo)
            check_released_requests(repo)
            # What waits on the human but isn't assigned to them: from before
            # assignment, or a missed webhook. Forgejo ignores a label filter
            # naming a label the repository lacks, so check each item's own.
            for labels in ("needs-human", "upstream,blocking"):
                for item in forge_list(f"/repos/{ORG}/{repo}/issues?state=open&type=all&labels={labels}"):
                    if waits_on_human({l["name"] for l in item.get("labels") or []}):
                        set_assigned(repo, item["number"], item, True)
        except Exception as e:
            log(f"reconciling {repo} failed: {e!r}")
    # Pull requests whose head passed CI (or that have no CI) but which
    # conformance hasn't reviewed, or the coordinator hasn't approved when
    # its review is requested.
    for repo in OWNER_OF:
        try:
            runs = forge("GET", f"/repos/{ORG}/{repo}/actions/runs?limit=50") or {}
            runs = runs.get("workflow_runs", []) if isinstance(runs, dict) else runs
            for pr in forge_list(f"/repos/{ORG}/{repo}/pulls?state=open"):
                sha = pr["head"]["sha"]
                mine = [r for r in runs if (r.get("commit_sha") or r.get("head_sha")) == sha]
                if not has_ci(repo) or (mine and mine[0].get("status") == "success"):
                    queue_conformance(repo, pr["number"], sha)
                    queue_coordinator_review(repo, pr["number"], sha)
        except Exception as e:
            log(f"reconciling {repo}'s reviews failed: {e!r}")
    kv_set("reconciled", now())


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
    """Wake the coordinator to tell the human what it needs from them, once
    the kit has gone quiet: no run, no queued work (except an agent's held by
    its cap, which is the human's to lift), the human active nowhere, no CI,
    and nothing new for IDLE seconds. Once per quiet spell."""
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
        descs = list(dict.fromkeys(r["description"] for r in rows))
        cur = DB.execute("INSERT INTO runs (agent, triggers, claimed) VALUES (?,?,?)",
                         (agent, json.dumps(descs), t))
        run_id = cur.lastrowid
        DB.execute(f"UPDATE triggers SET run_id=? WHERE id IN ({','.join('?' * len(rows))})",
                   (run_id, *[r["id"] for r in rows]))
    log(f"{agent} claimed run {run_id}: {', '.join(descs)}")
    return {"run": run_id, "triggers": descs}


def requeue(run_id):
    q("UPDATE triggers SET run_id=NULL WHERE run_id=?", run_id)


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
    if (r.get("outcome") or "").startswith("exited"):
        requeue(run_id)  # Claude never finished a reply: none of the work was done
    else:
        release_reviews(agent)
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
    return (f"Kit: quiet, and nothing waits on you: stalled or finished (the coordinator's idle summary "
            f"says which){tail}")


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
