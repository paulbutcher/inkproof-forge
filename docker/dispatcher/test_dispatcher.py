"""Unit tests for the dispatcher's decisions, with Forgejo stubbed out.
Run by scripts/test-dispatcher.sh, in the dispatcher's image."""
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
