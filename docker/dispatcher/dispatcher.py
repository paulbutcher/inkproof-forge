#!/usr/bin/env python3
"""Receives Forgejo's webhooks.

Stage 1: verifies each delivery's signature and records it in
/state/events.jsonl, with the full payload of the most recent deliveries in
/state/payloads/. Later stages queue work for agents from these events.
"""

import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(os.environ.get("STATE_DIR", "/state"))
SECRET_FILE = Path(os.environ.get("WEBHOOK_SECRET_FILE", "/run/secrets/webhook_secret"))
KEEP_PAYLOADS = 500
MAX_BODY = 25 * 1024 * 1024


def log(msg):
    print(f"dispatcher: {msg}", file=sys.stderr, flush=True)


def secret():
    # Read on every delivery, so a rotated secret takes effect without a restart.
    return SECRET_FILE.read_text().strip().encode()


def summarise(event, payload):
    repo = (payload.get("repository") or {}).get("full_name")
    sender = (payload.get("sender") or {}).get("login")
    number = (payload.get("issue") or payload.get("pull_request") or {}).get("number")
    label = (payload.get("label") or {}).get("name")
    return {k: v for k, v in {
        "repo": repo, "action": payload.get("action"), "sender": sender,
        "number": number, "label": label, "ref": payload.get("ref"),
    }.items() if v is not None}


def record(entry, delivery, body):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "events.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
    payloads = STATE / "payloads"
    payloads.mkdir(exist_ok=True)
    (payloads / f"{delivery}.json").write_bytes(body)
    old = sorted(payloads.iterdir(), key=lambda p: p.stat().st_mtime)[:-KEEP_PAYLOADS]
    for p in old:
        p.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/hook":
            return self.reply(404, "not found")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.reply(413, "too large")
        body = self.rfile.read(length)
        given = self.headers.get("X-Forgejo-Signature") or ""
        expected = hmac.new(secret(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(given, expected):
            log("refused a delivery with a bad signature")
            return self.reply(403, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            return self.reply(400, "not JSON")
        event = self.headers.get("X-Forgejo-Event") or "?"
        event_type = self.headers.get("X-Forgejo-Event-Type") or event
        delivery = "".join(c for c in (self.headers.get("X-Forgejo-Delivery") or str(time.time_ns()))
                           if c.isalnum() or c == "-")
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "event": event, "type": event_type, "delivery": delivery,
                 **summarise(event, payload)}
        record(entry, delivery, body)
        log(json.dumps(entry))
        self.reply(200, "ok")

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, "ok")
        self.reply(404, "not found")

    def reply(self, status, text):
        data = (text + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    log("listening on :8080")
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
