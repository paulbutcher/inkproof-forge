"""The completeness checks for states.py (proposals/explicit-state.md):
every combination of every unit's facts, never a sample.

1. Every combination has a state, and every state that isn't finished has
   holders, from the roles the dispatcher resolves; only the states listed
   in FINISHED here are finished, so a new one is a decision, not an accident.
2. Every holder's step moves the unit to another state (or other holders),
   so a holder that does its step is never woken for the same thing again.
3. From every combination, a finished state can be reached by holders doing
   their steps and the world doing what it does (CI passes or fails, a
   blocker closes, a review approves or asks for changes).

STEPS is the model the third check explores: for each state, what its
holders (and the world) may do next, as changes to the facts."""
import importlib.machinery
import importlib.util
import os
import unittest
from collections import defaultdict
from dataclasses import replace

import states as s
from states import IssueFacts, KitFacts, MilestoneFacts, PullFacts, ReleaseFacts

FINISHED = {
    IssueFacts: {"closed"},
    PullFacts: {"closed"},
    ReleaseFacts: {"up to date"},
    MilestoneFacts: {"closed", "empty"},
    KitFacts: {"finished", "not started"},
}


def r(f, **kw):
    return replace(f, **kw)


HELD = {"with the human", "upstream", "waiting", "invalid", "blocked", "blocked without a reason"}


def hold_steps(f, n):
    """What frees an issue or pull request from what holds it."""
    if n == "upstream":
        return [r(f, open=False), r(f, hold="none")]
    if n in ("invalid", "blocked without a reason"):
        return [r(f, hold="none"), r(f, hold="blocked")]
    return [r(f, hold="none")]


def issue_steps(f, st):
    n = st.name
    if n == "in review":
        return [r(f, unreported=False)]
    if n == "closed unreleased":
        return [r(f, open=True), r(f, rfr=False)]
    if n == "overtaken":
        return [r(f, open=False), r(f, overtaken=False, kind="task")]
    if n == "with the human":
        if f.kind == "release-decision":
            return [r(f, overtaken=True), r(f, open=False)]
        if f.kind in ("recommendation", "kit-decision"):
            return [r(f, open=False)]
    if n in HELD:
        return hold_steps(f, n)
    if n == "reviewed":
        return [r(f, open=False), r(f, hold="blocked"), r(f, hold="human"), r(f, hold="waiting")]
    if n == "untriaged":
        out = [r(f, untriaged=False), r(f, open=False)]
        if f.kind == "request":
            out.append(r(f, untriaged=False, counter=True))
        return out
    if n == "counter-proposed":
        return [r(f, counter=False, untriaged=True), r(f, hold="human"), r(f, open=False)]
    if n == "verified":
        return [r(f, open=False)]
    if n == "in progress":
        return [r(f, open_pr=False, merged=True), r(f, open_pr=False)]
    if n == "ready":
        return [r(f, rfr=False, released=True, merged=False, not_verified=False)]
    if n == "released":
        if f.kind == "request":
            return [r(f, verified=True), r(f, not_verified=True), r(f, hold="blocked")]
        return [r(f, open=False)]
    if n == "merged":
        return [r(f, rfr=True)]
    if n == "to do":
        return [r(f, open_pr=True), r(f, hold="blocked"), r(f, hold="waiting"), r(f, hold="human"),
                r(f, open=False)]
    raise AssertionError(f"no steps for {n}")


def pull_steps(f, st):
    n = st.name
    pushed = dict(ci="running" if f.ci != "unused" else "unused", ci_late=False, conformance="settling",
                  changes="older" if f.changes != "none" else "none", approved=False, stuck=False)
    if n in HELD:
        return hold_steps(f, n)
    if n == "in draft":
        return [r(f, wip=False), r(f, open=False)]
    if n in ("CI failing", "no CI", "CI stuck", "conflicting"):
        return [r(f, mergeable="yes", **pushed), r(f, open=False)]
    if n == "CI running":
        return [r(f, ci="success", ci_late=False), r(f, ci="failure", ci_late=False), r(f, ci_late=True)]
    if n == "changes requested":
        return [r(f, mergeable="yes", **pushed), r(f, changes="replied")]
    if n == "author replied":
        return [r(f, changes="none"), r(f, changes="at-head")]
    if n == "settling":
        return [r(f, conformance="due")]
    if n == "in review":
        # Any one of its reviewers approves or asks for changes; a settling
        # head settles.
        out = []
        if f.conformance == "due":
            out += [r(f, conformance="reviewed", approved=True), r(f, conformance="reviewed", changes="at-head")]
        if f.conformance == "settling":
            out.append(r(f, conformance="due"))
        if f.changes == "older":
            out += [r(f, changes="none", approved=True), r(f, changes="at-head")]
        for k in f.pending:
            out += [r(f, pending=f.pending - {k}, approved=True), r(f, changes="at-head")]
        return out
    if n == "unapproved":
        return [r(f, pending=frozenset({"agent"}))]
    if n == "checking":
        return [r(f, mergeable="yes"), r(f, mergeable="no")]
    if n == "unscheduled":
        return [r(f, scheduled=True), r(f, open=False)]
    if n == "stuck":
        return [r(f, open=False)]
    if n == "merging":
        return [r(f, open=False), r(f, stuck=True)]
    raise AssertionError(f"no steps for {n}")


def release_steps(f, st):
    n = st.name
    released = r(f, rfr=False, heading="none", main_ci="success", main_ci_late=False)
    if n == "main failing":
        return [r(f, main_ci="running", main_ci_late=False)]
    if n == "held":
        return [r(f, held=False)]
    if n == "main CI stuck":
        return [r(f, main_ci="running", main_ci_late=False)]
    if n == "main CI running":
        return [r(f, main_ci="success", main_ci_late=False), r(f, main_ci="failure", main_ci_late=False),
                r(f, main_ci_late=True)]
    if n in ("no entry", "wrong entry"):
        return [r(f, heading="right", main_ci="running", main_ci_late=False)]
    if n == "releasable":
        return [released] if f.level3 else [r(f, decision=True)]
    if n == "with the human":
        return [released]
    raise AssertionError(f"no steps for {n}")


def milestone_steps(f, st):
    n = st.name
    if n == "in progress":
        return [r(f, open_work=False, had_work=True, issue="closed" if f.issue == "open" else f.issue)]
    if n == "finished":
        return [r(f, issue="open", open_work=True), r(f, closed=True)]
    if n == "accepted":
        return [r(f, closed=True)]
    raise AssertionError(f"no steps for {n}")


def kit_steps(f, st):
    n = st.name
    if n == "in progress":
        return [r(f, milestones="done")]
    if n == "planning":
        return [r(f, planning=False), r(f, planning=False, finished=True)]
    if n == "with the human":
        return [r(f, decision=False)]
    if n == "what next":
        return [r(f, milestones="open-work"), r(f, decision=True), r(f, planning=True)]
    raise AssertionError(f"no steps for {n}")


STEPS = {IssueFacts: issue_steps, PullFacts: pull_steps, ReleaseFacts: release_steps,
         MilestoneFacts: milestone_steps, KitFacts: kit_steps}


SPACES = {}


class Complete(unittest.TestCase):
    def each(self):
        """Each kind of unit with its space of facts, cached: the space is
        the same for every check."""
        if not SPACES:
            SPACES.update((kind, list(kind.every())) for kind in STEPS)
        return SPACES.items()

    def test_every_combination_has_a_state_and_holders(self):
        for kind, space in self.each():
            names = set()
            for f in space:
                st = s.state(f)
                names.add(st.name)
                self.assertTrue(set(st.holders) <= s.ROLES, f"{f}: {st}")
                self.assertEqual(st.terminal, st.name in FINISHED[kind],
                                 f"{f} is {st.name!r}, with holders {st.holders}")
                if set(st.holders) & s.AGENT_ROLES:
                    self.assertTrue(st.step, f"{f}: {st.name} says nothing to its holders")
            self.assertTrue(FINISHED[kind] <= names, f"finished states never reached: {FINISHED[kind] - names}")

    def test_every_step_moves_it_on(self):
        for kind, space in self.each():
            inside = set(space)
            for f in space:
                st = s.state(f)
                if st.terminal:
                    continue
                for g in STEPS[kind](f, st):
                    self.assertIn(g, inside, f"{st.name}: a step leaves the facts' space: {g}")
                    after = s.state(g)
                    self.assertNotEqual((after.name, after.holders), (st.name, st.holders),
                                        f"{f}: {st.name}'s step leaves it as it was")

    def test_every_combination_can_finish(self):
        for kind, space in self.each():
            back = defaultdict(list)
            done = []
            for f in space:
                st = s.state(f)
                if st.terminal:
                    done.append(f)
                    continue
                for g in STEPS[kind](f, st):
                    back[g].append(f)
            reach, todo = set(done), list(done)
            while todo:
                g = todo.pop()
                for f in back[g]:
                    if f not in reach:
                        reach.add(f)
                        todo.append(f)
            stuck = [f for f in space if f not in reach]
            if stuck:
                self.fail(f"{len(stuck)} can never finish, such as {stuck[0]} ({s.state(stuck[0]).name})")


class Versions(unittest.TestCase):
    def test_the_same_rule_as_the_gatekeeper(self):
        """The gatekeeper refuses a release whose CHANGELOG.md heading isn't
        its version, so the states must tell the same version (demos#105)."""
        path = os.environ.get("GATEKEEPER_PY", os.path.join(os.path.dirname(__file__), "..", "gatekeeper",
                                                            "gatekeeper.py"))
        os.environ.setdefault("FORGE_ORG", "org")
        loader = importlib.machinery.SourceFileLoader("gatekeeper", path)
        gatekeeper = importlib.util.module_from_spec(importlib.util.spec_from_loader("gatekeeper", loader))
        loader.exec_module(gatekeeper)
        A, B2, C = "def a : Nat\n", "def b : Nat\n", "def c : Nat\n"
        for prev in (None, (0, 20, 0), (0, 3, 7), (1, 0, 0), (2, 4, 1)):
            for old, new in ((A + B2, A + B2), (A + B2, A + B2 + C), (A + B2, A), (A + B2, A + C), (A, " " + A),
                             (A + B2, B2 + A + "\n")):
                self.assertEqual(s.next_version(prev, old, new), gatekeeper.next_version(prev, old, new)[0],
                                 (prev, old, new))

    def test_the_gatekeepers_rule(self):
        A, B2 = "a\nb\n", "a\nb\nc\n"
        self.assertEqual(s.next_version(None, None, A), (0, 1, 0))
        self.assertEqual(s.next_version((0, 20, 0), A, A), (0, 20, 1))
        self.assertEqual(s.next_version((0, 20, 3), A, B2), (0, 21, 0))
        self.assertEqual(s.next_version((1, 2, 3), A, B2), (1, 3, 0))
        self.assertEqual(s.next_version((1, 2, 3), B2, A), (2, 0, 0))
        self.assertEqual(s.next_version((1, 2, 3), A, A), (1, 2, 4))


if __name__ == "__main__":
    unittest.main()
