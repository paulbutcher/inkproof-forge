#!/usr/bin/env python3
"""The gatekeeper: from autonomy level 2, it approves a pull request once the
Actions run for its head commit has succeeded, so no Claude run is spent on
checking CI.

It approves only in repositories whose CODEOWNERS on main names
`@gatekeeper` (scripts/setup-repo.sh <repo> 2 or 3), only pull requests into
main, and only on Forgejo's own record of the Actions runs for the head commit
(the runs API), never on commit statuses, which any account with write access
can post under CI's name. Every run for the head commit must have succeeded,
and one of them must be the CI workflow (GATEKEEPER_WORKFLOW, checks.yml).
While the kit is paused (control/paused) it approves nothing.

Its approval is one of the approvals a level-2 pull request needs:
conformance's is the other, and the human's too when the pull request changes
API.lock or a file the human owns. Branch protection dismisses it when a new
commit arrives, and the gatekeeper approves again once that commit's run has
succeeded.

It polls Forgejo every GATEKEEPER_POLL_SECONDS (default 30) rather than taking
webhooks, so a missed delivery can't lose an approval and nothing can reach
it: no agent shares a network with it. Its token is mounted only here.
Python 3 standard library only.
"""
import base64
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

FORGE = os.environ.get("FORGE_URL", "http://forgejo:3000") + "/api/v1"
ORG = os.environ["FORGE_ORG"]
ME = "gatekeeper"
POLL = int(os.environ.get("GATEKEEPER_POLL_SECONDS", "30"))
WORKFLOW = os.environ.get("GATEKEEPER_WORKFLOW", "checks.yml")
TOKEN_FILE = pathlib.Path(os.environ.get("TOKEN_FILE", "/run/secrets/gatekeeper/token"))
PAUSED = pathlib.Path("/control/paused")
NO_REPLY = "(no reply needed)"
CODEOWNERS = (".forgejo/CODEOWNERS", ".gitea/CODEOWNERS", "docs/CODEOWNERS", "CODEOWNERS")


def log(msg):
    print(f"gatekeeper: {msg}", file=sys.stderr, flush=True)


class ForgeError(Exception):
    pass


def forge(method, path, body=None, ok=(200,)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(FORGE + path, method=method, data=data, headers={
        "Authorization": f"token {TOKEN_FILE.read_text().strip()}", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        if e.code in ok:
            return e.code, None
        raise ForgeError(f"{method} {path}: {e.code} {e.read()[:300]!r}") from None


def pages(path):
    sep = "&" if "?" in path else "?"
    for page in range(1, 100):
        _, items = forge("GET", f"{path}{sep}limit=50&page={page}")
        if not items:
            return
        yield from items
        if len(items) < 50:
            return


# Which repositories are at level 2 or 3, by their CODEOWNERS on main,
# cached by main's commit.
_owners = {}


def gated(repo):
    _, branch = forge("GET", f"/repos/{ORG}/{repo}/branches/main", ok=(200, 404))
    if not branch:
        return False
    sha = branch["commit"]["id"]
    if _owners.get(repo, (None,))[0] == sha:
        return _owners[repo][1]
    named = False
    for path in CODEOWNERS:  # Forgejo reads the first that exists
        s, f = forge("GET", f"/repos/{ORG}/{repo}/contents/{path}?ref={sha}", ok=(200, 404))
        if s == 200:
            text = base64.b64decode(f.get("content") or "").decode(errors="replace")
            named = any(f"@{ME}" in line.split()[1:] for line in text.splitlines()
                        if line.strip() and not line.lstrip().startswith("#"))
            break
    _owners[repo] = (sha, named)
    return named


def ci(repo, sha):
    """success, failure, running or none, from the Actions runs for sha."""
    _, runs = forge("GET", f"/repos/{ORG}/{repo}/actions/runs?head_sha={sha}")
    runs = [r for r in (runs or {}).get("workflow_runs") or [] if r.get("commit_sha") == sha]
    if not any(r.get("workflow_id") == WORKFLOW for r in runs):
        return "none"
    states = {r.get("status") for r in runs}
    if states == {"success"}:
        return "success"
    if states & {"failure", "cancelled", "skipped", "blocked"}:
        return "failure"
    return "running"


_seen = {}  # (repo, n) -> (sha, state) last logged, to log each change once


def consider(repo, pr):
    n, sha = pr["number"], pr["head"]["sha"]
    if pr.get("base", {}).get("ref") != "main":
        return
    reviews = list(pages(f"/repos/{ORG}/{repo}/pulls/{n}/reviews"))
    if any(r["user"]["login"] == ME and r.get("state") == "APPROVED" and r.get("commit_id") == sha
           and not r.get("dismissed") and not r.get("stale") for r in reviews):
        return
    state = ci(repo, sha)
    if _seen.get((repo, n)) != (sha, state):
        _seen[(repo, n)] = (sha, state)
        log(f"{repo}#{n} at {sha[:12]}: the Actions run is {state}")
    if state != "success":
        return
    if PAUSED.exists():  # again, just before approving
        return
    body = (f"{NO_REPLY}\n\nThe Actions run for {sha[:12]} succeeded "
            f"(`{WORKFLOW}`). Approved by the gatekeeper; this pull request "
            "still needs every other review its code owners require.")
    forge("POST", f"/repos/{ORG}/{repo}/pulls/{n}/reviews",
          {"event": "APPROVED", "body": body, "commit_id": sha})
    log(f"approved {repo}#{n} at {sha[:12]}")


def poll():
    for r in pages(f"/orgs/{ORG}/repos"):
        repo = r["name"]
        if r.get("archived") or not r.get("has_pull_requests", True):
            continue
        try:
            if not gated(repo):
                continue
            for pr in pages(f"/repos/{ORG}/{repo}/pulls?state=open"):
                if PAUSED.exists():
                    return
                consider(repo, pr)
        except (ForgeError, urllib.error.URLError, OSError, KeyError, ValueError) as e:
            log(f"{repo}: {e}")


def main():
    log(f"approving on {WORKFLOW} for {ORG}'s level 2 and 3 repositories, every {POLL}s")
    was_paused = None
    while True:
        paused = PAUSED.exists()
        if paused != was_paused:
            log("paused: approving nothing" if paused else "running")
            was_paused = paused
        if not paused:
            try:
                poll()
            except (ForgeError, urllib.error.URLError, OSError) as e:
                log(f"poll failed: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
