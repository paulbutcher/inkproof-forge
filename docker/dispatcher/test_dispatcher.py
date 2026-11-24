"""Unit tests for the dispatcher's decisions, against a fake Forgejo that
serves issues and pull requests as they really look. Run by
scripts/test-dispatcher.sh, in the dispatcher's image. test_states.py
checks the states themselves, over every combination of their facts.

The numbers are those of DESIGN.md's Triggers."""
import datetime
import base64
import json
import os
import re
import tempfile
import unittest
import urllib.error
import urllib.parse

STATE = tempfile.mkdtemp()
CONF = os.path.join(STATE, "agents.conf")
with open(CONF, "w") as f:
    f.write("coordinator coordinator inkproof-coordination *\n"
            "core library inkproof-core inkproof-coordination\n"
            "scale library inkproof-scale inkproof-coordination,inkproof-core\n"
            "devtools library inkproof-tools inkproof-coordination\n"
            "demos site inkproof-demos inkproof-coordination,inkproof-core\n"
            "conformance pr-reviewer - *\n"
            "architecture milestone-reviewer - *\n"
            "lasm milestone-reviewer - *\n")
os.environ.update(STATE_DIR=STATE, AGENTS_CONF=CONF, CONTROL_DIR=STATE, FORGE_ORG="org",
                  HUMAN_USER="human", CONFORMANCE_SETTLE_SECONDS="600")

import dispatcher as d  # noqa: E402

CORE, SCALE, COORD = "inkproof-core", "inkproof-scale", "inkproof-coordination"
# Rule 15's trigger for a repository without level 3's CODEOWNERS on main.
BELOW_RELEASE = f"{CORE} is below level 3, so the human releases it: put the release to them as a decision"
PAUSED = os.path.join(STATE, "paused")
DRAINING = os.path.join(STATE, "draining")


def at(seconds_ago):
    return d.iso(d.now() - seconds_ago)


def comment(who, ago, body="A question?", cid=None):
    comment.n += 1
    return {"type": "comment", "id": cid or comment.n, "user": {"login": who}, "body": body, "created_at": at(ago)}


comment.n = 1000


def label(name, added, ago, who="core"):
    return {"type": "label", "label": {"name": name}, "body": "1" if added else "", "user": {"login": who},
            "created_at": at(ago)}


def event(kind, who, ago):
    return {"type": kind, "user": {"login": who}, "created_at": at(ago)}


class Forge:
    """A fake Forgejo: issues and pull requests with their timelines and
    reviews, commit statuses, main, milestones and labels. Writes are
    recorded."""

    def __init__(self):
        self.items, self.statuses, self.mains, self.milestones, self.writes = {}, {}, {}, {}, []
        self.codeowners, self.releases, self.changelogs = {}, {}, {}
        self.files = {}  # (repo, path, ref): text, for any other file
        self.tags = {}  # repo: [tag], besides each published release's
        self.runs = {}  # repo: [Actions run], each with its status

    def release(self, repo, tag, ago, draft=False):
        """Publish a release: the newest first, as Forgejo lists them."""
        self.releases.setdefault(repo, []).insert(0, {"tag_name": tag, "published_at": at(ago), "draft": draft})

    def issue(self, repo, n, author, labels=(), state="open", body="", created=7200, events=(), closed=None,
              title="An issue", assignees=()):
        self.items[(repo, n)] = {"number": n, "state": state, "user": {"login": author}, "body": body, "title": title,
                                 "assignees": [{"login": a} for a in assignees],
                                 "html_url": f"http://forge/org/{repo}/issues/{n}",
                                 "labels": [{"name": l} for l in labels], "created_at": at(created),
                                 "updated_at": at(0), "closed_at": at(closed) if closed is not None else None,
                                 "events": list(events), "reviews": [], "pr": None}
        return self.items[(repo, n)]

    def pull(self, repo, n, author, labels=(), sha="a" * 40, events=(), reviews=(), mergeable=True, title="Work",
             body="", requested=(), merged=None, state="open", created=7200, ci="pending",
             files=("Inkproof/Core.lean",)):
        i = self.issue(repo, n, author, labels, state, body, created, events)
        # A head CI has started on, as Forgejo marks it at once, unless a test
        # says otherwise: ci=None for a branch with no workflow.
        if ci:
            self.statuses.setdefault(sha, [ci])
        i["reviews"] = list(reviews)
        i["pr"] = {"number": n, "state": state, "user": {"login": author}, "head": {"sha": sha},
                   "mergeable": mergeable, "title": title, "body": body, "created_at": at(created),
                   "merged": merged is not None, "merged_at": at(merged) if merged is not None else None,
                   "requested_reviewers": [{"login": r} for r in requested]}
        i["files"] = list(files)
        return i

    def issue_json(self, i):
        j = {k: v for k, v in i.items() if k not in ("events", "reviews", "pr")}
        if i["pr"]:
            j["pull_request"] = {"merged": i["pr"]["merged"], "merged_at": i["pr"]["merged_at"]}  # as Forgejo gives it
        return j

    def __call__(self, method, path, body=None):
        path, _, query = path.partition("?")
        args = dict(urllib.parse.parse_qsl(query))
        if method != "GET":
            self.writes.append((method, path, body))
            # An issue the kit opens (a decision) exists from then on, numbered after the others.
            if method == "POST" and (m := re.fullmatch(r"/repos/org/([\w.-]+)/issues", path)):
                n = max([k for (r, k) in self.items if r == m[1]] + [900]) + 1
                self.issue(m[1], n, "dispatcher", ["needs-human"], body=body["body"], title=body["title"],
                           assignees=body.get("assignees") or ())
                return {"number": n}
            return {}
        repo, rest = re.fullmatch(r"/repos/org/([\w.-]+)/?(.*)", path).groups()
        # Forgejo serves every comment at once, ignoring page and limit.
        if m := re.fullmatch(r"issues/(\d+)/comments", rest):
            return [e for e in self.items[(repo, int(m[1]))]["events"] if e["type"] == "comment"]
        if int(args.get("page", 1)) > 1:
            return []
        if repo == "inkproof-tools":  # in agents.conf, not yet in Forgejo
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if rest == "":
            return {"name": repo}
        if m := re.fullmatch(r"issues/(\d+)", rest):
            return self.issue_json(self.items[(repo, int(m[1]))])
        if m := re.fullmatch(r"pulls/(\d+)", rest):
            return self.items[(repo, int(m[1]))]["pr"]
        if m := re.fullmatch(r"issues/(\d+)/timeline", rest):
            return self.items[(repo, int(m[1]))]["events"]
        if m := re.fullmatch(r"pulls/(\d+)/reviews", rest):
            return self.items[(repo, int(m[1]))]["reviews"]
        if re.fullmatch(r"pulls/(\d+)/commits", rest):
            return []
        if m := re.fullmatch(r"pulls/(\d+)/files", rest):
            return [{"filename": p} for p in self.items[(repo, int(m[1]))]["files"]]
        mine = [i for (r, _), i in self.items.items() if r == repo]
        if rest == "pulls":
            prs = [i["pr"] for i in mine if i["pr"]]
            return [p for p in prs if p["state"] == args["state"] and (args["state"] == "open" or p["merged"])]
        if rest == "issues":
            want = set(args.get("labels", "").split(",")) - {""}
            kind = args.get("type", "all")
            return [self.issue_json(i) for i in mine if args.get("state", "open") in ("all", i["state"])
                    and (kind == "all" or (kind == "issues") == (i["pr"] is None))
                    and want <= {l["name"] for l in i["labels"]}
                    and (not args.get("assigned_by") or args["assigned_by"] in {a["login"] for a in i["assignees"]})]
        if m := re.fullmatch(r"commits/(\w+)/status", rest):
            return {"statuses": [{"context": "checks / build (pull_request)", "status": s if isinstance(s, str) else s[0],
                                   "updated_at": at(0 if isinstance(s, str) else s[1])} for s in self.statuses.get(m[1], [])]}
        if rest == "branches/main":
            return {"commit": self.mains.get(repo, {"id": "m" * 40, "timestamp": at(86400)})}
        if rest == ".forgejo/CODEOWNERS" or rest == "contents/.forgejo/CODEOWNERS":
            if repo not in self.codeowners:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return {"content": base64.b64encode(self.codeowners[repo].encode()).decode()}
        if rest == "contents/CHANGELOG.md" and (repo, "CHANGELOG.md", args.get("ref")) not in self.files:
            if repo not in self.changelogs:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return {"content": base64.b64encode(self.changelogs[repo].encode()).decode()}
        if rest.startswith("contents/"):
            text = self.files.get((repo, rest[len("contents/"):], args.get("ref")))
            if text is None:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return {"content": base64.b64encode(text.encode()).decode()}
        if rest == "tags":  # each published release's
            return [{"name": r["tag_name"], "commit": {"sha": "t" * 40}} for r in self.releases.get(repo, [])
                    if not r["draft"]] + [{"name": n, "commit": {"sha": "t" * 40}} for n in self.tags.get(repo, [])]
        if rest == "releases":
            return self.releases.get(repo, [])
        if rest == "actions/runs":
            return {"workflow_runs": [r for r in self.runs.get(repo, []) if r["status"] == args["status"]]}
        if rest == "milestones":
            return [m for m in self.milestones.get(repo, []) if args.get("state", "open") in ("all", m.get("state", "open"))]
        if rest == "labels":
            return [{"id": 1, "name": "blocked"}, {"id": 2, "name": "needs-human"}, {"id": 3, "name": "blocking"}]
        raise AssertionError(f"unexpected call {method} {path}")


class Case(unittest.TestCase):
    def setUp(self):
        for t in ("triggers", "runs", "kv", "notices", "blocked_by", "labels", "units", "obligations",
                  "human_waits"):
            d.q(f"DELETE FROM {t}")
        d.CI_MARKED.clear()
        d.CI_BUSY.clear()
        d.kv_set("rules-seeded", d.now())
        d.kv_set("obligations-started", d.now())
        d._cache.clear()
        self.f = Forge()
        d.forge = self.f
        # Timers aren't run here: a test says when time passes.
        self.addCleanup(setattr, d, "recheck", d.recheck)
        d.recheck = lambda unit, at: None

    def what(self, key):
        """What a trigger is for: an obligation's state or ask, or a notice's kind."""
        if key.startswith("owe:"):
            o = d.obligation(int(key[len("owe:"):].rsplit(":", 2)[1]))
            return o["what"].split(":")[0] if o else "gone"
        return key.split(":")[0]

    def queued(self, agent=None):
        """(agent, what) of each waiting trigger, or just what, for one agent."""
        rows = d.q("SELECT agent, key FROM triggers WHERE run_id IS NULL ORDER BY id")
        if agent:
            return [self.what(r["key"]) for r in rows if r["agent"] == agent]
        return [(r["agent"], self.what(r["key"])) for r in rows]

    def owing(self, unit=None):
        """(agent, what) of each obligation standing, on one unit or all, an
        ask without its comment's id."""
        rows = d.q("SELECT agent, unit, what FROM obligations ORDER BY id")
        return sorted((r["agent"], r["what"].split(":")[0]) for r in rows if unit in (None, r["unit"]))

    def state(self, unit):
        row = d.q("SELECT state, holders FROM units WHERE unit=?", unit)
        return (row[0]["state"], json.loads(row[0]["holders"])) if row else None

    def descriptions(self, agent):
        return [r["description"] for r in d.q("SELECT description FROM triggers WHERE agent=? AND run_id IS NULL "
                                              "ORDER BY id", agent)]

    def handled(self):
        d.q("UPDATE triggers SET run_id=1 WHERE run_id IS NULL")

    def running(self, agent):
        return d.q("INSERT INTO runs (agent, triggers, claimed) VALUES (?, '[]', ?) RETURNING id",
                   agent, d.now())[0]["id"]


class Issues(Case):
    """An issue's state, and who owes its next step."""

    def test_untriaged_request_from_someone_else(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        self.f.issue(CORE, 2, "core", ["request", "new"])  # its own: a task, filing it accepted it
        self.f.issue(CORE, 3, "stranger", ["request", "new"])  # an outsider's, which no one else answers
        for n in (1, 2, 3):
            d.evaluate(CORE, n)
        self.assertEqual(self.owing(), [("core", "to do"), ("core", "untriaged"), ("core", "untriaged")])
        self.assertEqual(self.descriptions("core")[0],
                         f"issue {CORE}#1 (untriaged): accept it, counter-propose, or close it saying why")
        self.assertEqual(self.state(f"{CORE}#2"), ("to do", ["core"]))

    def test_a_site_owns_its_repository_as_a_library_does(self):
        self.assertEqual(d.OWNER_OF["inkproof-demos"], "demos")
        self.f.issue("inkproof-demos", 1, "core", ["request", "new"])
        d.evaluate("inkproof-demos", 1)
        self.assertEqual(self.queued("demos"), ["untriaged"])

    def test_an_issue_filed_without_labels_viewport_9(self):
        self.f.issue(CORE, 9, "scale")  # its labels dropped, as Forgejo does for anyone without write access
        self.f.issue(CORE, 10, "scale", events=[comment("core", 100, "(no reply needed) On it.")])  # looked at
        for n in (9, 10):
            d.evaluate(CORE, n)
        self.assertEqual(self.state(f"{CORE}#9")[0], "untriaged")
        self.assertIn("if it's a request, add `request` and `new`", self.descriptions("core")[0])
        self.assertEqual(self.state(f"{CORE}#10")[0], "to do")
        rows = d.q("SELECT id, key FROM triggers WHERE run_id IS NULL")
        self.f.items[(CORE, 9)]["labels"] = [{"name": "request"}, {"name": "accepted"}]
        self.assertEqual(d.no_longer_owed(rows), [rows[0]["id"]], "a claim drops it once triaged")
        self.assertEqual(self.state(f"{CORE}#9")[0], "to do")

    def test_counter_proposed(self):
        self.f.issue(CORE, 1, "scale", ["request", "counter-proposal"], events=[label("counter-proposal", True, 100)])
        d.evaluate(CORE, 1)
        self.assertEqual(self.owing(), [("scale", "counter-proposed")])

    def test_merged_work_still_to_finish(self):
        self.f.issue(CORE, 5, "scale", ["request", "accepted"])
        self.f.pull(CORE, 7, "core", body="Refs org/inkproof-core#5", state="closed", merged=100)
        d.evaluate(CORE, 5)
        self.assertEqual(self.descriptions("core"),
                         [f"issue {CORE}#5 (merged): label it ready-for-release, with its CHANGELOG.md entry"])

    def test_an_owners_own_issue_is_finished_as_a_request_is_demos_22(self):
        self.f.issue(CORE, 5, "core")
        self.f.issue(CORE, 6, "core", ["ready-for-release"])
        self.f.issue(CORE, 8, "scale")  # someone else's, untriaged
        self.f.pull(CORE, 7, "core", body="Refs #5\nRefs #6\nRefs #8", state="closed", merged=100)
        for n in (5, 6, 8):
            d.evaluate(CORE, n)
        self.assertEqual([self.state(f"{CORE}#{n}")[0] for n in (5, 6, 8)], ["merged", "ready", "untriaged"])
        # The coordinator's own issues are its decisions and records, never released.
        self.f.issue(COORD, 3, "coordinator")
        self.f.pull(COORD, 4, "coordinator", body="Refs #3", state="closed", merged=100)
        d.evaluate(COORD, 3)
        self.assertEqual(self.state(f"{COORD}#3")[0], "to do")

    def test_part_of_and_refs_in_a_line_count_demo_18_28(self):
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"])
        self.f.pull(CORE, 28, "core", title="Tile build from real files (part of #2)", state="closed", merged=100)
        self.f.issue(CORE, 3, "coordinator", ["request", "accepted"])
        self.f.pull(CORE, 18, "core", body="Not done yet for #3: performance gates. Refs org/inkproof-core#3.",
                    state="closed", merged=100)
        d.evaluate(CORE, 2)
        d.evaluate(CORE, 3)
        self.assertEqual(self.owing(), [("core", "merged"), ("core", "merged")])
        found = [r for m in d.REFS_INLINE.findall("Done. Refs #2, #3 and org/x#4. Not #5; prefs #6") for r in
                 d.REF.findall(m)]
        self.assertEqual([k for _, _, k in found], ["2", "3", "4"])

    def test_labelled_or_released_it_isnt_the_owners(self):
        self.f.issue(CORE, 5, "scale", ["request", "accepted", "ready-for-release"])
        self.f.issue(CORE, 6, "scale", ["request", "accepted"],
                     events=[comment("gatekeeper", 50, "(no reply needed)\n\nReleased in v0.2.0: ...")])
        self.f.pull(CORE, 7, "core", body="Refs #5\nRefs #6", state="closed", merged=100)
        d.evaluate(CORE, 5)
        d.evaluate(CORE, 6)
        self.assertEqual(self.state(f"{CORE}#5"), ("ready", ["unit:release"]))
        self.assertEqual(self.owing(), [("scale", "released")])

    def test_released_wakes_the_requester_to_verify(self):
        self.f.issue(CORE, 9, "scale", ["request", "accepted"],
                     events=[comment("gatekeeper", 100, "(no reply needed)\n\nReleased in v1.2.3: ...")])
        d.evaluate(CORE, 9)
        self.assertEqual(self.queued("scale"), ["released"])
        self.assertIn("(released in v1.2.3)", self.descriptions("scale")[0])
        self.f.items[(CORE, 9)]["labels"].append({"name": "verified"})
        d.evaluate(CORE, 9)
        self.assertEqual(self.owing(), [("scale", "verified")], "and then to close it")

    def test_not_verified_sends_it_back_to_its_owner_web_20(self):
        # Its requester found it short at the release; the owner's fix merged afterwards.
        events = [comment("gatekeeper", 5000, "(no reply needed)\n\nReleased in v0.2.0: ..."),
                  comment("scale", 4000, "The README names the wrong function."),
                  label("not-verified", True, 4000, who="scale")]
        self.f.issue(CORE, 6, "scale", ["request", "accepted", "not-verified"], events=events)
        d.evaluate(CORE, 6)
        self.assertEqual(self.state(f"{CORE}#6")[0], "to do")
        self.f.pull(CORE, 7, "core", body="Refs #6", state="closed", merged=6000)  # the released work
        self.f.pull(CORE, 8, "core", body="Refs #6", state="closed", merged=100)
        d._cache.clear()
        d.evaluate(CORE, 6)
        self.assertEqual(self.state(f"{CORE}#6")[0], "merged")
        # Released again: the label is from before, so it is the requester's again.
        self.f.items[(CORE, 6)]["events"].append(comment("gatekeeper", 50, "(no reply needed)\n\nReleased in v0.2.1."))
        d.evaluate(CORE, 6)
        self.assertEqual(self.state(f"{CORE}#6"), ("released", ["scale"]))

    def test_a_fix_merged_after_the_release_waits_for_not_verified_gpu_59(self):
        # The requester held back for the owner's pull request, which merged:
        # the release it has is still the one to check, until it says it falls short.
        self.f.issue(CORE, 6, "scale", ["request", "accepted"],
                     events=[comment("gatekeeper", 5000, "(no reply needed)\n\nReleased in v0.2.0: ..."),
                             comment("scale", 300, "I'll switch once it's released.\n\nBlocked-by: org/inkproof-core#8")])
        self.f.pull(CORE, 8, "core", body="Refs #6", state="closed", merged=100)["closed_at"] = at(100)
        d.evaluate(CORE, 6)
        self.assertEqual(self.state(f"{CORE}#6"), ("released", ["scale"]))
        self.f.items[(CORE, 6)]["labels"].append({"name": "not-verified"})
        self.f.items[(CORE, 6)]["events"].append(label("not-verified", True, 50, who="scale"))
        d.evaluate(CORE, 6)
        self.assertEqual(self.state(f"{CORE}#6"), ("merged", ["core"]))

    def test_a_released_request_labelled_again_waits_for_its_release_gpu_59(self):
        self.f.issue(CORE, 6, "scale", ["request", "accepted", "ready-for-release"],
                     events=[comment("gatekeeper", 5000, "(no reply needed)\n\nReleased in v0.2.0: ...")])
        d.evaluate(CORE, 6)
        self.assertEqual(self.state(f"{CORE}#6"), ("ready", ["unit:release"]))

    def test_work_merged_before_it_was_unblocked_isnt_its_work_scale_38(self):
        self.f.issue(CORE, 4, "coordinator", ["request"], state="closed", closed=1000)
        self.f.issue(CORE, 38, "coordinator", ["request", "accepted"], body="Blocked-by: org/inkproof-core#4")
        self.f.pull(CORE, 42, "core", body="Refs #38", state="closed", merged=2000)  # the part it could do
        d.evaluate(CORE, 38)
        self.assertEqual(self.owing(), [("core", "to do")])

    def test_an_owners_own_issue_demos_74(self):
        self.f.issue(CORE, 74, "core", created=600)
        self.f.issue(CORE, 75, "core", ["blocked"], body="Blocked-by: org/inkproof-scale#93")
        self.f.issue(SCALE, 93, "core", ["request", "new"])
        self.f.issue(CORE, 76, "core")
        self.f.pull(CORE, 77, "core", body="Refs #76", ci="success")  # under way
        for n in (74, 75, 76):
            d.evaluate(CORE, n)
        self.assertEqual([self.state(f"{CORE}#{n}")[0] for n in (74, 75, 76)], ["to do", "blocked", "in progress"])
        self.assertEqual(self.descriptions("core"),
                         [f"issue {CORE}#74 (to do): work on it: a pull request with a Refs line, or say what it "
                          "waits for (a Blocked-by: line, Waiting-on:, or needs-human with a decision)"])

    def test_to_do_waits_while_its_owner_is_busy(self):
        self.f.issue(CORE, 5, "coordinator", ["request", "accepted"])
        run = self.running("core")
        d.evaluate(CORE, 5)
        self.assertEqual(self.owing(), [("core", "to do")])
        self.assertEqual(self.queued("core"), [], "quiet: not while it runs")
        d.q("UPDATE runs SET ended=? WHERE id=?", d.now(), run)
        d.wake_due()
        self.assertEqual(self.queued("core"), ["to do"])

    def test_waits_for_every_blocker_consumer_metrics_12(self):
        self.f.issue(CORE, 51, "coordinator", ["request"], state="closed", closed=3000)
        self.f.issue(SCALE, 38, "coordinator", ["request"])
        self.f.issue(CORE, 12, "coordinator", ["request", "accepted"],
                     events=[comment("core", 7000, "(no reply needed)\nBlocked-by: org/inkproof-core#51\n"
                                                   "Blocked-by: org/inkproof-scale#38")])
        d.evaluate(CORE, 12)
        self.assertEqual(self.state(f"{CORE}#12"), ("blocked", ["unit:blockers"]))
        self.assertIn(("POST", f"/repos/org/{CORE}/issues/12/labels", {"labels": [1]}), self.f.writes)
        # scale#38's release unblocks it.
        self.f.items[(SCALE, 38)]["events"].append(
            comment("gatekeeper", 100, "(no reply needed)\n\nReleased in v0.5.3: ..."))
        self.f.items[(CORE, 12)]["labels"].append({"name": "blocked"})
        d.evaluate(CORE, 12)
        self.assertEqual(self.state(f"{CORE}#12")[0], "to do")

    def test_a_line_may_name_a_pull_request_coordination_198(self):
        self.f.pull(CORE, 13, "core", ci="success")
        self.f.issue(CORE, 19, "scale", ["request", "accepted"],
                     events=[comment("core", 7000, "(no reply needed)\nBlocked-by: org/inkproof-core#13")])
        d.evaluate(CORE, 19)
        self.assertEqual(self.state(f"{CORE}#19")[0], "blocked")
        self.f.items[(CORE, 13)]["state"] = "closed"
        self.f.items[(CORE, 13)]["pr"].update(state="closed", merged=True, merged_at=at(100))
        d.evaluate(CORE, 19)
        self.assertEqual(self.state(f"{CORE}#19")[0], "to do")

    def test_closed_but_never_released_still_blocks_coordination_208(self):
        self.f.issue(SCALE, 62, "core", ["request", "accepted", "ready-for-release"], state="closed", closed=3000)
        self.f.issue(CORE, 57, "coordinator", ["request", "accepted", "blocked"],
                     events=[comment("core", 7000, "(no reply needed)\nBlocked-by: org/inkproof-scale#62")])
        d.evaluate(CORE, 57)
        self.assertEqual(self.state(f"{CORE}#57")[0], "blocked")
        self.f.items[(SCALE, 62)]["events"].append(
            comment("gatekeeper", 100, "(no reply needed)\n\nReleased in v0.22.0: ..."))
        d.evaluate(CORE, 57)
        self.assertEqual(self.state(f"{CORE}#57")[0], "to do")

    def test_a_requesters_own_line_on_its_released_request_scene_51(self):
        self.f.pull(SCALE, 59, "scale", state="closed", merged=100)
        self.f.items[(SCALE, 59)]["state"] = "closed"
        self.f.issue(CORE, 51, "scale", ["request", "accepted"],
                     events=[comment("gatekeeper", 5000, "(no reply needed)\n\nReleased in v0.18.0: ..."),
                             comment("scale", 4000, "(no reply needed) Not yet.\n\nBlocked-by: org/inkproof-scale#59")])
        d.evaluate(CORE, 51)
        self.assertEqual(self.owing(), [("scale", "released")])

    def test_the_dispatchers_label_comes_off_with_its_last_line_demos_74(self):
        self.f.issue(CORE, 74, "core", ["blocked"], created=600, events=[label("blocked", True, 500, who="dispatcher")])
        # The human's own label, with no lines, stays: and asks for a line.
        self.f.issue(CORE, 75, "core", ["blocked"], created=600, events=[label("blocked", True, 500, who="human")])
        d.evaluate(CORE, 74)
        d.evaluate(CORE, 75)
        self.assertIn(("DELETE", f"/repos/org/{CORE}/issues/74/labels/1", None), self.f.writes)
        self.assertNotIn(("DELETE", f"/repos/org/{CORE}/issues/75/labels/1", None), self.f.writes)
        self.assertEqual(self.state(f"{CORE}#74")[0], "to do")
        self.assertEqual(self.state(f"{CORE}#75"), ("blocked without a reason", ["core"]))

    def test_a_loop_of_blocked_by_lines_is_its_writers(self):
        # Each waits on the other: the old rules saw two blocked items and woke no one.
        self.f.issue(CORE, 1, "core", events=[comment("core", 600, "(no reply needed)\nBlocked-by: org/inkproof-core#2")])
        self.f.issue(CORE, 2, "core", events=[comment("core", 500, "(no reply needed)\nBlocked-by: org/inkproof-core#1")])
        for n in (1, 2, 1):
            d.evaluate(CORE, n, dependents=False)
        self.assertEqual(self.state(f"{CORE}#1"), ("invalid", ["core"]))

    def test_a_line_naming_the_item_itself_is_invalid_while_it_is_the_newest(self):
        self.f.issue(CORE, 4, "core")
        events = self.f.issue(CORE, 3, "scale", ["request", "accepted"], events=[
            comment("gatekeeper", 900, "(no reply needed)\nReleased in v0.2.0."),
            comment("scale", 600, "(no reply needed)\nWaiting for the next release.\n\n"
                                  "Blocked-by: org/inkproof-core#3")])["events"]
        d.evaluate(CORE, 3)
        self.assertEqual(self.state(f"{CORE}#3"), ("invalid", ["scale"]))
        events.append(comment("scale", 300, "(no reply needed)\nBlocked-by: org/inkproof-core#4"))
        d.evaluate(CORE, 3)
        self.assertEqual(self.state(f"{CORE}#3")[0], "blocked")

    def test_with_the_human_until_they_answer(self):
        events = self.f.issue(CORE, 4, "core", ["needs-human"], events=[label("needs-human", True, 500)])["events"]
        d.evaluate(CORE, 4)
        self.assertEqual(self.state(f"{CORE}#4"), ("with the human", ["human"]))
        events.append(comment("human", 100, "Do the first."))
        d.evaluate(CORE, 4)
        self.assertEqual(self.owing(), [("core", "reply"), ("core", "to do")])

    def test_closed_while_ready_for_release_coordination_208(self):
        self.f.issue(SCALE, 62, "demos", ["request", "accepted", "ready-for-release"], state="closed", closed=100,
                     events=[event("close", "demos", 100)])
        self.f.issue(SCALE, 63, "demos", ["request", "accepted", "ready-for-release"], state="closed", closed=100,
                     events=[comment("gatekeeper", 200, "(no reply needed)\n\nReleased in v0.22.0: ..."),
                             event("close", "demos", 100)])
        d.evaluate(SCALE, 62)
        d.evaluate(SCALE, 63)
        self.assertEqual(self.owing(), [("coordinator", "closed unreleased")])

    def test_a_release_overtakes_its_decision_gpu_27(self):
        self.f.issue(CORE, 27, "coordinator", ["needs-human"], title=f"Release {CORE}", created=5000,
                     events=[label("needs-human", True, 5000, "coordinator"), comment("human", 100, "Go ahead")])
        self.f.release(CORE, "v0.6.0", 9000)  # before the decision
        d.evaluate(CORE, 27)
        self.assertEqual(self.state(f"{CORE}#27")[0], "with the human")
        self.f.release(CORE, "v0.6.1", 3000)
        self.f.release(CORE, "v0.6.2", 2000, draft=True)
        d._cache.clear()
        d.evaluate(CORE, 27)
        self.assertEqual(self.state(f"{CORE}#27"), ("overtaken", ["coordinator"]))
        # Once it has acted since, the human holds it again.
        self.f.items[(CORE, 27)]["events"].append(comment("coordinator", 50, "(no reply needed)\nUpdated."))
        d.evaluate(CORE, 27)
        self.assertEqual(self.state(f"{CORE}#27")[0], "with the human")

    def test_a_release_published_evaluates_the_decision(self):
        self.f.issue(CORE, 27, "coordinator", ["needs-human"], title=f"Release {CORE}", created=5000)
        self.f.release(CORE, "v0.6.1", 10)
        d.handle("release", {"action": "published", "repository": {"name": CORE, "owner": {"login": "org"}},
                             "sender": {"login": "gatekeeper"}, "release": {"tag_name": "v0.6.1"}})
        self.assertEqual(self.queued("coordinator"), ["overtaken"])

    def test_the_humans_issues_and_recommendations_are_theirs(self):
        self.f.issue(COORD, 107, "process", ["needs-human", "recommendation", "review:process"],
                     events=[label("needs-human", True, 100, "process"), comment("human", 50, "Why?")])
        self.f.issue(COORD, 108, "dispatcher", ["needs-human"], title="Stalled: x")
        d.evaluate(COORD, 107)
        d.evaluate(COORD, 108)
        self.assertEqual(self.state(f"{COORD}#107")[0], "with the human")
        self.assertEqual(self.state(f"{COORD}#108")[0], "with the human")
        self.assertEqual(self.owing(), [("coordinator", "reply")], "the coordinator answers the human's question")


class Asks(Case):
    """What is asked of an agent on an item: owed until it acts there."""

    def test_2_a_comment_wanting_a_reply_newer_than_the_owner(self):
        held = lambda *events: dict(labels=["needs-human"], events=[label("needs-human", True, 400)] + list(events))
        self.f.issue(CORE, 1, "core", **held(comment("scale", 300, cid=1)))
        self.f.issue(CORE, 2, "core", **held(comment("scale", 300), comment("core", 200, "Answered.")))
        self.f.issue(CORE, 3, "core", **held(comment("scale", 300, "(no reply needed)\n\nThanks.")))
        self.f.issue(CORE, 4, "core", **held(comment("stranger", 300)))
        for n in (1, 2, 3, 4):
            d.evaluate(CORE, n)
        self.assertEqual(self.descriptions("core"),
                         [f"comment on issue {CORE}#1 from scale: http://forge/org/{CORE}/issues/1#issuecomment-1"])

    def test_2_in_the_same_second_the_timelines_order_decides(self):
        self.f.issue(CORE, 1, "core", ["needs-human"], events=[comment("core", 100, "Mine."), comment("scale", 100, cid=1)])
        self.f.issue(CORE, 2, "core", ["needs-human"], events=[comment("scale", 100), comment("core", 100, "Answered.")])
        d.evaluate(CORE, 1)
        d.evaluate(CORE, 2)
        self.assertEqual(self.owing(), [("core", "reply")])
        self.assertEqual(d.q("SELECT unit FROM obligations")[0]["unit"], f"{CORE}#1")

    def test_2_the_newest_ask_is_the_one_owed_so_a_burst_is_one(self):
        events = self.f.issue(CORE, 1, "core", ["needs-human"],
                              events=[comment("scale", 300), comment("scale", 200), comment("scale", 100)])["events"]
        d.evaluate(CORE, 1)
        self.assertEqual(self.queued("core"), ["reply"])
        self.handled()
        events.append(comment("scale", 10))
        d.evaluate(CORE, 1)
        self.assertEqual(self.queued("core"), ["reply"], "the newer one replaces it")
        self.assertEqual(len(d.q("SELECT 1 FROM obligations")), 1)

    def test_2_not_answered_by_a_run_that_started_before_it_coordination_140(self):
        self.f.issue(CORE, 1, "core", ["needs-human"],
                     events=[comment("human", 300, "Follow the latest release."),
                             comment("core", 200, "Shortened the bullets.")])
        run = d.q("INSERT INTO runs (agent, triggers, claimed) VALUES ('core', '[]', ?) RETURNING id",
                  d.now() - 360)[0]["id"]
        d.evaluate(CORE, 1)
        self.assertEqual(self.owing(), [("core", "reply")])
        d.q("UPDATE runs SET ended=? WHERE id=?", d.now() - 150, run)
        d.q("INSERT INTO runs (agent, triggers, claimed, ended) VALUES ('core', '[]', ?, ?)", d.now() - 100, d.now() - 40)
        self.f.items[(CORE, 1)]["events"].append(comment("core", 50, "Reworded to follow the latest release."))
        d.evaluate(CORE, 1)
        self.assertEqual(self.owing(), [])

    def test_2_reviews_that_ask_and_one_that_doesnt(self):
        self.f.pull(CORE, 7, "core", reviews=[
            {"id": 1, "user": {"login": "conformance"}, "state": "REQUEST_CHANGES", "body": "(no reply needed)",
             "commit_id": "a" * 40, "submitted_at": at(300)}])
        self.f.pull(CORE, 8, "core", reviews=[
            {"id": 2, "user": {"login": "human"}, "state": "COMMENT", "body": "Why?", "submitted_at": at(300)}])
        self.f.pull(CORE, 9, "core", reviews=[
            {"id": 3, "user": {"login": "coordinator"}, "state": "APPROVED", "body": "Checked the scope.",
             "commit_id": "a" * 40, "submitted_at": at(300)}])
        for n in (7, 8, 9):
            d.evaluate(CORE, n)
        self.assertIn(f"changes requested on pull request {CORE}#7 by conformance (`forge reviews {CORE} 7`)",
                      self.descriptions("core"))
        self.assertIn(f"review on pull request {CORE}#8 by human (`forge reviews {CORE} 8`)", self.descriptions("core"))
        self.assertFalse([x for x in self.descriptions("core") if f"{CORE}#9" in x and "review" in x])

    def test_26_the_humans_mention_wakes_that_agent_gpu_9(self):
        asks = comment("human", 300, "@coordinator inkproof-core is at level 3; decide. cc @scale @core @stranger "
                                     "(not mail@lasm.org)", cid=26)
        self.f.pull(CORE, 9, "core", events=[asks, comment("scale", 200, "(no reply needed)\nSeen.")])
        self.f.pull(CORE, 10, "core", sha="b" * 40, events=[comment("scale", 100, "@coordinator please look")])
        d.evaluate(CORE, 9)
        d.evaluate(CORE, 10)
        self.assertEqual([o for o in self.owing() if o[1] == "mention"], [("coordinator", "mention")],
                         "scale has acted since; core is the owner, whom any comment reaches; an agent's mention "
                         "reaches no one")
        self.assertIn(f"the human mentioned you on pull request {CORE}#9: "
                      f"http://forge/org/{CORE}/issues/9#issuecomment-26", self.descriptions("coordinator"))
        rows = d.q("SELECT id, key FROM triggers WHERE agent='coordinator' AND key LIKE 'owe:%'")
        self.f.items[(CORE, 9)]["events"].append(comment("coordinator", 50, "(no reply needed)\nApproved."))
        self.assertEqual(d.no_longer_owed(rows), [rows[0]["id"]], "a claim drops it once it has acted")

    def test_26_the_owners_mention_wakes_its_requester_gpu_59(self):
        self.f.issue(CORE, 9, "scale", ["request", "accepted"], events=[
            comment("core", 300, "I can't reproduce it. @scale, can you attach the frame? @gpu might know too.", cid=59),
            comment("core", 250, "(no reply needed)\n\n@scale, merged.")])
        self.f.issue(CORE, 10, "scale", events=[comment("core", 300, "@scale, what's this?")])
        self.f.issue(CORE, 11, "scale", ["request", "accepted"],
                     events=[comment("core", 300, "@scale, the frame?"), comment("scale", 200, "Attached.")])
        for n in (9, 10, 11):
            d.evaluate(CORE, n)
        self.assertEqual([o for o in self.owing() if o[1] == "mention"], [("scale", "mention")])
        self.assertIn(f"core, its owner, mentioned you on issue {CORE}#9: "
                      f"http://forge/org/{CORE}/issues/9#issuecomment-59", self.descriptions("scale"))

    def test_30_waiting_on_holds_it_for_the_agent_it_names_gpu_59(self):
        def waits(n, *events, author="scale", labels=("request", "accepted")):
            self.f.issue(CORE, n, author, labels, events=events)
            d.evaluate(CORE, n)
            return self.state(f"{CORE}#{n}")
        self.assertEqual(waits(1, comment("core", 300, "(no reply needed)\nI need your frame.\n\nWaiting-on: scale",
                                          cid=31)), ("waiting", ["scale"]))
        self.assertIn(f"issue {CORE}#1 (waiting): answer, or do what it waits for", self.descriptions("scale"))
        # Answered: scale has acted since.
        self.assertEqual(waits(2, comment("core", 300, "(no reply needed)\nWaiting-on: scale"),
                               comment("scale", 200, "(no reply needed)\n\nAttached."))[0], "to do")
        # A later line decides: none, the human (needs-human reaches them), or the writer itself.
        self.assertEqual(waits(3, comment("core", 300, "(no reply needed)\nWaiting-on: scale"),
                               comment("core", 200, "(no reply needed)\nWaiting-on: none"))[0], "to do")
        self.assertEqual(waits(4, comment("core", 300, "(no reply needed)\nWaiting-on: human"))[0], "to do")
        self.assertEqual(waits(5, comment("scale", 300, "(no reply needed)\n\nWaiting-on: scale"))[0], "to do")
        self.assertEqual(waits(6, comment("stranger", 300, "Waiting-on: scale"))[0], "to do")
        # On a closed request too, where the talk after its verification goes on: there, an ask.
        self.f.issue(CORE, 9, "scale", ["request", "verified"], state="closed",
                     events=[comment("coordinator", 300, "Fix your changelog first.\n\nWaiting-on: scale")])
        d.evaluate(CORE, 9)
        self.assertEqual(self.owing(f"{CORE}#9"), [("scale", "waiting")])

    def test_26_the_supervisors_mention_reaches_the_owner_at_most_three_a_day(self):
        self.addCleanup(d.AGENTS.pop, "process", None)
        d.AGENTS["process"] = {"kind": "supervisor", "own": None}
        self.addCleanup(setattr, d, "SUPERVISOR", d.SUPERVISOR)
        d.SUPERVISOR = "process"
        nudges = [comment("process", 500 - 10 * i, f"(no reply needed)\n\n@core Move this on ({i}).") for i in range(4)]
        self.f.issue(CORE, 4, "core", ["needs-human"], events=nudges)
        d.evaluate(CORE, 4)
        self.assertEqual([r["what"] for r in d.q("SELECT what FROM obligations WHERE agent='core'")],
                         [f"mention:{nudges[2]['id']}"],
                         "one, for the newest of the three it may send that day; the fourth reaches no one")

    def test_answered_by_acting_not_by_time(self):
        self.f.issue(CORE, 1, "core", ["needs-human"], events=[comment("human", 300, "Which?")])
        d.evaluate(CORE, 1)
        self.f.items[(CORE, 1)]["events"].append(label("priority", True, 100, who="core"))
        d.evaluate(CORE, 1)
        self.assertEqual(self.owing(), [], "any action of the owner's there answers it")


class Notices(Case):
    """Something to look at once: never what work waits on."""

    def test_12_a_request_filed(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        self.f.issue(CORE, 2, "coordinator", ["request", "new"])
        d.evaluate(CORE, 1)
        d.evaluate(CORE, 2)
        self.assertEqual([x for x in self.descriptions("coordinator") if x.startswith("request filed")],
                         [f"request filed: {CORE}#1"])

    def test_once_per_version(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        d.evaluate(CORE, 1)
        self.handled()
        d.evaluate(CORE, 1)
        self.assertEqual(self.queued("coordinator"), [])

    def test_13_a_pull_request_opened_is_held_until_ci_finishes(self):
        self.f.pull(CORE, 7, "core", created=60, events=[event("pull_scheduled_merge", "core", 50)])
        self.f.statuses["a" * 40] = ["pending"]
        d.evaluate(CORE, 7)
        held = d.q("SELECT not_before FROM triggers WHERE key LIKE 'pr-opened:%'")[0]["not_before"]
        self.assertGreater(held, d.now() + 400)
        self.f.statuses["a" * 40] = ["success"]
        d.evaluate(CORE, 7)
        self.assertIsNone(d.q("SELECT not_before FROM triggers WHERE key LIKE 'pr-opened:%'")[0]["not_before"])

    def test_13_not_for_the_humans_or_a_changelog_only_pull_request_coordination_171(self):
        self.f.pull(CORE, 7, "human", title="Set up for autonomy level 3", files=(".forgejo/CODEOWNERS",))
        self.f.pull(CORE, 8, "core", title="Changelog for 0.3.0", files=("CHANGELOG.md",))
        self.f.pull(CORE, 9, "core", files=("CHANGELOG.md", "Inkproof/Core.lean"))
        for n in (7, 8, 9):
            self.f.statuses["a" * 40] = ["success"]
            d.evaluate(CORE, n)
        self.assertEqual([x for x in self.descriptions("coordinator") if "opened" in x],
                         [f"pull request opened: {CORE}#9"])

    def test_13_a_changelog_only_pull_request_that_grows(self):
        self.f.pull(CORE, 8, "core", files=("CHANGELOG.md",), ci="success")
        d.evaluate(CORE, 8)
        self.assertNotIn("pr-opened", self.queued("coordinator"))
        self.f.items[(CORE, 8)]["files"].append("Inkproof/Core.lean")
        d.evaluate(CORE, 8)
        self.assertIn("pr-opened", self.queued("coordinator"))

    def test_11_rejected(self):
        self.f.issue(CORE, 2, "scale", ["request"], state="closed", events=[event("close", "core", 100)])
        self.f.issue(CORE, 3, "scale", ["request"], state="closed", events=[event("close", "scale", 100)])
        self.f.issue(CORE, 4, "scale", ["request", "verified"], state="closed", events=[event("close", "core", 100)])
        for n in (2, 3, 4):
            d.evaluate(CORE, n)
        self.assertEqual(self.descriptions("scale"), [f"your request {CORE}#2 was closed without being verified"])

    def test_16_needs_human_added_but_not_once_refused(self):
        self.f.issue(CORE, 4, "core", ["needs-human"], events=[label("needs-human", True, 100)])
        self.f.issue(CORE, 5, "core", [], events=[label("needs-human", True, 100),
                                                  label("needs-human", False, 90, "dispatcher")])
        d.evaluate(CORE, 4)
        d.evaluate(CORE, 5)
        self.assertEqual([x for x in self.descriptions("coordinator") if "needs-human" in x],
                         [f"needs-human added to issue {CORE}#4"])

    def test_16_not_on_process_or_lasm_recommendations_coordination_128(self):
        for n, review in ((107, "review:process"), (117, "review:lasm"), (120, "review:architecture")):
            self.f.issue(COORD, n, "process", ["needs-human", "recommendation", review],
                         events=[label("needs-human", True, 100, "process")])
            d.evaluate(COORD, n)
        self.assertEqual([x for x in self.descriptions("coordinator") if "needs-human" in x],
                         [f"needs-human added to issue {COORD}#120"])

    def test_8_the_humans_comment_on_an_upstream_issue(self):
        self.f.issue(COORD, 9, "core", ["upstream"], events=[comment("human", 100, "Fixed in Lean 4.36.")])
        self.f.issue(CORE, 3, "core", ["blocked"], created=5000, body="Blocked-by: org/inkproof-coordination#9")
        d.evaluate(CORE, 3)
        self.assertEqual(self.descriptions("core"),
                         [f"human commented on upstream issue {COORD}#9; {CORE}#3 is blocked on it"])

    def test_8_an_upstream_issue_an_agent_waits_on_is_blocking_web_2(self):
        self.f.issue(COORD, 116, "core", ["upstream", "upstream:lasm"])
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"], body="Blocked-by: org/inkproof-coordination#116")
        d.evaluate(CORE, 2)
        self.assertIn(("POST", f"/repos/org/{COORD}/issues/116/labels", {"labels": [3]}), self.f.writes)
        self.f.items[(COORD, 116)]["labels"].append({"name": "blocking"})
        self.f.items[(CORE, 2)]["state"] = "closed"
        self.f.writes.clear()
        d.evaluate(CORE, 2)
        self.assertEqual([w for w in self.f.writes if "/issues/116/" in w[1]],
                         [("DELETE", f"/repos/org/{COORD}/issues/116/labels/3", None)])

    def test_8_blocking_follows_the_lines_not_who_set_it(self):
        self.f.issue(COORD, 9, "core", ["upstream", "blocking"])
        self.f.issue(SCALE, 4, "scale", ["request"])
        self.f.issue(CORE, 3, "core", ["request", "accepted"], body="Blocked-by: org/inkproof-scale#4")
        d.sweep()
        writes = [w for w in self.f.writes if "/labels" in w[1] and not w[1].endswith("/labels/1")
                  and not (w[0] == "POST" and w[2] == {"labels": [1]})]
        self.assertEqual(writes, [("DELETE", f"/repos/org/{COORD}/issues/9/labels/3", None)])

    def test_22_a_blocking_lasm_issue(self):
        self.f.issue(COORD, 31, "core", ["upstream", "upstream:lasm", "blocking"])
        d.evaluate(COORD, 31)
        self.assertEqual(self.queued("lasm"), ["lasm-blocking"])
        self.assertEqual(self.state(f"{COORD}#31"), ("upstream", ["world"]))

    def test_28_the_owner_is_told_when_someone_else_makes_it_a_priority(self):
        self.f.issue(CORE, 2, "scale", ["request", "accepted", "priority"],
                     events=[label("priority", True, 100, who="human")])
        self.f.issue(CORE, 3, "scale", ["request", "accepted", "priority"],
                     events=[label("priority", True, 100, who="core")])  # its own doing
        d.evaluate(CORE, 2)
        d.evaluate(CORE, 3)
        self.assertEqual([x for x in self.descriptions("core") if "priority" in x],
                         [f"{CORE}#2 is now a priority: it comes before anything new (Protocol: Priority)"])

    def test_18_a_release_the_coordinator_didnt_ask_for(self):
        for by in ("gatekeeper", "human"):
            d.handle("release", {"action": "published", "repository": {"name": SCALE, "owner": {"login": "org"}},
                                 "sender": {"login": by}, "release": {"tag_name": f"v1.0.{len(by)}"}})
        self.assertEqual(self.descriptions("coordinator"), [f"{SCALE} released v1.0.5"])


class PullRequests(Case):
    def pr(self, n=7, **kw):
        kw.setdefault("events", [event("pull_scheduled_merge", "core", 3000)])
        kw.setdefault("reviews", [{"id": n, "user": {"login": "conformance"}, "state": "APPROVED",
                                   "commit_id": kw.get("sha", "a" * 40), "submitted_at": at(100)}])
        kw.setdefault("ci", "success")
        self.f.pull(CORE, n, "core", **kw)
        d.evaluate(CORE, n)
        return self.state(f"{CORE}#{n}")

    def test_what_its_author_must_fix(self):
        self.assertEqual(self.pr(1, ci="failure"), ("CI failing", ["core"]))
        self.assertEqual(self.pr(2, sha="b" * 40, mergeable=False), ("conflicting", ["core"]))
        self.assertEqual(self.pr(3, sha="c" * 40, title="WIP: later"), ("in draft", ["core"]))
        self.assertEqual(self.pr(4, sha="d" * 40, reviews=[
            {"id": 4, "user": {"login": "conformance"}, "state": "REQUEST_CHANGES", "commit_id": "d" * 40,
             "submitted_at": at(100)}]), ("changes requested", ["core"]))

    def test_ci_running_is_a_timer_until_it_is_late(self):
        self.f.statuses["a" * 40] = [("running", 600)]
        self.assertEqual(self.pr(7, ci=None)[0], "CI running")
        self.f.statuses["b" * 40] = [("running", 4000)]
        self.assertEqual(self.pr(8, sha="b" * 40, ci=None, events=[event("pull_push", "core", 4000),
                                                                   event("pull_scheduled_merge", "core", 4000)]),
                         ("CI stuck", ["core"]))

    def test_25_no_ci_run_for_its_head_export_4(self):
        self.assertEqual(self.pr(4, ci=None, events=[event("pull_push", "core", 3000)])[0], "no CI")
        self.assertEqual(self.pr(5, sha="b" * 40, ci=None, events=[event("pull_push", "core", 60)])[0], "CI running",
                         "pushed a minute ago: CI may yet start")
        self.assertIn("Merge `origin/main` into it and push", self.descriptions("core")[0])

    def test_19_conformance_reviews_a_passing_head_once_it_has_settled(self):
        self.assertEqual(self.pr(7, reviews=[]), ("in review", ["conformance"]))
        run = self.running("core")
        self.assertEqual(self.pr(8, sha="b" * 40, reviews=[], events=[event("pull_push", "core", 30)]),
                         ("settling", ["timer"]))
        # Its author's run ends: the head has settled.
        later = d.later
        self.addCleanup(setattr, d, "later", later)
        d.later = lambda fn, *args, delay=30: fn(*args)
        d.end_run("core", run, {"outcome": "completed"})
        self.assertEqual(self.state(f"{CORE}#8"), ("in review", ["conformance"]))

    def test_14_the_coordinators_review_once_ci_passed(self):
        self.f.codeowners[SCALE] = ".* @gatekeeper @conformance\nAPI\\.lock @coordinator\n"
        for repo, owner in ((SCALE, "scale"), (CORE, "core")):  # Core has no CODEOWNERS on main
            self.f.pull(repo, 9, owner, requested=["coordinator"], ci="success",
                        events=[event("pull_scheduled_merge", owner, 50)],
                        reviews=[{"id": 9, "user": {"login": "conformance"}, "state": "APPROVED",
                                  "commit_id": "a" * 40, "submitted_at": at(100)}])
            d.evaluate(repo, 9)
        self.assertEqual(self.state(f"{SCALE}#9"), ("in review", ["coordinator"]))
        level3, below = sorted((x for x in self.descriptions("coordinator") if "in review" in x),
                               key=lambda x: SCALE not in x)
        self.assertIn(f"{SCALE} is at level 3: CODEOWNERS on main gives `API.lock` to you", level3)
        self.assertIn("a comment alone leaves it waiting", level3)
        self.assertIn(f"{CORE} is below level 3, so the human approves its `API.lock`", below)

    def test_14_after_a_push_dismissed_its_approval_web_8(self):
        self.f.pull(SCALE, 8, "scale", sha="b" * 40, ci="success", events=[
            event("pull_push", "scale", 3000),
            {"type": "review_request", "user": {"login": "scale"}, "assignee": {"login": "coordinator"},
             "created_at": at(3000)},
            event("pull_push", "scale", 1000)],
            reviews=[{"id": 1, "user": {"login": "coordinator"}, "state": "APPROVED", "commit_id": "a" * 40,
                      "submitted_at": at(2000), "dismissed": True}])
        d.evaluate(SCALE, 8)
        self.assertIn("coordinator", self.state(f"{SCALE}#8")[1])

    def test_20_the_author_replied_to_its_request_for_changes(self):
        changes = [{"id": 1, "user": {"login": "conformance"}, "state": "REQUEST_CHANGES", "commit_id": "a" * 40,
                    "submitted_at": at(100)}]
        self.assertEqual(self.pr(7, reviews=changes, events=[comment("core", 50, "I keep it because ...")]),
                         ("author replied", ["conformance"]))

    def test_20_not_while_the_pull_request_is_blocked_demo_35(self):
        self.f.issue(CORE, 37, "core")
        changes = [{"id": 1, "user": {"login": "conformance"}, "state": "REQUEST_CHANGES", "commit_id": "a" * 40,
                    "submitted_at": at(100)}]
        self.assertEqual(self.pr(35, labels=["blocked"], reviews=changes, events=[
            comment("core", 50, "#37 replaces the gate; I'll merge main in once it lands.\n\n"
                                "Blocked-by: org/inkproof-core#37")]), ("blocked", ["unit:blockers"]))

    def test_5_its_merge_to_schedule_once_nothing_else_stands_in_its_way_scale_43(self):
        self.assertEqual(self.pr(7, events=[]), ("unscheduled", ["core"]))
        self.assertEqual(self.descriptions("core"),
                         [f"pull request {CORE}#7 (unscheduled): schedule its merge (`forge pr automerge`), or "
                          "close it if it's no longer wanted"])
        run = self.running("core")
        self.assertEqual(self.pr(8, sha="b" * 40, events=[]), ("unscheduled", ["core"]))
        self.assertEqual(len(self.queued("core")), 1, "quiet: not while its owner runs (viewport#5)")
        d.q("UPDATE runs SET ended=? WHERE id=?", d.now(), run)

    def test_5_a_scheduled_merge_that_never_fired_scale_43(self):
        head = "a" * 40
        approvals = [{"id": i, "user": {"login": w}, "state": "APPROVED", "commit_id": head, "body": "",
                      "submitted_at": at(1600)} for i, w in ((1, "gatekeeper"), (2, "conformance"))]
        everyone = [{**event("review_request", "core", 4000), "assignee": {"login": w}}
                    for w in ("gatekeeper", "conformance")]
        self.assertEqual(self.pr(7, events=everyone + [event("pull_scheduled_merge", "core", 1500)],
                                 reviews=approvals), ("stuck", ["core"]))
        self.assertEqual(self.pr(8, sha="a" * 40, events=everyone + [event("pull_scheduled_merge", "core", 300)],
                                 reviews=approvals), ("merging", ["timer"]))

    def test_the_humans_part_is_the_humans(self):
        self.f.pull(CORE, 7, "human", ci="success", reviews=[
            {"id": 1, "user": {"login": "conformance"}, "state": "APPROVED", "commit_id": "a" * 40,
             "submitted_at": at(100)}])
        d.evaluate(CORE, 7)
        self.assertEqual(self.state(f"{CORE}#7"), ("unscheduled", ["human"]))
        self.assertEqual(self.owing(f"{CORE}#7"), [])


class Releases(Case):
    def setUp(self):
        super().setUp()
        self.f.statuses["m" * 40] = ["success"]
        self.f.release(CORE, "v0.2.0", 9000)
        self.f.files[(CORE, "API.lock", "v0.2.0")] = "def a : Nat\n"
        self.f.files[(CORE, "API.lock", "m" * 40)] = "def a : Nat\ndef b : Nat\n"

    def release(self, repo=CORE):
        d._cache.clear()
        d.evaluate_repo(repo)
        return self.state(f"release:{repo}")

    def test_9_ci_failing_on_main(self):
        self.f.statuses["m" * 40] = ["failure"]
        self.assertEqual(self.release(), ("main failing", ["core"]))

    def test_up_to_date(self):
        self.assertEqual(self.release(), ("up to date", []))

    def test_15_ready_for_release_is_the_coordinators_and_says_who_releases_gpu_27(self):
        self.f.changelogs[CORE] = "## v0.3.0\n\n- b.\n\n## v0.2.0\n"
        self.f.issue(CORE, 13, "scale", ["request", "ready-for-release"])
        self.assertEqual(self.release(), ("releasable", ["coordinator"]))
        self.assertEqual(self.descriptions("coordinator"), [
            f"the release of {CORE} (releasable): it is below level 3: put the release to the human as a "
            f"`Release <repo>` decision, whose command is `scripts/release.sh {CORE} '<one-line note>' 13`, "
            f"completing {CORE}#13"])
        self.f.codeowners[CORE] = ".* @gatekeeper @conformance\nAPI\\.lock @coordinator\n"
        self.release()
        self.assertEqual(d.q("SELECT description FROM obligations")[0]["description"],
                         f"the release of {CORE} (releasable): release it with `forge release`, naming every "
                         f"ready-for-release issue it completes: `forge release {CORE} '<one-line note>' 13`, "
                         f"completing {CORE}#13")
        # Below level 3, once the decision is open, the human releases it.
        del self.f.codeowners[CORE]
        self.f.issue(CORE, 14, "coordinator", ["needs-human"], title=f"Release {CORE}")
        self.assertEqual(self.release(), ("with the human", ["human"]))

    def test_an_entry_above_the_last_release_is_enough_demos_105(self):
        self.f.changelogs[CORE] = "## v0.3.0\n\n- b.\n\n## v0.2.0\n"
        self.assertEqual(self.release(), ("releasable", ["coordinator"]))
        self.assertIn("nothing is labelled ready-for-release, but CHANGELOG.md on main heads v0.3.0",
                      self.descriptions("coordinator")[0])

    def test_lines_under_an_already_released_heading_say_so_demos_235(self):
        # Written before v0.2.0 was tagged, merged after: its lines land under v0.2.0.
        self.f.files[(CORE, "CHANGELOG.md", "v0.2.0")] = "## v0.2.0\n\n- a.\n"
        self.f.changelogs[CORE] = "## v0.2.0\n\n- a.\n- b.\n"
        self.f.issue(CORE, 5, "scale", ["request", "accepted", "ready-for-release"])
        self.assertEqual(self.release(), ("no entry", ["core"]))
        self.assertIn("(CHANGELOG.md's newest heading, v0.2.0, is already released, and lines were added under it "
                      "after v0.2.0 was tagged: move them under a new heading)", self.descriptions("core")[0])
        # Nothing added: only which heading is the last release.
        self.f.changelogs[CORE] = "## v0.2.0\n\n- a.\n"
        self.release()
        self.assertTrue(d.q("SELECT description FROM obligations WHERE agent='core'")[0]["description"]
                        .endswith("(CHANGELOG.md's newest heading, v0.2.0, is the last release)"))

    def test_a_heading_the_gatekeeper_would_refuse_is_the_owners(self):
        self.f.changelogs[CORE] = "## v0.2.1\n\n- b.\n\n## v0.2.0\n"
        self.f.issue(CORE, 5, "scale", ["request", "accepted", "ready-for-release"])
        self.assertEqual(self.release(), ("wrong entry", ["core"]))
        self.assertIn("(`forge version` gives v0.3.0; CHANGELOG.md heads v0.2.1)", self.descriptions("core")[0])

    def test_the_last_release_is_the_highest_tag_as_the_gatekeeper_finds_it(self):
        # Tagged before Forgejo had releases for it, as a migrated repository may be.
        self.f.tags[CORE] = ["v0.3.0", "not-a-version"]
        self.f.files[(CORE, "API.lock", "v0.3.0")] = "def a : Nat\ndef b : Nat\n"
        self.f.changelogs[CORE] = "## v0.3.0\n\n- b.\n"
        self.assertEqual(self.release(), ("up to date", []), "v0.3.0 is released")
        self.f.changelogs[CORE] = "## v0.3.1\n\n- c.\n\n## v0.3.0\n"
        self.assertEqual(self.release(), ("releasable", ["coordinator"]), "API.lock unchanged since v0.3.0: a patch")

    def test_ready_for_release_with_no_entry_is_the_owners(self):
        self.f.issue(CORE, 5, "scale", ["request", "accepted", "ready-for-release"])
        self.assertEqual(self.release(), ("no entry", ["core"]))

    def test_not_while_mains_ci_runs_scale_38(self):
        self.f.changelogs[CORE] = "## v0.3.0\n\n## v0.2.0\n"
        self.f.statuses["m" * 40] = [("running", 100)]
        self.assertEqual(self.release(), ("main CI running", ["timer"]))
        self.f.statuses["m" * 40] = [("success", 50)]
        self.assertEqual(self.release()[0], "releasable")

    def test_held_by_what_holds_a_ready_issue_consumer_metrics_13(self):
        # The coordinator found it wanting: it says so where the kit can see it.
        self.f.changelogs[CORE] = "## v0.3.0\n\n## v0.2.0\n"
        self.f.issue(CORE, 13, "coordinator", ["request", "ready-for-release"],
                     events=[comment("coordinator", 300, "Refused: the README is stale.\n\nWaiting-on: core")])
        self.assertEqual(self.release(), ("held", ["unit:held"]))
        d.evaluate(CORE, 13)
        self.assertEqual(self.owing(), [("core", "reply"), ("core", "waiting")])

    def test_ci_on_main_finishing_evaluates_the_release(self):
        self.f.changelogs[CORE] = "## v0.3.0\n\n## v0.2.0\n"
        later = d.later
        self.addCleanup(setattr, d, "later", later)
        d.later = lambda fn, *args, delay=30: None
        d.handle("action_run_success", {"repository": {"name": CORE, "owner": {"login": "org"}},
                                        "sender": {"login": "ci"}, "run": {"event": "push"}})
        self.assertEqual(self.state(f"release:{CORE}")[0], "releasable")


class Milestones(Case):
    def ms(self, i, title="M1", state="open", open_=0, closed=3):
        return {"id": i, "title": title, "state": state, "open_issues": open_, "closed_issues": closed,
                "created_at": at(86400)}

    def test_17_finished_on_every_repository_it_spans_coordination_107(self):
        self.f.milestones[SCALE] = [self.ms(7)]
        self.f.milestones[CORE] = [self.ms(8, open_=2)]
        self.f.milestones[COORD] = [self.ms(6, closed=0)]  # an empty namesake counts neither way
        d.evaluate_milestones()
        self.assertEqual(self.state("milestone:M1"), ("in progress", ["unit:issues"]))
        self.assertEqual(self.state("kit"), ("in progress", ["unit:milestones"]))
        self.f.milestones[CORE] = [self.ms(8)]
        d.evaluate_milestones()
        self.assertEqual(self.owing("milestone:M1"), [("coordinator", "finished")])
        self.assertEqual(self.owing("kit"), [("coordinator", "what next")], "and no milestone has open work")
        self.assertIn("open its milestone issue and add milestone-review", self.descriptions("coordinator")[0])

    def test_its_issue_its_reviews_and_its_acceptance(self):
        self.f.milestones[COORD] = [self.ms(1, open_=1)]
        events = self.f.issue(COORD, 9, "coordinator", ["milestone", "milestone-review"], title="Milestone: M1",
                              events=[label("milestone-review", True, 600, "coordinator"),
                                      comment("lasm", 500, "(no reply needed)\nNo findings.")])["events"]
        d.evaluate(COORD, 9)
        d.evaluate_milestones()
        self.assertEqual(self.state(f"{COORD}#9"), ("in review", ["architecture"]))
        events.append(comment("architecture", 400, "(no reply needed)\nReport."))
        d.evaluate(COORD, 9)
        self.assertEqual(self.state(f"{COORD}#9"), ("reviewed", ["coordinator"]))
        # A comment isn't an answer: a Blocked-by: line is (coordination#82).
        events.append(comment("coordinator", 300, "(no reply needed)\nWaiting on #85."))
        d.evaluate(COORD, 9)
        self.assertEqual(self.state(f"{COORD}#9")[0], "reviewed")
        self.f.issue(COORD, 85, "coordinator")
        events.append(comment("coordinator", 200, "(no reply needed)\nBlocked-by: org/inkproof-coordination#85"))
        d.evaluate(COORD, 9)
        self.assertEqual(self.state(f"{COORD}#9")[0], "blocked")
        # Accepted: its issue closed, its milestones still open.
        self.f.items[(COORD, 9)]["state"] = "closed"
        self.f.milestones[COORD] = [self.ms(1)]
        d.evaluate(COORD, 9)
        d.evaluate_milestones()
        self.assertEqual(self.state("milestone:M1"), ("accepted", ["coordinator"]))
        self.f.milestones[COORD] = [self.ms(1, state="closed")]
        d.evaluate_milestones()
        self.assertEqual(self.state("milestone:M1"), ("closed", []))

    def test_21_its_reviews_even_once_accepted_coordination_73(self):
        self.f.issue(COORD, 73, "coordinator", ["milestone-review"], state="closed", closed=60, events=[
            label("milestone-review", True, 70, "coordinator"), comment("coordinator", 60, "(no reply needed)\nAccepted."),
            comment("lasm", 30, "(no reply needed)\nNo findings.")])
        d.evaluate(COORD, 73)
        self.assertEqual(self.owing(), [("architecture", "in review")])
        self.f.items[(COORD, 73)]["events"].append(comment("architecture", 10, "(no reply needed)\nReport."))
        d.evaluate(COORD, 73)
        self.assertEqual(self.owing(), [])

    def test_23_what_next_once_no_milestone_has_open_work(self):
        self.f.milestones[COORD] = [self.ms(1, state="closed")]
        run = self.running("coordinator")
        d.evaluate_milestones()
        self.assertEqual(self.owing(), [("coordinator", "what next")])
        self.assertEqual(self.queued("coordinator"), [], "quiet: not while it runs, drafting the next one perhaps")
        d.q("UPDATE runs SET ended=? WHERE id=?", d.now(), run)
        d.wake_due()
        self.assertIn("close out any left open", self.descriptions("coordinator")[0])
        # A decision for the human, a pull request on its repository, or the project finished answers it.
        self.f.issue(COORD, 5, "coordinator", ["needs-human"], assignees=("human",))
        d.evaluate_milestones()
        self.assertEqual(self.state("kit"), ("with the human", ["human"]))
        self.f.items[(COORD, 5)]["state"] = "closed"
        self.f.files[(COORD, "ROADMAP.md", "main")] = "# Roadmap\n\nStatus: finished\n"
        d.evaluate_milestones()
        self.assertEqual(self.state("kit"), ("finished", []))
        self.assertEqual(self.owing(), [])


class Obligations(Case):
    """Woken at once, again as each run ends with it still owed, with
    OBLIGATION_BACKOFF between; then the human's."""

    def setUp(self):
        super().setUp()
        self.clock = d.now()
        self.addCleanup(setattr, d, "now", d.now)
        d.now = lambda: self.clock
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        d.evaluate(CORE, 1)

    def finish(self, agent="core", outcome="completed", **more):
        """Its runner claims its work and the run ends."""
        self.clock += d.DEBOUNCE + 1
        c = d.claim(agent)
        self.assertIsNotNone(c, "work was due")
        self.clock += 600
        d.end_run(agent, c["run"], {"outcome": outcome, "turns": 20, "cost": 1.0, **more})
        return c

    def test_woken_again_with_backoff_then_the_humans(self):
        self.assertEqual(self.queued("core"), ["untriaged"])
        d.wake_due()
        self.assertEqual(len(self.queued("core")), 1, "not again while queued")
        self.finish()
        d.wake_due()
        self.assertEqual(self.queued("core"), ["untriaged"], "at once, the first time")
        self.assertIn("woken again, 2 of 3", self.descriptions("core")[0])
        self.finish()
        d.wake_due()
        self.assertEqual(self.queued("core"), [], "then not for 15 minutes")
        self.clock += 900
        d.wake_due()
        self.assertEqual(self.queued("core"), ["untriaged"])
        self.finish()
        self.clock += 900
        d.wake_due()
        self.assertEqual(self.queued("core"), [], "three runs: it is the human's")
        decisions = [w[2] for w in self.f.writes if w[1] == f"/repos/org/{COORD}/issues"]
        self.assertEqual([x["title"] for x in decisions], [f"Stalled: {CORE}#1"])
        self.assertIn("`core` was woken 3 times", decisions[0]["body"])
        self.clock += 3600
        d.wake_due()
        self.assertEqual(self.queued("core"), [], "and stays so")

    def stall(self):
        for _ in range(3):
            self.finish()
            self.clock += 900
            d.wake_due()
        n = max(k for (r, k) in self.f.items if r == COORD)
        self.assertEqual(self.f.items[(COORD, n)]["title"], f"Stalled: {CORE}#1")
        return n

    def test_the_decision_links_to_the_item_as_the_browser_reaches_it(self):
        self.addCleanup(setattr, d, "FORGE_WEB", d.FORGE_WEB)
        d.FORGE_WEB = "http://localhost:3000"
        n = self.stall()
        self.assertIn(f"[{CORE}#1](http://localhost:3000/org/{CORE}/issues/1)", self.f.items[(COORD, n)]["body"])

    def test_a_stalled_decision_is_closed_once_it_moves_on_demos_235(self):
        n = self.stall()
        self.f.writes.clear()
        self.f.items[(CORE, 1)]["labels"] = [{"name": "request"}, {"name": "accepted"}]
        d.evaluate(CORE, 1)
        self.assertEqual([w[:2] for w in self.f.writes if COORD in w[1]],
                         [("POST", f"/repos/org/{COORD}/issues/{n}/comments"), ("PATCH", f"/repos/org/{COORD}/issues/{n}")])
        said = self.f.writes[[w[1] for w in self.f.writes].index(f"/repos/org/{COORD}/issues/{n}/comments")][2]["body"]
        self.assertIn("Moved on: `core` no longer owes this (untriaged)", said)

    def test_a_stalled_decision_the_human_closed_is_left_alone(self):
        n = self.stall()
        self.f.items[(COORD, n)]["state"] = "closed"
        self.f.writes.clear()
        self.f.items[(CORE, 1)]["labels"] = [{"name": "request"}, {"name": "accepted"}]
        d.evaluate(CORE, 1)
        self.assertEqual([w for w in self.f.writes if COORD in w[1]], [])

    def test_discharged_when_what_it_owes_changes(self):
        self.f.items[(CORE, 1)]["labels"] = [{"name": "request"}, {"name": "accepted"}]
        self.clock += d.DEBOUNCE + 1
        self.assertIsNone(d.claim("core"), "its trigger is dropped at the claim")
        self.assertEqual(self.owing(), [("core", "to do")])
        # Back to what it was: owed afresh.
        self.f.items[(CORE, 1)]["labels"] = [{"name": "request"}, {"name": "new"}]
        d.evaluate(CORE, 1)
        self.assertEqual(d.q("SELECT wakes FROM obligations WHERE what='untriaged'")[0]["wakes"], 1)

    def test_a_run_that_never_got_going_uses_up_nothing(self):
        self.finish(outcome="failed: ERR_PROXY_TUNNEL", turns=1, cost=0)
        self.assertEqual(d.q("SELECT wakes FROM obligations")[0]["wakes"], 1)
        self.assertEqual(self.queued("core"), ["untriaged"], "its own trigger, queued again to retry")

    def test_dropped_by_hand_it_comes_back_after_the_wait(self):
        self.clock += d.DEBOUNCE + 1
        d.q("UPDATE triggers SET run_id=-1 WHERE run_id IS NULL")
        d.wake_due()
        self.assertEqual(self.queued("core"), ["untriaged"], "the first wait is none")

    def test_the_supervisor_is_woken_with_the_human(self):
        self.addCleanup(d.AGENTS.pop, "process", None)
        d.AGENTS["process"] = {"kind": "supervisor", "own": None}
        self.addCleanup(setattr, d, "SUPERVISOR", d.SUPERVISOR)
        d.SUPERVISOR = "process"
        for _ in range(3):
            self.finish()
            self.clock += 900
            d.wake_due()
        sup = self.descriptions("process")
        self.assertEqual(len(sup), 1)
        self.assertTrue(sup[0].startswith(f"{CORE}#1 has stalled: core was woken 3 times"))

    def test_a_quiet_one_waits_while_its_agent_has_other_work(self):
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"])
        d.evaluate(CORE, 2)
        self.assertEqual(self.queued("core"), ["untriaged"])
        self.finish()
        d.wake_due()
        self.assertEqual(self.queued("core"), ["untriaged"], "woken again for #1, so #2 still waits")
        self.f.items[(CORE, 1)]["state"] = "closed"
        self.clock += d.DEBOUNCE + 1
        self.assertIsNone(d.claim("core"), "#1 is no longer owed")
        d.wake_due()
        self.assertEqual(self.queued("core"), ["to do"])


class Mechanics(Case):
    def test_a_claim_keeps_what_is_still_owed(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        d.evaluate(CORE, 1)
        d.q("UPDATE triggers SET created=?", d.now() - 3600)
        self.assertEqual(d.claim("core")["triggers"],
                         [f"issue {CORE}#1 (untriaged): accept it, counter-propose, or close it saying why"])

    def test_the_first_sweep_records_the_notices_the_old_triggers_covered(self):
        d.q("DELETE FROM kv")
        self.f.issue(CORE, 1, "scale", ["request", "new"], events=[comment("scale", 100)])
        d.reconcile()  # inkproof-tools, not in Forgejo yet, is skipped
        self.assertEqual(sorted(self.queued("core")), ["reply", "untriaged"])
        self.assertEqual(self.queued("coordinator"), [], "its notice of the filing is recorded as handled")
        self.assertTrue(d.kv_get("rules-seeded"))

    def test_the_old_rules_triggers_are_dropped_once(self):
        d.q("DELETE FROM kv WHERE key='obligations-started'")
        d.add_trigger("core", f"request:{CORE}#1:x", "untriaged request")
        d.add_trigger("core", f"filed:{CORE}#1:", "request filed")
        d.add_trigger("core", "idle:1", "the kit is idle")
        d.retire_rule_triggers()
        self.assertEqual(self.queued("core"), ["filed", "idle"])
        d.add_trigger("core", f"request:{CORE}#2:x", "untriaged request")
        d.retire_rule_triggers()
        self.assertEqual(len(self.queued("core")), 3, "once")

    def test_a_blocker_closing_evaluates_what_waits_on_it(self):
        self.f.issue(SCALE, 38, "coordinator", ["request"])
        self.f.issue(CORE, 12, "coordinator", ["request", "accepted", "blocked"], body="Blocked-by: org/inkproof-scale#38")
        d.evaluate(CORE, 12)
        self.assertEqual(self.state(f"{CORE}#12")[0], "blocked")
        self.f.items[(SCALE, 38)].update(state="closed", closed_at=at(5))
        d.handle("issues", {"action": "closed", "repository": {"name": SCALE, "owner": {"login": "org"}},
                            "sender": {"login": "coordinator"},
                            "issue": {"number": 38, "state": "closed", "labels": [{"name": "request"}]}})
        self.assertEqual(self.state(f"{CORE}#12")[0], "to do")

    def test_a_merge_evaluates_the_requests_it_answers(self):
        self.f.issue(CORE, 5, "scale", ["request", "accepted"])
        self.f.pull(CORE, 7, "core", body="Refs #5", state="closed", merged=5)
        d.handle("pull_request", {"action": "closed", "repository": {"name": CORE, "owner": {"login": "org"}},
                                  "sender": {"login": "core"},
                                  "pull_request": {"number": 7, "merged": True, "user": {"login": "core"}}})
        self.assertEqual(self.state(f"{CORE}#5")[0], "merged")

    def test_a_deleted_item_owes_nothing(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        d.evaluate(CORE, 1)
        del self.f.items[(CORE, 1)]
        real = self.f.__call__

        def gone(method, path, body=None):
            if path.startswith(f"/repos/org/{CORE}/issues/1"):
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return real(method, path, body)
        d.forge = gone
        d.evaluate(CORE, 1)
        self.assertEqual(self.owing(), [])


class Paging(Case):
    """inkproof-demo#2 reached 50 comments, and paging through them, which
    Forgejo doesn't page, took the dispatcher to 82 GiB."""

    def test_a_blocker_with_fifty_comments_or_more_is_read_once(self):
        released = comment("gatekeeper", 10, "(no reply needed)\n\nReleased in v0.4.3: http://forge/r")
        self.f.issue(SCALE, 38, "coordinator", ["request"],
                     events=[comment("scale", 100 - k) for k in range(59)] + [released])
        self.assertEqual(d.blocker_state(SCALE, 38)["what"], f"{SCALE}#38 was released in v0.4.3")

    def test_a_list_that_ignores_paging_ends_at_its_first_page(self):
        calls, page = [], [{"id": k} for k in range(50)]

        def same_page(method, path, body=None):
            calls.append(path)
            return page
        d.forge = same_page
        self.assertEqual(d.forge_all("/repos/org/x/things"), page)
        self.assertEqual(len(calls), 2)


class UnstartedRuns(Case):
    """A claim whose reply never reached the runner blocked the demo's runs
    until its container restarted."""

    def setUp(self):
        super().setUp()
        d.add_trigger("core", "work:1", "some work", once=True)
        d.q("UPDATE triggers SET created=?", d.now() - 3600)
        self.run = d.claim("core")["run"]

    def test_an_undelivered_claim_gives_its_work_back(self):
        d.undo_claim(self.run, "the reply didn't reach the runner")
        self.assertEqual(self.queued("core"), ["work"])
        self.assertIsNotNone(d.q("SELECT ended FROM runs WHERE id=?", self.run)[0]["ended"])
        self.assertEqual(d.claim("core")["triggers"], ["some work"])

    def test_a_claim_no_one_starts_is_closed_after_a_while(self):
        d.abandon_unstarted()
        self.assertEqual(self.queued("core"), [])
        d.q("UPDATE runs SET claimed=? WHERE id=?", d.now() - d.CLAIM_START - 1, self.run)
        d.abandon_unstarted()
        self.assertEqual(self.queued("core"), ["work"])

    def test_a_started_run_is_left_alone(self):
        d.q("UPDATE runs SET claimed=?, started=? WHERE id=?", d.now() - d.CLAIM_START - 1, d.now(), self.run)
        d.abandon_unstarted()
        self.assertIsNone(d.q("SELECT ended FROM runs WHERE id=?", self.run)[0]["ended"])


class IdleSummary(Case):
    """The coordinator's idle summary, once per quiet spell."""

    def setUp(self):
        super().setUp()
        self.clock, self.waiting = d.now(), ["inkproof-core#1"]
        for name, value in (("now", lambda: self.clock), ("ci_running", lambda: False),
                            ("waiting_for_human", lambda: list(self.waiting))):
            self.addCleanup(setattr, d, name, getattr(d, name))
            setattr(d, name, value)

    def spell(self):
        """Something happens, then a quiet spell: whether it brought a summary."""
        self.clock += 60
        d.add_trigger("core", f"work:{self.clock}", "some work", once=True)
        self.handled()
        self.clock += d.IDLE + 60
        d.check_idle()
        found = [r["description"] for r in d.q("SELECT description FROM triggers WHERE run_id IS NULL")]
        self.handled()
        return found

    def test_a_restart_starts_a_quiet_spell_demos_105(self):
        self.addCleanup(setattr, d, "STARTED", d.STARTED)
        self.assertEqual(self.spell(), [f"{d.IDLE_TRIGGER}; 1 item waits on you"])
        self.waiting = ["inkproof-core#1", "inkproof-scale#4"]
        self.clock += 600
        d.STARTED = self.clock
        d.check_idle()
        self.assertEqual(self.queued(), [], "not until it has been quiet IDLE seconds since the start")
        self.clock += d.IDLE + 60
        d.check_idle()
        self.assertEqual(self.queued(), [("coordinator", "idle")])

    def test_only_when_what_waits_on_the_human_changed_coordination_156(self):
        self.assertEqual(self.spell(), [f"{d.IDLE_TRIGGER}; 1 item waits on you"])
        # Releases and verifications in between, but the same item waits.
        self.assertEqual(self.spell(), [])
        self.waiting = ["inkproof-core#1", "inkproof-scale#4"]
        self.assertEqual(self.spell(), [f"{d.IDLE_TRIGGER}; 2 items wait on you"])
        # Finished, with nothing waiting: said once.
        self.waiting = []
        self.assertEqual(self.spell(), [f"{d.IDLE_TRIGGER}; nothing waits on you"])
        self.assertEqual(self.spell(), [])


class Summary(Case):
    def setUp(self):
        super().setUp()
        for name, value in (("waiting_on_human", lambda: 0), ("ci_running", lambda: False)):
            self.addCleanup(setattr, d, name, getattr(d, name))
            setattr(d, name, value)

    def lines(self):
        return d.kit_summary().split("\n")

    def test_says_what_each_run_is_for_one_line_each(self):
        d.q("INSERT INTO runs (agent, triggers, claimed, started) VALUES ('core', ?, ?, ?)",
            '["untriaged request inkproof-core#1", "comment on inkproof-core#2"]', d.now() - 600, d.now() - 600)
        d.add_trigger("scale", "work:1", "some work", once=True)
        d.runner_seen("scale")
        self.assertEqual(self.lines(), [
            "Kit: working; nothing waits on you",
            "  - core running for 10 minutes: untriaged request inkproof-core#1 (and 1 more)",
            "  - 1 queued for scale, the oldest for under a minute"])

    def test_a_runner_that_stopped_calling_in_is_stuck(self):
        d.add_trigger("core", "work:1", "some work", once=True)
        d.kv_set("seen:core", d.now() - 2400)
        self.assertEqual(self.lines(), [
            "Kit: stuck; nothing waits on you",
            "  - stuck: 1 queued for core, whose runner hasn't called in for 40 minutes: restart its container"])

    def test_a_runner_running_a_run_isnt_stuck(self):
        d.q("INSERT INTO runs (agent, triggers, claimed, started) VALUES ('core', '[\"some work\"]', ?, ?)",
            d.now() - 360, d.now() - 360)
        d.add_trigger("core", "work:2", "more work", once=True)
        d.kv_set("seen:core", d.now() - 360)
        self.assertEqual(self.lines(), [
            "Kit: working; nothing waits on you",
            "  - core running for 6 minutes: some work",
            "  - 1 queued for core, the oldest for under a minute"])

    def test_queued_work_says_how_long_it_has_waited(self):
        d.add_trigger("core", "work:1", "some work", once=True)
        d.runner_seen("core")
        d.q("UPDATE triggers SET created=?", d.now() - 300)
        self.assertIn("  - 1 queued for core, the oldest for 5 minutes", self.lines())

    def test_queued_work_not_yet_due_says_when_it_is(self):
        d.add_trigger("core", "work:1", "some work", once=True, not_before=d.now() + 600)
        d.runner_seen("core")
        self.assertIn(f"  - 1 queued for core, the oldest for under a minute (1 not yet due, the first at "
                      f"{d.iso(d.now() + 600)})", self.lines())

    def test_a_claim_that_never_started_is_stuck(self):
        d.q("INSERT INTO runs (agent, triggers, claimed) VALUES ('core', '[]', ?)", d.now() - 600)
        lines = self.lines()
        self.assertEqual(lines[0], "Kit: stuck; nothing waits on you")
        self.assertTrue(lines[1].startswith("  - stuck: core's run"), lines)

    def test_quiet(self):
        self.assertEqual(self.lines()[0], "Kit: quiet; nothing waits on you")


class FailedRunsAreRetried(Case):
    """A run's triggers are consumed when it's claimed; most are queued only
    once, so a run that doesn't finish must give them back."""

    def setUp(self):
        super().setUp()
        d.add_trigger("core", "work:1", "some work", once=True)

    def claim(self):
        d.q("UPDATE triggers SET created=?, not_before=NULL", d.now() - 3600)
        return d.claim("core")["run"]

    def run_ending(self, outcome):
        d.end_run("core", self.claim(), {"outcome": outcome})
        return d.q("SELECT run_id, attempts, not_before FROM triggers WHERE key='work:1'")[0]

    def test_a_completed_run_consumes_its_work(self):
        self.assertIsNotNone(self.run_ending("completed")["run_id"])

    def test_a_failed_run_is_retried_later_then_put_to_the_human(self):
        t = self.run_ending("failed: error_max_turns")
        self.assertEqual((t["run_id"], t["attempts"]), (None, 1))
        self.assertAlmostEqual(t["not_before"], d.now() + d.RETRY_DELAY, delta=5)
        t = self.run_ending("timed out after 240 minutes")
        self.assertEqual((t["run_id"], t["attempts"]), (None, 2))
        self.assertAlmostEqual(t["not_before"], d.now() + 2 * d.RETRY_DELAY, delta=5)
        t = self.run_ending("exited (status 1) before finishing a reply")
        self.assertIsNotNone(t["run_id"])
        posted = [w[2]["title"] for w in self.f.writes if w[1] == f"/repos/org/{CORE}/issues"]
        self.assertEqual(len(posted), 1)
        self.assertIn("failed 3 times", posted[0])

    def test_a_run_that_never_got_going_waits_longer_and_isnt_counted_coordination_202(self):
        proxy = {"outcome": "failed: ERR_PROXY_TUNNEL", "turns": 1, "cost": 0}
        for delay in d.ENV_DELAYS + [d.ENV_DELAYS[-1]]:
            d.end_run("core", self.claim(), proxy)
            t = d.q("SELECT run_id, attempts, not_before FROM triggers WHERE key='work:1'")[0]
            self.assertEqual((t["run_id"], t["attempts"]), (None, 0))
            self.assertAlmostEqual(t["not_before"], d.now() + delay, delta=5)
        self.assertEqual(d.runs_today("core"), 0)  # nor do they use up its day
        # No decision for the human, and one notice however many fail.
        self.assertFalse([w for w in self.f.writes if w[1].endswith("/issues")])
        self.assertEqual([r["kind"] for r in d.q("SELECT kind FROM notices")], ["env-failing"])
        # Once a run gets going, the delays start again, and so does the notice.
        d.add_trigger("core", "work:2", "other work", once=True)
        d.q("UPDATE triggers SET created=?, not_before=NULL", d.now() - 3600)
        run = d.claim("core")["run"]
        d.end_run("core", run, {"outcome": "completed", "turns": 30, "cost": 1.5})
        self.assertIsNone(d.kv_get("env-failures:core"))
        self.assertIsNone(d.kv_get("env-failing"))

    def test_work_held_for_its_environment_keeps_its_delay_when_its_rule_queues_it_again(self):
        d.end_run("core", self.claim(), {"outcome": "failed: API Error", "turns": 1, "cost": 0})
        d.add_trigger("core", "work:1", "some work", once=True)
        self.assertGreater(d.q("SELECT not_before FROM triggers WHERE key='work:1'")[0]["not_before"], d.now())

    def test_a_run_getting_going_releases_every_agents_work_held_for_the_environment(self):
        limit = {"outcome": "failed: Claude AI usage limit reached", "turns": 1, "cost": 0}
        d.end_run("core", self.claim(), limit)
        d.add_trigger("scale", "work:2", "other work", once=True)
        d.q("UPDATE triggers SET created=?, not_before=NULL WHERE key='work:2'", d.now() - 3600)
        d.end_run("scale", d.claim("scale")["run"], limit)
        for name, value in (("waiting_on_human", lambda: 0), ("ci_running", lambda: False)):
            self.addCleanup(setattr, d, name, getattr(d, name))
            setattr(d, name, value)
        self.assertIn("failing: runs have failed before getting going since", d.kit_summary())
        self.assertIn("(core, scale); the latest, scale's, ended failed: Claude AI usage limit reached",
                      d.kit_summary())
        d.add_trigger("demos", "work:3", "demos work", once=True)
        d.q("UPDATE triggers SET created=? WHERE key='work:3'", d.now() - 3600)
        d.end_run("demos", d.claim("demos")["run"], {"outcome": "completed", "turns": 9, "cost": 0.4})
        held = d.q("SELECT not_before, env_held FROM triggers WHERE key IN ('work:1', 'work:2')")
        self.assertTrue(all(t["not_before"] <= d.now() and not t["env_held"] for t in held), [dict(t) for t in held])
        # The others still count as failing until a run of theirs gets going.
        self.assertIsNotNone(d.kv_get("env-failing"))

    def test_a_usage_limit_is_retried_when_it_resets_for_every_agent_however_far_the_run_got(self):
        reset = d.limit_resets("resets 4:40pm (UTC)")
        d.add_trigger("scale", "work:2", "other work", once=True)
        d.q("UPDATE triggers SET created=?, not_before=NULL WHERE key='work:2'", d.now() - 3600)
        d.end_run("scale", d.claim("scale")["run"], {"outcome": "failed: API Error", "turns": 1, "cost": 0})
        # Cut short after 33 turns: not counted, and held until the reset.
        d.end_run("core", self.claim(), {"outcome": "failed: You've hit your session limit · resets 4:40pm (UTC)",
                                         "turns": 33, "cost": 0.71})
        rows = d.q("SELECT key, attempts, not_before FROM triggers WHERE key IN ('work:1', 'work:2') ORDER BY key")
        expected = max(reset + d.LIMIT_MARGIN, d.now() + 60)
        for t in rows:
            self.assertEqual(t["attempts"], 0)
            self.assertAlmostEqual(t["not_before"], expected, delta=5)
        self.assertFalse([w for w in self.f.writes if w[1].endswith("/issues")])

    def test_when_a_usage_limit_resets(self):
        at = datetime.datetime(2026, 11, 23, 16, 20, tzinfo=datetime.timezone.utc).timestamp()
        utc = lambda *a: datetime.datetime(*a, tzinfo=datetime.timezone.utc).timestamp()
        for text, expected in (
                ("failed: You've hit your session limit · resets 4:40pm (UTC)", utc(2026, 11, 23, 16, 40)),
                ("resets 5pm (UTC)", utc(2026, 11, 23, 17, 0)),
                ("resets 3am (UTC)", utc(2026, 11, 24, 3, 0)),  # tomorrow
                ("resets 4pm (UTC)", utc(2026, 11, 23, 16, 0)),  # just passed: still today
                ("resets 12:15am (Europe/London)", utc(2026, 11, 24, 0, 15)),
                ("resets 9am (America/New_York)", utc(2026, 11, 24, 14, 0)),
                ("weekly limit · resets Nov 27, 9am (UTC)", utc(2026, 11, 27, 9, 0)),
                ("failed: error_max_turns", None),
                ("resets 4:40pm", None),  # no zone: can't tell when
                ("resets 4:40pm (Nowhere/Atlantis)", None),
                ("resets Dec 31, 9am (UTC)", None)):  # too far off to be a usage limit
            self.assertEqual(d.limit_resets(text, at), expected, text)

    def test_a_run_that_did_something_is_an_ordinary_failure(self):
        d.end_run("core", self.claim(), {"outcome": "failed: error_during_execution", "turns": 1, "cost": 0.2})
        self.assertEqual(d.q("SELECT attempts FROM triggers WHERE key='work:1'")[0]["attempts"], 1)
        self.assertEqual(d.q("SELECT kind FROM notices"), [])

    def test_a_retry_keeps_its_delay_when_its_rule_queues_it_again(self):
        self.run_ending("failed: error_max_turns")
        d.add_trigger("core", "work:1", "some work", once=True)
        self.assertIsNotNone(d.q("SELECT not_before FROM triggers WHERE key='work:1'")[0]["not_before"])

    def test_a_run_the_human_stopped_isnt_retried(self):
        self.assertIsNotNone(self.run_ending("interrupted")["run_id"])

    def test_a_run_cut_short_by_a_pause_is_queued_again_as_it_was(self):
        run = self.claim()
        open(PAUSED, "w").close()
        try:
            d.end_run("core", run, {"outcome": "completed"})
        finally:
            os.remove(PAUSED)
        t = d.q("SELECT run_id, attempts FROM triggers WHERE key='work:1'")[0]
        self.assertEqual((t["run_id"], t["attempts"]), (None, 0))


class Draining(Case):
    """A clean stop: runs under way finish, nothing new starts, and the quiet
    that follows brings no summary."""

    def setUp(self):
        super().setUp()
        self.ci = False
        for name, value in (("waiting_on_human", lambda: 0), ("ci_running", lambda: self.ci)):
            self.addCleanup(setattr, d, name, getattr(d, name))
            setattr(d, name, value)
        d.add_trigger("core", "work:1", "some work", once=True)
        d.add_trigger("scale", "work:2", "other work", once=True)
        d.q("UPDATE triggers SET created=?", d.now() - 3600)
        self.run = d.claim("core")["run"]
        open(DRAINING, "w").close()
        self.addCleanup(lambda: os.path.exists(DRAINING) and os.remove(DRAINING))

    def test_a_run_under_way_finishes_and_nothing_starts(self):
        self.assertIsNone(d.claim("scale"))
        self.assertEqual(d.agent_state("scale"), "draining")
        s = d.status()
        self.assertEqual((s["draining"], s["drained"], s["under_way"]), (True, False, [f"core's run {self.run}"]))
        d.end_run("core", self.run, {"outcome": "completed", "turns": 20, "cost": 1.0})
        self.ci = True
        self.assertEqual(d.status()["under_way"], ["CI"])
        self.ci = False
        s = d.status()
        self.assertEqual((s["drained"], s["under_way"]), (True, []))
        self.assertIn("drained: safe to stop", d.kit_summary())
        # The work it didn't start waits, and starts once resumed.
        os.remove(DRAINING)
        self.assertIsNotNone(d.claim("scale"))

    def test_the_quiet_brings_no_summary(self):
        d.end_run("core", self.run, {"outcome": "completed", "turns": 20, "cost": 1.0})
        d.q("UPDATE triggers SET run_id=-1 WHERE run_id IS NULL")
        real_now = d.now
        self.addCleanup(setattr, d, "now", real_now)
        d.now = lambda: real_now() + d.IDLE + 3600
        d.check_idle()
        self.assertEqual(d.q("SELECT key FROM triggers WHERE run_id IS NULL"), [])


class StatusReadsNoForge(Case):
    """The status page, asked every few seconds, reads nothing from Forgejo:
    what waits on the human is an index the evaluations keep, and whether CI
    runs is what refresh_ci last found where a webhook said it may have
    changed."""

    def no_forge(self):
        def refuse(method, path, body=None):
            raise AssertionError(f"the status page called Forgejo: {method} {path}")
        d.forge = refuse

    def test_what_waits_on_the_human_follows_the_evaluations(self):
        self.f.issue(CORE, 1, "core", ["needs-human"], assignees=["human"])
        self.f.pull(CORE, 2, "core", requested=["human"])
        self.f.issue(CORE, 3, "core", ["request", "accepted"])
        for n in (1, 2, 3):
            d.evaluate(CORE, n)
        self.assertEqual(d.waiting_for_human(), [f"{CORE}#1", f"{CORE}#2"])
        # Closed, and seen closed.
        self.f.items[(CORE, 2)]["state"] = "closed"
        d.evaluate(CORE, 2)
        self.assertEqual(d.waiting_for_human(), [f"{CORE}#1"])
        # Closed with no webhook: the sweep's pass over open items drops it.
        self.f.items[(CORE, 1)]["state"] = "closed"
        d.evaluate_repo(CORE, items=True)
        self.assertEqual(d.waiting_for_human(), [])

    def test_ci_is_checked_only_where_a_webhook_marked_it(self):
        d.ci_may_run(CORE)
        self.no_forge()
        self.assertTrue(d.ci_running(), "marked and not yet checked: it may be running")
        d.forge = self.f
        self.f.runs[CORE] = [{"status": "running"}]
        d.refresh_ci()
        self.assertEqual((d.CI_MARKED, d.CI_BUSY), ({}, {CORE}))
        self.assertTrue(d.ci_running())
        # Busy, so checked again until the run is over; then nothing is.
        self.f.runs[CORE] = []
        d.refresh_ci()
        self.assertFalse(d.ci_running())
        self.no_forge()
        d.refresh_ci()
        # A run long left waiting doesn't count.
        d.forge = self.f
        self.f.runs[SCALE] = [{"status": "waiting", "created": at(7200)}]
        d.ci_may_run(SCALE)
        d.refresh_ci()
        self.assertFalse(d.ci_running())

    def test_a_mark_during_a_check_stands(self):
        d.ci_may_run(CORE)
        busy = d.ci_busy
        self.addCleanup(setattr, d, "ci_busy", busy)

        def marked_meanwhile(repo):
            d.CI_MARKED[repo] = d.now() + 1
            return False
        d.ci_busy = marked_meanwhile
        d.refresh_ci()
        self.assertEqual(set(d.CI_MARKED), {CORE})

    def test_what_can_start_ci_marks_its_repository(self):
        d.handle("push", {"repository": {"name": CORE, "owner": {"login": "org"}}, "sender": {"login": "core"},
                          "ref": "refs/heads/agent/x"})
        d.handle("issue_comment", {"repository": {"name": SCALE, "owner": {"login": "org"}},
                                   "sender": {"login": "scale"}, "action": "created"})
        self.assertEqual(set(d.CI_MARKED), {CORE})

    def test_the_status_page_calls_nothing(self):
        self.f.issue(CORE, 1, "core", ["needs-human"], assignees=["human"])
        d.evaluate(CORE, 1)
        self.no_forge()
        s = d.status()
        self.assertTrue(s["summary"].startswith("Kit: quiet, nothing happens until you act; 1 item waits on you"),
                        s["summary"])


class Priority(Case):
    """Work labelled priority comes before anything new."""


    def test_a_runs_priority_work_comes_first(self):
        self.f.issue(CORE, 1, "scale", ["request", "accepted"])
        self.f.issue(CORE, 2, "scale", ["request", "new", "priority"])
        self.f.issue(CORE, 3, "scale", ["request", "accepted", "priority"])  # nothing queued for it
        self.f.issue(CORE, 4, "scale", ["request", "accepted", "priority"], state="closed")
        self.addCleanup(setattr, d, "no_longer_owed", d.no_longer_owed)
        d.no_longer_owed = lambda rows: []  # this test is about the order alone
        d.add_trigger("core", f"comment:{CORE}#1:1", f"a comment on {CORE}#1")
        d.add_trigger("core", f"request:{CORE}#2:1", f"untriaged request {CORE}#2")
        d.add_trigger("core", "idle:1", "the kit is idle")
        d.q("UPDATE triggers SET created=?", d.now() - 3600)
        self.assertEqual(d.claim("core")["triggers"], [
            f"priority: untriaged request {CORE}#2",
            f"priority items open on your repository: {CORE}#3: they come before anything new (Protocol: Priority)",
            f"a comment on {CORE}#1",
            "the kit is idle"])


if __name__ == "__main__":
    unittest.main()
