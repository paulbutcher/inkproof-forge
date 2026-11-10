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
try:
    DB.execute("ALTER TABLE triggers ADD COLUMN not_before REAL")  # ledgers from before stage 5
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


def notify_agent(agent, key, kind, text):
    """A notice about an agent rather than an issue (caps, failed transcript
    rewrites): on one needs-human issue per agent, reused until the human
    closes it."""
    repo = AGENTS[agent]["own"] or COORDINATION
    if not add_notice(key, kind, repo, None, text):
        return
    try:
        n = kv_get(f"notice-issue:{agent}")
        if n:
            issue = forge("GET", f"/repos/{ORG}/{repo}/issues/{n}")
            if issue.get("state") != "open":
                n = None
        if not n:
            labels = [l["id"] for l in forge_list(f"/repos/{ORG}/{repo}/labels", 100) if l["name"] == "needs-human"]
            issue = forge("POST", f"/repos/{ORG}/{repo}/issues", {
                "title": f"Kit notices: {agent}",
                "body": f"{NO_REPLY}\n\nThe dispatcher posts notices about `{agent}` here: reached caps, and runs "
                        "the extension doesn't list. Close this issue once you've dealt with them; the next "
                        "notice opens a new one.",
                "labels": labels})
            n = issue["number"]
            kv_set(f"notice-issue:{agent}", n)
        comment(repo, n, text)
        q("UPDATE notices SET posted=1, number=? WHERE key=?", n, key)
        log(f"notified {HUMAN} about {agent}: {kind}")
    except Exception as e:
        log(f"couldn't post the notice about {agent}: {e}")


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


def has_ci(repo):
    return repo != COORDINATION


def queue_conformance(repo, n, sha=None):
    """Work for conformance: review pull request n at its head, once that head
    has settled (no newer commit for SETTLE seconds) and if it hasn't already
    reviewed it. Pull requests marked as work in progress are skipped."""
    if REVIEWER not in AGENTS:
        return
    pr = forge("GET", f"/repos/{ORG}/{repo}/pulls/{n}")
    head = pr["head"]["sha"]
    if pr["state"] != "open" or wip(pr.get("title")) or (sha and sha != head):
        return
    drop_pending(REVIEWER, f"conf:{repo}#{n}:")
    if any(r["user"]["login"] == REVIEWER and r.get("commit_id") == head
           for r in forge_list(f"/repos/{ORG}/{repo}/pulls/{n}/reviews")):
        return
    add_trigger(REVIEWER, f"conf:{repo}#{n}:{head[:12]}", f"review pull request {repo}#{n} at {head[:12]}",
                once=True, not_before=now() + SETTLE)


def conformance_wants_changes(repo, n):
    mine = [r for r in forge_list(f"/repos/{ORG}/{repo}/pulls/{n}/reviews") if r["user"]["login"] == REVIEWER]
    return bool(mine) and mine[-1].get("state") == "REQUEST_CHANGES"


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
            if "needs-human" in added:
                add_trigger(coord, f"nh+:{repo}#{n}:{int(now())}", f"needs-human added to {kind} {repo}#{n}")
            if "needs-human" in removed:
                add_trigger(coord, f"nh-:{repo}#{n}:{int(now())}", f"needs-human removed from {kind} {repo}#{n}")
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
        # The author answers conformance's findings in a comment: conformance looks again.
        if (is_pr and issue.get("state") == "open" and sender == (issue.get("user") or {}).get("login")
                and sender != REVIEWER and wants_reply(c.get("body")) and conformance_wants_changes(repo, n)):
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
        if (owner and trusted and sender != owner and (pr.get("user") or {}).get("login") == owner
                and pr.get("state") == "open" and (rejected or wants_reply(review.get("content")))):
            what = "changes requested on" if rejected else "review on"
            add_trigger(owner, f"review:{repo}#{n}:{payload.get('_delivery')}", f"{what} pull request {repo}#{n}")

    elif event == "pull_request":
        pr = payload.get("pull_request") or {}
        n = pr.get("number")
        if not trusted:
            return
        if action == "synchronized":
            drop_pending(REVIEWER, f"conf:{repo}#{n}:")  # a newer head supersedes the waiting review
        if not has_ci(repo) and action in ("opened", "reopened", "synchronized"):
            queue_conformance(repo, n)  # nothing to wait for: no CI here
        if not coord:
            return
        if action == "opened":
            add_trigger(coord, f"pr-opened:{repo}#{n}", f"pull request opened: {repo}#{n}", once=True)
        elif action == "closed" and pr.get("merged"):
            add_trigger(coord, f"pr-merged:{repo}#{n}", f"pull request merged: {repo}#{n}", once=True)
        elif action == "review_requested" and (payload.get("requested_reviewer") or {}).get("login") == COORDINATOR:
            add_trigger(coord, f"review-requested:{repo}#{n}:{pr.get('head', {}).get('sha', '')[:12]}",
                        f"review requested from you on pull request {repo}#{n}", once=True)

    elif event == "release" and action == "published":
        tag = (payload.get("release") or {}).get("tag_name", "")
        if coord and trusted:
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

    elif event == "action_run_failure":
        run = payload.get("run") or {}
        if run.get("event") != "pull_request" or not owner:
            return
        try:
            pr = json.loads(run.get("event_payload") or "{}").get("pull_request") or {}
        except ValueError:
            return
        if (pr.get("user") or {}).get("login") == owner:
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
            check_behind(repo)
        except Exception as e:
            log(f"reconciling {repo} failed: {e!r}")
    # Pull requests whose head passed CI (or that have no CI) but which
    # conformance hasn't reviewed.
    for repo in OWNER_OF:
        try:
            runs = forge("GET", f"/repos/{ORG}/{repo}/actions/runs?limit=50") or {}
            runs = runs.get("workflow_runs", []) if isinstance(runs, dict) else runs
            for pr in forge_list(f"/repos/{ORG}/{repo}/pulls?state=open"):
                sha = pr["head"]["sha"]
                mine = [r for r in runs if (r.get("commit_sha") or r.get("head_sha")) == sha]
                if not has_ci(repo) or (mine and mine[0].get("status") == "success"):
                    queue_conformance(repo, pr["number"], sha)
        except Exception as e:
            log(f"reconciling {repo}'s reviews failed: {e!r}")
    kv_set("reconciled", now())


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


def capped(agent):
    """Why the agent can't start another run today, or None."""
    if runs_today(agent) >= CAP:
        return f"its daily cap of {CAP} runs"
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
            notify_agent(agent, f"cap:{agent}:{day}", "cap",
                         f"`{agent}` reached {why} at {iso(now())}, with {len(rows)} trigger(s) waiting. "
                         "They start after midnight UTC, or sooner if you raise the cap in `.env` and rerun "
                         "`scripts/up.sh`.")
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
    if not r.get("in_extension") and r.get("session_id"):
        notify_agent(agent, f"not-listed:{agent}:{run_id}", "transcript-rewrite",
                     f"run {run_id} of `{agent}` isn't listed in the Claude extension, because its transcript "
                     f"couldn't be rewritten. Resume it by its session ID, `{r.get('session_id')}`.")
    log(f"{agent} run {run_id} ended: {r.get('outcome')}")
    return True


def status():
    agents = {}
    for name in AGENTS:
        pending = q("SELECT description, created, not_before FROM triggers WHERE agent=? AND run_id IS NULL ORDER BY created", name)
        agents[name] = {"state": agent_state(name), "runs_today": runs_today(name), "cap": CAP,
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
            "agents": agents, "runs": runs, "sessions": sessions, "notices": notices}


def notices_text(after):
    """One notice per line: id, kind, where, text; tabs and newlines removed."""
    out = []
    for n in q("SELECT * FROM notices WHERE id>? ORDER BY id", after):
        where = f"{n['repo']}#{n['number']}" if n["number"] else (n["repo"] or "")
        clean = lambda s: " ".join(str(s or "").split())
        out.append(f"{n['id']}\t{n['kind']}\t{where}\t{clean(n['text'])}")
    return "\n".join(out) + ("\n" if out else "")


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
        delivery = "".join(c for c in (self.headers.get("X-Forgejo-Delivery") or str(time.time_ns()))
                           if c.isalnum() or c == "-")
        repo = (payload.get("repository") or {}).get("name")
        sender = (payload.get("sender") or {}).get("login")
        number = (payload.get("issue") or payload.get("pull_request") or {}).get("number")
        t = now()
        q("INSERT INTO events (at, event, type, action, repo, sender, number, delivery) VALUES (?,?,?,?,?,?,?,?)",
          t, event, event_type, payload.get("action"), repo, sender, number, delivery)
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
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
