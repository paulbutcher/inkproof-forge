#!/usr/bin/env python3
"""The gatekeeper: approvals on CI from autonomy level 2, and releases at
level 3. Python 3 standard library only.

Approvals. It approves a pull request into main once Forgejo's own record of
the Actions runs for its head commit (the runs API) shows the CI workflow
(GATEKEEPER_WORKFLOW, checks.yml) and every run for that commit succeeded. It
never reads commit statuses, which any account with write access can post
under CI's name. It acts only in repositories whose CODEOWNERS on main names
`@gatekeeper` (scripts/setup-repo.sh <repo> 2 or 3). Branch protection
dismisses its approval when a new commit arrives, and it approves again once
that commit's run has succeeded. It polls Forgejo every
GATEKEEPER_POLL_SECONDS (default 30), so a missed webhook can't lose an
approval.

Releases. At level 3 the coordinator asks for a release over HTTP, with a key
only its container holds; no other agent shares a network with the
gatekeeper. The gatekeeper computes the version from the API.lock diff since
the last release tag, refuses unless CHANGELOG.md on main has an entry for that
version and CI passed on main's head, then tags main's head and publishes the
release. Only it and the human may create tags. The human releases a
repository at any level with scripts/release.sh, which runs this same code
with the human's token (`gatekeeper release --human ...`).

While the kit is paused (control/paused) it approves and releases nothing.
"""
import base64
import hmac
import json
import os
import pathlib
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORGE = os.environ.get("FORGE_URL", "http://forgejo:3000") + "/api/v1"
ORG = os.environ["FORGE_ORG"]
ME = "gatekeeper"
POLL = int(os.environ.get("GATEKEEPER_POLL_SECONDS", "30"))
WORKFLOW = os.environ.get("GATEKEEPER_WORKFLOW", "checks.yml")
TOKEN_FILE = pathlib.Path(os.environ.get("TOKEN_FILE", "/run/secrets/gatekeeper/token"))
COORDINATOR_KEY = pathlib.Path(os.environ.get("COORDINATOR_KEY_FILE", "/run/secrets/gatekeeper/coordinator-key"))
PAUSED = pathlib.Path("/control/paused")
NO_REPLY = "(no reply needed)"
CODEOWNERS = (".forgejo/CODEOWNERS", ".gitea/CODEOWNERS", "docs/CODEOWNERS", "CODEOWNERS")
TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
RELEASE_LOCK = threading.Lock()


def log(msg):
    print(f"gatekeeper: {msg}", file=sys.stderr, flush=True)


class ForgeError(Exception):
    pass


class Refused(Exception):
    """A release the gatekeeper won't make, with the reason for the asker."""


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


def file_at(repo, path, ref):
    """A file's text at ref, or None."""
    s, f = forge("GET", f"/repos/{ORG}/{repo}/contents/{path}?ref={ref}", ok=(200, 404))
    if s != 200 or not isinstance(f, dict):
        return None
    return base64.b64decode(f.get("content") or "").decode(errors="replace")


def main_sha(repo):
    _, branch = forge("GET", f"/repos/{ORG}/{repo}/branches/main", ok=(200, 404))
    return branch["commit"]["id"] if branch else None


def owners(repo, sha):
    """CODEOWNERS on main as [(pattern, [owners])], from the file Forgejo reads."""
    for path in CODEOWNERS:  # Forgejo reads the first that exists
        text = file_at(repo, path, sha)
        if text is not None:
            return [(line.split()[0], line.split()[1:]) for line in text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    return []


# A repository's level, from its CODEOWNERS on main, cached by main's commit:
# level 2 or 3 names @gatekeeper; level 3 gives API.lock to @coordinator.
_levels = {}


def level(repo):
    sha = main_sha(repo)
    if not sha:
        return 0
    if _levels.get(repo, (None,))[0] != sha:
        rules = owners(repo, sha)
        gated = any(f"@{ME}" in who for _, who in rules)
        api_coord = any(p in (r"API\.lock", "API.lock") and "@coordinator" in who for p, who in rules)
        _levels[repo] = (sha, (3 if api_coord else 2) if gated else 1)
    return _levels[repo][1]


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


# --- Approvals ------------------------------------------------------------------

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
            if level(repo) < 2:
                continue
            for pr in pages(f"/repos/{ORG}/{repo}/pulls?state=open"):
                if PAUSED.exists():
                    return
                consider(repo, pr)
        except (ForgeError, urllib.error.URLError, OSError, KeyError, ValueError) as e:
            log(f"{repo}: {e}")


def poller():
    log(f"approving on {WORKFLOW} for {ORG}'s level 2 and 3 repositories, every {POLL}s")
    was_paused = None
    while True:
        paused = PAUSED.exists()
        if paused != was_paused:
            log("paused: approving and releasing nothing" if paused else "running")
            was_paused = paused
        if not paused:
            try:
                poll()
            except (ForgeError, urllib.error.URLError, OSError) as e:
                log(f"poll failed: {e}")
        time.sleep(POLL)


# --- Releases -------------------------------------------------------------------

def last_tag(repo):
    """The highest vX.Y.Z tag, as (name, (x, y, z), commit sha), or None."""
    best = None
    for t in pages(f"/repos/{ORG}/{repo}/tags"):
        m = TAG.match(t.get("name", ""))
        if m:
            v = tuple(int(x) for x in m.groups())
            if best is None or v > best[1]:
                best = (t["name"], v, (t.get("commit") or {}).get("sha"))
    return best


def api_lines(text):
    return {line.rstrip() for line in (text or "").splitlines() if line.strip()}


def next_version(prev, old_lock, new_lock):
    """The next version and why, from the API.lock diff since prev. Before 1.0:
    minor if API.lock changed, patch otherwise. From 1.0: major if a line was
    removed or changed, minor if lines were only added, patch otherwise."""
    if prev is None:
        return (0, 1, 0), "the first release"
    old, new = api_lines(old_lock), api_lines(new_lock)
    removed, added = old - new, new - old
    x, y, z = prev
    since = f"v{x}.{y}.{z}"
    if x == 0:
        if removed or added:
            return (0, y + 1, 0), f"API.lock changed since {since}: before 1.0, that's a minor release"
        return (0, y, z + 1), f"API.lock is unchanged since {since}: a patch release"
    if removed:
        return (x + 1, 0, 0), f"API.lock lost or changed {len(removed)} line(s) since {since}: a major release"
    if added:
        return (x, y + 1, 0), f"API.lock only gained lines since {since}: a minor release"
    return (x, y, z + 1), f"API.lock is unchanged since {since}: a patch release"


def changelog_entry(text, version):
    """The CHANGELOG.md entry for version, accepting `## vX.Y.Z` and
    `## [X.Y.Z]` headings, or None."""
    x, y, z = version
    head = re.compile(rf"^##\s+\[?v?{x}\.{y}\.{z}\]?(\s|$)")
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if head.match(line):
            body = []
            for rest in lines[i + 1:]:
                if rest.startswith("## "):
                    break
                body.append(rest)
            return "\n".join(body).strip()
    return None


def plan(repo):
    """What a release of repo would be: version, reason, commit and checks."""
    sha = main_sha(repo)
    if not sha:
        raise Refused(f"{repo} has no main branch")
    prev = last_tag(repo)
    if prev and prev[2] == sha:
        raise Refused(f"main is already released as {prev[0]}: nothing to release")
    lock = file_at(repo, "API.lock", sha)
    if lock is None:
        raise Refused(f"{repo} has no API.lock on main, so the version can't be computed")
    old = file_at(repo, "API.lock", prev[0]) if prev else None
    version, why = next_version(prev[1] if prev else None, old, lock)
    tag = "v%d.%d.%d" % version
    entry = changelog_entry(file_at(repo, "CHANGELOG.md", sha), version)
    return {"repo": repo, "sha": sha, "previous": prev[0] if prev else None, "version": tag,
            "reason": why, "changelog": entry, "ci": ci(repo, sha)}


def release(repo, note, issues, by_human=False):
    """Tag main's head with the computed version and publish the release, or
    raise Refused. by_human: the human's own release (scripts/release.sh), at
    any level and while paused."""
    with RELEASE_LOCK:
        if not by_human:
            if PAUSED.exists():
                raise Refused("the kit is paused: nothing is released until the human resumes it")
            if level(repo) < 3:
                raise Refused(f"{repo} isn't at autonomy level 3: the human releases it")
        p = plan(repo)
        if not p["changelog"]:
            existing = [l for l in (file_at(repo, "CHANGELOG.md", p["sha"]) or "").splitlines() if l.startswith("## ")]
            raise Refused(f"CHANGELOG.md on main has no entry for {p['version']} ({p['reason']}); "
                          f"its headings: {', '.join(existing[:5]) or 'none'}")
        if p["ci"] != "success":
            raise Refused(f"the Actions run for main's head ({p['sha'][:12]}) is {p['ci']}")
        found = []
        for n in issues:
            s, issue = forge("GET", f"/repos/{ORG}/{repo}/issues/{n}", ok=(200, 404))
            if s != 200 or issue.get("pull_request"):
                raise Refused(f"{repo}#{n} isn't an issue")
            if issue["state"] != "open" or "ready-for-release" not in {l["name"] for l in issue.get("labels") or []}:
                raise Refused(f"{repo}#{n} isn't an open issue labelled ready-for-release")
            found.append(issue)
        body = note.strip() + "\n\n" + p["changelog"]
        if found:
            body += "\n\nIssues: " + ", ".join(f"#{i['number']}" for i in found)
        body += f"\n\n{p['reason'][0].upper()}{p['reason'][1:]}."
        _, rel = forge("POST", f"/repos/{ORG}/{repo}/releases", {
            "tag_name": p["version"], "target_commitish": p["sha"], "name": p["version"], "body": body},
            ok=(201,))
        url = (rel or {}).get("html_url", "")
        for i in found:
            forge("POST", f"/repos/{ORG}/{repo}/issues/{i['number']}/comments",
                  {"body": f"{NO_REPLY}\n\nReleased in {p['version']}: {url}"}, ok=(201,))
            forge("DELETE", f"/repos/{ORG}/{repo}/issues/{i['number']}/labels/"
                  f"{next(l['id'] for l in i['labels'] if l['name'] == 'ready-for-release')}", ok=(204,))
        log(f"released {repo} {p['version']} at {p['sha'][:12]} ({'the human' if by_human else 'the coordinator'}): {p['reason']}")
        return {**p, "url": url, "issues": [i["number"] for i in found]}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        key = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        try:
            want = COORDINATOR_KEY.read_text().strip()
        except OSError:
            want = ""
        if not want or not hmac.compare_digest(key, want):
            return self.reply(403, {"error": "only the coordinator asks the gatekeeper for releases"})
        if self.path != "/release":
            return self.reply(404, {"error": "not found"})
        try:
            req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)))
            repo, note, issues = req["repo"], str(req.get("note") or ""), [int(n) for n in req.get("issues") or []]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo) or not note.strip():
                raise ValueError("a repository name and a note are required")
        except (ValueError, KeyError, TypeError) as e:
            return self.reply(400, {"error": f"bad request: {e}"})
        try:
            self.reply(200, release(repo, note, issues))
        except Refused as e:
            log(f"refused to release {repo}: {e}")
            self.reply(409, {"error": str(e)})
        except (ForgeError, urllib.error.URLError, OSError) as e:
            log(f"releasing {repo} failed: {e}")
            self.reply(502, {"error": f"Forgejo: {e}"})


def serve():
    threading.Thread(target=poller, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


USAGE = """usage: gatekeeper                       run: approve on CI, answer the coordinator
       gatekeeper plan <repo>           what a release of <repo> would be, as JSON
       gatekeeper release --human <repo> <note> [<issue>...]
                                        the human's release (scripts/release.sh), with TOKEN_FILE"""


def main(argv):
    if not argv:
        return serve()
    try:
        if argv[0] == "plan" and len(argv) == 2:
            print(json.dumps(plan(argv[1]), indent=2))
        elif argv[0] == "release" and len(argv) >= 4 and argv[1] == "--human":
            print(json.dumps(release(argv[2], argv[3], [int(n) for n in argv[4:]], by_human=True), indent=2))
        else:
            print(USAGE, file=sys.stderr)
            sys.exit(2)
    except Refused as e:
        print(f"Not released: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
