#!/usr/bin/env python3
"""The dispatcher: turns Forgejo's webhooks into work for agents, hands that
work to each agent's runner, and keeps the run ledger.

  POST /hook                 Forgejo's webhooks, signed with the webhook secret
  POST /claim                a runner asks for work (Bearer: its runner key)
  POST /runs/<id>/start      the runner started the claimed run
  POST /runs/<id>/end        the run ended: outcome, cost, turns, denials...
  POST /runs/abandon         the runner restarted; unfinished runs are closed
  POST /sessions             the human's session in an agent's container
  GET  /status               agents, queues, recent runs, sessions, notices
  GET  /healthz

Only trusted authors (the human and the kit's accounts) start work, never an
agent's own actions, and never a comment whose first line is
`(no reply needed)`. Trigger descriptions carry only numbers and names, never
issue text, because they become a run's prompt.

A claim is granted only when the agent has no run, is under its daily cap,
the kit isn't paused, and its newest trigger has settled for the debounce
period (or its oldest has waited the maximum delay), so a burst of events
becomes one run.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(os.environ.get("STATE_DIR", "/state"))
SECRET_FILE = Path(os.environ.get("WEBHOOK_SECRET_FILE", "/run/secrets/dispatcher/webhook-secret"))
TOKEN_FILE = Path(os.environ.get("DISPATCHER_TOKEN_FILE", "/run/secrets/dispatcher/token"))
KEYS_DIR = Path(os.environ.get("RUNNER_KEYS_DIR", "/run/secrets/runner-keys"))
AGENTS_CONF = Path(os.environ.get("AGENTS_CONF", "/etc/kit/agents.conf"))
CONTROL = Path(os.environ.get("CONTROL_DIR", "/control"))
FORGE = os.environ.get("FORGE_URL", "http://forgejo:3000").rstrip("/")
ORG = os.environ.get("FORGE_ORG", "")
HUMAN = os.environ.get("HUMAN_USER", "")
CAP = int(os.environ.get("MAX_RUNS_PER_DAY", "20"))
DEBOUNCE = int(os.environ.get("DEBOUNCE_SECONDS", "60"))
MAX_DELAY = int(os.environ.get("MAX_DELAY_SECONDS", "900"))
KEEP_PAYLOADS = 500
MAX_BODY = 25 * 1024 * 1024
NO_REPLY = "(no reply needed)"


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
LOCK = threading.Lock()
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
CREATE INDEX IF NOT EXISTS triggers_pending ON triggers (agent, run_id);
CREATE INDEX IF NOT EXISTS events_sender ON events (sender, at);
""")


def q(sql, *args):
    with LOCK:
        return DB.execute(sql, args).fetchall()


def add_trigger(agent, key, description):
    with LOCK:
        if DB.execute("SELECT 1 FROM triggers WHERE agent=? AND key=? AND run_id IS NULL",
                      (agent, key)).fetchone():
            return
        DB.execute("INSERT INTO triggers (agent, key, description, created) VALUES (?,?,?,?)",
                   (agent, key, description, now()))
    log(f"queued for {agent}: {description}")


# --- Notices --------------------------------------------------------------------

def forge_post(path, body):
    req = urllib.request.Request(FORGE + "/api/v1" + path, method="POST",
                                 data=json.dumps(body).encode(), headers={
                                     "Authorization": f"token {TOKEN_FILE.read_text().strip()}",
                                     "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def notify(key, kind, repo, number, text):
    """Once per key: comment on the issue as dispatcher, mentioning the human,
    so it shows in their Forgejo notifications."""
    with LOCK:
        if DB.execute("SELECT 1 FROM notices WHERE key=?", (key,)).fetchone():
            return
        DB.execute("INSERT INTO notices (at, key, kind, repo, number, text, posted) VALUES (?,?,?,?,?,?,0)",
                   (now(), key, kind, repo, number, text))
    try:
        forge_post(f"/repos/{ORG}/{repo}/issues/{number}/comments",
                   {"body": f"{NO_REPLY}\n\n@{HUMAN} {text}"})
        q("UPDATE notices SET posted=1 WHERE key=?", key)
        log(f"notified {HUMAN}: {repo}#{number}: {kind}")
    except Exception as e:
        log(f"couldn't post the notice for {repo}#{number}: {e}")


# --- Webhooks -> triggers -------------------------------------------------------

def first_line(text):
    return ((text or "").strip().splitlines() or [""])[0].strip()


def wants_reply(body):
    return not first_line(body).startswith(NO_REPLY)


def labels_changed(repo, number, labels):
    """Labels added since the last time this issue was seen."""
    with LOCK:
        row = DB.execute("SELECT labels FROM labels WHERE repo=? AND number=?", (repo, number)).fetchone()
        DB.execute("INSERT OR REPLACE INTO labels (repo, number, labels) VALUES (?,?,?)",
                   (repo, number, json.dumps(sorted(labels))))
    before = set(json.loads(row["labels"])) if row else set()
    return set(labels) - before


def handle(event, payload):
    repo = (payload.get("repository") or {}).get("name") or ""
    sender = (payload.get("sender") or {}).get("login") or ""
    if (payload.get("repository") or {}).get("owner", {}).get("login", ORG).lower() != ORG.lower():
        return
    owner = OWNER_OF.get(repo)
    trusted = sender in TRUSTED

    if event == "issues":
        issue = payload.get("issue") or {}
        n = issue.get("number")
        labels = {l["name"] for l in issue.get("labels") or []}
        added = labels_changed(repo, n, labels)
        author = (issue.get("user") or {}).get("login")
        if not trusted:
            return
        if "blocking" in added and "upstream" in labels:
            notify(f"blocking:{repo}#{n}", "blocking", repo, n,
                   "this upstream issue is marked `blocking`: an agent can't continue until it's resolved upstream. "
                   "Close it or comment on it once a fix is available.")
        if "needs-human" in added:
            notify(f"needs-human:{repo}#{n}:{int(now())}", "needs-human", repo, n,
                   "this needs your decision (`needs-human`).")
        if (owner and issue.get("state") == "open" and not issue.get("pull_request")
                and {"request", "new"} <= labels and author in TRUSTED and author != owner
                and sender != owner and payload.get("action") in ("opened", "reopened", "label_updated")):
            add_trigger(owner, f"request:{repo}#{n}", f"untriaged request {repo}#{n}")

    elif event == "issue_comment" and payload.get("action") == "created":
        issue, comment = payload.get("issue") or {}, payload.get("comment") or {}
        n = issue.get("number")
        if (owner and trusted and sender != owner and issue.get("state") == "open"
                and wants_reply(comment.get("body"))):
            kind = "pull request" if payload.get("is_pull") or issue.get("pull_request") else "issue"
            add_trigger(owner, f"comment:{comment.get('id')}", f"comment on {kind} {repo}#{n}")

    elif event.startswith("pull_request_review") or event == "pull_request_comment":
        pr = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        n = pr.get("number")
        rejected = "rejected" in event or review.get("type") == "pull_request_review_rejected"
        if (owner and trusted and sender != owner and (pr.get("user") or {}).get("login") == owner
                and pr.get("state") == "open" and (rejected or wants_reply(review.get("content")))):
            what = "changes requested on" if rejected else "review on"
            add_trigger(owner, f"review:{repo}#{n}:{payload.get('_delivery')}", f"{what} pull request {repo}#{n}")

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
                        f"CI failed on pull request {repo}#{n}")


def record(entry, delivery, body):
    payloads = STATE / "payloads"
    payloads.mkdir(exist_ok=True)
    (payloads / f"{delivery}.json").write_bytes(body)
    old = sorted(payloads.iterdir(), key=lambda p: p.stat().st_mtime)[:-KEEP_PAYLOADS]
    for p in old:
        p.unlink(missing_ok=True)
    with open(STATE / "events.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


# --- Claims and runs ------------------------------------------------------------

def day_start():
    """Midnight UTC today: caps are per UTC day."""
    return now() // 86400 * 86400


def runs_today(agent):
    return q("SELECT COUNT(*) AS n FROM runs WHERE agent=? AND claimed>=?", agent, day_start())[0]["n"]


def agent_state(agent):
    if q("SELECT 1 FROM runs WHERE agent=? AND ended IS NULL", agent):
        return "running"
    if paused():
        return "paused"
    if runs_today(agent) >= CAP:
        return "capped"
    return "idle"


def claim(agent):
    with LOCK:
        if DB.execute("SELECT 1 FROM runs WHERE agent=? AND ended IS NULL", (agent,)).fetchone():
            return None
        if paused():
            return None
        n = DB.execute("SELECT COUNT(*) FROM runs WHERE agent=? AND claimed>=?",
                       (agent, day_start())).fetchone()[0]
        if n >= CAP:
            return None
        rows = DB.execute("SELECT id, description, created FROM triggers WHERE agent=? AND run_id IS NULL "
                          "ORDER BY created", (agent,)).fetchall()
        if not rows:
            return None
        t = now()
        if t - rows[-1]["created"] < DEBOUNCE and t - rows[0]["created"] < MAX_DELAY:
            return None
        descs = [r["description"] for r in rows]
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
    if not r.get("in_extension"):
        notice_key = f"not-listed:{agent}:{run_id}"
        with LOCK:
            DB.execute("INSERT OR IGNORE INTO notices (at, key, kind, repo, number, text, posted) "
                       "VALUES (?,?,?,?,?,?,0)", (t, notice_key, "transcript-rewrite", AGENTS[agent]["own"], None,
                                                  f"run {run_id} of {agent} isn't listed in the extension; "
                                                  f"resume it by session ID {r.get('session_id')}"))
    log(f"{agent} run {run_id} ended: {r.get('outcome')}")
    return True


def status():
    agents = {}
    for name in AGENTS:
        pending = q("SELECT description, created FROM triggers WHERE agent=? AND run_id IS NULL ORDER BY created", name)
        agents[name] = {"state": agent_state(name), "runs_today": runs_today(name), "cap": CAP,
                        "pending": [{"trigger": p["description"], "since": iso(p["created"])} for p in pending]}
    runs = [dict(r, claimed=iso(r["claimed"]), started=iso(r["started"]), ended=iso(r["ended"]),
                 triggers=json.loads(r["triggers"] or "[]"), denied=json.loads(r["denied"] or "[]"))
            for r in q("SELECT * FROM runs ORDER BY id DESC LIMIT 50")]
    sessions = [dict(s, started=iso(s["started"]), ended=iso(s["ended"]))
                for s in q("SELECT * FROM sessions ORDER BY id DESC LIMIT 20")]
    notices = [dict(n, at=iso(n["at"])) for n in q("SELECT * FROM notices ORDER BY id DESC LIMIT 50")]
    return {"paused": paused(), "agents": agents, "runs": runs, "sessions": sessions, "notices": notices}


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

    def agent(self):
        auth = self.headers.get("Authorization") or ""
        return agent_for_key(auth[7:].strip() if auth.startswith("Bearer ") else "")

    def do_POST(self):
        try:
            if self.path == "/hook":
                return self.hook()
            agent = self.agent()
            if not agent or agent not in AGENTS:
                return self.reply(403, {"error": "unknown runner key"})
            data = self.json_body()
            parts = self.path.strip("/").split("/")
            if self.path == "/claim":
                c = claim(agent)
                return self.reply(200, c) if c else self.reply(204, None)
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

    def hook(self):
        body = self.body()
        given = self.headers.get("X-Forgejo-Signature") or ""
        expected = hmac.new(SECRET_FILE.read_text().strip().encode(), body, hashlib.sha256).hexdigest()
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
        record({"at": iso(t), "event": event, "type": event_type, "delivery": delivery, "repo": repo,
                "action": payload.get("action"), "sender": sender, "number": number}, delivery, body)
        payload["_delivery"] = delivery
        try:
            handle(event_type if event_type.startswith("pull_request_review") else event, payload)
        except Exception as e:
            log(f"error handling a {event_type} event: {e!r}")
        self.reply(200, {})

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"ok": True})
        if self.path == "/status":
            return self.reply(200, status())
        self.reply(404, {"error": "not found"})

    def reply(self, code, obj):
        data = b"" if obj is None else (json.dumps(obj, indent=1) + "\n").encode()
        self.send_response(code)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    log(f"listening on :8080; agents: {', '.join(AGENTS)}")
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
