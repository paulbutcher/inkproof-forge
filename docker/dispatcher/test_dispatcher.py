"""Unit tests for the dispatcher's decisions, against a fake Forgejo that
serves issues and pull requests as they really look. Run by
scripts/test-dispatcher.sh, in the dispatcher's image.

The rules' numbers are those in the dispatcher's Rules section."""
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
            "conformance pr-reviewer - *\n"
            "architecture milestone-reviewer - *\n"
            "lasm milestone-reviewer - *\n")
os.environ.update(STATE_DIR=STATE, AGENTS_CONF=CONF, CONTROL_DIR=STATE, FORGE_ORG="org",
                  HUMAN_USER="human", CONFORMANCE_SETTLE_SECONDS="600")

import dispatcher as d  # noqa: E402

CORE, SCALE, COORD = "inkproof-core", "inkproof-scale", "inkproof-coordination"
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

    def issue(self, repo, n, author, labels=(), state="open", body="", created=7200, events=(), closed=None):
        self.items[(repo, n)] = {"number": n, "state": state, "user": {"login": author}, "body": body,
                                 "labels": [{"name": l} for l in labels], "created_at": at(created),
                                 "updated_at": at(0), "closed_at": at(closed) if closed is not None else None,
                                 "events": list(events), "reviews": [], "pr": None}
        return self.items[(repo, n)]

    def pull(self, repo, n, author, labels=(), sha="a" * 40, events=(), reviews=(), mergeable=True, title="Work",
             body="", requested=(), merged=None, state="open", created=7200):
        i = self.issue(repo, n, author, labels, state, body, created, events)
        i["reviews"] = list(reviews)
        i["pr"] = {"number": n, "state": state, "user": {"login": author}, "head": {"sha": sha},
                   "mergeable": mergeable, "title": title, "body": body, "created_at": at(created),
                   "merged": merged is not None, "merged_at": at(merged) if merged is not None else None,
                   "requested_reviewers": [{"login": r} for r in requested]}
        return i

    def issue_json(self, i):
        j = {k: v for k, v in i.items() if k not in ("events", "reviews", "pr")}
        if i["pr"]:
            j["pull_request"] = {}
        return j

    def __call__(self, method, path, body=None):
        path, _, query = path.partition("?")
        args = dict(urllib.parse.parse_qsl(query))
        if method != "GET":
            self.writes.append((method, path, body))
            return {}
        if int(args.get("page", 1)) > 1:
            return []
        repo, rest = re.fullmatch(r"/repos/org/([\w.-]+)/?(.*)", path).groups()
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
        if m := re.fullmatch(r"issues/(\d+)/comments", rest):
            return [e for e in self.items[(repo, int(m[1]))]["events"] if e["type"] == "comment"]
        if m := re.fullmatch(r"pulls/(\d+)/reviews", rest):
            return self.items[(repo, int(m[1]))]["reviews"]
        if re.fullmatch(r"pulls/(\d+)/commits", rest):
            return []
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
        if rest == "milestones":
            return self.milestones.get(repo, [])
        if rest == "labels":
            return [{"id": 1, "name": "blocked"}, {"id": 2, "name": "needs-human"}]
        raise AssertionError(f"unexpected call {method} {path}")


class Case(unittest.TestCase):
    def setUp(self):
        for t in ("triggers", "runs", "kv", "notices", "blocked_by", "labels"):
            d.q(f"DELETE FROM {t}")
        d.kv_set("rules-seeded", d.now())
        d._cache.clear()
        self.f = Forge()
        d.forge = self.f

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
    def test_1_an_untriaged_request_from_someone_else(self):
        self.f.issue(CORE, 1, "scale", ["request", "new"])
        self.f.issue(CORE, 2, "core", ["request", "new"])
        self.f.issue(CORE, 3, "stranger", ["request", "new"])
        for n in (1, 2, 3):
            d.evaluate(CORE, n)
        self.assertEqual(self.descriptions("core"), [f"untriaged request {CORE}#1"])

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

    def test_6_not_once_labelled_or_released(self):
        self.f.issue(CORE, 5, "scale", ["request", "accepted", "ready-for-release"])
        self.f.issue(CORE, 6, "scale", ["request", "accepted"],
                     events=[comment("gatekeeper", 50, "(no reply needed)\n\nReleased in v0.2.0: ...")])
        self.f.pull(CORE, 7, "core", body="Refs #5\nRefs #6", state="closed", merged=100)
        d.evaluate(CORE, 5)
        d.evaluate(CORE, 6)
        self.assertEqual(self.queued("core"), [])

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

    def test_8_the_humans_comment_on_an_upstream_issue(self):
        self.f.issue(COORD, 9, "core", ["upstream"], events=[comment("human", 100, "Fixed in Lean 4.36.")])
        self.f.issue(CORE, 3, "core", ["blocked"], created=5000, body="Blocked-by: org/inkproof-coordination#9")
        d.evaluate(CORE, 3)
        self.assertEqual(self.descriptions("core"),
                         [f"human commented on upstream issue {COORD}#9; {CORE}#3 is blocked on it"])

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

    def test_15_consumer_metrics_13_fixed_with_the_label_left_on(self):
        self.f.issue(CORE, 13, "coordinator", ["request", "ready-for-release"],
                     events=[label("ready-for-release", True, 4000), comment("coordinator", 3900, "Refused: ...")])
        self.f.mains[CORE] = {"id": "n" * 40, "timestamp": at(2000)}
        d.evaluate(CORE, 13)
        self.assertEqual([x for x in self.descriptions("coordinator") if "ready-for-release" in x],
                         [f"{CORE}#13 is still labelled ready-for-release, and main has changed or its CI has finished "
                          "since you last looked at it"])

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
                          "finished since you last looked at it"])

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
                         [f"ready-for-release added to issue {CORE}#13"])

    def test_16_needs_human_added_but_not_once_refused(self):
        self.f.issue(CORE, 4, "core", ["needs-human"], events=[label("needs-human", True, 100)])
        self.f.issue(CORE, 5, "core", [], events=[label("needs-human", True, 100),
                                                  label("needs-human", False, 90, "dispatcher")])
        d.evaluate(CORE, 4)
        d.evaluate(CORE, 5)
        self.assertEqual([x for x in self.descriptions("coordinator") if "needs-human" in x],
                         [f"needs-human added to issue {CORE}#4"])

    def test_17_a_milestone_with_nothing_left_open(self):
        self.f.milestones[CORE] = [{"id": 3, "title": "Float library", "open_issues": 0, "closed_issues": 4},
                                   {"id": 4, "title": "Next", "open_issues": 0, "closed_issues": 0}]
        d.evaluate_repo(CORE)
        self.assertEqual(self.descriptions("coordinator"),
                         [f"the last open issue in milestone Float library on {CORE} was closed"])


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

    def test_21_and_22_milestone_reviews_and_blocking_lasm_issues(self):
        self.f.issue(COORD, 30, "coordinator", ["milestone-review"])
        self.f.issue(COORD, 31, "core", ["upstream", "upstream:lasm", "blocking"])
        d.evaluate(COORD, 30)
        d.evaluate(COORD, 31)
        self.assertEqual(self.queued("architecture"), ["milestone"])
        self.assertEqual(self.queued("lasm"), ["milestone", "lasm-blocking"])


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
        n, found = self.spell()
        self.assertEqual(found, sorted([("core", f"{CORE}#1"), ("coordinator", f"{CORE}#5"),
                                        ("conformance", f"{CORE}#7"), ("core", f"{CORE}#10"),
                                        ("core", f"{CORE}#11"), ("scale", f"{CORE}#13")]))
        self.assertEqual(n, 6)

    def test_twice_without_moving_then_the_humans_and_a_fix_starts_again(self):
        self.f.issue(CORE, 1, "scale", ["request", "accepted"])
        self.assertEqual(self.spell(), (1, [("core", f"{CORE}#1")]))
        self.assertEqual(self.spell(), (1, [("core", f"{CORE}#1")]))
        self.assertEqual(self.spell(), (0, []))
        self.assertEqual([r["kind"] for r in d.q("SELECT kind FROM notices")], ["stalled"])
        # Once it waits visibly, a later stall starts from nothing.
        self.f.items[(CORE, 1)]["labels"].append({"name": "needs-human"})
        self.assertEqual(self.spell(), (0, []))
        self.f.items[(CORE, 1)]["labels"].pop()
        self.assertEqual(self.spell(), (1, [("core", f"{CORE}#1")]))

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
