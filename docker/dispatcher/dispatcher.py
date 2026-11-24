#!/usr/bin/env python3
"""The dispatcher: turns Forgejo's webhooks into work for agents, hands that
work to each agent's runner, keeps the run ledger, and tells the human what
needs them.

  POST /hook                 Forgejo's webhooks, signed with the webhook secret
  POST /claim                a runner asks for work (Bearer: its runner key)
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
`(no reply needed)`. Trigger descriptions carry only numbers, names and
links, never issue text, because they become a run's prompt.

What an agent owes comes from each unit of work as it is now (an issue,
a pull request, a repository's release, a milestone, the kit itself): its
state (states.py), whose holders owe its next step, and what is asked of
agents on it (see What is owed). A webhook only says which units to evaluate
again, and every RECONCILE_SECONDS, and on start, a sweep evaluates them all,
so nothing is lost to a missed webhook. Each obligation wakes its agent, and
again while it stands, then it is the human's (see Obligations).

A claim is granted only when the agent has no run, is under its daily caps,
the kit isn't paused, and its newest trigger has settled for the debounce
period (or its oldest has waited the maximum delay), so a burst of events
becomes one run; and only for triggers whose items still owe them. A run
that fails, times out or exits early has its work queued again, up to
MAX_RUN_RETRIES times; then it's the human's.

Once the kit has been quiet for IDLE_SECONDS (no run, no queued work, no
CI), it wakes the coordinator to tell the human what it needs from them.
Once per quiet spell.
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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import states as ST

STATE = Path(os.environ.get("STATE_DIR", "/state"))
SECRETS = Path(os.environ.get("DISPATCHER_SECRETS", "/run/secrets/dispatcher"))
KEYS_DIR = Path(os.environ.get("RUNNER_KEYS_DIR", "/run/secrets/runner-keys"))
AGENTS_CONF = Path(os.environ.get("AGENTS_CONF", "/etc/kit/agents.conf"))
CONTROL = Path(os.environ.get("CONTROL_DIR", "/control"))
FORGE = os.environ.get("FORGE_URL", "http://forgejo:3000").rstrip("/")
# Forgejo as the human's browser reaches it, for links in what the kit writes.
FORGE_WEB = os.environ.get("FORGE_WEB_URL", FORGE).rstrip("/")
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
# A run that failed before it got going (a turn at most, nothing spent, no
# change in Forgejo) failed for its environment: an outage, the proxy, the
# API. It isn't counted against RETRIES; its work waits these delays in turn,
# then the last one each time, until the agent's runs get going again.
ENV_DELAYS = [int(s) for s in os.environ.get("ENV_RETRY_DELAYS", "300,900,3600").split(",")]
# A run cut short by the account's usage limit is retried this long after
# the limit resets, by the time its error gives.
LIMIT_MARGIN = int(os.environ.get("LIMIT_RESET_MARGIN_SECONDS", "120"))
REVIEWER = "conformance"
MILESTONE_REVIEWERS = ("architecture", "process", "lasm")
KEEP_PAYLOADS = 500
MAX_BODY = 25 * 1024 * 1024
NO_REPLY = "(no reply needed)"
BLOCKED_BY = re.compile(r"Blocked-by:[ \t]*([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)")
# Who an item waits on, as its latest such line says: an agent's name, or
# `none` to say it waits on no one any more.
WAITING_ON = re.compile(r"(?im)^[ \t]*Waiting-on:[ \t]*@?([A-Za-z0-9_.-]+)")


def log(msg):
    print(f"dispatcher: {msg}", file=sys.stderr, flush=True)


def now():
    return time.time()


# When this dispatcher started: set at startup, and counted as activity, so
# a rollout starts a quiet spell of its own and the stall check runs the new
# rules over a kit that went quiet under the old ones (demos#105).
STARTED = 0.0


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
            if a["own"] and a["kind"] in ("library", "site", "coordinator")}
COORDINATOR = next((n for n, a in AGENTS.items() if a["kind"] == "coordinator"), None)
# The supervisor: woken when an obligation becomes the human's, to find out
# why, move it on and report the gap (Obligations). None without one.
SUPERVISOR = next((n for n, a in AGENTS.items() if a["kind"] == "supervisor"), None)
# How many of the supervisor's comments on one item a day may wake someone.
SUPERVISOR_NUDGES = int(os.environ.get("SUPERVISOR_NUDGES", "3"))
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


def draining():
    """Draining for a clean stop (scripts/drain.sh): runs under way finish,
    and no run starts until scripts/resume.sh."""
    return (CONTROL / "draining").exists()


def under_way():
    """What a clean stop waits for: runs not yet ended, and CI."""
    work = [f"{r['agent']}'s run {r['id']}" for r in q("SELECT id, agent FROM runs WHERE ended IS NULL ORDER BY id")]
    if ci_running():
        work.append("CI")
    return work


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
CREATE TABLE IF NOT EXISTS units (
  unit TEXT PRIMARY KEY, kind TEXT, state TEXT, holders TEXT, step TEXT, since REAL, seen REAL);
CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY, unit TEXT, agent TEXT, side TEXT, state TEXT, rules TEXT,
  first REAL, last REAL, resolved REAL);
CREATE INDEX IF NOT EXISTS shadow_unit ON shadow (unit, resolved);
CREATE TABLE IF NOT EXISTS human_waits (repo TEXT, number INTEGER, PRIMARY KEY (repo, number));
""")
for column in ("triggers ADD COLUMN not_before REAL",  # ledgers from before stage 5
               "events ADD COLUMN detail TEXT",      # ledgers from before the activity stream
               "triggers ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",  # before failed runs were retried
               "triggers ADD COLUMN env_held INTEGER NOT NULL DEFAULT 0"):  # before work held for its environment was released
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
    (a pull request whose CI has finished), unless it waits to be retried, or
    for its environment (env_retry).
    Returns the new trigger's id, or None."""
    if agent not in AGENTS:
        return
    with LOCK:
        dup = DB.execute("SELECT id, run_id, not_before, attempts, env_held FROM triggers WHERE agent=? AND key=? "
                         + ("" if once else "AND run_id IS NULL ") + "ORDER BY id DESC", (agent, key)).fetchone()
        if dup:
            if (dup["run_id"] is None and dup["not_before"] and not_before is None
                    and not dup["attempts"] and not dup["env_held"]):
                DB.execute("UPDATE triggers SET not_before=NULL WHERE id=?", (dup["id"],))
            return
        cur = DB.execute("INSERT INTO triggers (agent, key, description, created, not_before) VALUES (?,?,?,?,?)",
                         (agent, key, description, now(), not_before))
    log(f"queued for {agent}: {description}")
    return cur.lastrowid


# --- Forgejo --------------------------------------------------------------------

# At most this many requests to Forgejo at once, from all of the dispatcher's
# threads: when Forgejo slows down, the rest wait here instead of piling more
# work onto it.
FORGE_SLOTS = threading.BoundedSemaphore(int(os.environ.get("FORGE_CONCURRENCY", "8")))


ITEM_PATH = re.compile(r"/repos/[^/]+/([^/]+)/(?:issues|pulls)/(\d+)(?:/|$)")


def forge(method, path, body=None):
    if method != "GET" and (m := ITEM_PATH.match(path)):
        forget(m[1], int(m[2]))  # what the kit writes, it reads afresh
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(FORGE + "/api/v1" + path, method=method, data=data, headers={
        "Authorization": f"token {secret('token')}", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {})})
    with FORGE_SLOTS, urllib.request.urlopen(req, timeout=30) as r:
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


MAX_PAGES = 200


def forge_all(path):
    """Every page of a list. Forgejo ignores `page` on some lists, issue
    comments among them (forge_comments), and serves the same page again:
    that ends the list rather than looping on it (inkproof-demo#2, at 50
    comments, took the dispatcher to 82 GiB)."""
    sep = "&" if "?" in path else "?"
    out, page, last = [], 1, None
    while True:
        batch = forge("GET", f"{path}{sep}limit=50&page={page}") or []
        if page > 1 and batch == last:
            log(f"{path} ignores paging: served page 1 again as page {page}")
            return out
        out += batch
        if len(batch) < 50:
            return out
        if page >= MAX_PAGES:
            raise RuntimeError(f"{path} has more than {MAX_PAGES} pages")
        last, page = batch, page + 1


def forge_comments(repo, n):
    """Every comment on an issue or pull request. Forgejo serves them all in
    one response, with no paging."""
    return forge("GET", f"/repos/{ORG}/{repo}/issues/{n}/comments") or []


# --- Items, read once until they change -------------------------------------------
#
# An issue or pull request as the rules read it (Item): its issue, timeline
# and reviews, and a pull request's files for its head. Kept until a webhook
# names the item, the kit writes to it, a listing shows a newer updated_at, or
# ITEM_CACHE_SECONDS pass, so a sweep or a dependent's evaluation reads only
# what changed. Forgejo bumps updated_at for labels, comments, assignees, a
# push to a pull request, a review, an edit and closing, but not for a review
# request or main moving (checked 2026-11-24): a pull request is read afresh
# every time (its mergeability, head and requested reviewers), and its
# updated_at checks the rest.

ITEM_MAX_AGE = int(os.environ.get("ITEM_CACHE_SECONDS", "3600"))
ITEMS = {}     # (repo, n): what was read, and when
ITEM_GEN = {}  # (repo, n): bumped by forget, so a read under way isn't kept
ITEMS_LOCK = threading.Lock()


def forget(repo, n):
    with ITEMS_LOCK:
        ITEMS.pop((repo, n), None)
        ITEM_GEN[(repo, n)] = ITEM_GEN.get((repo, n), 0) + 1


def listed(repo, items):
    """Forget what a listing shows has changed since it was read."""
    with ITEMS_LOCK:
        stale = [i["number"] for i in items
                 if (e := ITEMS.get((repo, i["number"]))) and e["updated"] != i.get("updated_at")]
    for n in stale:
        forget(repo, n)


def prune_items():
    with ITEMS_LOCK:
        for key in [k for k, e in ITEMS.items() if now() - e["at"] >= ITEM_MAX_AGE]:
            del ITEMS[key]


def item_reads(repo, n):
    """An item's issue, pull request, timeline and reviews, read afresh or
    kept (see above). Raises HTTPError 404 for an item that doesn't exist."""
    key = (repo, n)
    with ITEMS_LOCK:
        kept, gen = ITEMS.get(key), ITEM_GEN.get(key, 0)
    if kept and now() - kept["at"] >= ITEM_MAX_AGE:
        kept = None
    if kept and kept["issue"].get("pull_request") is not None:
        pr = forge("GET", f"/repos/{ORG}/{repo}/pulls/{n}")
        kept = dict(kept, pr=pr) if pr.get("updated_at") == kept["updated"] else None
    if kept:
        return kept
    issue = forge("GET", f"/repos/{ORG}/{repo}/issues/{n}")
    is_pr = issue.get("pull_request") is not None
    read = {"at": now(), "updated": issue.get("updated_at"), "issue": issue,
            "pr": forge("GET", f"/repos/{ORG}/{repo}/pulls/{n}") if is_pr else None,
            "timeline": forge_all(f"/repos/{ORG}/{repo}/issues/{n}/timeline"),
            "reviews": sorted(forge_all(f"/repos/{ORG}/{repo}/pulls/{n}/reviews"), key=lambda r: r["id"])
            if is_pr else [],
            "files": {}}  # head sha: paths, filled when a rule first asks
    with ITEMS_LOCK:
        if ITEM_MAX_AGE > 0 and ITEM_GEN.get(key, 0) == gen:
            ITEMS[key] = read
    return read


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


def latest_release(repo):
    """The newest published release (not a draft), or None."""
    def read():
        return next((r for r in forge("GET", f"/repos/{ORG}/{repo}/releases?limit=10") or []
                     if not r.get("draft")), None)
    return cached(("release", repo), 20, read)


def last_tag(repo):
    """The highest `vX.Y.Z` tag, as the gatekeeper finds it (last_tag there):
    (name, (x, y, z)), or None. Tags, not releases, since a repository's tags
    may predate its releases in Forgejo."""
    def read():
        best = None
        for t in forge_all(f"/repos/{ORG}/{repo}/tags"):
            v = version(t.get("name"))
            if v and (best is None or v > best[1]):
                best = (t["name"], v)
        return best
    return cached(("tag", repo), 20, read)


# A CHANGELOG.md version heading, in either form the libraries use.
CHANGELOG_VERSION = re.compile(r"(?m)^##[ \t]+\[?v?(\d+)\.(\d+)\.(\d+)\]?")


def version(tag):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def unreleased_entry(repo):
    """CHANGELOG.md's newest heading on main, as `vX.Y.Z`, if it is above
    the last release's tag (or there is none): work merged and described,
    waiting for a release. None if it isn't, or there is no CHANGELOG.md."""
    def read():
        try:
            f = forge("GET", f"/repos/{ORG}/{repo}/contents/CHANGELOG.md?ref=main") or {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        m = CHANGELOG_VERSION.search(base64.b64decode(f.get("content") or "").decode(errors="replace"))
        if not m:
            return None
        top = tuple(int(x) for x in m.groups())
        last = (last_tag(repo) or (None, None))[1]
        return "v%d.%d.%d" % top if last is None or top > last else None
    return cached(("unreleased", repo), 60, read)


def released_top(repo):
    """If CHANGELOG.md's newest heading on main is the last release's: (its
    tag, whether lines were added under it after the tag), else None. An
    entry written before the tag and merged after it lands there (demos,
    2026-11-23: #119's lines under an already released v0.22.0)."""
    def section(text):
        m = CHANGELOG_VERSION.search(text or "")
        if not m:
            return None
        nxt = re.compile(r"(?m)^##[ \t]").search(text, m.end())
        return text[m.start():nxt.start() if nxt else len(text)].strip()

    def read():
        last = last_tag(repo)
        text = file_text(repo, "CHANGELOG.md", "main")
        m = CHANGELOG_VERSION.search(text or "")
        if not last or not m or tuple(int(x) for x in m.groups()) != last[1]:
            return None
        return last[0], section(text) != section(file_text(repo, "CHANGELOG.md", last[0]))
    return cached(("released-top", repo), 60, read)


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
# "Refs #2" in the middle of a line, as in "… not done yet. Refs #2." (demo#18):
# only the references that follow it, not the rest of the line.
_ONE_REF = r"(?:[\w.-]+/[\w.-]+)?#\d+"
REFS_INLINE = re.compile(rf"(?i)(?<![\w/-])refs:?\s+({_ONE_REF}(?:(?:\s*,\s*|\s+and\s+|\s+){_ONE_REF})*)")
REF = re.compile(r"(?:([\w.-]+)/([\w.-]+))?#(\d+)")
# "Part of #2", as agents write it in titles and descriptions when they forget
# the Refs line (demo#2).
PART_OF = re.compile(r"(?i)\bpart of\s+((?:[\w.-]+/[\w.-]+)?#\d+)")


def pr_refs(repo, pr):
    """The issues on repo a pull request refers to with `Refs` (or "part
    of"), in its title, its description or its commits."""
    def read():
        texts = [pr.get("title") or "", pr.get("body") or ""]
        texts += [(c.get("commit") or {}).get("message") or ""
                  for c in forge_all(f"/repos/{ORG}/{repo}/pulls/{pr['number']}/commits")]
        found = set()
        for text in texts:
            for line in REFS.findall(text) + REFS_INLINE.findall(text) + PART_OF.findall(text):
                for org, r, k in REF.findall(line):
                    if (not org or (org.lower() == ORG.lower() and r == repo)) and int(k) != pr["number"]:
                        found.add(int(k))
        return found
    return cached(("refs", repo, pr["number"], pr["head"]["sha"], pr.get("title"), pr.get("body")), 3600, read)


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
    """Whether an agent could still push: a run under way. The human's
    sessions in its container can't push (inkproof-hook)."""
    return bool(q("SELECT 1 FROM runs WHERE agent=? AND ended IS NULL", agent))


def busy(agent):
    """Running, or with work queued: what it leaves undone may yet be done."""
    return bool(may_push(agent) or q("SELECT 1 FROM triggers WHERE agent=? AND run_id IS NULL", agent))


# --- What waits on the human ------------------------------------------------------
#
# Everything waiting on the human is an open issue or pull request assigned to
# them (labelled needs-human, or a blocking upstream issue), or a pull request
# requesting their review: Forgejo's own dashboards are the list. A
# needs-human item must put its decision so it can be answered as it stands.

DECISION = ("Question", "Options", "Recommendation", "If you say go ahead", "Blocked on this")
RELEASED = re.compile(r"^Released in (v\d+\.\d+\.\d+)\b", re.M)


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
                                        for c in forge_comments(repo, n)]
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


def waits_on_you(issue, pr):
    """Whether an item, as Forgejo serves it, waits on the human: open, and
    assigned to them or requesting their review."""
    return issue.get("state") == "open" and HUMAN in (
        {a.get("login") for a in issue.get("assignees") or []}
        | {u.get("login") for u in (pr or {}).get("requested_reviewers") or []})


def note_waiting(repo, n, waits):
    """Keep the index of what waits on the human in step with an item as
    last read: every evaluation reads the item anyway (Item), on a webhook
    or in the sweep, which also drops what closed unseen (evaluate_repo)."""
    if waits:
        q("INSERT OR IGNORE INTO human_waits (repo, number) VALUES (?,?)", repo, n)
    else:
        q("DELETE FROM human_waits WHERE repo=? AND number=?", repo, n)


def waiting_for_human():
    """The open issues and pull requests that wait on the human, as sorted
    `repo#n`: assigned to them, or requesting their review. From the index
    the evaluations keep, never from Forgejo: the status page asks this every
    few seconds."""
    return [f"{r['repo']}#{r['number']}" for r in q("SELECT repo, number FROM human_waits ORDER BY repo, number")
            if r["repo"] in OWNER_OF]


def waiting_on_human():
    """How many open issues and pull requests wait on the human."""
    return len(waiting_for_human())


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
    # What can start an Actions run (checks.yml: pushes, pull requests), and
    # a run's end: whether CI runs there is checked again (refresh_ci).
    if event in ("push", "create", "pull_request") or event.startswith("action_run"):
        ci_may_run(repo)
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
        # 18. The one notice that is an event, not a state: the coordinator
        #     hears of a release it didn't ask the gatekeeper for.
        tag = (payload.get("release") or {}).get("tag_name", "")
        if COORDINATOR and trusted and sender not in ("gatekeeper", COORDINATOR):
            add_trigger(COORDINATOR, f"released:{repo}:{tag}", f"{repo} released {tag}", once=True)
        # And a `Release <repo>` decision it overtook, and the release itself.
        _cache.pop(("release", repo), None)
        if kv_get("rules-seeded"):
            evaluate_labelled(repo, "needs-human")
            evaluate_release(repo)
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
            # CI on main: the release waits on it.
            evaluate_release(repo)
            later(evaluate_release, repo)
            return
        try:
            n = (json.loads(run.get("event_payload") or "{}").get("pull_request") or {}).get("number")
        except ValueError:
            return
        if n:
            later(evaluate, repo, n)
    if n:
        evaluate(repo, n)
    if event == "issues":
        labels = {l["name"] for l in item.get("labels") or []}
        if "ready-for-release" in labels or action in ("closed", "reopened", "label_updated", "label_cleared"):
            evaluate_release(repo)
        if action in ("opened", "closed", "reopened", "milestoned", "demilestoned"):
            evaluate_milestones()  # a milestone's work, and so the kit's


# --- What is owed -----------------------------------------------------------------
#
# proposals/explicit-state.md. Every issue, pull request, repository's
# release, milestone and the kit itself is a unit with a state (states.py),
# derived from facts read here (Facts, below). Each agent that holds a state
# that isn't finished owes its step; each agent asked something on an item (a
# reply, a mention, a closed item's Waiting-on: line) owes an answer until it
# acts there. Both are obligations, and all obligations are woken alike
# (Obligations, below): at once, again as each run ends with the obligation
# still standing, OBLIGATION_BACKOFF apart, then the human's.
#
# Notices are something to look at once, never progress, so never what work
# waits on: each is queued once per version of the state it reads, and a
# claim drops one no longer owed. The numbers are DESIGN.md's (Triggers).
#
# A webhook only says which units to evaluate again; the sweep evaluates
# them all every RECONCILE seconds; a claim evaluates again the units behind
# its work; and nothing is owed because of an event, only because of a state.

Owed = namedtuple("Owed", "agent key description not_before quiet", defaults=(None, False))

# Kinds of notice, which a claim checks are still owed.
RULE_KINDS = {"upstream-comment", "rejected", "filed", "pr-opened", "nh", "lasm-blocking", "priority"}
# Kinds whose state, when the notices first run on an existing ledger, was
# already reported under the old event-driven triggers: recorded as handled.
SEED_KINDS = {"upstream-comment", "rejected", "filed", "pr-opened", "nh", "lasm-blocking"}
RULE_KEY = re.compile(r"^([a-z-]+):([\w.-]+)#(\d+):")
# Kinds of trigger the rules queued before the states replaced them: those
# still waiting are dropped once, at start, as obligations take their place.
RETIRED_KINDS = {"request", "comment", "review", "ci-failed", "conflicts", "unscheduled", "stuck-merge", "no-ci",
                 "merged", "idle-request", "unblocked", "released", "unanswered", "unverified", "answered",
                 "review-requested", "rfr", "reviewed", "conf", "conf-reply", "milestone", "mention", "overtaken",
                 "closed-unreleased", "waiting", "main-failed", "milestone-done", "next-milestone", "stalled"}


def notices(it):
    """What an item asks agents to look at, once."""
    o, at, c, owner = [], it.at, COORDINATOR, it.owner
    # 28. Made a priority by someone else: before anything new.
    added = [e for e in it.events if e.get("type") == "label" and (e.get("label") or {}).get("name") == PRIORITY
             and (e.get("body") or "") == "1"]
    if owner and it.open and not it.is_pr and PRIORITY in it.labels and added and login(added[-1]) != owner:
        o.append(Owed(owner, f"priority:{at}:{added[-1]['created_at']}",
                      f"{at} is now a priority: it comes before anything new (Protocol: Priority)"))
    # 11. A request closed without being verified, by someone else: rejected.
    req = it.author
    if (not it.is_pr and not it.open and "request" in it.labels and "verified" not in it.labels
            and req in AGENTS and AGENTS[req]["own"] and req != owner):
        closes = [e for e in it.events if e.get("type") == "close"]
        if closes and login(closes[-1]) != req:
            o.append(Owed(req, f"rejected:{at}:{closes[-1]['created_at']}",
                          f"your request {at} was closed without being verified"))
    if c:
        # 12. A request filed by someone else: check it's well formed and placed.
        if it.open and not it.is_pr and "request" in it.labels and it.author in TRUSTED and it.author != c:
            o.append(Owed(c, f"filed:{at}:", f"request filed: {at}"))
        if (it.is_pr and it.author in TRUSTED and it.author not in (c, HUMAN) and (it.open or it.pr.get("merged"))
                and it.files() != {"CHANGELOG.md"}):
            # 13. A pull request opened by an agent: check its scope. Held a
            #     while, or until CI has finished, so that a review asked of
            #     the coordinator shares the run. Not the human's, nor one
            #     that changes only CHANGELOG.md, which the coordinator checks
            #     when it releases; its files are read again at the claim, so
            #     one that grows beyond CHANGELOG.md wakes it then
            #     (coordination#171).
            settled = not it.open or not has_ci(it.repo) or it.ci() in ("success", "failure")
            o.append(Owed(c, f"pr-opened:{at}:", f"pull request opened: {at}",
                          None if settled else when(it.pr["created_at"]) + OPENED_HOLD))
        # 16. needs-human added or removed by someone else (not the kit), and
        #     it hasn't looked since. Not on a process or Lasm recommendation:
        #     those change the kit or go upstream, which is the human's, never
        #     its own remit (coordination#128).
        nh = [e for e in it.events if e.get("type") == "label"
              and (e.get("label") or {}).get("name") == "needs-human"]
        theirs = "recommendation" in it.labels and it.labels & {"review:process", "review:lasm"}
        if (nh and not theirs and login(nh[-1]) not in (c, "dispatcher")
                and it.by(c, ("comment", "label")) < when(nh[-1]["created_at"])):
            added = (nh[-1].get("body") or "") == "1"
            o.append(Owed(c, f"nh:{at}:{nh[-1]['created_at']}",
                          f"needs-human {'added to' if added else 'removed from'} {it.kind} {at}"))
    # 22. A Lasm issue that blocks an agent.
    if (it.repo == COORDINATION and not it.is_pr and it.open and {"upstream", "upstream:lasm", "blocking"} <= it.labels
            and "lasm" in AGENTS):
        o.append(Owed("lasm", f"lasm-blocking:{at}:", f"blocking Lasm issue {at}"))
    return [x for x in o if x.agent in AGENTS]


MENTION = re.compile(r"(?<![\w@/.-])@([\w.-]+)")


def asks(it):
    """What is asked of agents on an item, as (agent, what, description):
    each owes an answer until it acts there."""
    o, at, owner = [], it.at, it.owner
    link = lambda c: f"{it.issue.get('html_url', '')}#issuecomment-{c['id']}"
    # 2. A comment or review from someone else that wants a reply, newer than
    #    anything the owner has done here, or answered only by a run that
    #    started before it, which never saw it (coordination#140). An approval
    #    asks nothing, whatever it says; a request for changes always asks.
    #    Timestamps are to the second, so ties go by the timeline's order.
    #    It names who asked and links the comment, so a run reads that one
    #    rather than looking for it (web#94).
    if owner and it.open:
        order = lambda e, i: (when(e["created_at"]), i)
        placed = lambda r: (when(r["submitted_at"]), next(
            (i for i, e in enumerate(it.events) if e.get("type") == "review" and login(e) == login(r)
             and e["created_at"] == r["submitted_at"]), -1))
        found = [(order(c, i), f"reply:{c['id']}", f"comment on {it.kind} {at} from {login(c)}: {link(c)}")
                 for i, c in enumerate(it.events) if c.get("type") == "comment"
                 and login(c) in TRUSTED and login(c) != owner and wants_reply(c.get("body"))]
        found += [(placed(r), f"reply:review-{r['id']}",
                   f"{'changes requested on' if r['state'] == 'REQUEST_CHANGES' else 'review on'} pull request {at} "
                   f"by {login(r)} (`forge reviews {it.repo} {it.n}`)")
                  for r in it.reviews if login(r) in TRUSTED and login(r) != owner and r.get("submitted_at")
                  and (r.get("state") == "REQUEST_CHANGES"
                       or (r.get("state") == "COMMENT" and wants_reply(r.get("body"))))]
        if found:
            t, what, desc = max(found)
            acted = max([order(e, i) for i, e in enumerate(it.events) if login(e) == owner]
                        + [placed(r) for r in it.reviews if login(r) == owner and r.get("submitted_at")]
                        or [(0, 0)])
            if t > acted or acted_in_earlier_run(owner, t[0], acted[0]):
                o.append((owner, what, desc))
    # 26. A comment by the human that @-mentions an agent, which that agent
    #     hasn't acted on since: the human's way to reach an agent other than
    #     the owner, whom any comment reaches (2). The supervisor's mentions
    #     reach the owner too, since its comments ask no reply (2); at most
    #     SUPERVISOR_NUDGES of its comments a day on one item, so it can't
    #     keep an agent busy. The owner's mention of a request's requester
    #     reaches the requester, if it wants a reply: the owner's way to ask
    #     what only the requester knows (gpu#59). Any other agent's mentions
    #     reach no one.
    mine = [c for c in it.comments() if SUPERVISOR and login(c) == SUPERVISOR]
    requester = it.author if (not it.is_pr and "request" in it.labels and it.author != owner) else None
    mentioned = {}  # the newest mention of each agent: acting answers them all
    for c in it.comments():
        who = login(c)
        if who == SUPERVISOR:
            day = [m for m in mine if when(c["created_at"]) - 86400 < when(m["created_at"]) <= when(c["created_at"])]
            if len(day) > SUPERVISOR_NUDGES:
                continue
        elif who == owner and requester and who != HUMAN:
            if not wants_reply(c.get("body")):
                continue
        elif who != HUMAN:
            continue
        by = "the human" if who == HUMAN else "the supervisor" if who == SUPERVISOR else f"{who}, its owner,"
        for name in dict.fromkeys(MENTION.findall(c.get("body") or "")):
            if who not in (HUMAN, SUPERVISOR) and name != requester:
                continue
            if (name in AGENTS and name != who and (name != owner or who == SUPERVISOR)
                    and it.by(name) < when(c["created_at"])):
                mentioned[name] = (name, f"mention:{c['id']}", f"{by} mentioned you on {it.kind} {at}: {link(c)}")
    o += mentioned.values()
    # 30. A closed item's `Waiting-on: <agent>` line, the latest on it, which
    #     that agent hasn't acted on since: a verified request is where the
    #     talk after it goes on (gpu#59, the coordinator asking demos there
    #     for its release). On an open item it is a hold (Facts).
    waiting = it.waiting_on() if not it.open else None
    if waiting:
        who, c = waiting
        o.append((who, f"waiting:{c['id']}", f"{login(c)} is waiting on you on {it.kind} {it.at}: {link(c)}"))
    return [x for x in o if x[0] in AGENTS]


class Item:
    """An issue or pull request, read once for all the rules."""

    def __init__(self, repo, n, fresh=False):
        """fresh: read afresh, it and what it waits on (blocked_rules), as for
        a webhook or a claim, rather than what was read before (Items)."""
        self.repo, self.n, self.fresh = repo, n, fresh
        if fresh:
            forget(repo, n)
        self._read = item_reads(repo, n)
        self.issue = self._read["issue"]
        self.is_pr = self.issue.get("pull_request") is not None
        self.pr = self._read["pr"]
        self.events = self._read["timeline"]
        self.reviews = self._read["reviews"]
        self.labels = {l["name"] for l in self.issue.get("labels") or []}
        self.author = login(self.issue)
        self.owner = OWNER_OF.get(repo)
        self.open = self.issue.get("state") == "open"
        self.kind = "pull request" if self.is_pr else "issue"
        self.at = f"{repo}#{n}"
        self._ci = None
        self._files = None

    @property
    def head(self):
        return self.pr["head"]["sha"]

    def ci(self):
        if self._ci is None:
            self._ci = ci_state(self.repo, self.head) if has_ci(self.repo) else "none"
        return self._ci

    def files(self):
        """The paths a pull request changes, read when a rule first asks."""
        if self._files is None:
            kept = self._read["files"]
            if self.head not in kept:
                kept[self.head] = {f["filename"] for f in forge_all(f"/repos/{ORG}/{self.repo}/pulls/{self.n}/files")}
            self._files = kept[self.head]
        return self._files

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

    def waiting_on(self):
        """The agent this item waits on, with the comment that says so, or
        None. The latest `Waiting-on:` line from a trusted account decides,
        and the wait holds until the agent it names acts here. A name that
        isn't an agent's, or the writer's own, says nothing: the human is
        reached with `needs-human`."""
        for c in reversed(self.comments()):
            names = WAITING_ON.findall(c.get("body") or "") if login(c) in TRUSTED else []
            if names:
                who = names[-1]
                if who in AGENTS and who != login(c) and self.by(who) < when(c["created_at"]):
                    return who, c
                return None
        return None

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
    read = item_reads(repo, n)
    issue = read["issue"]
    comments = [e for e in read["timeline"] if e.get("type") == "comment"]
    labels = {l["name"] for l in issue.get("labels") or []}
    s = {"done": None, "what": None, "human": None}
    # Closed while still labelled ready-for-release, its work isn't released:
    # it blocks until it is (coordination#208, scene#62 closed by its
    # requester before Scene's release).
    unreleased = (issue.get("state") == "closed" and issue.get("pull_request") is None
                  and "ready-for-release" in labels)
    if issue.get("state") == "closed" and not unreleased:
        s["done"] = issue.get("closed_at") or issue.get("updated_at")
        if issue.get("pull_request") is not None:
            merged = (issue["pull_request"] or {}).get("merged")
            s["what"] = f"pull request {repo}#{n} {'merged' if merged else 'was closed'}"
        else:
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
    """Whether an item is blocked by its Blocked-by: lines (None if it names
    nothing), when it last stopped being blocked, and what it names; and
    rule 8: the human's comment on an upstream issue it waits on, once per
    comment, for its owner to read."""
    found = blockers(it)
    if not found or not it.open or not it.owner:
        return [], None, None, found
    if it.fresh:
        for b in found:
            forget(*b)
    states = {b: blocker_state(*b) for b in found}
    done = sorted((when(s["done"]), s["what"]) for s in states.values() if s["done"])
    blocked = len(done) < len(found)
    owed = []
    # 8. The human's comment on an open upstream issue a line names, for the
    #    owner to read: it may say what to do about it.
    for (r, k), at in found.items():
        h = states[(r, k)]["human"]
        if h and when(h["created_at"]) >= when(at):  # to the second, on another issue
            owed.append(Owed(it.owner, f"upstream-comment:{it.at}:{h['id']}",
                             f"{HUMAN} commented on upstream issue {r}#{k}; {it.at} is blocked on it"))
    return owed, blocked, (done[-1][0] if done and not blocked else None), found


def acted_in_earlier_run(agent, asked, acted):
    """Whether the agent's action at `acted` came from a run that started
    before `asked`: a run under way when a comment arrived was briefed
    without it, so what it did afterwards doesn't answer it."""
    return bool(q("SELECT 1 FROM runs WHERE agent=? AND claimed<=? AND (ended IS NULL OR ended>=?)",
                  agent, asked, acted))


# --- Facts ------------------------------------------------------------------------
#
# What states.py reads, from Forgejo: each unit's facts, reduced to its
# enums and booleans, and who each of its roles is.

# CI that has been running this long without a change is late.
CI_LATE = int(os.environ.get("CI_LATE_SECONDS", "3600"))
ROADMAP_FINISHED = re.compile(r"(?im)^[ \t]*Status:[ \t]*finished\b")
PRIORITY = "priority"


def open_refs(repo):
    """The issues on repo that open pull requests, anyone's, refer to."""
    return cached(("refs", repo), 30, lambda: {n for p in forge_all(f"/repos/{ORG}/{repo}/pulls?state=open")
                                                for n in pr_refs(repo, p)})


def file_text(repo, path, ref):
    """A file's text at ref, or None."""
    try:
        f = forge("GET", f"/repos/{ORG}/{repo}/contents/{path}?ref={urllib.parse.quote(ref)}") or {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return base64.b64decode(f.get("content") or "").decode(errors="replace")


def expected_version(repo):
    """The version the gatekeeper would give a release of main now, as
    `vX.Y.Z`, or None if it can't be told (no API.lock on main)."""
    main = main_of(repo)
    tag, prev = last_tag(repo) or (None, None)

    def read():
        lock = file_text(repo, "API.lock", main["id"])
        if lock is None:
            return None
        old = file_text(repo, "API.lock", tag) if prev else None
        return "v%d.%d.%d" % ST.next_version(prev, old, lock)
    return cached(("expected", repo, tag, main["id"]), 3600, read)


def blocked_by_lines(it):
    """Every Blocked-by: line from a trusted author, oldest first: (repo, n,
    writer)."""
    texts = [(it.author, it.issue.get("body") or "")] if it.author in TRUSTED else []
    texts += [(login(c), c.get("body") or "") for c in it.comments() if login(c) in TRUSTED]
    return [(r, int(k), who) for who, text in texts for org, r, k in BLOCKED_BY.findall(text)
            if org.lower() == ORG.lower() and project_repo(r)]


def loops_back(start, first):
    """Whether the open items' Blocked-by: lines (the blocked_by index) lead
    from first back to start."""
    seen, todo = set(), [first]
    while todo:
        cur = todo.pop()
        if cur == start:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        todo += [(d["on_repo"], d["on_number"]) for d in
                 q("SELECT on_repo, on_number FROM blocked_by WHERE repo=? AND number=?", *cur)]
    return False


def blocker_fact(it, blocked):
    """The blockers fact, and who wrote an invalid line. A line naming the
    item itself is invalid while it is the newest; one that closes a loop of
    items each blocked by the next, while they wait."""
    me, lines = (it.repo, it.n), blocked_by_lines(it)
    if lines and lines[-1][:2] == me:
        return "invalid", lines[-1][2]
    if blocked:
        for r, k, who in lines:
            if (r, k) != me and loops_back(me, (r, k)):
                return "invalid", who
        return "pending", None
    if blocked is False:
        return "done", None
    if "blocked" in it.labels and not stale_blocked(it):
        return "bare-label", None
    return "none", None


def merged_since(it, unblocked_at):
    """Whether the owner merged work for it since it was last unblocked or
    accepted, and since its latest release: rule 6's test."""
    since = (unblocked_at and iso(unblocked_at)) or it.label_event("blocked", False) \
        or it.label_event("accepted") or it.issue["created_at"]
    released = it.released()
    if released:
        since = max(since, released[-1][0]["created_at"], key=when)
    return any(it.n in refs and when(p["merged_at"]) >= when(since)
               for p, refs in owner_prs(it.repo, it.owner, "merged"))


def issue_kind(it):
    if it.repo == COORDINATION and "milestone-review" in it.labels:
        return "milestone"
    if "recommendation" in it.labels:
        return "recommendation"
    if it.author == "dispatcher":
        return "kit-decision"
    if it.author == COORDINATOR and it.issue.get("title") == f"Release {it.repo}":
        return "release-decision"
    if "request" in it.labels and it.author != it.owner:
        return "request"
    return "task"


def author_of(it):
    """Who does an author's part: an agent, the human, or for an outsider
    (whom nothing wakes) the owner."""
    return it.author if it.author in AGENTS else HUMAN if it.author == HUMAN else it.owner


def hold_fact(it, blocked):
    """What holds an issue or pull request (states.HOLDS), whatever its
    kind; and the roles that names: who it waits on, who wrote an invalid
    line."""
    labels, who = it.labels, {}
    nh = it.label_event("needs-human")
    hand = HUMAN in {a.get("login") for a in it.issue.get("assignees") or []} and not waits_on_human(labels)
    waiting = it.waiting_on() if it.open else None
    blockers, writer = blocker_fact(it, blocked)
    if "needs-human" in labels and not (nh and it.by(HUMAN, ("comment",)) > when(nh)) or hand:
        hold = "human"
    elif "upstream" in labels and not it.is_pr:
        hold = "upstream"
    elif waiting:
        hold, who[ST.WAITED] = "waiting", [waiting[0]]
    elif blockers == "invalid":
        hold, who[ST.WRITER] = "invalid", [writer if writer in AGENTS or writer == HUMAN else it.owner]
    elif blockers == "pending":
        hold = "blocked"
    elif blockers == "bare-label":
        hold = "bare-label"
    else:
        hold = "none"
    return hold, who


def issue_facts(it, blocked, unblocked_at):
    """An issue's facts, and who each of its roles is."""
    kind, labels = issue_kind(it), it.labels
    hold, held_by = hold_fact(it, blocked)
    released = it.released()
    nv = it.label_event("not-verified")
    reviewers = []
    if kind == "milestone":
        labelled = when(it.label_event("milestone-review") or it.issue["created_at"])
        reviewers = [r for r in MILESTONE_REVIEWERS if r in AGENTS and it.by(r, ("comment",)) < labelled]
    overtaken = False
    if kind == "release-decision":
        rel = latest_release(it.repo)
        overtaken = bool(rel) and when(rel["published_at"]) > max(when(it.issue["created_at"]), it.by(COORDINATOR))
    # Labelled new (1), or someone else's that the owner has never touched:
    # a request whose labels Forgejo dropped, or any other issue (1, again).
    untriaged = "new" in labels if kind == "request" else False
    untriaged = untriaged or (it.author != it.owner and not it.by(it.owner)
                              and not labels & {"accepted", "counter-proposal"})
    f = ST.IssueFacts(
        open=it.open, kind=kind, untriaged=untriaged,
        counter=kind == "request" and "counter-proposal" in labels, hold=hold,
        # The coordination repository has no releases: its issues are
        # decisions and records, which merged work doesn't finish.
        open_pr=it.n in open_refs(it.repo),
        merged=bool(it.owner) and has_ci(it.repo) and merged_since(it, unblocked_at),
        rfr="ready-for-release" in labels, released=bool(released),
        verified=kind == "request" and "verified" in labels,
        not_verified=kind == "request" and bool(released) and "not-verified" in labels and bool(nv)
        and when(nv) > when(released[-1][0]["created_at"]),
        overtaken=overtaken, unreported=bool(reviewers))
    who = {ST.OWNER: [it.owner], ST.REQUESTER: [author_of(it)], ST.COORDINATOR: [COORDINATOR or HUMAN],
           ST.REVIEWERS: reviewers, **held_by}
    if released:
        who["notes"] = {ST.REQUESTER: f" (released in {released[-1][1]})"}
    return f, who


def pull_facts(it, blocked):
    """A pull request's facts, and who each of its roles is."""
    pushes = [e["created_at"] for e in it.events if e.get("type") == "pull_push"]
    pushed = when(pushes[-1] if pushes else it.pr["created_at"])
    if has_ci(it.repo):
        ci, ci_at = ci_settled(it.repo, it.head)
        late = (ci == "none" and now() >= pushed + NO_CI) or (ci == "running" and now() >= max(ci_at, pushed) + CI_LATE)
    else:
        ci, late = "unused", False
    verdicts = it.verdicts()
    asked = {w: r for w, r in verdicts.items() if r["state"] == "REQUEST_CHANGES"}

    def replied(w, r):
        since = max([when(r["submitted_at"])] + [when(c["created_at"]) for c in it.comments() if login(c) == w])
        return any(login(c) == it.author and wants_reply(c.get("body")) and when(c["created_at"]) > since
                   for c in it.comments())
    at_head = {w: r for w, r in asked.items() if r.get("commit_id") == it.head}
    if at_head:
        changes = "replied" if all(replied(w, r) for w, r in at_head.items()) else "at-head"
        changes_by = list(at_head)
    else:
        changes, changes_by = ("older", list(asked)) if asked else ("none", [])
    if REVIEWER not in AGENTS or it.author == REVIEWER or \
            any(login(r) == REVIEWER and r.get("commit_id") == it.head for r in it.reviews):
        conformance = "reviewed"
    elif not (it.author in AGENTS and not may_push(it.author)) and now() < pushed + SETTLE:
        conformance = "settling"
    else:
        conformance = "due"
    requested = it.requested() | {u["login"] for u in it.pr.get("requested_reviewers") or []}
    approved = {w for w, r in verdicts.items() if r["state"] == "APPROVED" and r.get("commit_id") == it.head}
    waiting = {w for w in requested - approved - {it.author} if w in AGENTS or w == HUMAN}
    pending = frozenset("coordinator" if w == COORDINATOR else "human" if w == HUMAN else "agent" for w in waiting)
    ready = it.ready_since() if it.open else None
    # When a timer it waits on runs out: a head settling, CI that may be
    # late, a scheduled merge that may be stuck.
    times = []
    if conformance == "settling":
        times.append(pushed + SETTLE)
    if ci == "none" and not late:
        times.append(pushed + NO_CI)
    if ci == "running" and not late:
        times.append(max(ci_at, pushed) + CI_LATE)
    if ready and it.merge_scheduled():
        times.append(ready + STUCK)
    hold, held_by = hold_fact(it, blocked)
    f = ST.PullFacts(
        open=it.open, hold=hold, wip=wip(it.pr.get("title")), ci=ci, ci_late=late,
        mergeable={True: "yes", False: "no"}.get(it.pr.get("mergeable"), "unknown"),
        changes=changes, conformance=conformance, pending=pending, approved=bool(approved),
        scheduled=it.merge_scheduled(), stuck=it.merge_scheduled() and bool(ready) and now() >= ready + STUCK)
    who = {ST.OWNER: [it.owner], ST.AUTHOR: [author_of(it)], ST.COORDINATOR: [COORDINATOR or HUMAN],
           ST.CONFORMANCE: [REVIEWER], ST.CHANGES_BY: changes_by,
           ST.PENDING: sorted(w for w in waiting if w in AGENTS and w != COORDINATOR), **held_by,
           "recheck": min([t for t in times if t > now()] or [0]) or None}
    if COORDINATOR in waiting:
        # Said outright, from CODEOWNERS on main, since a stale clone (from
        # before the setup pull request merged) suggests otherwise.
        who["notes"] = {ST.COORDINATOR: (
            f". {it.repo} is at level 3: CODEOWNERS on main gives `API.lock` to you, so it waits for your review. "
            "Approve it, request changes, or put a trade-off to the human as a decision; a comment alone leaves "
            "it waiting" if at_level_3(it.repo) else
            f". {it.repo} is below level 3, so the human approves its `API.lock`; your review is advisory")}
    return f, who


def release_facts(repo, items):
    """A repository's release: its facts, who its roles are, and the
    ready-for-release items (for the rules' side)."""
    main = main_of(repo)
    ci, ci_at = ci_settled(repo, main["id"])
    since = max(ci_at, when(main["timestamp"]))
    late = (ci == "none" and now() >= since + NO_CI) or (ci == "running" and now() >= since + CI_LATE)
    ready = [Item(repo, i["number"]) for i in items
             if "ready-for-release" in {l["name"] for l in i.get("labels") or []}]
    # What holds a ready-for-release issue (the human, a wait, a blocker)
    # holds its release: the release waits for it, as the coordinator would.
    held = [it for it in ready if hold_fact(it, blocked_rules(it)[1])[0] != "none"]
    entry = unreleased_entry(repo)
    expected = expected_version(repo) if entry else None
    f = ST.ReleaseFacts(
        main_ci=ci, main_ci_late=late, rfr=bool(ready),
        heading="none" if not entry else "wrong" if expected and entry != expected else "right",
        held=bool(held), level3=at_level_3(repo),
        decision=any(i.get("pull_request") is None and (i.get("user") or {}).get("login") == COORDINATOR
                     and i.get("title") == f"Release {repo}" for i in items))
    who = {ST.OWNER: [OWNER_OF.get(repo)], ST.COORDINATOR: [COORDINATOR or HUMAN]}
    names = " ".join(str(it.n) for it in ready)
    level3 = f.level3
    owner_note = ""
    if f.heading == "wrong":
        owner_note = f" (`forge version` gives {expected}; CHANGELOG.md heads {entry})"
    elif ST.state(f).name == "no entry":
        top = released_top(repo)
        if top and top[1]:
            owner_note = (f" (CHANGELOG.md's newest heading, {top[0]}, is already released, and lines were added "
                          f"under it after {top[0]} was tagged: move them under a new heading)")
        elif top:
            owner_note = f" (CHANGELOG.md's newest heading, {top[0]}, is the last release)"
    who["notes"] = {
        ST.OWNER: owner_note,
        ST.COORDINATOR: (
            f": `forge release {repo} '<one-line note>' {names}`".rstrip() if level3 else
            f", whose command is `scripts/release.sh {repo} '<one-line note>' {names}`".rstrip())
        + (f", completing {', '.join(it.at for it in ready)}" if ready else
           f"; nothing is labelled ready-for-release, but CHANGELOG.md on main heads {entry}, above its last "
           "release"),
    }
    if ci in ("none", "running") and not late:
        who["recheck"] = since + (NO_CI if ci == "none" else CI_LATE)
    return f, who, ready


def milestones_by_title():
    """Every milestone on every repository, by title: [(repo, milestone)]."""
    found = {}
    for repo in OWNER_OF:
        try:
            ms = forge_all(f"/repos/{ORG}/{repo}/milestones?state=all")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        for m in ms:
            found.setdefault(m.get("title"), []).append((repo, m))
    return found


# --- Obligations ----------------------------------------------------------------
#
# Each agent that owes something on a unit has one row in `obligations`, per
# state it holds there or ask it hasn't answered: its obligation. It is
# discharged (deleted) as soon as the unit no longer owes it, when the
# state, its holders or the ask change. Until then:
#   1. a new one wakes its agent at once (a quiet one once the agent has
#      nothing else queued or running);
#   2. each time a run it woke ends with it still standing, it wakes the agent
#      again after the next of OBLIGATION_BACKOFF (at once, 15 minutes, 15
#      minutes): a run can stop short, and an agent that can't act yet must
#      say why in a way that changes the state (Blocked-by:, Waiting-on:,
#      needs-human with a decision, not-verified), not only in a comment;
#   3. once that many runs have ended with it standing, and the last wait is
#      over, it is the human's: a `Stalled:` decision, and the supervisor
#      woken to find out why.
# A wake counts once its run has ended, so an outage, a cap or a pause, which
# hold the run back, uses up nothing.

BACKOFF = [int(s) for s in os.environ.get("OBLIGATION_BACKOFF", "0,900,900").split(",")]
TRIES = len(BACKOFF)
Owe = namedtuple("Owe", "agent what description quiet")

DB.executescript("""
CREATE TABLE IF NOT EXISTS obligations (
  id INTEGER PRIMARY KEY, agent TEXT, unit TEXT, what TEXT, description TEXT, quiet INTEGER,
  since REAL, wakes INTEGER NOT NULL DEFAULT 0, trigger INTEGER, next_at REAL, escalated INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS obligations_key ON obligations (unit, agent, what);
CREATE TABLE IF NOT EXISTS units (
  unit TEXT PRIMARY KEY, kind TEXT, state TEXT, holders TEXT, step TEXT, since REAL, seen REAL);
""")


def resolve(role, who):
    """The agents (or the human) a role names on a unit; a role that names
    no one falls to the owner, then to the human. Roles that aren't agents
    (the world, a timer, other units) stand for themselves."""
    if role not in ST.AGENT_ROLES:
        return [role]
    names = [n for n in who.get(role) or [] if n] or [n for n in who.get(ST.OWNER) or [] if n] or [HUMAN]
    return [ST.HUMAN if n == HUMAN else n for n in names]


def state_owes(unit, kind, state, who, label):
    """Record a unit's state, and return what its agent holders owe, each
    described as `<label> (<state>): <step>`. label names the unit in a run's
    prompt, such as `pull request inkproof-core#5`."""
    holders = sorted({a for role in state.holders for a in resolve(role, who)})
    t, encoded = now(), json.dumps(holders)
    with LOCK:
        row = DB.execute("SELECT state, holders FROM units WHERE unit=?", (unit,)).fetchone()
        if row and (row["state"], row["holders"]) == (state.name, encoded):
            DB.execute("UPDATE units SET seen=? WHERE unit=?", (t, unit))
        else:
            DB.execute("INSERT OR REPLACE INTO units (unit, kind, state, holders, step, since, seen) "
                       "VALUES (?,?,?,?,?,?,?)", (unit, kind, state.name, encoded, state.step, t, t))
    out = []
    for role in state.holders:
        for a in resolve(role, who):
            if a in AGENTS:
                more = (who.get("notes") or {}).get((role, a)) or (who.get("notes") or {}).get(role) or ""
                out.append(Owe(a, state.name, f"{label} ({state.name}): {state.step}{more}", state.quiet))
    return out


def sync(unit, owes):
    """What a unit owes now: what it no longer owes is discharged, and what
    is new is woken."""
    new = []
    with LOCK:
        rows = {(r["agent"], r["what"]): r for r in DB.execute("SELECT * FROM obligations WHERE unit=?", (unit,))}
        want = {}
        for o in owes:
            want.setdefault((o.agent, o.what), o)
        gone = [r for k, r in rows.items() if k not in want]
        for r in gone:
            DB.execute("DELETE FROM obligations WHERE id=?", (r["id"],))
        # What isn't quiet first, so that a quiet one waits for it.
        for k, o in sorted(want.items(), key=lambda kv: kv[1].quiet):
            if k in rows:
                DB.execute("UPDATE obligations SET description=?, quiet=? WHERE id=?",
                           (o.description, int(o.quiet), rows[k]["id"]))
            else:
                cur = DB.execute("INSERT INTO obligations (agent, unit, what, description, quiet, since, next_at) "
                                 "VALUES (?,?,?,?,?,?,?)", (o.agent, unit, o.what, o.description, int(o.quiet),
                                                            now(), now()))
                new.append(cur.lastrowid)
    for r in gone:
        if r["escalated"]:
            unstall(r)
    for i in new:
        advance(i, fresh=True)


def obligation(i):
    rows = q("SELECT * FROM obligations WHERE id=?", i)
    return rows[0] if rows else None


def advance(i, fresh=False):
    """Move an obligation on: wake its agent when it is due, or put it to the
    human once its tries are spent. Before a second wake, or the human, its
    unit is evaluated again, so only what still stands goes further."""
    r = obligation(i)
    if not r or r["escalated"]:
        return
    if r["trigger"] is not None:
        t = q("SELECT run_id FROM triggers WHERE id=?", r["trigger"])
        run = t[0]["run_id"] if t else -1
        if run is None:
            return  # queued
        if run > 0:
            ended = q("SELECT ended FROM runs WHERE id=?", run)
            if not ended or ended[0]["ended"] is None:
                return  # under way
            end = ended[0]["ended"]
        else:
            end = now()  # dropped by hand (scripts/drain.sh, /admin/drop)
        q("UPDATE obligations SET trigger=NULL, next_at=? WHERE id=?", end + BACKOFF[min(r["wakes"], TRIES) - 1], i)
        r = obligation(i)
    if r["next_at"] is None or r["next_at"] > now():
        return
    if r["quiet"] and busy(r["agent"]):
        return
    if not fresh:
        try:
            evaluate_unit(r["unit"])
        except Exception as e:
            log(f"evaluating {r['unit']} again failed: {e!r}")
            return
        r = obligation(i)
        if not r or r["escalated"] or r["trigger"] is not None:
            return
    if r["wakes"] >= TRIES:
        escalate(r)
        return
    again = (f" (woken again, {r['wakes'] + 1} of {TRIES}, since nothing has changed: do it now, or record what it "
             "waits for where the kit can see it, with a Blocked-by: line, Waiting-on:, or needs-human with a "
             "decision; otherwise it becomes the human's)") if r["wakes"] else ""
    tid = add_trigger(r["agent"], f"owe:{r['unit']}:{i}:{r['wakes'] + 1}", r["description"] + again)
    if tid:
        q("UPDATE obligations SET wakes=wakes+1, trigger=?, next_at=NULL WHERE id=?", tid, i)


def escalate(r):
    """An obligation its agent was woken for TRIES times without it going
    away: the human's, and the supervisor's to find out why."""
    q("UPDATE obligations SET escalated=1 WHERE id=?", r["id"])
    link = unit_link(r["unit"])
    decision_issue(COORDINATION, stalled_key(r), "stalled",
                   f"Stalled: {r['unit']}", {
        "Question": f"`{r['agent']}` was woken {r['wakes']} times for [{r['unit']}]({link}), and it still owes the "
                    f"same: {r['description']}. What should happen?",
        "Options": f"- Find out why: `scripts/activity.sh {r['unit']}` and `scripts/activity.sh {r['agent']}` show "
                   "what happened; if the kit lacks a step, fix the kit.\n"
                   f"- Tell `{r['agent']}` on the item what to do.\n"
                   "- Close the item, saying why.",
        "Recommendation": "Find out why first: an agent that can't do a step should say what it waits for in a way "
                          "the kit can see, and one that doesn't is a gap in the protocol or its role."
                          + (" The supervisor has been woken to look into it too." if SUPERVISOR else ""),
        "If you say go ahead": "Nothing changes by itself: say on the item what should happen, then close this "
                               "issue. The kit doesn't wake anyone for it again until what it owes changes. If "
                               f"`{r['agent']}` no longer owes it before you act, the kit says so here and closes "
                               "this issue.",
        "Blocked on this": f"{r['unit']}."})
    log(f"{r['agent']} was woken {r['wakes']} times for {r['unit']} ({r['what']}): it is the human's")
    if SUPERVISOR and SUPERVISOR != r["agent"] and not capped(SUPERVISOR):
        add_trigger(SUPERVISOR, f"supervise:{r['unit']}:{r['id']}",
                    f"{r['unit']} has stalled: {r['agent']} was woken {r['wakes']} times for it without it moving "
                    f"on, and it is now a decision for the human. What it owes: {r['description']}. Find out why, "
                    "move it on if you can, and report the gap (Role: Supervision)")


def stalled_key(r):
    return f"stalled:{r['unit']}:{r['what']}:{r['agent']}:{int(r['since'])}"


def unstall(r):
    """An escalated obligation that went away: its `Stalled:` decision is
    closed, saying so, unless the human closed it already."""
    rows = q("SELECT number FROM notices WHERE key=? AND posted=1", stalled_key(r))
    if not rows or not rows[0]["number"]:
        return
    n = rows[0]["number"]
    try:
        if forge("GET", f"/repos/{ORG}/{COORDINATION}/issues/{n}").get("state") != "open":
            return
        forge("POST", f"/repos/{ORG}/{COORDINATION}/issues/{n}/comments", {"body": (
            f"{NO_REPLY}\n\n@{HUMAN} Moved on: `{r['agent']}` no longer owes this ({r['what']}), so it needs "
            "nothing from you. Closed by the kit; whatever kept it stalled may still be worth a look.")})
        forge("PATCH", f"/repos/{ORG}/{COORDINATION}/issues/{n}", {"state": "closed"})
        log(f"{r['unit']} moved on: closed its decision {COORDINATION}#{n}")
    except Exception as e:
        log(f"couldn't close the decision {COORDINATION}#{n} for {r['unit']}: {e!r}")


def unit_link(unit):
    """A unit's page in Forgejo, as the human's browser reaches it."""
    m = re.fullmatch(r"([\w.-]+)#(\d+)", unit)
    if m:
        return f"{FORGE_WEB}/{ORG}/{m[1]}/issues/{m[2]}"
    if unit.startswith("release:"):
        return f"{FORGE_WEB}/{ORG}/{unit[len('release:'):]}"
    return f"{FORGE_WEB}/{ORG}/{COORDINATION}/milestones"


def wake_due():
    """Move every obligation on that may be due."""
    for r in q("SELECT id FROM obligations WHERE escalated=0 ORDER BY id"):
        try:
            advance(r["id"])
        except Exception as e:
            log(f"moving obligation {r['id']} on failed: {e!r}")


def evaluate_unit(unit):
    m = re.fullmatch(r"([\w.-]+)#(\d+)", unit)
    if m:
        evaluate(m[1], int(m[2]), dependents=False)
    elif unit.startswith("release:"):
        evaluate_release(unit[len("release:"):])
    else:
        evaluate_milestones()


# Timers: a state held by a timer (a head settling, CI that may be late, a
# scheduled merge that may be stuck) is evaluated again when it runs out.
_rechecks = {}


def recheck(unit, at):
    if not at or at <= now():
        return
    with LOCK:
        if unit in _rechecks and _rechecks[unit] <= at:
            return
        _rechecks[unit] = at

    def run():
        with LOCK:
            if _rechecks.get(unit) != at:
                return
            del _rechecks[unit]
        try:
            evaluate_unit(unit)
        except Exception as e:
            log(f"evaluating {unit} again failed: {e!r}")
    t = threading.Timer(at - now() + 1, run)
    t.daemon = True
    t.start()


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


def item_state(it):
    """An item's state, its facts and roles, and what blocked_rules found:
    its notices, whether it is blocked, and what it names."""
    owed, blocked, unblocked_at, found = blocked_rules(it)
    if it.is_pr:
        facts, who = pull_facts(it, blocked)
    else:
        facts, who = issue_facts(it, blocked, unblocked_at)
    return ST.state(facts), facts, who, owed, blocked, found


def owed_on(it):
    """Everything owed on an item: its obligations (Owe) and its notices
    (Owed), whether it is blocked (None if it names no blocker), and the
    issues it names."""
    state, facts, who, owed, blocked, found = item_state(it)
    owes = state_owes(it.at, "pull request" if it.is_pr else facts.kind, state, who, f"{it.kind} {it.at}")
    owes += [Owe(a, what, desc, False) for a, what, desc in asks(it)]
    recheck(it.at, who.get("recheck"))
    return owes, owed + notices(it), blocked, found


def stale_blocked(it):
    """Whether its `blocked` label is the dispatcher's, left with no
    Blocked-by: lines to keep it (demos#74, whose label outlived its lines).
    One someone else put on stays theirs."""
    added = [e for e in it.events if e.get("type") == "label"
             and (e.get("label") or {}).get("name") == "blocked" and (e.get("body") or "") == "1"]
    return "blocked" in it.labels and bool(added) and login(added[-1]) == "dispatcher"


def set_blocked(it, blocked):
    """Keep the `blocked` label in step with the Blocked-by: lines; with none
    left, the dispatcher's own label comes off."""
    if blocked is None and it.open and stale_blocked(it):
        blocked = False
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
    issue = item_reads(repo, n)["issue"]
    labels = {l["name"] for l in issue.get("labels") or []}
    if issue.get("state") != "open" or "upstream" not in labels:
        return
    want = False
    for d in q("SELECT repo, number FROM blocked_by WHERE on_repo=? AND on_number=?", repo, n):
        try:
            if item_reads(d["repo"], d["number"])["issue"].get("state") == "open":
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


def evaluate(repo, n, dependents=True, seed=False, fresh=True):
    """What an item owes now: its obligations, and its notices queued; keep
    its `blocked` label and the index of what it waits on; then do the same
    for the items waiting on it, and the issues a pull request refers to.
    fresh reads the item afresh, as for a webhook or a claim; the sweep and
    the items around it use what was read before, if unchanged (Items)."""
    try:
        it = Item(repo, n, fresh=fresh)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            note_waiting(repo, n, False)
            sync(f"{repo}#{n}", [])
            return
        raise
    note_waiting(repo, n, waits_on_you(it.issue, it.pr))
    owes, owed, blocked, found = owed_on(it)
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
    sync(it.at, owes)
    if not dependents:
        return
    if it.is_pr:
        # A pull request opened or merged changes what the requests it refers
        # to owe.
        for state in ("open", "merged"):
            _cache.pop(("prs", repo, state), None)
        _cache.pop(("refs", repo), None)
        for k in pr_refs(repo, it.pr):
            try:
                evaluate(repo, k, dependents=False, fresh=False)
            except Exception as e:
                log(f"evaluating {repo}#{k} failed: {e!r}")
    for d in q("SELECT repo, number FROM blocked_by WHERE on_repo=? AND on_number=?", repo, n):
        try:
            evaluate(d["repo"], d["number"], dependents=False, fresh=False)
        except Exception as e:
            log(f"evaluating {d['repo']}#{d['number']} failed: {e!r}")


def evaluate_release(repo):
    """What a repository's release owes now."""
    unit = f"release:{repo}"
    if not OWNER_OF.get(repo) or not has_ci(repo):
        return
    items = forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=all")
    listed(repo, items)
    facts, who, _ = release_facts(repo, items)
    sync(unit, state_owes(unit, "release", ST.state(facts), who, f"the release of {repo}"))
    recheck(unit, who.get("recheck"))


def evaluate_milestones():
    """What each milestone, and the kit itself, owes now."""
    if not COORDINATOR or forge_missing(COORDINATION):
        return
    by_title = milestones_by_title()
    issues = forge_all(f"/repos/{ORG}/{COORDINATION}/issues?state=all&type=issues&labels=milestone")
    who = {ST.OWNER: [COORDINATOR], ST.COORDINATOR: [COORDINATOR]}
    seen = set()
    for title, spans in by_title.items():
        mine = [i for i in issues if "milestone" in {l["name"] for l in i.get("labels") or []}
                and (i.get("title") == f"Milestone: {title}" or (i.get("milestone") or {}).get("title") == title)]
        f = ST.MilestoneFacts(
            closed=all(m.get("state") == "closed" for _, m in spans),
            open_work=any(m.get("open_issues", 0) > 0 for _, m in spans),
            had_work=any(m.get("closed_issues", 0) > 0 for _, m in spans),
            issue="none" if not mine else "open" if any(i.get("state") == "open" for i in mine) else "closed")
        unit = f"milestone:{title}"
        seen.add(unit)
        repos = ", ".join(sorted({r for r, m in spans if m.get("state", "open") == "open"}))
        notes = {ST.COORDINATOR: f" (open on {repos})" if repos else ""}
        sync(unit, state_owes(unit, "milestone", ST.state(f), dict(who, notes=notes), f"milestone {title}"))
    for r in q("SELECT DISTINCT unit FROM obligations WHERE unit LIKE 'milestone:%'"):
        if r["unit"] not in seen:
            sync(r["unit"], [])
    every = [m for spans in by_title.values() for _, m in spans]
    roadmap = file_text(COORDINATION, "ROADMAP.md", "main") or ""
    decisions = [i for i in forge_list(f"/repos/{ORG}/{COORDINATION}/issues?state=open&type=issues&assigned_by={HUMAN}")
                 if (i.get("user") or {}).get("login") == COORDINATOR
                 and not {l["name"] for l in i.get("labels") or []} & {"recommendation", "milestone"}]
    f = ST.KitFacts(
        milestones="open-work" if any(m.get("open_issues", 0) > 0 for m in every)
        else "done" if any(m.get("closed_issues", 0) > 0 for m in every) else "none-ever",
        finished=bool(ROADMAP_FINISHED.search(roadmap)),
        planning=bool(forge_list(f"/repos/{ORG}/{COORDINATION}/pulls?state=open")),
        decision=bool(decisions))
    sync("kit", state_owes("kit", "kit", ST.state(f), who, "the project"))


def evaluate_repo(repo, items=False, seed=False):
    """A repository's release, and with items, its open issues and pull
    requests."""
    try:
        evaluate_release(repo)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    if items:
        open_now = forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=all")
        listed(repo, open_now)
        for i in open_now:
            try:
                evaluate(repo, i["number"], dependents=False, seed=seed, fresh=False)
            except Exception as e:
                log(f"evaluating {repo}#{i['number']} failed: {e!r}")
        # Only an open item waits on the human: one that closed with no
        # webhook to say so leaves the index here.
        still = {i["number"] for i in open_now}
        for r in q("SELECT number FROM human_waits WHERE repo=?", repo):
            if r["number"] not in still:
                note_waiting(repo, r["number"], False)


def no_longer_owed(rows):
    """The ids of queued triggers, among obligations and notices, that are no
    longer owed: the pull request merged meanwhile, the comment was answered,
    the head moved on. Each unit behind them is evaluated again first."""
    units, items, stale = {}, {}, []
    for r in rows:
        if r["key"].startswith("owe:"):
            unit, i, _ = r["key"][len("owe:"):].rsplit(":", 2)
            units.setdefault(unit, []).append((r, int(i)))
            continue
        m = RULE_KEY.match(r["key"])
        if m and m[1] in RULE_KINDS:
            items.setdefault((m[2], int(m[3])), []).append(r)
    for unit, rs in units.items():
        try:
            evaluate_unit(unit)
        except Exception as e:
            log(f"couldn't check {unit} is still owed: {e!r}")
            continue
        for r, i in rs:
            o = obligation(i)
            if not o or o["escalated"] or o["trigger"] != r["id"]:
                stale.append(r["id"])
    for (repo, n), rs in items.items():
        try:
            it = Item(repo, n, fresh=True)
            keys = {o.key for o in blocked_rules(it)[0] + notices(it)}
        except urllib.error.HTTPError as e:
            if e.code != 404:
                continue
            keys = set()
        except Exception as e:
            log(f"couldn't check {repo}#{n} is still owed: {e!r}")
            continue
        stale += [r["id"] for r in rs if r["key"] not in keys]
    return stale


def sweep(seed=False):
    """Every repository's release and open issues and pull requests, issues
    closed lately (a rejected request), the milestones and the kit
    evaluated: what a missed webhook would have lost, and what no webhook
    brings."""
    prune_items()
    since = iso(max(float(kv_get("reconciled", 0)) - RECONCILE, now() - 86400))
    ok = True
    for repo in OWNER_OF:
        try:
            evaluate_repo(repo, items=True, seed=seed)
            closed = forge_all(f"/repos/{ORG}/{repo}/issues?state=closed&type=issues&since={since}")
            listed(repo, closed)
            for i in closed:
                evaluate(repo, i["number"], dependents=False, seed=seed, fresh=False)
            # Upstream issues, once every item's Blocked-by: lines are read.
            upstream = forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=issues&labels=upstream")
            listed(repo, upstream)
            for item in upstream:
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
    try:
        evaluate_milestones()
    except Exception as e:
        ok = False
        log(f"evaluating the milestones failed: {e!r}")
    kv_set("reconciled", now())
    return ok


def retire_rule_triggers():
    """Once, on a ledger from before the states: the work the old rules
    queued and no run has taken is dropped, since obligations take its
    place, with descriptions of their own."""
    if kv_get("obligations-started"):
        return
    rows = [r for r in q("SELECT id, key FROM triggers WHERE run_id IS NULL")
            if r["key"].split(":")[0] in RETIRED_KINDS]
    for r in rows:
        q("UPDATE triggers SET run_id=-1 WHERE id=?", r["id"])
    kv_set("obligations-started", now())
    if rows:
        log(f"dropped {len(rows)} trigger(s) the old rules queued: obligations take their place")


def reconcile():
    """The first sweep on a ledger from before the notices records what they
    find as handled where the old triggers covered it (SEED_KINDS)."""
    retire_rule_triggers()
    if not kv_get("rules-seeded"):
        if sweep(seed=True):
            kv_set("rules-seeded", now())
            log("the rules' first sweep is done: from now on they queue work")
    else:
        sweep()


def evaluate_after_run(agent):
    """An agent's run has ended: what it left its open pull requests owing
    (5) was held while it could still act on them, and its accepted requests
    (7) while it was busy. Without this, they wait for the sweep."""
    repo = AGENTS.get(agent, {}).get("own")
    if repo not in OWNER_OF or may_push(agent):
        return
    _cache.pop(("prs", repo, "open"), None)
    _cache.pop(("prs", repo, "merged"), None)
    items = [p["number"] for p, _ in owner_prs(repo, agent, "open")]
    items += [i["number"] for i in forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=issues&labels=accepted")
              if "accepted" in {l["name"] for l in i.get("labels") or []}]
    for n in items:
        try:
            evaluate(repo, n, dependents=False)
        except Exception as e:
            log(f"evaluating {repo}#{n} failed: {e!r}")


def evaluate_labelled(repo, name):
    for i in forge_all(f"/repos/{ORG}/{repo}/issues?state=open&type=all&labels={name}"):
        try:
            evaluate(repo, i["number"], dependents=False)
        except Exception as e:
            log(f"evaluating {repo}#{i['number']} failed: {e!r}")


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


# --- Idle summaries -------------------------------------------------------------

IDLE = int(os.environ.get("IDLE_SECONDS", "1200"))
IDLE_TRIGGER = "the kit is idle"  # each idle trigger begins so


def last_activity():
    """When anything last happened: an event, work queued, a run ending or
    the dispatcher starting, leaving out the idle summaries themselves (and
    the human's sessions, so reading one doesn't call for another)."""
    found = [STARTED, q("SELECT MAX(at) AS m FROM events")[0]["m"],
             q("SELECT MAX(created) AS m FROM triggers WHERE key NOT LIKE 'idle:%'")[0]["m"],
             q("SELECT MAX(ended) AS m FROM runs WHERE triggers NOT LIKE ?", f"%{IDLE_TRIGGER}%")[0]["m"]]
    return max([t for t in found if t] or [0])


# Whether CI is running is read from Forgejo only where it may have changed:
# a webhook that can start or end a run marks its repository (ci_may_run),
# and the idler checks the marked ones, and those found busy, once a minute
# (refresh_ci). The sweep checks every repository, for what no webhook said.
# So ci_running, which the status page asks every few seconds, reads nothing
# from Forgejo, and a quiet kit asks it nothing about CI.

CI_LOCK = threading.Lock()
CI_MARKED = {}   # repo: when a webhook marked it, until a check after that
CI_BUSY = set()  # repos whose last check found a run under way


def ci_may_run(repo):
    if repo in OWNER_OF and has_ci(repo):
        with CI_LOCK:
            CI_MARKED[repo] = now()


def ci_busy(repo):
    """Whether an Actions run is under way on a repository. A run left
    waiting for over an hour (its pull request closed before a runner took
    it) doesn't count."""
    if cached(("missing", repo), 300, lambda: forge_missing(repo)):
        return False
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


def refresh_ci(every=False):
    """Check the repositories marked or found busy, or with every, all of
    them. A repository marked again during its check stays marked, and one
    that can't be checked is marked, so that ci_running errs towards busy."""
    with CI_LOCK:
        repos = [r for r in OWNER_OF if has_ci(r)] if every else sorted(set(CI_MARKED) | CI_BUSY)
    for repo in repos:
        checked = now()
        try:
            busy = ci_busy(repo)
        except Exception as e:
            log(f"couldn't tell whether CI runs on {repo}: {e!r}")
            ci_may_run(repo)
            continue
        with CI_LOCK:
            (CI_BUSY.add if busy else CI_BUSY.discard)(repo)
            if CI_MARKED.get(repo, checked + 1) <= checked:
                del CI_MARKED[repo]


def ci_running():
    """Whether an Actions run is under way, or may be: a repository marked
    and not yet checked counts. From what refresh_ci last found, never from
    Forgejo."""
    with CI_LOCK:
        return bool(CI_MARKED or CI_BUSY)


def check_idle():
    """Once the kit has gone quiet (no run, no queued work except an agent's
    held by its cap, which is the human's to lift, no CI, and nothing new for
    IDLE seconds): wake the coordinator to tell the human what it needs from
    them. Once per quiet spell."""
    # Draining looks quiet, but only because nothing may start: the summary
    # couldn't tell anything true.
    if not COORDINATOR or IDLE <= 0 or paused() or draining():
        return
    if q("SELECT 1 FROM runs WHERE ended IS NULL"):
        return
    if any(not capped(r["agent"]) for r in q("SELECT DISTINCT agent FROM triggers WHERE run_id IS NULL")):
        return
    last = last_activity()
    if now() - last < IDLE or float(kv_get("idle-summary", 0)) >= last:
        return
    if ci_running():
        return
    kv_set("idle-summary", now())
    # Only when what waits on the human has changed since the last summary:
    # otherwise it says again what they already know (coordination#156).
    items = waiting_for_human()
    if kv_get("idle-waiting") == json.dumps(items):
        log("quiet, and the same items wait on the human as at the last idle summary: no summary")
        return
    kv_set("idle-waiting", json.dumps(items))
    n = len(items)
    waiting = f"{n} item{'' if n == 1 else 's'} wait{'s' if n == 1 else ''} on you" if n else "nothing waits on you"
    add_trigger(COORDINATOR, f"idle:{int(now())}", f"{IDLE_TRIGGER}; {waiting}")


def idler():
    while True:
        time.sleep(60)
        try:
            abandon_unstarted()
        except Exception as e:
            log(f"closing unstarted runs failed: {e!r}")
        try:
            wake_due()
        except Exception as e:
            log(f"moving obligations on failed: {e!r}")
        try:
            refresh_ci()
        except Exception as e:
            log(f"checking CI failed: {e!r}")
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
        try:
            refresh_ci(every=True)
        except Exception as e:
            log(f"checking CI everywhere failed: {e!r}")
        time.sleep(RECONCILE)


# --- Claims and runs ------------------------------------------------------------

def day_start():
    """Midnight UTC today: caps are per UTC day."""
    return now() // 86400 * 86400


def runs_today(agent):
    """Runs claimed today, except those that never got going: retrying
    through an outage mustn't use up the agent's day."""
    return q("""SELECT COUNT(*) AS n FROM runs WHERE agent=? AND claimed>=?
                AND NOT (ended IS NOT NULL AND turns IS NOT NULL AND turns<=1 AND COALESCE(cost, 0)=0
                         AND COALESCE(forge_changes, 0)=0 AND outcome NOT IN ('completed', 'interrupted'))""",
             agent, day_start())[0]["n"]


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


# An idle runner asks for work every RUNNER_TICK_SECONDS (15): one silent this
# long can't take the work queued for it.
RUNNER_SILENT = 180


def runner_seen(agent):
    """Record that the agent's runner called in, at most every 30 seconds."""
    if now() - float(kv_get(f"seen:{agent}", 0)) >= 30:
        kv_set(f"seen:{agent}", now())


def runner_silent_for(agent):
    """How long since the agent's runner last called in, or None if it has
    called lately or never has (since the dispatcher started recording)."""
    seen = float(kv_get(f"seen:{agent}", 0))
    return now() - seen if seen and now() - seen > RUNNER_SILENT else None


def agent_state(agent):
    if q("SELECT 1 FROM runs WHERE agent=? AND ended IS NULL", agent):
        return "running"
    if paused():
        return "paused"
    if draining():
        return "draining"
    if capped(agent):
        return "capped"
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
    try:
        prio = priorities(agent, [r["key"] for r in rows])
    except Exception as e:
        log(f"couldn't read {agent}'s priority items: {e!r}")
        prio = (set(), [])
    return due(agent, grant=True, prio=prio)


TRIGGER_ITEM = re.compile(r"^[a-z-]+:([A-Za-z0-9_.-]+)#(\d+)(?::|$)")


def priorities(agent, keys):
    """Which of these triggers' items are labelled priority, and every open
    issue labelled priority on the agent's own repository,
    as `repo#n`."""
    def labelled(repo, n):
        issue = cached(("issue", repo, n), 20, lambda: forge("GET", f"/repos/{ORG}/{repo}/issues/{n}"))
        return issue.get("state") == "open" and PRIORITY in {l["name"] for l in issue.get("labels") or []}
    items = {(m[1], int(m[2])) for k in keys for m in [TRIGGER_ITEM.match(k)] if m}
    hot = {f"{r}#{n}" for r, n in items if labelled(r, n)}
    own = AGENTS[agent]["own"]
    mine = []
    if own and not forge_missing(own):
        mine = [f"{own}#{i['number']}" for i in
                forge_list(f"/repos/{ORG}/{own}/issues?state=open&type=all&labels={PRIORITY}")]
    return hot, mine


def due(agent, grant=False, prio=(set(), [])):
    """Whether the agent's work is due: no run, not paused, under its caps,
    and its newest trigger settled for DEBOUNCE (or its oldest waited
    MAX_DELAY). With grant, the run is started and returned, its priority
    work first: prio is the triggers' items labelled priority, and the
    priority items open on the agent's own repository."""
    with LOCK:
        if DB.execute("SELECT 1 FROM runs WHERE agent=? AND ended IS NULL", (agent,)).fetchone():
            return None
        if paused() or draining():
            return None
        rows = DB.execute("SELECT id, key, description, created FROM triggers WHERE agent=? AND run_id IS NULL "
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
        hot, mine = prio
        first = [f"priority: {r['description']}" for r in rows if item_of(r["key"]) in hot]
        rest = [r["description"] for r in rows if item_of(r["key"]) not in hot]
        others = [m for m in mine if m not in hot]
        if others:
            first.append(f"priority items open on your repository: {', '.join(others)}: they come before "
                         "anything new (Protocol: Priority)")
        descs = list(dict.fromkeys(first + rest))
        cur = DB.execute("INSERT INTO runs (agent, triggers, claimed) VALUES (?,?,?)",
                         (agent, json.dumps(descs), t))
        run_id = cur.lastrowid
        DB.execute(f"UPDATE triggers SET run_id=?, env_held=0 WHERE id IN ({','.join('?' * len(rows))})",
                   (run_id, *[r["id"] for r in rows]))
    log(f"{agent} claimed run {run_id}: {', '.join(descs)}")
    return {"run": run_id, "triggers": descs}


def item_of(key):
    """The `repo#n` a trigger's key names, if it names one."""
    m = TRIGGER_ITEM.match(key)
    return f"{m[1]}#{m[2]}" if m else None


def requeue(run_id):
    """The run's work is still to do, through no fault of the run (the runner
    restarted, or the kit was paused under it): queue it again as it was."""
    q("UPDATE triggers SET run_id=NULL WHERE run_id=?", run_id)


# A claimed run whose runner hasn't reported its start by then never will: the
# runner clones new repositories (up to ten minutes) before it starts one.
CLAIM_START = int(os.environ.get("CLAIM_START_SECONDS", "900"))


def undo_claim(run_id, why):
    """A claim no runner will start: close it unstarted, and queue its work
    again as it was, since while it is open the agent gets no other run."""
    q("UPDATE runs SET ended=?, outcome=? WHERE id=? AND started IS NULL AND ended IS NULL",
      now(), f"never started: {why}", run_id)
    requeue(run_id)
    log(f"run {run_id} never started ({why}); its work is queued again")


def abandon_unstarted():
    """Close the runs claimed over CLAIM_START seconds ago that no runner started."""
    for r in q("SELECT id FROM runs WHERE started IS NULL AND ended IS NULL AND claimed<?", now() - CLAIM_START):
        undo_claim(r["id"], f"no runner started it within {CLAIM_START // 60} minutes of its claim")


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
        q("UPDATE triggers SET run_id=NULL, attempts=?, not_before=?, env_held=0 WHERE run_id=?",
          attempt, now() + RETRY_DELAY * attempt, run_id)
        log(f"{agent} run {run_id} {outcome}: its work is queued again (retry {attempt} of {RETRIES})")
        return True
    work = "\n".join(f"- {d}" for d in dict.fromkeys(r["description"] for r in rows))
    notify_agent(agent, f"retries:{agent}:{run_id}", "failed-run",
                 f"{agent}'s work failed {attempt} times (run {run_id})", {
        "Question": f"Run {run_id} of `{agent}` ended `{outcome}`, the last of {attempt} tries at the same work. "
                    "It won't be tried again by itself. What should happen to it?",
        "Options": f"- Find out why from the run's log (`scripts/activity.sh {agent}`), or by opening the run "
                   f"in the Claude extension (`scripts/open.sh {agent}`) and asking it, then fix the cause. Your "
                   "session can't do the run's work itself: file what remains as an issue for the agent.\n"
                   "- Leave it: some of it (pull requests, untriaged requests, stalled work) is found again "
                   "by the dispatcher's reconciliation; the rest waits for the next event on it.",
        "Recommendation": "Find out why first: a run that fails every time is usually a cause outside it, such "
                          "as a credential, the run length (`MAX_RUN_MINUTES`) or the turn limit.",
        "If you say go ahead": "Nothing happens on its own. Close this issue once you've dealt with it.",
        "Blocked on this": f"`{agent}`'s work from that run:\n{work}"})
    return False


def never_got_going(r, changes):
    """A failed run that did nothing: at most one turn, nothing spent, no
    change in Forgejo (coordination#202). A run with no turns reported
    didn't say, so it is an ordinary failure."""
    return (r.get("outcome") or "") not in ("completed", "interrupted") and r.get("turns") is not None \
        and r["turns"] <= 1 and not r.get("cost") and not changes


# "You've hit your session limit · resets 4:40pm (UTC)"; a weekly limit may
# name its day ("resets Nov 30, 9am (Europe/London)").
RESETS = re.compile(r"\bresets\s+(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\s*\((?P<tz>[^)]+)\)", re.I)
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def limit_resets(outcome, at=None):
    """When the usage limit a run's outcome names resets, as a timestamp; None
    if it names none, or not in a form understood here."""
    m = RESETS.search(outcome or "")
    if not m:
        return None
    try:
        tz = timezone.utc if m["tz"].strip().upper() == "UTC" else ZoneInfo(m["tz"].strip())
        at = now() if at is None else at
        local = datetime.fromtimestamp(at, tz)
        t = local.replace(hour=int(m["h"]) % 12 + (12 if m["ap"].lower() == "pm" else 0),
                          minute=int(m["m"] or 0), second=0, microsecond=0)
        if m["mon"]:
            t = t.replace(month=MONTHS.index(m["mon"].lower()) + 1, day=int(m["day"]))
            if t < local - timedelta(days=1):
                t = t.replace(year=t.year + 1)
        elif t < local - timedelta(hours=1):  # a clock a little ahead of the API's is still today
            t += timedelta(days=1)
    except (ValueError, KeyError, ZoneInfoNotFoundError):
        return None
    return t.timestamp() if t.timestamp() - at < 8 * 86400 else None


def env_retry(agent, run_id, outcome):
    """Queue a run's work again without counting it as a try: after the usage
    limit its outcome names resets, or else after the next of ENV_DELAYS. The
    human hears once while runs keep failing this way."""
    k = int(kv_get(f"env-failures:{agent}", "0"))
    kv_set(f"env-failures:{agent}", str(k + 1))
    reset = limit_resets(outcome)
    until = max(reset + LIMIT_MARGIN, now() + 60) if reset else now() + ENV_DELAYS[min(k, len(ENV_DELAYS) - 1)]
    q("UPDATE triggers SET run_id=NULL, not_before=?, env_held=1 WHERE run_id=?", until, run_id)
    if reset:
        # The limit is the account's, so every agent's held work waits for it,
        # and none waits longer.
        q("UPDATE triggers SET not_before=? WHERE env_held=1 AND run_id IS NULL", until)
    since = kv_get("env-failing")
    if not since:
        since = iso(now())
        kv_set("env-failing", since)
    add_notice(f"env-failing:{since}", "env-failing", None, None,
               f"runs are failing before they get going ({agent} run {run_id}: {outcome}). Their work is retried "
               + (f"after the usage limit resets, at {iso(until)}. " if reset else
                  f"after {', then '.join(f'{s // 60} minutes' for s in ENV_DELAYS)}, and then at that interval, "
                  "until runs get going again. Look for an outage, the proxy or the Claude credential."))
    log(f"{agent} run {run_id} {outcome}: its work is queued again at {iso(until)}"
        + (", when the usage limit has reset" if reset else ""))


def got_going(agent):
    """The agent's run got going: its environment works again. The agents
    share most of it (the Claude credential and its usage limit, the
    proxies), so work held back for theirs is due now too, rather than
    up to an hour later."""
    q("DELETE FROM kv WHERE key=?", f"env-failures:{agent}")
    held = q("SELECT DISTINCT agent FROM triggers WHERE env_held=1 AND run_id IS NULL AND not_before>?", now())
    if held:
        q("UPDATE triggers SET not_before=?, env_held=0 WHERE env_held=1 AND run_id IS NULL", now())
        log(f"{agent}'s run got going: work held back for the environment is due now "
            f"({', '.join(r['agent'] for r in held)})")
    if not q("SELECT 1 FROM kv WHERE key LIKE 'env-failures:%'"):
        q("DELETE FROM kv WHERE key='env-failing'")


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
    # A usage limit cuts a run short however far it got: its environment's
    # fault, not its work's, so it is retried as one that never got going.
    idle = never_got_going(r, changes) or bool(limit_resets(outcome))
    if not idle:
        got_going(agent)
    if paused() and outcome != "interrupted":
        requeue(run_id)  # the pause refused its tool calls: it did what it could, not its work
    elif idle:
        env_retry(agent, run_id, outcome)
    elif outcome in ("completed", "interrupted") or not retry(agent, run_id, outcome):
        # Done; or stopped by the human; or given up on. What it left owing
        # (its obligations) moves on within a minute (wake_due).
        later(evaluate_after_run, agent)
    if not r.get("in_extension") and r.get("session_id"):
        sid = r.get("session_id")
        notify_agent(agent, f"not-listed:{agent}:{run_id}", "transcript-rewrite",
                     f"Run {run_id} of {agent} isn't listed in the Claude extension", {
            "Question": f"Run {run_id} of `{agent}` isn't listed in the Claude extension in its container, because "
                        "its transcript couldn't be rewritten. Do you need to open that conversation?",
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
    drain = draining()
    left = under_way() if drain else None
    return {"paused": paused(), "draining": drain, "under_way": left, "drained": drain and not left,
            "reconciled": iso(float(reconciled)) if reconciled else None,
            "agents": agents, "runs": runs, "sessions": sessions, "notices": notices,
            "obligations": [dict(o, since=iso(o["since"]), next_at=iso(o["next_at"])) for o in
                            q("SELECT unit, agent, what, description, wakes, escalated, since, next_at "
                              "FROM obligations ORDER BY agent, since")],
            "summary": kit_summary()}


def span(seconds):
    """A duration, as the human would say it."""
    m = int(seconds // 60)
    if m < 1:
        return "under a minute"
    if m < 120:
        return f"{m} minute{'' if m == 1 else 's'}"
    return f"{m // 60} hours"


def clip_line(text, n):
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def kit_summary():
    """Whether the kit will make progress by itself, is stuck, or nothing
    happens until the human acts, on its first line; then one indented line
    for each thing it is doing or waiting on. The same test as check_idle,
    without its wait for quiet."""
    waiting = waiting_on_human()
    ci = ci_running()
    wait = (f"{waiting} item{'' if waiting == 1 else 's'} wait{'s' if waiting == 1 else ''} on you"
            if waiting else "nothing waits on you")
    if paused():
        return f"Kit: paused, nothing starts until scripts/resume.sh; {wait}"
    if draining():
        left = under_way()
        return (f"Kit: draining, nothing starts until scripts/resume.sh; {wait}\n"
                + ("\n".join(f"  - finishing: {w}" for w in left) if left else "  - drained: safe to stop"))
    runs = q("SELECT id, agent, triggers, claimed, started FROM runs WHERE ended IS NULL ORDER BY id")
    queued = q("SELECT id, agent, created, not_before FROM triggers WHERE run_id IS NULL")
    capped_agents = sorted({t["agent"] for t in queued if capped(t["agent"])})
    queued = [t for t in queued if t["agent"] not in capped_agents]
    later_ids = {t["id"] for t in queued if t["not_before"] and t["not_before"] > now()}
    work, stuck = [], []
    for r in runs:
        if r["started"]:
            triggers = json.loads(r["triggers"] or "[]")
            more = f" (and {len(triggers) - 1} more)" if len(triggers) > 1 else ""
            work.append(f"{r['agent']} running for {span(now() - r['started'])}: "
                        f"{clip_line(triggers[0] if triggers else 'its work', 100)}{more}")
        elif now() - r["claimed"] > 120:
            stuck.append(f"{r['agent']}'s run {r['id']} was claimed {span(now() - r['claimed'])} ago and hasn't "
                         f"started (closed by itself after {CLAIM_START // 60} minutes, or restart its container)")
        else:
            work.append(f"{r['agent']} starting a run")
    busy_agents = {r["agent"] for r in runs}
    for agent in sorted({t["agent"] for t in queued}):
        mine = [t for t in queued if t["agent"] == agent]
        # A runner is silent while it runs one: only an idle one should call in.
        silent = (runner_silent_for(agent) if agent not in busy_agents
                  and any(t["id"] not in later_ids for t in mine) else None)
        if silent:
            stuck.append(f"{len(mine)} queued for {agent}, whose runner hasn't called in for {span(silent)}: "
                         f"restart its container")
            continue
        due = [t["not_before"] for t in mine if t["id"] in later_ids]
        held = f" ({len(due)} not yet due, the first at {iso(min(due))})" if due else ""
        work.append(f"{len(mine)} queued for {agent}, the oldest for {span(now() - min(t['created'] for t in mine))}"
                    f"{held}")
    if ci:
        work.append("CI running")
    failing = env_failing_text()
    notes = []
    if capped_agents:
        notes.append(f"capped with work waiting: {', '.join(capped_agents)} (raise its cap, or it waits for midnight UTC)")
    if stuck:
        head = "stuck"
    elif failing:
        head = "failing"
    elif work:
        head = "working"
    elif waiting or capped_agents:
        head = "quiet, nothing happens until you act"
    else:
        head = "quiet"
        notes.append(f"once it has been quiet for {IDLE // 60} minutes, the coordinator summarises what waits on you")
    lines = [f"stuck: {s}" for s in stuck] + ([f"failing: {failing}"] if failing else []) + work + notes
    return "\n".join([f"Kit: {head}; {wait}"] + [f"  - {line}" for line in lines])


def env_failing_text():
    """While runs fail before they get going: since when, for whom, and the
    latest such run's outcome, which names the cause (a usage limit, say)."""
    since = kv_get("env-failing")
    agents = sorted(r["key"].split(":", 1)[1] for r in q("SELECT key FROM kv WHERE key LIKE 'env-failures:%'"))
    if not since or not agents:
        return None
    last = q(f"SELECT agent, outcome FROM runs WHERE ended IS NOT NULL AND agent IN ({','.join('?' * len(agents))}) "
             "ORDER BY ended DESC LIMIT 1", *agents)
    latest = f"; the latest, {last[0]['agent']}'s, ended {clip_line(last[0]['outcome'] or '?', 160)}" if last else ""
    return (f"runs have failed before getting going since {since} ({', '.join(agents)}){latest}. "
            "Their work is tried again when due")


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
                        "text": f"session started in {s['agent']}'s container"})
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
            runner_seen(agent)
            data = self.json_body()
            parts = self.path.strip("/").split("/")
            if self.path == "/claim":
                c = claim(agent)
                if not c:
                    return self.reply(204, None)
                try:
                    return self.reply(200, c)
                except OSError as e:
                    # The runner gave up waiting: no one will run it.
                    undo_claim(c["run"], f"the claim's reply didn't reach the runner ({e!r})")
                    return
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
            if agent_for_key(self.bearer()) not in ("process", SUPERVISOR):
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
    STARTED = now()
    # Until the idler's first check, CI may be running anywhere.
    for r in OWNER_OF:
        ci_may_run(r)
    log(f"listening on :8080; agents: {', '.join(AGENTS)}")
    threading.Thread(target=reconciler, daemon=True).start()
    threading.Thread(target=idler, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
