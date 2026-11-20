"""Unit tests for the dispatcher's decisions, against a fake Forgejo that
serves issues and pull requests as they really look. Run by
scripts/test-dispatcher.sh, in the dispatcher's image.

The rules' numbers are those in the dispatcher's Rules section."""
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
        self.codeowners, self.releases = {}, {}

    def release(self, repo, tag, ago, draft=False):
        """Publish a release: the newest first, as Forgejo lists them."""
        self.releases.setdefault(repo, []).insert(0, {"tag_name": tag, "published_at": at(ago), "draft": draft})

    def issue(self, repo, n, author, labels=(), state="open", body="", created=7200, events=(), closed=None,
              title="An issue"):
        self.items[(repo, n)] = {"number": n, "state": state, "user": {"login": author}, "body": body, "title": title,
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
            return [self.issue_json(i) for i in mine if i["state"] == args.get("state", "open")
                    and (kind == "all" or (kind == "issues") == (i["pr"] is None))
                    and want <= {l["name"] for l in i["labels"]}]
        if m := re.fullmatch(r"commits/(\w+)/status", rest):
            return {"statuses": [{"context": "checks / build (pull_request)", "status": s if isinstance(s, str) else s[0],
                                   "updated_at": at(0 if isinstance(s, str) else s[1])} for s in self.statuses.get(m[1], [])]}
        if rest == "branches/main":
            return {"commit": self.mains.get(repo, {"id": "m" * 40, "timestamp": at(86400)})}
        if rest == ".forgejo/CODEOWNERS" or rest == "contents/.forgejo/CODEOWNERS":
            if repo not in self.codeowners:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return {"content": base64.b64encode(self.codeowners[repo].encode()).decode()}
        if rest == "releases":
            return self.releases.get(repo, [])
        if rest == "milestones":
            return [m for m in self.milestones.get(repo, []) if args.get("state", "open") in ("all", m.get("state", "open"))]
        if rest == "labels":
            return [{"id": 1, "name": "blocked"}, {"id": 2, "name": "needs-human"}, {"id": 3, "name": "blocking"}]
        raise AssertionError(f"unexpected call {method} {path}")


class Case(unittest.TestCase):
    def setUp(self):
        for t in ("triggers", "runs", "kv", "notices", "blocked_by", "labels"):
            d.q(f"DELETE FROM {t}")
        d.kv_set("rules-seeded", d.now())
        d._cache.clear()
        self.f = Forge()
        d.forge = self.f
        # A claim checks a trigger is still owed on the item its key names,
        # so every rule's key must name the item it was evaluated on.
        owed_on = d.owed_on
        self.addCleanup(setattr, d, "owed_on", owed_on)

        def checked(it):
            found = owed_on(it)
            for o in found[0]:
                m = d.RULE_KEY.match(o.key)
                self.assertTrue(m and (m[2], int(m[3])) == (it.repo, it.n), f"{o.key} evaluated on {it.at}")
            return found
        d.owed_on = checked

    def queued(self, agent=None):
        """(agent, kind) of each waiting trigger, or just the kinds for one agent."""
        rows = d.q("SELECT agent, key FROM triggers WHERE run_id IS NULL ORDER BY id")
        if agent:
            return [r["key"].split(":")[0] for r in rows if r["agent"] == agent]
        return [(r["agent"], r["key"].split(":")[0]) for r in rows]

    def descriptions(self, agent):
        return [r["description"] for r in d.q("SELECT description FROM triggers WHERE agent=? AND run_id IS NULL "
                                              "ORDER BY id", agent)]

    def handled(self):
        d.q("UPDATE triggers SET run_id=1 WHERE run_id IS NULL")

    def running(self, agent):
        return d.q("INSERT INTO runs (agent, triggers, claimed) VALUES (?, '[]', ?) RETURNING id",
                   agent, d.now())[0]["id"]


class OwnerRules(Case):
    def test_1_a_site_owns_its_repository_as_a_library_does(self):
        self.assertEqual(d.OWNER_OF["inkproof-demos"], "demos")
        self.f.issue("inkproof-demos", 1, "core", ["request", "new"])
        d.evaluate("inkproof-demos", 1)
        self.assertEqual(self.descriptions("demos"), ["untriaged request inkproof-demos#1"])

    def test_1_an_untriaged_request_from_someone_else(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        self.f.issue(CORE, 2, "core", ["request", "new"])
        self.f.issue(CORE, 3, "stranger", ["request", "new"])
        for n in (1, 2, 3):
            d.evaluate(CORE, n)
        self.assertEqual(self.descriptions("core"), [f"untriaged request {CORE}#1"])

    def test_1_an_issue_filed_without_labels_viewport_9(self):
        self.f.issue(CORE, 9, "scale")  # its labels dropped, as Forgejo does for anyone without write access
        self.f.issue(CORE, 10, "scale", events=[comment("core", 100, "On it.")])  # the owner has looked
        self.f.issue(CORE, 11, "scale", ["question"])  # labelled, so not this rule
        self.f.issue(CORE, 12, "stranger")
        for n in (9, 10, 11, 12):
            d.evaluate(CORE, n)
        self.assertEqual(self.descriptions("core"),
                         [f"untriaged issue {CORE}#9, filed without labels: if it's a request, add `request` and "
                          "`new` and handle it as one; otherwise answer it or close it"])
        rows = d.q("SELECT id, key FROM triggers WHERE run_id IS NULL")
        self.f.items[(CORE, 9)]["labels"] = [{"name": "request"}, {"name": "accepted"}]
        self.assertEqual(d.no_longer_owed(rows), [rows[0]["id"]], "a claim drops it once triaged")

    def test_26_the_humans_mention_wakes_that_agent_gpu_9(self):
        asks = comment("human", 300, "@coordinator inkproof-core is at level 3; decide. cc @scale @core @stranger "
                                     "(not mail@lasm.org)")
        self.f.pull(CORE, 9, "core", events=[asks, comment("scale", 200, "(no reply needed)\nSeen.")])
        self.f.pull(CORE, 10, "core", sha="b" * 40, events=[comment("scale", 100, "@coordinator please look")])
        d.evaluate(CORE, 9)
        d.evaluate(CORE, 10)
        self.assertEqual(sorted(k for k in self.queued() if k[1] == "mention"), [("coordinator", "mention")],
                         "scale has acted since; core is the owner, whom any comment wakes; an agent's mention "
                         "wakes no one")
        self.assertIn(f"the human mentioned you on pull request {CORE}#9", self.descriptions("coordinator"))
        rows = d.q("SELECT id, key FROM triggers WHERE key LIKE 'mention:%'")
        self.f.items[(CORE, 9)]["events"].append(comment("coordinator", 50, "(no reply needed)\nApproved."))
        self.assertEqual(d.no_longer_owed(rows), [rows[0]["id"]], "a claim drops it once it has acted")

    def test_2_a_comment_wanting_a_reply_newer_than_the_owner(self):
        self.f.issue(CORE, 1, "core", events=[comment("scale", 300)])
        self.f.issue(CORE, 2, "core", events=[comment("scale", 300), comment("core", 200, "Answered.")])
        self.f.issue(CORE, 3, "core", events=[comment("scale", 300, "(no reply needed)\n\nThanks.")])
        self.f.issue(CORE, 4, "core", events=[comment("stranger", 300)])
        for n in (1, 2, 3, 4):
            d.evaluate(CORE, n)
        self.assertEqual(self.descriptions("core"), [f"comment on issue {CORE}#1"])

    def test_2_in_the_same_second_the_timelines_order_decides(self):
        self.f.issue(CORE, 1, "core", events=[comment("core", 100, "Mine."), comment("scale", 100)])
        self.f.issue(CORE, 2, "core", events=[comment("scale", 100), comment("core", 100, "Answered.")])
        d.evaluate(CORE, 1)
        d.evaluate(CORE, 2)
        self.assertEqual(self.descriptions("core"), [f"comment on issue {CORE}#1"])

    def test_2_the_newest_ask_is_the_version_so_a_burst_is_one_trigger(self):
        self.f.issue(CORE, 1, "core", events=[comment("scale", 300), comment("scale", 200), comment("scale", 100)])
        d.evaluate(CORE, 1)
        self.handled()
        d.evaluate(CORE, 1)
        self.assertEqual(self.queued("core"), [])
        self.f.items[(CORE, 1)]["events"].append(comment("scale", 10))
        d.evaluate(CORE, 1)
        self.assertEqual(self.queued("core"), ["comment"])

    def test_2_not_answered_by_a_run_that_started_before_it_coordination_140(self):
        # The human commented while the owner was mid-run on a review; the
        # run's later comment and push answered the review, not the human.
        self.f.issue(CORE, 1, "core", events=[comment("human", 300, "Follow the latest release."),
                                              comment("core", 200, "Shortened the bullets.")])
        run = d.q("INSERT INTO runs (agent, triggers, claimed) VALUES ('core', '[]', ?) RETURNING id",
                  d.now() - 360)[0]["id"]
        d.evaluate(CORE, 1)
        self.assertEqual(self.queued("core"), ["comment"])
        # Still owed once that run has ended, so the next run is woken for it.
        d.q("UPDATE runs SET ended=? WHERE id=?", d.now() - 150, run)
        self.assertEqual(d.no_longer_owed(d.q("SELECT id, key FROM triggers WHERE run_id IS NULL")), [])
        # A run that started after the comment answers it.
        self.handled()
        d.q("INSERT INTO runs (agent, triggers, claimed, ended) VALUES ('core', '[]', ?, ?)", d.now() - 100, d.now() - 40)
        self.f.items[(CORE, 1)]["events"].append(comment("core", 50, "Reworded to follow the latest release."))
        d.evaluate(CORE, 1)
        self.assertEqual(d.no_longer_owed(d.q("SELECT id, key FROM triggers WHERE run_id=1")), [d.q("SELECT id FROM triggers WHERE run_id=1")[0]["id"]])

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
        self.assertIn(f"changes requested on pull request {CORE}#7", self.descriptions("core"))
        self.assertIn(f"review on pull request {CORE}#8", self.descriptions("core"))
        self.assertFalse([x for x in self.descriptions("core") if f"{CORE}#9" in x and "review" in x])

    def test_3_ci_failed_on_its_head(self):
        self.f.pull(CORE, 7, "core", events=[event("pull_scheduled_merge", "core", 3000)])
        self.f.statuses["a" * 40] = ["failure"]
        d.evaluate(CORE, 7)
        self.assertEqual(self.queued("core"), ["ci-failed"])

    def test_4_a_conflict_wakes_the_owner_but_merely_behind_doesnt(self):
        self.f.pull(CORE, 7, "core", mergeable=False, events=[event("pull_scheduled_merge", "core", 3000)])
        self.f.pull(CORE, 8, "core", mergeable=True, events=[event("pull_scheduled_merge", "core", 3000)])
        d.evaluate(CORE, 7)
        d.evaluate(CORE, 8)
        self.assertEqual(self.descriptions("core"), [f"pull request {CORE}#7 conflicts with main"])

    def test_5_no_merge_scheduled_scale_43(self):
        self.f.pull(CORE, 7, "core", reviews=[
            {"id": 1, "user": {"login": "conformance"}, "state": "APPROVED", "commit_id": "a" * 40,
             "body": "Fine.", "submitted_at": at(100)}])
        self.f.pull(CORE, 8, "core", events=[event("pull_scheduled_merge", "core", 3000)])
        self.f.pull(CORE, 9, "core", events=[event("pull_scheduled_merge", "core", 3000),
                                             event("pull_cancel_scheduled_merge", "core", 2000)])
        self.f.pull(CORE, 10, "core", title="WIP: later")
        for n in (7, 8, 9, 10):
            d.evaluate(CORE, n)
        self.assertEqual([x for x in self.descriptions("core") if "no merge scheduled" in x],
                         [f"your pull request {CORE}#{n} has no merge scheduled, so it won't merge: schedule it "
                          "(`forge pr automerge`), or close it if it's no longer wanted" for n in (7, 9)])

    def test_5_not_while_its_owner_runs_viewport_5(self):
        later = d.later
        self.addCleanup(setattr, d, "later", later)
        d.later = lambda fn, *args, delay=30: fn(*args)
        self.f.pull(CORE, 7, "core")
        self.f.pull(CORE, 8, "core")
        run = self.running("core")
        d.evaluate(CORE, 7)
        self.assertEqual(self.queued("core"), [], "it may yet schedule it")
        self.f.items[(CORE, 7)]["events"].append(event("pull_scheduled_merge", "core", 5))
        d.end_run("core", run, {"outcome": "completed"})
        self.assertEqual(self.descriptions("core"),
                         [f"your pull request {CORE}#8 has no merge scheduled, so it won't merge: schedule it "
                          "(`forge pr automerge`), or close it if it's no longer wanted"])

    def test_25_no_ci_run_for_its_head_export_4(self):
        self.f.pull(CORE, 4, "core", ci=None, events=[event("pull_push", "core", 3000),
                                                      event("pull_scheduled_merge", "core", 3000)])
        self.f.pull(CORE, 5, "core", sha="b" * 40, ci=None, events=[event("pull_push", "core", 60),
                                                                    event("pull_scheduled_merge", "core", 60)])
        d.evaluate(CORE, 4)
        d.evaluate(CORE, 5)
        due = d.q("SELECT key, not_before FROM triggers WHERE key LIKE 'no-ci:%' ORDER BY key")
        self.assertEqual([r["key"] for r in due], [f"no-ci:{CORE}#4:{'a' * 12}", f"no-ci:{CORE}#5:{'b' * 12}"])
        self.assertLess(due[0]["not_before"], d.now(), "pushed long ago")
        self.assertGreater(due[1]["not_before"], d.now() + 400, "pushed a minute ago: CI may yet start")
        self.assertEqual(self.descriptions("core")[0],
                         f"CI has never run on your pull request {CORE}#4 at {'a' * 12}, so nothing reviews it: its "
                         "branch probably lacks `.forgejo/workflows/`. Merge `origin/main` into it and push")
        rows = d.q("SELECT id, key FROM triggers WHERE key LIKE 'no-ci:%' ORDER BY key")
        self.f.statuses["b" * 40] = ["pending"]
        d._cache.clear()
        self.assertEqual(d.no_longer_owed(rows), [rows[1]["id"]], "a claim drops it once CI has started")

    def test_5_a_scheduled_merge_that_never_fired_scale_43(self):
        """Approved by everyone requested and CI passed before the merge was
        scheduled, so Forgejo never acted on the schedule."""
        head, old = "a" * 40, "b" * 40

        def requested(*who):
            return [{**event("review_request", "core", 4000), "assignee": {"login": w}} for w in who]

        def review(rid, who, ago, state="APPROVED", sha=head, dismissed=False):
            return {"id": rid, "user": {"login": who}, "state": state, "commit_id": sha, "body": "",
                    "submitted_at": at(ago), "dismissed": dismissed}

        approvals = [review(1, "gatekeeper", 1700), review(2, "conformance", 1600), review(3, "coordinator", 1600)]
        everyone = requested("gatekeeper", "conformance", "coordinator")
        scheduled = [event("pull_scheduled_merge", "core", 1500)]
        self.f.statuses[head] = ["success"]
        # 7: ready 25 minutes ago. 8: scheduled only 5 minutes ago.
        self.f.pull(CORE, 7, "core", events=everyone + scheduled, reviews=approvals)
        self.f.pull(CORE, 8, "core", events=everyone + [event("pull_scheduled_merge", "core", 300)],
                    reviews=approvals)
        # 9: the coordinator's approval is of an older head.
        self.f.pull(CORE, 9, "core", events=everyone + scheduled, reviews=approvals[:2] + [
            review(3, "coordinator", 1600, sha=old)])
        # 10: a request for changes still stands; 11: it was dismissed, then approved.
        self.f.pull(CORE, 10, "core", events=everyone + scheduled, reviews=approvals + [
            review(4, "human", 1600, "REQUEST_CHANGES")])
        self.f.pull(CORE, 11, "core", events=everyone + scheduled, reviews=[
            review(4, "conformance", 2000, "REQUEST_CHANGES", sha=old, dismissed=True)] + approvals)
        # 12: CI still running on it.
        self.f.pull(CORE, 12, "core", sha="c" * 40, events=everyone + scheduled, reviews=[
            {**r, "commit_id": "c" * 40} for r in approvals])
        self.f.statuses["c" * 40] = ["pending"]
        for n in (7, 8, 9, 10, 11, 12):
            d.evaluate(CORE, n)
        stuck = [x for x in self.descriptions("core") if "hasn't gone through" in x]
        self.assertEqual(stuck, [f"your pull request {CORE}#{n} has every approval it needs and CI passed, but its "
                                 f"scheduled merge hasn't gone through: merge it now (`forge pr merge {n}`)"
                                 for n in (7, 11)])
        # Once per state: looked at again, it isn't queued again.
        self.handled()
        d.evaluate(CORE, 7)
        self.assertNotIn("stuck-merge", self.queued("core"))

    def test_6_merged_work_still_to_finish(self):
        self.f.issue(CORE, 5, "scale", ["request", "accepted"])
        self.f.pull(CORE, 7, "core", body="Refs org/inkproof-core#5", state="closed", merged=100)
        d.evaluate(CORE, 5)
        self.assertEqual(self.descriptions("core"),
                         [f"your pull request {CORE}#7 merged; it answers {CORE}#5, not yet labelled ready-for-release"])

    def test_6_an_owners_own_issue_demos_22(self):
        # Not a request, but finished as one: nothing else wakes it to label it.
        self.f.issue(CORE, 5, "core")
        self.f.issue(CORE, 6, "core", ["ready-for-release"])
        self.f.issue(CORE, 8, "scale")  # someone else's, untriaged: rule 1's
        self.f.pull(CORE, 7, "core", body="Refs #5\nRefs #6\nRefs #8", state="closed", merged=100)
        for n in (5, 6, 8):
            d.evaluate(CORE, n)
        self.assertIn(f"your pull request {CORE}#7 merged; it answers {CORE}#5, not yet labelled ready-for-release",
                      self.descriptions("core"))
        self.assertEqual([k for k in self.queued("core") if k == "merged"], ["merged"])
        # The coordinator's own issues are its decisions and records, never released.
        self.f.issue(COORD, 3, "coordinator")
        self.f.pull(COORD, 4, "coordinator", body="Refs #3", state="closed", merged=100)
        d.evaluate(COORD, 3)
        self.assertNotIn("merged", self.queued("coordinator"))

    def test_6_not_once_labelled_or_released(self):
        self.f.issue(CORE, 5, "scale", ["request", "accepted", "ready-for-release"])
        self.f.issue(CORE, 6, "scale", ["request", "accepted"],
                     events=[comment("gatekeeper", 50, "(no reply needed)\n\nReleased in v0.2.0: ...")])
        self.f.pull(CORE, 7, "core", body="Refs #5\nRefs #6", state="closed", merged=100)
        d.evaluate(CORE, 5)
        d.evaluate(CORE, 6)
        self.assertEqual(self.queued("core"), [])

    def test_6_work_merged_after_its_release_web_20(self):
        # Its requester found it short of its criteria at the release, and the
        # owner's fix merged afterwards: nothing else would release it again.
        self.f.issue(CORE, 6, "scale", ["request", "accepted"],
                     events=[comment("gatekeeper", 5000, "(no reply needed)\n\nReleased in v0.2.0: ..."),
                             comment("scale", 4000, "Not verified yet: the README names the wrong function.")])
        self.f.pull(CORE, 7, "core", body="Refs #6", state="closed", merged=6000)  # the released work
        self.f.pull(CORE, 8, "core", body="Refs #6", state="closed", merged=100)
        d.evaluate(CORE, 6)
        self.assertIn(f"your pull request {CORE}#8 merged; it answers {CORE}#6, released in v0.2.0 and not yet "
                      "verified, not yet labelled ready-for-release", self.descriptions("core"))
        self.assertNotIn("idle-request", self.queued("core"))  # 7 still ends at the release

    def test_6_not_while_blocked_consumer_metrics_12(self):
        self.f.issue(SCALE, 38, "coordinator", ["request"])
        self.f.issue(CORE, 12, "coordinator", ["request", "accepted", "blocked"], body="Blocked-by: org/inkproof-scale#38")
        self.f.pull(CORE, 15, "core", body="Refs #12", state="closed", merged=100)
        d.evaluate(CORE, 12)
        self.assertNotIn("merged", self.queued("core"))

    def test_6_not_for_work_merged_before_it_was_unblocked_scale_38(self):
        self.f.issue(CORE, 4, "coordinator", ["request"], state="closed", closed=1000)
        self.f.issue(CORE, 38, "coordinator", ["request", "accepted"], body="Blocked-by: org/inkproof-core#4")
        self.f.pull(CORE, 42, "core", body="Refs #38", state="closed", merged=2000)  # the part it could do
        d.evaluate(CORE, 38)
        self.assertEqual(self.queued("core"), ["unblocked"])

    def test_7_and_8_float_5_unblocked_then_left_alone(self):
        self.f.issue(CORE, 2, "coordinator", ["request"], state="closed", closed=3000)
        self.f.issue(CORE, 5, "coordinator", ["request", "accepted", "blocked"], created=9000,
                     body="Blocked-by: org/inkproof-core#2",
                     events=[label("accepted", True, 8000), label("blocked", True, 8000)])
        d.evaluate(CORE, 5)
        self.assertEqual(self.queued("core"), ["unblocked"])
        self.assertIn(("DELETE", f"/repos/org/{CORE}/issues/5/labels/1", None), self.f.writes)
        # Its run did other work and left it: once idle, one more nudge, and no more.
        self.handled()
        d.evaluate(CORE, 5)
        self.assertEqual(self.queued("core"), ["idle-request"])
        self.handled()
        d.evaluate(CORE, 5)
        self.assertEqual(self.queued("core"), [])

    def test_7_after_a_merge_that_left_part_of_it_to_do_demo_3(self):
        self.f.issue(CORE, 3, "coordinator", ["request", "accepted"], created=9000,
                     events=[label("accepted", True, 8000)])
        self.f.pull(CORE, 11, "core", body="Refs #3", state="closed", merged=2000)
        d.evaluate(CORE, 3)
        self.assertEqual(self.queued("core"), ["merged"])
        self.handled()
        # Its run left it open, with the rest still to do.
        d.evaluate(CORE, 3)
        self.assertEqual(self.queued("core"), ["idle-request"])
        self.handled()
        d.evaluate(CORE, 3)
        self.assertEqual(self.queued("core"), [])

    def test_6_part_of_counts_as_refs_demo_28(self):
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"], created=9000,
                     events=[label("accepted", True, 8000)])
        self.f.pull(CORE, 28, "core", title="Tile build from real files (part of #2)",
                    body="Part of #2, second step.", state="closed", merged=100)
        self.f.pull(CORE, 29, "core", title="Decode in place (part of tentenths/inkproof-scale#2)",
                    body="Prior art: none found.", state="closed", merged=50)
        d.evaluate(CORE, 2)
        self.assertEqual(self.descriptions("core")[:1],
                         [f"your pull request {CORE}#28 merged; it answers {CORE}#2, not yet labelled ready-for-release"])

    def test_6_refs_in_the_middle_of_a_line_demo_18(self):
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"], created=9000,
                     events=[label("accepted", True, 8000)])
        self.f.pull(CORE, 18, "core", title="Browser app",
                    body="Not done yet for #2: performance gates. Refs org/inkproof-core#2.",
                    state="closed", merged=100)
        d.evaluate(CORE, 2)
        self.assertEqual(self.descriptions("core")[:1],
                         [f"your pull request {CORE}#18 merged; it answers {CORE}#2, not yet labelled ready-for-release"])
        # Only the references after it, not everything else on the line.
        found = [r for m in d.REFS_INLINE.findall("Done. Refs #2, #3 and org/x#4. Not #5; prefs #6") for r in
                 d.REF.findall(m)]
        self.assertEqual([k for _, _, k in found], ["2", "3", "4"])

    def test_7_as_soon_as_the_run_that_left_it_ends_demo_2(self):
        # Its pull request merged as its run was ending: nothing else looks
        # at the request again until the sweep.
        later = d.later
        self.addCleanup(setattr, d, "later", later)
        d.later = lambda fn, *args, delay=30: fn(*args)
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"], created=9000,
                     events=[label("accepted", True, 8000)])
        self.f.pull(CORE, 21, "core", body="Refs #2", state="closed", merged=100)
        run = self.running("core")
        d.evaluate(CORE, 2)
        self.assertEqual(self.queued("core"), ["merged"])
        self.handled()
        d.end_run("core", run, {"outcome": "completed"})
        self.assertEqual(self.queued("core"), ["idle-request"])

    def test_7_a_run_that_worked_on_it_and_stopped_short_demo_2(self):
        accepted = [label("accepted", True, 8000)]
        progress = lambda ago: comment("core", ago, "(no reply needed) Progress: half done.")
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"], created=9000, events=accepted)
        d.evaluate(CORE, 2)
        self.assertEqual(self.queued("core"), ["idle-request"])
        self.handled()
        # Its run worked on it, reported progress and ended with nothing merged.
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"], created=9000,
                     events=accepted + [progress(5000)])
        d.evaluate(CORE, 2)
        self.assertEqual(self.descriptions("core"), [
            f"your accepted request {CORE}#2 isn't finished: you last acted on it at {at(5000)}, and it has no "
            "pull request under way. Carry on with it, or say on it what it waits for (a `Blocked-by:` line, "
            "or `needs-human`)"])
        self.handled()
        # A run that looks and does nothing isn't woken again.
        d.evaluate(CORE, 2)
        self.assertEqual(self.queued("core"), [])
        # At most FOLLOW_UPS runs that act without merging anything for it.
        for ago in (4000, 3000, 2000):
            self.f.issue(CORE, 2, "coordinator", ["request", "accepted"], created=9000,
                         events=accepted + [progress(5000), progress(ago)])
            d.evaluate(CORE, 2)
            self.handled()
        self.assertEqual(len(d.q("SELECT 1 FROM triggers WHERE key LIKE 'idle-request:%'")), 1 + d.FOLLOW_UPS)
        # A merge for it starts the count again.
        self.f.pull(CORE, 21, "core", body="Refs #2", state="closed", merged=1000)
        d._cache.clear()
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"], created=9000,
                     events=accepted + [progress(500)])
        d.evaluate(CORE, 2)
        self.assertEqual(self.queued("core"), ["merged"])
        self.handled()
        d.evaluate(CORE, 2)
        self.assertEqual(self.queued("core"), ["idle-request"])

    def test_7_not_while_the_owner_is_busy_or_a_pull_request_is_under_way(self):
        self.f.issue(CORE, 5, "coordinator", ["request", "accepted"])
        self.running("core")
        d.evaluate(CORE, 5)
        self.assertEqual(self.queued("core"), [])
        d.q("UPDATE runs SET ended=?", d.now())
        self.f.pull(CORE, 7, "core", body="Refs #5", events=[event("pull_scheduled_merge", "core", 3000)])
        d.evaluate(CORE, 7)  # as its webhook would: the requests it refers to are evaluated again
        self.assertEqual(self.queued("core"), [])

    def test_8_consumer_metrics_12_waits_for_every_blocker(self):
        self.f.issue(CORE, 51, "coordinator", ["request"], state="closed", closed=3000)
        self.f.issue(SCALE, 38, "coordinator", ["request"])
        self.f.issue(CORE, 12, "coordinator", ["request", "accepted"],
                     events=[comment("core", 7000, "Blocked-by: org/inkproof-core#51\nBlocked-by: org/inkproof-scale#38")])
        d.evaluate(CORE, 12)
        self.assertEqual(self.queued("core"), [])
        self.assertIn(("POST", f"/repos/org/{CORE}/issues/12/labels", {"labels": [1]}), self.f.writes)
        # A release of Scale that isn't scale#38's wakes no one; scale#38's own release does.
        self.f.items[(SCALE, 38)]["events"].append(
            comment("gatekeeper", 100, "(no reply needed)\n\nReleased in v0.5.3: ..."))
        self.f.items[(CORE, 12)]["labels"].append({"name": "blocked"})
        d.evaluate(CORE, 12)
        self.assertEqual(self.descriptions("core"),
                         [f"{SCALE}#38 was released in v0.5.3; {CORE}#12 is blocked on it"])

    def test_8_a_line_may_name_a_pull_request_coordination_198(self):
        # demos#19 waited for its owner's own pull request, which doesn't name it.
        self.f.pull(CORE, 13, "core")
        self.f.issue(CORE, 19, "scale", ["request", "accepted"],
                     events=[comment("core", 7000, "Blocked-by: org/inkproof-core#13")])
        d.evaluate(CORE, 19)
        self.assertEqual(self.queued("core"), [])
        self.assertIn(("POST", f"/repos/org/{CORE}/issues/19/labels", {"labels": [1]}), self.f.writes)
        self.f.items[(CORE, 13)]["state"] = "closed"
        self.f.items[(CORE, 13)]["pr"].update(state="closed", merged=True, merged_at=at(100))
        self.f.items[(CORE, 19)]["labels"].append({"name": "blocked"})
        d.evaluate(CORE, 19)
        self.assertEqual(self.descriptions("core"), [f"pull request {CORE}#13 merged; {CORE}#19 is blocked on it"])

    def test_8_the_humans_comment_on_an_upstream_issue(self):
        self.f.issue(COORD, 9, "core", ["upstream"], events=[comment("human", 100, "Fixed in Lean 4.36.")])
        self.f.issue(CORE, 3, "core", ["blocked"], created=5000, body="Blocked-by: org/inkproof-coordination#9")
        d.evaluate(CORE, 3)
        self.assertEqual(self.descriptions("core"),
                         [f"human commented on upstream issue {COORD}#9; {CORE}#3 is blocked on it"])

    def test_8_an_upstream_issue_an_agent_waits_on_is_blocking_web_2(self):
        # web filed coordination#116 without `blocking` and marked its task
        # blocked on it: nothing told the human, and the kit went quiet.
        self.f.issue(COORD, 116, "core", ["upstream", "upstream:lasm"])
        self.f.issue(CORE, 2, "coordinator", ["request", "accepted"], body="Blocked-by: org/inkproof-coordination#116")
        d.evaluate(CORE, 2)
        self.assertIn(("POST", f"/repos/org/{COORD}/issues/116/labels", {"labels": [3]}), self.f.writes)
        # Once nothing open waits on it, it no longer waits on the human.
        self.f.items[(COORD, 116)]["labels"].append({"name": "blocking"})
        self.f.items[(CORE, 2)]["state"] = "closed"
        self.f.writes.clear()
        d.evaluate(CORE, 2)
        self.assertEqual([w for w in self.f.writes if "/issues/116/" in w[1]],
                         [("DELETE", f"/repos/org/{COORD}/issues/116/labels/3", None)])

    def test_8_blocking_follows_the_lines_not_who_set_it(self):
        # Kept by the sweep too: an upstream issue labelled `blocking` that
        # nothing names loses it; an issue that isn't upstream never gets it.
        self.f.issue(COORD, 9, "core", ["upstream", "blocking"])
        self.f.issue(SCALE, 4, "scale", ["request"])
        self.f.issue(CORE, 3, "core", ["request", "accepted"], body="Blocked-by: org/inkproof-scale#4")
        d.sweep()
        writes = [w for w in self.f.writes if "/labels" in w[1] and not w[1].endswith("/labels/1")
                  and not (w[0] == "POST" and w[2] == {"labels": [1]})]
        self.assertEqual(writes, [("DELETE", f"/repos/org/{COORD}/issues/9/labels/3", None)])

    def test_9_ci_failing_on_main(self):
        self.f.statuses["m" * 40] = ["failure"]
        d.evaluate_repo(CORE)
        self.assertEqual(self.queued("core"), ["main-failed"])


class RequesterRules(Case):
    def test_10_released_wakes_the_requester_to_verify(self):
        self.f.issue(CORE, 9, "scale", ["request", "accepted"],
                     events=[comment("gatekeeper", 100, "(no reply needed)\n\nReleased in v1.2.3: ...")])
        d.evaluate(CORE, 9)
        self.assertEqual(self.queued("scale"), ["released"])

    def test_10_not_the_coordinators_own_release_but_the_humans(self):
        self.f.issue(CORE, 9, "coordinator", ["request"],
                     events=[comment("gatekeeper", 100, "(no reply needed)\n\nReleased in v1.2.3: ...")])
        self.f.issue(CORE, 10, "coordinator", ["request"],
                     events=[comment("human", 100, "(no reply needed)\n\nReleased in v1.2.4: ...")])
        d.evaluate(CORE, 9)
        d.evaluate(CORE, 10)
        self.assertEqual([k for k in self.queued("coordinator") if k == "released"], ["released"])

    def test_10_still_unverified_a_day_later(self):
        self.f.issue(CORE, 9, "scale", ["request"],
                     events=[comment("gatekeeper", 90000, "(no reply needed)\n\nReleased in v1.2.3: ...")])
        d.evaluate(CORE, 9)
        self.assertEqual(self.queued("scale"), ["released", "unverified"])

    def test_11_counter_proposed_or_rejected(self):
        self.f.issue(CORE, 1, "scale", ["request", "counter-proposal"], events=[label("counter-proposal", True, 100)])
        self.f.issue(CORE, 2, "scale", ["request"], state="closed", events=[event("close", "core", 100)])
        self.f.issue(CORE, 3, "scale", ["request"], state="closed", events=[event("close", "scale", 100)])
        self.f.issue(CORE, 4, "scale", ["request", "verified"], state="closed", events=[event("close", "core", 100)])
        for n in (1, 2, 3, 4):
            d.evaluate(CORE, n)
        self.assertEqual(self.descriptions("scale"), [f"your request {CORE}#1 has a counter-proposal",
                                                      f"your request {CORE}#2 was closed without being verified"])


class CoordinatorRules(Case):
    def test_12_a_request_filed(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        self.f.issue(CORE, 2, "coordinator", ["request", "new"])
        d.evaluate(CORE, 1)
        d.evaluate(CORE, 2)
        self.assertEqual([x for x in self.descriptions("coordinator") if x.startswith("request filed")],
                         [f"request filed: {CORE}#1"])

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
        self.assertEqual(self.descriptions("coordinator"), [f"pull request opened: {CORE}#9"])

    def test_13_a_changelog_only_pull_request_that_grows(self):
        self.f.pull(CORE, 8, "core", files=("CHANGELOG.md",), ci="success")
        d.evaluate(CORE, 8)
        self.assertEqual(self.queued("coordinator"), [])
        self.f.items[(CORE, 8)]["files"].append("Inkproof/Core.lean")
        d.evaluate(CORE, 8)
        self.assertEqual(self.queued("coordinator"), ["pr-opened"])

    def test_14_its_review_once_ci_passed_and_until_it_approves(self):
        self.f.pull(CORE, 7, "core", requested=["coordinator"], events=[event("pull_scheduled_merge", "core", 50)])
        self.f.statuses["a" * 40] = ["pending"]
        d.evaluate(CORE, 7)
        self.assertNotIn("review-requested", self.queued("coordinator"))
        self.f.statuses["a" * 40] = ["success"]
        d.evaluate(CORE, 7)
        self.assertIn("review-requested", self.queued("coordinator"))
        d.q("DELETE FROM triggers")
        self.f.items[(CORE, 7)]["reviews"].append({"id": 1, "user": {"login": "coordinator"}, "state": "APPROVED",
                                                   "commit_id": "a" * 40, "submitted_at": at(10)})
        d.evaluate(CORE, 7)
        self.assertNotIn("review-requested", self.queued("coordinator"))

    def test_14_says_whether_its_review_is_the_one_needed_gpu_9(self):
        self.f.codeowners[SCALE] = ".* @gatekeeper @conformance\nAPI\\.lock @coordinator\n"
        for repo, owner in ((SCALE, "scale"), (CORE, "core")):  # Core has no CODEOWNERS on main
            self.f.pull(repo, 9, owner, requested=["coordinator"], ci="success")
            d.evaluate(repo, 9)
        level3, below = [r["description"] for r in d.q(
            "SELECT description FROM triggers WHERE key LIKE 'review-requested:%' ORDER BY key DESC")]
        self.assertIn(f"{SCALE} is at level 3: CODEOWNERS on main gives `API.lock` to you, so this pull request "
                      "waits for your review", level3)
        self.assertIn("a comment alone leaves it waiting", level3)
        self.assertIn(f"{CORE} is below level 3, so the human approves its `API.lock`", below)

    def test_14_after_a_push_dismissed_its_approval_web_8(self):
        # Forgejo stops listing it as requested once it reviews; the push
        # dismissed its approval and requested no one again.
        self.f.pull(SCALE, 8, "scale", sha="b" * 40, ci="success", events=[
            event("pull_push", "scale", 3000),
            {"type": "review_request", "user": {"login": "scale"}, "assignee": {"login": "coordinator"},
             "created_at": at(3000)},
            event("pull_push", "scale", 1000)],
            reviews=[{"id": 1, "user": {"login": "coordinator"}, "state": "APPROVED", "commit_id": "a" * 40,
                      "submitted_at": at(2000), "dismissed": True}])
        d.evaluate(SCALE, 8)
        self.assertIn("review-requested", self.queued("coordinator"))

    def test_15_consumer_metrics_13_fixed_with_the_label_left_on(self):
        self.f.issue(CORE, 13, "coordinator", ["request", "ready-for-release"],
                     events=[label("ready-for-release", True, 4000), comment("coordinator", 3900, "Refused: ...")])
        self.f.mains[CORE] = {"id": "n" * 40, "timestamp": at(2000)}
        d.evaluate(CORE, 13)
        self.assertEqual([x for x in self.descriptions("coordinator") if "ready-for-release" in x],
                         [f"{CORE}#13 is still labelled ready-for-release, and main has changed or its CI has finished "
                          f"since you last looked at it. {BELOW_RELEASE}"])

    def test_15_not_once_it_has_looked(self):
        self.f.issue(CORE, 13, "coordinator", ["request", "ready-for-release"],
                     events=[label("ready-for-release", True, 4000), comment("coordinator", 1000, "Not yet.")])
        self.f.mains[CORE] = {"id": "n" * 40, "timestamp": at(2000)}
        d.evaluate(CORE, 13)
        self.assertNotIn("rfr", self.queued("coordinator"))

    def test_15_scale_38_refused_while_mains_ci_ran_then_it_passed(self):
        """The coordinator looked while CI on main was running (the gatekeeper
        refused), and main didn't change after: CI finishing is the change."""
        self.f.issue(CORE, 13, "scale", ["request", "ready-for-release"],
                     events=[label("ready-for-release", True, 400, "core"),
                             comment("coordinator", 300, "The gatekeeper declined: CI on main is running.")])
        self.f.mains[CORE] = {"id": "n" * 40, "timestamp": at(420)}
        self.f.statuses["n" * 40] = [("running", 410)]
        d.evaluate(CORE, 13)
        self.assertNotIn("rfr", self.queued("coordinator"))
        self.f.statuses["n" * 40] = [("failure", 100)]
        d._cache.clear()
        d.evaluate(CORE, 13)
        self.assertNotIn("rfr", self.queued("coordinator"))
        self.f.statuses["n" * 40] = [("success", 100)]
        d._cache.clear()
        d.evaluate(CORE, 13)
        self.assertEqual([x for x in self.descriptions("coordinator") if "ready-for-release" in x],
                         [f"{CORE}#13 is still labelled ready-for-release, and main has changed or its CI has "
                          f"finished since you last looked at it. {BELOW_RELEASE}"])

    def test_15_not_while_mains_ci_runs(self):
        self.f.issue(CORE, 13, "scale", ["request", "ready-for-release"],
                     events=[label("ready-for-release", True, 400, "core")])
        self.f.statuses["m" * 40] = [("running", 410)]
        d.evaluate(CORE, 13)
        self.assertNotIn("rfr", self.queued("coordinator"))
        self.f.statuses["m" * 40] = [("success", 100)]
        d._cache.clear()
        d.evaluate(CORE, 13)
        self.assertEqual([x for x in self.descriptions("coordinator") if "ready-for-release" in x],
                         [f"ready-for-release added to issue {CORE}#13. {BELOW_RELEASE}"])

    def test_15_says_who_releases_gpu_27(self):
        self.f.codeowners[SCALE] = ".* @gatekeeper @conformance\nAPI\\.lock @coordinator\n"
        for repo, owner in ((SCALE, "scale"), (CORE, "core")):  # Core has no CODEOWNERS on main
            self.f.issue(repo, 13, owner, ["request", "ready-for-release"],
                         events=[label("ready-for-release", True, 400, owner)])
            d.evaluate(repo, 13)
        self.assertEqual(sorted(x for x in self.descriptions("coordinator") if "ready-for-release" in x),
                         [f"ready-for-release added to issue {CORE}#13. {BELOW_RELEASE}",
                          f"ready-for-release added to issue {SCALE}#13. {SCALE} is at level 3: CODEOWNERS on main "
                          "gives `API.lock` to you, so you release it with `forge release`"])

    def test_27_a_release_overtakes_its_decision_gpu_27(self):
        self.f.issue(CORE, 27, "coordinator", ["needs-human"], title=f"Release {CORE}", created=5000,
                     events=[label("needs-human", True, 5000, "coordinator"), comment("human", 100, "Go ahead")])
        self.f.release(CORE, "v0.6.0", 9000)  # before the decision
        d.evaluate(CORE, 27)
        self.assertNotIn("overtaken", self.queued("coordinator"))
        self.f.release(CORE, "v0.6.1", 3000)
        self.f.release(CORE, "v0.6.2", 2000, draft=True)
        d._cache.clear()
        d.evaluate(CORE, 27)
        self.assertEqual([x for x in self.descriptions("coordinator") if "released" in x],
                         [f"{CORE} released v0.6.1 after your decision {CORE}#27 asked the human to release it: "
                          "close the decision if that release covers it, or update it for what's left"])

    def test_27_not_once_it_has_looked_nor_on_other_issues(self):
        self.f.issue(CORE, 27, "coordinator", ["needs-human"], title=f"Release {CORE}", created=5000,
                     events=[comment("coordinator", 1000, "Updated for v0.6.2.")])
        self.f.issue(CORE, 28, "coordinator", ["needs-human"], title="Release notes", created=5000)
        self.f.issue(CORE, 29, "core", ["needs-human"], title=f"Release {CORE}", created=5000)
        self.f.release(CORE, "v0.6.1", 3000)
        for n in (27, 28, 29):
            d.evaluate(CORE, n)
        self.assertNotIn("overtaken", self.queued("coordinator"))

    def test_27_a_release_published_evaluates_the_decision(self):
        self.f.issue(CORE, 27, "coordinator", ["needs-human"], title=f"Release {CORE}", created=5000)
        self.f.release(CORE, "v0.6.1", 10)
        d.handle("release", {"action": "published", "repository": {"name": CORE, "owner": {"login": "org"}},
                             "sender": {"login": "gatekeeper"}, "release": {"tag_name": "v0.6.1"}})
        self.assertEqual(self.queued("coordinator"), ["overtaken"])

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

    def test_17_a_milestone_with_nothing_left_open(self):
        self.f.milestones[CORE] = [{"id": 3, "title": "Float library", "open_issues": 0, "closed_issues": 4},
                                   {"id": 4, "title": "Next", "open_issues": 0, "closed_issues": 0}]
        d.evaluate_repo(CORE)
        self.assertEqual(self.descriptions("coordinator"),
                         [f"the last open issue in milestone Float library was closed, on every repository it "
                          f"spans ({CORE})"])

    def test_17_once_per_milestone_when_every_namesake_is_done_coordination_107(self):
        def ms(i, open_, closed, state="open"):
            return {"id": i, "title": "Milestone 5", "state": state, "open_issues": open_, "closed_issues": closed,
                    "created_at": at(1000)}
        self.f.milestones[SCALE] = [ms(7, 0, 1)]
        self.f.milestones[CORE] = [ms(8, 2, 1)]
        self.f.milestones[COORD] = [ms(6, 0, 0)]  # an empty namesake counts neither way
        d.evaluate_repo(SCALE)
        self.assertEqual(self.queued("coordinator"), [], "Core's namesake still has open issues")
        self.f.milestones[CORE] = [ms(8, 0, 3)]
        for repo in (SCALE, CORE, COORD):
            d.evaluate_repo(repo)
        self.assertEqual(d.q("SELECT key FROM triggers WHERE key LIKE 'milestone-done:%'")[0]["key"],
                         "milestone-done:Milestone 5:8")
        self.assertEqual(self.queued("coordinator").count("milestone-done"), 1, "once, from whichever repository")

    def test_23_no_milestone_with_open_work_review_follow_ups_5(self):
        def ms(i, title, state, created, open_=0, closed_issues=2, closed=None):
            return {"id": i, "title": title, "state": state, "open_issues": open_, "closed_issues": closed_issues,
                    "created_at": at(created), "updated_at": at(closed if closed is not None else 100),
                    "closed_at": at(closed) if closed is not None else None}
        self.f.milestones[CORE] = [ms(1, "Milestone 2", "closed", 99000, closed=90000),
                                   ms(3, "Follow-ups", "open", 3000, open_=1, closed_issues=1)]
        self.f.milestones[COORD] = [ms(2, "Milestone 5", "closed", 9000, closed=6000),
                                    ms(4, "Milestone 5", "open", 2000, closed_issues=0)]  # an empty duplicate
        d.evaluate_repo(COORD)
        self.assertEqual(self.queued("coordinator"), [], "Core's follow-ups still have open work")
        # Its last issue closes; the milestone is left open.
        self.f.milestones[CORE][1] = ms(3, "Follow-ups", "open", 3000, closed_issues=2)
        run = self.running("coordinator")
        d.evaluate_repo(COORD)
        self.assertEqual(self.queued("coordinator"), [], "not while it runs: it may be drafting the next one")
        d.end_run("coordinator", run, {"outcome": "completed"})
        d.evaluate_repo(COORD)
        self.assertEqual(d.q("SELECT key FROM triggers WHERE run_id IS NULL")[0]["key"], f"next-milestone:{CORE}:3")
        self.assertIn("close out any milestone left open", self.descriptions("coordinator")[0])
        self.handled()
        # Closing what was left open, the empty duplicate included, doesn't wake it again.
        self.f.milestones[CORE][1] = ms(3, "Follow-ups", "closed", 3000, closed=50)
        self.f.milestones[COORD][1] = ms(4, "Milestone 5", "closed", 2000, closed_issues=0, closed=40)
        d.evaluate_repo(COORD)
        self.assertEqual(self.queued("coordinator"), [], "once per milestone finished")
        # The next milestone's work, once done, is due in its own right.
        self.f.milestones[COORD].append(ms(5, "Milestone 6", "open", 30, closed_issues=3))
        d.evaluate_repo(COORD)
        self.handled()  # rule 17 for it comes first; 23 waits until the coordinator has nothing queued
        d.evaluate_repo(COORD)
        self.assertIn(f"next-milestone:{COORD}:5", [r["key"] for r in d.q("SELECT key FROM triggers WHERE run_id IS NULL")])


class ReviewerRules(Case):
    def conf(self):
        return d.q("SELECT not_before FROM triggers WHERE agent='conformance' AND key LIKE 'conf:%' "
                   "AND run_id IS NULL")

    def test_19_a_passing_head_from_an_idle_agent_at_once(self):
        self.f.pull(CORE, 7, "core", events=[event("pull_scheduled_merge", "core", 50)])
        self.f.statuses["a" * 40] = ["success"]
        d.evaluate(CORE, 7)
        self.assertIsNone(self.conf()[0]["not_before"])

    def test_19_a_running_author_settles_first_and_its_run_ending_releases_it(self):
        self.f.pull(CORE, 7, "core", events=[event("pull_push", "core", 30), event("pull_scheduled_merge", "core", 50)])
        self.f.statuses["a" * 40] = ["success"]
        run = self.running("core")
        d.evaluate(CORE, 7)
        self.assertGreater(self.conf()[0]["not_before"], d.now() + 500)
        d.end_run("core", run, {"outcome": "completed"})
        self.assertIsNone(self.conf()[0]["not_before"])

    def test_19_not_before_ci_passes_nor_once_reviewed(self):
        self.f.pull(CORE, 7, "core")
        self.f.statuses["a" * 40] = ["pending"]
        self.f.pull(CORE, 8, "core", sha="b" * 40, reviews=[
            {"id": 1, "user": {"login": "conformance"}, "state": "APPROVED", "commit_id": "b" * 40,
             "submitted_at": at(10)}])
        self.f.statuses["b" * 40] = ["success"]
        d.evaluate(CORE, 7)
        d.evaluate(CORE, 8)
        self.assertEqual(self.conf(), [])

    def test_20_the_author_replied_to_its_request_for_changes(self):
        self.f.pull(CORE, 7, "core", events=[comment("core", 50, "I keep it because ...")], reviews=[
            {"id": 1, "user": {"login": "conformance"}, "state": "REQUEST_CHANGES", "commit_id": "a" * 40,
             "submitted_at": at(100)}])
        d.evaluate(CORE, 7)
        self.assertIn("conf-reply", self.queued("conformance"))

    def test_20_not_while_the_pull_request_is_blocked_demo_35(self):
        # The author agreed and will fix it in another pull request first:
        # the head changes before there is anything to review again.
        self.f.issue(CORE, 37, "core")
        self.f.pull(CORE, 35, "core", ["blocked"], events=[
            comment("core", 50, "#37 replaces the gate; I'll merge main in once it lands.\n\n"
                                "Blocked-by: org/inkproof-core#37")], reviews=[
            {"id": 1, "user": {"login": "conformance"}, "state": "REQUEST_CHANGES", "commit_id": "a" * 40,
             "submitted_at": at(100)}])
        d.evaluate(CORE, 35)
        self.assertNotIn("conf-reply", self.queued("conformance"))

    def test_21_and_22_milestone_reviews_and_blocking_lasm_issues(self):
        self.f.issue(COORD, 30, "coordinator", ["milestone-review"])
        self.f.issue(COORD, 31, "core", ["upstream", "upstream:lasm", "blocking"])
        d.evaluate(COORD, 30)
        d.evaluate(COORD, 31)
        self.assertEqual(self.queued("architecture"), ["milestone"])
        self.assertEqual(self.queued("lasm"), ["milestone", "lasm-blocking"])

    def test_24_every_review_reported_on_an_open_milestone_issue_coordination_82(self):
        events = [label("milestone-review", True, 600, "coordinator"),
                  comment("lasm", 500, "(no reply needed)\nNo findings.")]
        events = self.f.issue(COORD, 82, "coordinator", ["milestone-review"], events=events)["events"]
        d.evaluate(COORD, 82)
        self.assertNotIn("reviewed", self.queued("coordinator"), "architecture hasn't reported")
        events.append(comment("architecture", 400, "(no reply needed)\nReport."))
        d.evaluate(COORD, 82)
        self.assertEqual(self.descriptions("coordinator")[-1],
                         f"every milestone review has reported on {COORD}#82: accept the milestone (say what you "
                         "checked, close the issue and its Forgejo milestones, record it in ROADMAP.md), or say "
                         "on it what it waits for")
        rows = d.q("SELECT id, key FROM triggers WHERE key LIKE 'reviewed:%' AND run_id IS NULL")
        events.append(comment("coordinator", 100, "(no reply needed)\nWaiting on #85."))
        self.assertEqual(d.no_longer_owed(rows), [rows[0]["id"]], "it has looked since")

    def test_21_the_coordinator_accepted_and_closed_it_at_once_coordination_73(self):
        self.f.issue(COORD, 73, "coordinator", ["milestone-review"], state="closed", closed=60, events=[
            label("milestone-review", True, 70, "coordinator"), comment("coordinator", 60, "(no reply needed)\nAccepted."),
            comment("lasm", 30, "(no reply needed)\nNo findings.")])
        d.evaluate(COORD, 73)
        self.assertEqual(self.queued(), [("architecture", "milestone")], "lasm has reported")
        rows = d.q("SELECT id, key FROM triggers WHERE run_id IS NULL")
        self.assertEqual(d.no_longer_owed(rows), [], "a claim keeps it")
        self.f.items[(COORD, 73)]["events"].append(comment("architecture", 10, "(no reply needed)\nReport."))
        self.assertEqual(d.no_longer_owed(rows), [rows[0]["id"]], "until it has reported")


class Stalls(Case):
    """Once the kit is quiet, every open item must wait for something the kit
    can see; whoever should move on one that doesn't is woken."""

    def setUp(self):
        super().setUp()
        self.clock = d.now()
        self.real_now = d.now
        d.now = lambda: self.clock

    def tearDown(self):
        d.now = self.real_now

    def spell(self):
        """A quiet spell later: the stall check, its work then handled."""
        self.clock += 3600
        d._cache.clear()
        n = d.check_stalls()
        found = sorted((r["agent"], r["key"].split(":")[1]) for r in
                       d.q("SELECT agent, key FROM triggers WHERE run_id IS NULL AND key LIKE 'stalled:%'"))
        self.handled()
        return n, found

    def test_what_waits_visibly_and_what_doesnt(self):
        approved = [{"id": 1, "user": {"login": "conformance"}, "state": "APPROVED", "commit_id": "a" * 40,
                     "submitted_at": at(100)}]

        def requested(*who):
            return [{**event("review_request", "core", 4000), "assignee": {"login": w}} for w in who]

        self.f.issue(CORE, 1, "scale", ["request", "accepted"])  # nothing moves it
        self.f.issue(CORE, 2, "scale", ["request", "needs-human"])  # the human's
        self.f.issue(CORE, 3, "coordinator", ["request", "accepted"], body="Blocked-by: org/inkproof-core#2")
        self.f.issue(CORE, 5, "scale", ["request", "ready-for-release"])  # the coordinator's
        self.f.issue(CORE, 6, "scale", ["request", "accepted"])  # its pull request #7 is checked instead
        self.f.pull(CORE, 7, "core", body="Refs #6", events=requested("conformance"))  # conformance's
        self.f.issue(CORE, 10, "scale", ["request", "blocked"])  # blocked, but naming nothing
        self.f.pull(CORE, 11, "core", events=requested("conformance"), reviews=approved)  # approved, not merging
        self.f.pull(CORE, 12, "core", events=requested("conformance", "human"), reviews=approved)  # the human's
        self.f.issue(CORE, 13, "scale", ["request", "counter-proposal"])  # the requester's answer
        self.f.issue(CORE, 14, "scale", ["request"], events=[comment("gatekeeper", 100, "Released in v1.0.0")])
        self.f.issue(COORD, 9, "core", ["upstream", "upstream:lasm"])  # the world outside
        # Its reviewers wait for CI, so only its author can move it (export#4).
        self.f.pull(CORE, 15, "core", sha="f" * 40, ci=None, events=requested("conformance"))
        self.f.pull(CORE, 16, "core", sha="e" * 40, ci="failure", events=requested("conformance"))
        n, found = self.spell()
        self.assertEqual(found, sorted([("core", f"{CORE}#1"), ("coordinator", f"{CORE}#5"),
                                        ("conformance", f"{CORE}#7"), ("core", f"{CORE}#10"),
                                        ("core", f"{CORE}#11"), ("scale", f"{CORE}#13"),
                                        ("core", f"{CORE}#15"), ("core", f"{CORE}#16")]))
        self.assertEqual(n, 8)

    def test_twice_without_moving_then_the_humans_and_a_fix_starts_again(self):
        self.f.issue(CORE, 1, "scale", ["request", "accepted"])
        self.assertEqual(self.spell(), (1, [("core", f"{CORE}#1")]))
        self.assertEqual(self.spell(), (1, [("core", f"{CORE}#1")]))
        self.assertEqual(self.spell(), (0, []))
        # The human hears of the gap once, when it is first caught, then of the stall.
        self.assertEqual([r["kind"] for r in d.q("SELECT kind FROM notices ORDER BY rowid")], ["stall", "stalled"])
        caught = [b["body"] for m, p, b in self.f.writes if p == f"/repos/org/{CORE}/issues/1/comments"]
        self.assertEqual(len(caught), 1)
        self.assertIn("the stall check woke core for this issue", caught[0])
        # Once it waits visibly, a later stall starts from nothing, and is a gap of its own.
        self.f.items[(CORE, 1)]["labels"].append({"name": "needs-human"})
        self.assertEqual(self.spell(), (0, []))
        self.f.items[(CORE, 1)]["labels"].pop()
        self.assertEqual(self.spell(), (1, [("core", f"{CORE}#1")]))
        self.assertEqual([r["kind"] for r in d.q("SELECT kind FROM notices ORDER BY rowid")],
                         ["stall", "stalled", "stall"])

    def test_moved_on_between_checks_starts_again_demos_22(self):
        self.f.issue(CORE, 1, "scale", ["request", "accepted"])
        self.assertEqual(self.spell(), (1, [("core", f"{CORE}#1")]))
        # Its owner only says what it will do, and the dispatcher's own labels
        # are no progress: the count goes on.
        self.f.items[(CORE, 1)]["events"] += [comment("core", 60, "I'll do it once #2 merges."),
                                              label("blocked", False, 30, who="dispatcher")]
        self.assertEqual(self.spell(), (1, [("core", f"{CORE}#1")]))
        self.assertEqual(json.loads(d.kv_get(f"stall:{CORE}#1"))[0], 2)
        # A pull request referring to it opens and merges while the kit is busy,
        # so no check sees it moving: still, it starts again from nothing.
        self.f.items[(CORE, 1)]["events"].append(event("pull_ref", "core", 60))
        self.assertEqual(self.spell(), (1, [("core", f"{CORE}#1")]))
        self.assertEqual(json.loads(d.kv_get(f"stall:{CORE}#1"))[0], 1)
        self.assertEqual([r["kind"] for r in d.q("SELECT kind FROM notices ORDER BY rowid")], ["stall", "stall"])

    def test_a_count_from_before_the_mark_carries_on(self):
        self.f.issue(CORE, 1, "scale", ["request", "accepted"], events=[event("pull_ref", "core", 60)])
        d.kv_set(f"stall:{CORE}#1", json.dumps([2, 1]))
        self.assertEqual(self.spell(), (0, []))
        self.assertEqual([r["kind"] for r in d.q("SELECT kind FROM notices ORDER BY rowid")], ["stalled"])

    def test_a_capped_agent_isnt_woken_or_counted(self):
        self.f.issue(CORE, 1, "scale", ["request", "accepted"])
        for _ in range(d.CAP):
            d.q("INSERT INTO runs (agent, triggers, claimed, ended) VALUES ('core', '[]', ?, ?)", self.clock + 3600,
                self.clock + 3600)
        self.assertEqual(self.spell(), (0, []))
        self.assertIsNone(d.kv_get(f"stall:{CORE}#1"))


class Mechanics(Case):
    def test_once_per_version(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        d.evaluate(CORE, 1)
        self.handled()
        d.evaluate(CORE, 1)
        self.assertEqual(self.queued("core"), [])

    def test_a_claim_drops_what_is_no_longer_owed(self):
        self.f.pull(CORE, 7, "core")
        d.evaluate(CORE, 7)
        d.q("UPDATE triggers SET created=?", d.now() - 3600)
        self.f.items[(CORE, 7)]["events"].append(event("pull_scheduled_merge", "core", 10))
        claimed = d.claim("core")
        self.assertIsNone(claimed)
        self.assertEqual(self.queued("core"), [])
        # Renamed, so the same state can queue it again.
        self.f.items[(CORE, 7)]["events"].append(event("pull_cancel_scheduled_merge", "core", 5))
        d.evaluate(CORE, 7)
        self.assertEqual(self.queued("core"), ["unscheduled"])

    def test_a_claim_keeps_what_is_still_owed(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        d.evaluate(CORE, 1)
        d.q("UPDATE triggers SET created=?", d.now() - 3600)
        self.assertEqual(d.claim("core")["triggers"], [f"untriaged request {CORE}#1"])

    def test_a_claim_keeps_merged_work_to_finish_consumer_metrics_12(self):
        self.f.issue(CORE, 5, "scale", ["request", "accepted"])
        self.f.pull(CORE, 7, "core", body="Refs #5", state="closed", merged=100)
        d.evaluate(CORE, 5)
        d.q("UPDATE triggers SET created=?", d.now() - 3600)
        self.assertEqual(d.claim("core")["triggers"],
                         [f"your pull request {CORE}#7 merged; it answers {CORE}#5, not yet labelled ready-for-release"])

    def test_the_first_sweep_records_what_the_old_triggers_covered(self):
        d.q("DELETE FROM kv")
        self.f.issue(CORE, 1, "scale", ["request", "new"], events=[comment("scale", 100)])
        d.reconcile()  # inkproof-tools, not in Forgejo yet, is skipped
        self.assertEqual(self.queued("core"), ["request"])  # the comment's kind is recorded as handled
        self.assertTrue(d.kv_get("rules-seeded"))

    def test_a_blocker_closing_evaluates_what_waits_on_it(self):
        self.f.issue(SCALE, 38, "coordinator", ["request"])
        self.f.issue(CORE, 12, "coordinator", ["request", "accepted", "blocked"], body="Blocked-by: org/inkproof-scale#38")
        d.evaluate(CORE, 12)
        self.assertEqual(self.queued("core"), [])
        self.f.items[(SCALE, 38)].update(state="closed", closed_at=at(5))
        d.handle("issues", {"action": "closed", "repository": {"name": SCALE, "owner": {"login": "org"}},
                            "sender": {"login": "coordinator"},
                            "issue": {"number": 38, "state": "closed", "labels": [{"name": "request"}]}})
        self.assertIn("unblocked", self.queued("core"))

    def test_a_merge_evaluates_the_requests_it_answers(self):
        self.f.issue(CORE, 5, "scale", ["request", "accepted"])
        self.f.pull(CORE, 7, "core", body="Refs #5", state="closed", merged=5)
        d.handle("pull_request", {"action": "closed", "repository": {"name": CORE, "owner": {"login": "org"}},
                                  "sender": {"login": "core"},
                                  "pull_request": {"number": 7, "merged": True, "user": {"login": "core"}}})
        self.assertIn("merged", self.queued("core"))

    def test_a_release_the_coordinator_didnt_ask_for(self):
        for by in ("gatekeeper", "human"):
            d.handle("release", {"action": "published", "repository": {"name": SCALE, "owner": {"login": "org"}},
                                 "sender": {"login": by}, "release": {"tag_name": f"v1.0.{len(by)}"}})
        self.assertEqual(self.descriptions("coordinator"), [f"{SCALE} released v1.0.5"])


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
    """The coordinator's idle summary, once a quiet spell finds nothing stalled."""

    def setUp(self):
        super().setUp()
        self.clock, self.waiting = d.now(), ["inkproof-core#1"]
        for name, value in (("now", lambda: self.clock), ("ci_running", lambda: False),
                            ("check_stalls", lambda: 0), ("waiting_for_human", lambda: list(self.waiting))):
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


if __name__ == "__main__":
    unittest.main()
