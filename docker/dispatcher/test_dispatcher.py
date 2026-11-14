"""Unit tests for the dispatcher's decisions, with Forgejo stubbed out.
Run by scripts/test-dispatcher.sh, in the dispatcher's image."""
import os
import tempfile
import unittest
from datetime import datetime, timezone

STATE = tempfile.mkdtemp()
CONF = os.path.join(STATE, "agents.conf")
with open(CONF, "w") as f:
    f.write("coordinator coordinator inkproof-coordination *\n"
            "core library inkproof-core inkproof-coordination\n"
            "scale library inkproof-scale inkproof-coordination,inkproof-core\n"
            "conformance pr-reviewer - *\n")
os.environ.update(STATE_DIR=STATE, AGENTS_CONF=CONF, CONTROL_DIR=STATE, FORGE_ORG="org",
                  HUMAN_USER="human", CONFORMANCE_SETTLE_SECONDS="600")

import dispatcher as d  # noqa: E402


class Forge:
    """Pull request 7 on inkproof-core, by `author`, at head `sha`."""

    def __init__(self, author, sha="a" * 40):
        self.author, self.sha = author, sha

    def __call__(self, method, path, body=None):
        if path.endswith("/pulls/7"):
            return {"state": "open", "title": "Add a thing", "user": {"login": self.author},
                    "head": {"sha": self.sha}}
        if path.endswith("/pulls/7/reviews?limit=50"):
            return []
        raise AssertionError(f"unexpected call {method} {path}")


class ConformanceSettles(unittest.TestCase):
    def setUp(self):
        d.q("DELETE FROM triggers")
        d.q("DELETE FROM runs")
        d.q("DELETE FROM kv")

    def review(self):
        rows = d.q("SELECT not_before FROM triggers WHERE agent='conformance' AND run_id IS NULL")
        self.assertEqual(len(rows), 1)
        return rows[0]["not_before"]

    def run_core(self):
        return d.q("INSERT INTO runs (agent, triggers, claimed) VALUES ('core', '[]', ?) RETURNING id",
                   d.now())[0]["id"]

    def test_idle_agent_author_is_reviewed_now(self):
        d.forge = Forge("core")
        d.queue_conformance("inkproof-core", 7)
        self.assertIsNone(self.review())

    def test_running_author_waits_until_its_run_ends(self):
        d.forge = Forge("core")
        run = self.run_core()
        d.queue_conformance("inkproof-core", 7)
        self.assertGreater(self.review(), d.now() + 500)
        d.end_run("core", run, {"outcome": "success"})
        self.assertIsNone(self.review())

    def test_run_that_exited_early_keeps_the_wait(self):
        d.forge = Forge("core")
        run = self.run_core()
        d.queue_conformance("inkproof-core", 7)
        d.end_run("core", run, {"outcome": "exited 1"})
        self.assertGreater(self.review(), d.now() + 500)

    def test_human_in_the_container_counts_as_busy(self):
        d.forge = Forge("core")
        d.kv_set("human:core", d.now())
        d.queue_conformance("inkproof-core", 7)
        self.assertGreater(self.review(), d.now() + 500)

    def test_human_author_waits_the_settling_period(self):
        d.forge = Forge("human")
        d.queue_conformance("inkproof-core", 7)
        self.assertGreater(self.review(), d.now() + 500)

    def test_another_agents_run_ending_releases_nothing(self):
        d.forge = Forge("coordinator")
        d.q("INSERT INTO runs (agent, triggers, claimed) VALUES ('coordinator', '[]', ?)", d.now())
        d.queue_conformance("inkproof-core", 7)
        d.end_run("core", self.run_core(), {"outcome": "success"})
        self.assertGreater(self.review(), d.now() + 500)

    def test_requeueing_once_settled_clears_the_wait(self):
        d.forge = Forge("core")
        d.kv_set("human:core", d.now())
        d.queue_conformance("inkproof-core", 7)
        d.kv_set("human:core", 0)
        d.queue_conformance("inkproof-core", 7)  # the reconciler, later
        self.assertIsNone(self.review())


class OpenedHoldEndsWithCI(unittest.TestCase):
    def setUp(self):
        d.q("DELETE FROM triggers")
        d.q("DELETE FROM runs")
        d.forge = Forge("core")
        for n in (7, 8):
            d.add_trigger("coordinator", f"pr-opened:inkproof-core#{n}", f"pull request opened: inkproof-core#{n}",
                          once=True, not_before=d.now() + 600)

    def held(self, n):
        return d.q("SELECT not_before FROM triggers WHERE agent='coordinator' AND key=?",
                   f"pr-opened:inkproof-core#{n}")[0]["not_before"]

    def ci(self, outcome):
        d.handle(f"action_run_{outcome}", {
            "repository": {"name": "inkproof-core", "owner": {"login": "org"}}, "sender": {"login": "core"},
            "run": {"event": "pull_request", "commit_sha": "a" * 40,
                    "event_payload": '{"pull_request": {"number": 7, "user": {"login": "core"}}}'}})

    def test_ci_passing_ends_the_hold_on_that_pull_request_only(self):
        self.ci("success")
        self.assertIsNone(self.held(7))
        self.assertGreater(self.held(8), d.now() + 500)

    def test_ci_failing_ends_the_hold_too(self):
        self.ci("failure")
        self.assertIsNone(self.held(7))
        self.assertGreater(self.held(8), d.now() + 500)


class ReleaseWakesTheRequester(unittest.TestCase):
    def setUp(self):
        d.q("DELETE FROM triggers")

    def release(self, requester, by):
        d.handle("issue_comment", {
            "action": "created", "repository": {"name": "inkproof-core", "owner": {"login": "org"}},
            "sender": {"login": by},
            "issue": {"number": 9, "state": "open", "user": {"login": requester},
                      "labels": [{"name": "request"}]},
            "comment": {"id": 1, "body": "(no reply needed)\n\nReleased in v1.2.3: http://forgejo/rel"}})
        return [r["agent"] for r in d.q("SELECT agent FROM triggers WHERE key LIKE 'released:%'")]

    def test_another_agents_request_wakes_it_to_verify(self):
        self.assertEqual(self.release("scale", "gatekeeper"), ["scale"])

    def test_the_coordinators_own_release_wakes_no_one(self):
        self.assertEqual(self.release("coordinator", "gatekeeper"), [])

    def test_the_humans_release_of_a_coordinator_request_wakes_it(self):
        self.assertEqual(self.release("coordinator", "human"), ["coordinator"])


class ClosingWakesWhatItBlocks(unittest.TestCase):
    """inkproof-core#3 is blocked on whatever its Blocked-by: line names."""

    def setUp(self):
        d.q("DELETE FROM triggers")
        d.q("DELETE FROM labels")
        self.blocker = "org/inkproof-coordination#70"

        def forge(method, path, body=None):
            if path.startswith("/repos/org/inkproof-core/issues?") and "labels=blocked" in path:
                return [{"number": 3, "body": f"Blocked-by: {self.blocker}", "user": {"login": "coordinator"}}]
            if "/issues?" in path:
                return []
            if path.startswith("/repos/org/inkproof-core/issues/3/comments"):
                return []
            raise AssertionError(f"unexpected call {method} {path}")
        d.forge = forge

    def close(self, repo, n, labels=()):
        d.handle("issues", {
            "action": "closed", "repository": {"name": repo, "owner": {"login": "org"}},
            "sender": {"login": "coordinator"},
            "issue": {"number": n, "state": "closed", "user": {"login": "float"},
                      "labels": [{"name": l} for l in labels]}})
        return [r["description"] for r in d.q("SELECT description FROM triggers WHERE agent='core'")]

    def test_a_closed_request_wakes_the_issue_it_blocks(self):
        self.assertEqual(self.close("inkproof-coordination", 70, ["request"]),
                         ["issue inkproof-coordination#70 was closed; inkproof-core#3 is blocked on it"])

    def test_a_closed_upstream_issue_still_does(self):
        self.assertEqual(self.close("inkproof-coordination", 70, ["upstream"]),
                         ["upstream issue inkproof-coordination#70 was closed; inkproof-core#3 is blocked on it"])

    def test_another_issue_closing_wakes_nothing(self):
        self.assertEqual(self.close("inkproof-coordination", 71, ["request"]), [])


class Stalls(unittest.TestCase):
    """Work no event will wake anyone for, found by the reconciler: from
    2026-11-14, scale#43 (approved, never scheduled to merge),
    consumer-metrics#13 (fixed after a refused release with the label left
    on) and float#5 (unblocked in a run spent on other work)."""

    def setUp(self):
        d.q("DELETE FROM triggers")
        d.q("DELETE FROM runs")
        d.q("DELETE FROM kv")
        self.pulls, self.issues, self.timelines = [], [], {}
        self.main = {"id": "m" * 40, "timestamp": self.at(3600)}

        def forge(method, path, body=None):
            path = path.split("&limit=")[0].split("?limit=")[0]
            if path == "/repos/org/inkproof-core/pulls?state=open":
                return self.pulls
            if path.startswith("/repos/org/inkproof-core/issues?"):
                want = path.split("labels=")[1]
                return [i for i in self.issues if want in {l["name"] for l in i["labels"]}]
            if path.startswith("/repos/org/inkproof-core/issues/") and path.endswith("/timeline"):
                return self.timelines.get(int(path.split("/")[-2]), [])
            if path == "/repos/org/inkproof-core/branches/main":
                return {"commit": self.main}
            raise AssertionError(f"unexpected call {method} {path}")
        d.forge = forge

    @staticmethod
    def at(seconds_ago):
        return datetime.fromtimestamp(d.now() - seconds_ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def label(self, name, added, seconds_ago, by="core"):
        return {"type": "label", "label": {"name": name}, "body": "1" if added else "",
                "created_at": self.at(seconds_ago), "user": {"login": by}}

    def comment(self, by, seconds_ago, body="..."):
        return {"type": "comment", "user": {"login": by}, "body": body, "created_at": self.at(seconds_ago)}

    def issue(self, n, *labels, updated=3600):
        self.issues.append({"number": n, "labels": [{"name": l} for l in labels], "user": {"login": "coordinator"},
                            "created_at": self.at(7200), "updated_at": self.at(updated)})

    def woken(self, agent):
        return [r["key"].split(":")[0] for r in d.q("SELECT key FROM triggers WHERE agent=?", agent)]

    # Pull requests

    def pr(self, n=7, updated=3600, title="Use float", body=""):
        self.pulls.append({"number": n, "title": title, "body": body, "user": {"login": "core"},
                           "head": {"sha": "a" * 40}, "updated_at": self.at(updated)})

    def test_an_approved_pull_request_never_scheduled_wakes_its_owner(self):
        self.pr()
        self.timelines[7] = [{"type": "review", "created_at": self.at(3600)}]
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("core"), ["unscheduled"])
        d.q("UPDATE triggers SET run_id=1")
        d.check_stalls("inkproof-core", "core")  # once per head
        self.assertEqual(len(self.woken("core")), 1)

    def test_a_scheduled_merge_wakes_no_one(self):
        self.pr()
        self.timelines[7] = [{"type": "pull_scheduled_merge", "created_at": self.at(3600)}]
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("core"), [])

    def test_a_cancelled_schedule_does(self):
        self.pr()
        self.timelines[7] = [{"type": "pull_scheduled_merge", "created_at": self.at(3600)},
                             {"type": "pull_cancel_scheduled_merge", "created_at": self.at(3000)}]
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("core"), ["unscheduled"])

    def test_a_pull_request_just_opened_or_wip_waits(self):
        self.pr(7, updated=30)
        self.pr(8, title="WIP: later")
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("core"), [])

    def test_a_busy_owner_is_left_alone(self):
        self.pr()
        d.q("INSERT INTO runs (agent, triggers, claimed) VALUES ('core', '[]', ?)", d.now())
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("core"), [])

    def test_nothing_while_paused(self):
        self.pr()
        open(os.path.join(STATE, "paused"), "w").close()
        try:
            d.check_stalls("inkproof-core", "core")
        finally:
            os.remove(os.path.join(STATE, "paused"))
        self.assertEqual(self.woken("core"), [])

    # ready-for-release

    def test_a_fix_after_a_refused_release_wakes_the_coordinator(self):
        self.issue(13, "request", "accepted", "ready-for-release")
        self.timelines[13] = [self.label("ready-for-release", True, 4000), self.comment("coordinator", 3900)]
        self.main["timestamp"] = self.at(2000)
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("coordinator"), ["rfr-again"])

    def test_one_the_coordinator_has_looked_at_since_wakes_no_one(self):
        self.issue(13, "request", "accepted", "ready-for-release")
        self.timelines[13] = [self.label("ready-for-release", True, 4000), self.comment("coordinator", 1000)]
        self.main["timestamp"] = self.at(2000)
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("coordinator"), [])

    def test_a_label_the_coordinator_never_saw_wakes_it(self):
        self.issue(13, "request", "accepted", "ready-for-release")
        self.timelines[13] = [self.label("ready-for-release", True, 4000)]
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("coordinator"), ["rfr-again"])

    def test_a_label_just_added_waits_for_its_own_wake(self):
        self.issue(13, "request", "accepted", "ready-for-release")
        self.timelines[13] = [self.label("ready-for-release", True, 30)]
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("coordinator"), [])

    # Accepted requests

    def test_an_unblocked_request_left_alone_wakes_its_owner(self):
        self.issue(5, "request", "accepted")
        self.timelines[5] = [self.label("accepted", True, 7000), self.label("blocked", True, 6000),
                             self.label("blocked", False, 3000)]
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("core"), ["idle-request"])
        d.q("UPDATE triggers SET run_id=1")
        d.check_stalls("inkproof-core", "core")  # once per unblocking
        self.assertEqual(len(self.woken("core")), 1)

    def test_requests_held_up_or_under_way_wake_no_one(self):
        self.issue(1, "request", "accepted", "blocked")
        self.issue(2, "request", "accepted", "needs-human")
        self.issue(3, "request", "accepted")
        self.pr(9, body="Refs org/inkproof-core#3")
        self.timelines[9] = [{"type": "pull_scheduled_merge", "created_at": self.at(3600)}]
        self.issue(4, "request", "accepted")
        self.timelines[4] = [self.comment("gatekeeper", 3000, "(no reply needed)\n\nReleased in v0.2.0: ...")]
        self.issue(6, "request", "accepted", updated=30)
        d.check_stalls("inkproof-core", "core")
        self.assertEqual(self.woken("core"), [])


if __name__ == "__main__":
    unittest.main()
