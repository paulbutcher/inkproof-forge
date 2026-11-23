"""The kit's units of work and their states (proposals/explicit-state.md).

Each unit (an issue, a pull request, a repository's release, a milestone,
the kit itself) has a state, derived here from a snapshot of facts by a
pure function: no Forgejo, no clock, no ledger. Each state that isn't
finished has holders, the roles that owe its next step, which the
dispatcher resolves to agents, the human, the world outside, a timer or
other units. test_states checks every combination of facts and every path
through them, rather than samples.

The facts are small enums and booleans, each read by the dispatcher (the
I/O layer), which reduces timestamps and prose to them first.
"""

from dataclasses import dataclass, fields
from itertools import product

# --- Holders ------------------------------------------------------------------------
#
# Roles, which the dispatcher resolves for each unit. A role that names no
# agent falls back as `resolve` there says: an outsider's part to the owner,
# a missing coordinator's to the human.

OWNER = "owner"                # the repository's owner
REQUESTER = "requester"        # whoever filed the request (agent, human or outsider)
AUTHOR = "author"              # a pull request's author
COORDINATOR = "coordinator"
CONFORMANCE = "conformance"    # the pull request reviewer
REVIEWERS = "reviewers"        # milestone reviewers that haven't reported
WAITED = "waited"              # the agent a `Waiting-on:` line names
WRITER = "writer"              # whoever wrote an invalid line
CHANGES_BY = "changes-by"      # whoever requested changes on a pull request
PENDING = "pending"            # agents whose review is requested and not given
HUMAN = "human"
WORLD = "world"                # outside the kit: an upstream project
TIMER = "timer"                # Forgejo or CI, with a deadline the facts carry
# Other units: whatever they wait on has holders of its own.
BLOCKERS = "unit:blockers"     # the issues its Blocked-by: lines name
PR = "unit:pr"                 # the open pull requests that refer to it
RELEASE = "unit:release"       # its repository's release
HELD = "unit:held"             # the ready-for-release issues that hold a release
ISSUES = "unit:issues"         # a milestone's open issues
MILESTONES = "unit:milestones"  # the milestones with open work
PLANNING = "unit:planning"     # the open pull requests on the coordination repository

AGENT_ROLES = {OWNER, REQUESTER, AUTHOR, COORDINATOR, CONFORMANCE, REVIEWERS, WAITED, WRITER, CHANGES_BY, PENDING}
ROLES = AGENT_ROLES | {HUMAN, WORLD, TIMER, BLOCKERS, PR, RELEASE, HELD, ISSUES, MILESTONES, PLANNING}


@dataclass(frozen=True)
class State:
    """A unit's state: its name, who holds it, and what they do next. No
    holders means it's finished: nothing is owed until its facts change.
    Quiet: its agent holders are woken only when they have nothing else."""
    name: str
    holders: tuple = ()
    step: str = ""
    quiet: bool = False

    @property
    def terminal(self):
        return not self.holders


# --- Facts --------------------------------------------------------------------------

class Facts:
    """A snapshot of one unit's facts. DOMAIN gives each field's values, for
    the exhaustive check."""
    DOMAIN = {}

    @classmethod
    def every(cls):
        """Every combination of the fields' values."""
        names = [f.name for f in fields(cls)]
        for values in product(*(cls.DOMAIN[n] for n in names)):
            yield cls(**dict(zip(names, values)))


B = (False, True)
# What holds an issue or pull request whatever its kind, the first that
# applies: needs-human the human hasn't answered, or assigned to them by hand;
# an upstream issue; an unanswered `Waiting-on: <agent>`; a Blocked-by: line
# naming the item itself (while it is the newest) or closing a loop of items
# each blocked by the next; a Blocked-by: line naming an open, unreleased
# item; the `blocked` label with no such line.
HOLDS = ("none", "human", "upstream", "waiting", "invalid", "blocked", "bare-label")


@dataclass(frozen=True)
class IssueFacts(Facts):
    open: bool = True
    # request: filed by someone other than the owner, labelled `request`;
    # task: any other issue the owner should finish (its own, the human's,
    # another agent's unlabelled one); milestone: a milestone issue;
    # recommendation, kit-decision (the dispatcher's own), release-decision
    # (the coordinator's `Release <repo>`): the human's to close.
    kind: str = "task"
    untriaged: bool = False        # request: `new`, or no triage label; task: someone else's, owner never touched
    counter: bool = False          # `counter-proposal`
    hold: str = "none"             # what holds it, whatever its kind (HOLDS)
    open_pr: bool = False          # an open pull request refers to it
    merged: bool = False           # work for it merged since it was last unblocked, accepted or released
    rfr: bool = False              # `ready-for-release`
    released: bool = False         # a `Released in vX.Y.Z` comment
    verified: bool = False
    not_verified: bool = False     # `not-verified` added since its latest release
    overtaken: bool = False        # release-decision: a release published since it last acted
    unreported: bool = False       # milestone: a review requested and not reported since

    DOMAIN = dict(open=B, kind=("request", "task", "milestone", "recommendation", "kit-decision", "release-decision"),
                  untriaged=B, counter=B, hold=HOLDS, open_pr=B, merged=B,
                  rfr=B, released=B, verified=B, not_verified=B, overtaken=B, unreported=B)

    @classmethod
    def every(cls):
        """Fields only one kind reads are left at their default for the others,
        and not-verified only follows a release."""
        for f in super().every():
            if f.kind != "request" and (f.counter or f.verified or f.not_verified):
                continue
            if f.kind != "release-decision" and f.overtaken:
                continue
            if f.kind != "milestone" and f.unreported:
                continue
            if f.not_verified and not f.released:
                continue
            yield f


@dataclass(frozen=True)
class PullFacts(Facts):
    open: bool = True
    hold: str = "none"             # as for an issue (HOLDS); never upstream
    wip: bool = False
    ci: str = "success"            # none, running, failure, success, or unused (a repository without CI)
    ci_late: bool = False          # none or running for longer than it should be
    mergeable: str = "yes"         # yes, no, or unknown (Forgejo is still checking)
    changes: str = "none"          # a standing request for changes: none, at-head, replied (by the author since), older
    conformance: str = "reviewed"  # its review of the head: reviewed (or no reviewer), settling, due
    pending: frozenset = frozenset()  # reviews requested at the head, not given: coordinator, agent, human
    approved: bool = True          # someone approved the head
    scheduled: bool = False        # a merge is scheduled
    stuck: bool = False            # scheduled and ready for MERGE_STUCK_SECONDS without merging

    DOMAIN = dict(open=B, hold=tuple(h for h in HOLDS if h != "upstream"), wip=B,
                  ci=("none", "running", "failure", "success", "unused"), ci_late=B,
                  mergeable=("yes", "no", "unknown"), changes=("none", "at-head", "replied", "older"),
                  conformance=("reviewed", "settling", "due"),
                  pending=tuple(frozenset(s) for s in ((), ("coordinator",), ("agent",), ("human",),
                                                       ("coordinator", "human"))),
                  approved=B, scheduled=B, stuck=B)

    @classmethod
    def every(cls):
        for f in super().every():
            if f.ci_late and f.ci not in ("none", "running"):
                continue
            if f.stuck and not f.scheduled:
                continue
            yield f


@dataclass(frozen=True)
class ReleaseFacts(Facts):
    main_ci: str = "success"       # on main's head: success, failure, running, none
    main_ci_late: bool = False     # none or running for longer than it should be
    rfr: bool = False              # an open issue labelled ready-for-release
    heading: str = "none"          # CHANGELOG.md's newest heading on main: none (not above the last tag), wrong, right
    held: bool = False             # a ready-for-release issue is itself blocked, waiting or with the human
    level3: bool = False           # the coordinator releases it; below, the human does
    decision: bool = False         # an open `Release <repo>` decision

    DOMAIN = dict(main_ci=("success", "failure", "running", "none"), main_ci_late=B, rfr=B,
                  heading=("none", "wrong", "right"), held=B, level3=B, decision=B)


@dataclass(frozen=True)
class MilestoneFacts(Facts):
    """A milestone and its namesakes on every repository."""
    closed: bool = False           # closed on every repository
    open_work: bool = False        # an open issue on any of them (its milestone issue included)
    had_work: bool = False         # a closed issue on any of them
    issue: str = "none"            # its milestone issue: none, open, closed

    DOMAIN = dict(closed=B, open_work=B, had_work=B, issue=("none", "open", "closed"))


@dataclass(frozen=True)
class KitFacts(Facts):
    milestones: str = "none-ever"  # none-ever (no milestone had work), open-work (one has), done
    finished: bool = False         # ROADMAP.md on the coordination repository says `Status: finished`
    planning: bool = False         # an open pull request on the coordination repository
    decision: bool = False         # an open decision for the human the coordinator filed there

    DOMAIN = dict(milestones=("none-ever", "open-work", "done"), finished=B, planning=B, decision=B)


# --- States -------------------------------------------------------------------------

S = State


def held(hold):
    """The state of an issue or pull request something holds (HOLDS)."""
    return {
        "human": S("with the human", (HUMAN,), "decide"),
        "upstream": S("upstream", (WORLD,)),
        "waiting": S("waiting", (WAITED,), "answer, or do what it waits for"),
        "invalid": S("invalid", (WRITER,), "a Blocked-by: line names this item itself, or closes a loop of items "
                                           "each blocked by the next: fix it"),
        "blocked": S("blocked", (BLOCKERS,)),
        "bare-label": S("blocked without a reason", (OWNER,), "it is labelled blocked, but no Blocked-by: line "
                                                              "names what it waits for: add one, or take the "
                                                              "label off"),
    }[hold]


def issue_state(f):
    if f.kind == "milestone" and f.unreported:
        # Reviews run whether it's open or closed: the coordinator may accept
        # a milestone before they report (21).
        return S("in review", (REVIEWERS,), "review the milestone and report on its issue")
    if not f.open:
        if f.rfr and not f.released and f.kind in ("request", "task"):
            return S("closed unreleased", (COORDINATOR,),
                     "it was closed while labelled ready-for-release and never released: reopen it and release "
                     "it, or, if its work truly isn't wanted, take the label off and say why on it")
        return S("closed")
    if f.kind == "release-decision":
        if f.overtaken:
            return S("overtaken", (COORDINATOR,), "a release overtook it: close it if that release covers it, or "
                                                  "update it for what's left")
        return S("with the human", (HUMAN,), "release it with scripts/release.sh")
    if f.kind in ("recommendation", "kit-decision"):
        return S("with the human", (HUMAN,), "decide, and close it")
    if f.hold != "none":
        return held(f.hold)
    if f.kind == "milestone":
        return S("reviewed", (COORDINATOR,), "accept the milestone (say what you checked, close the issue and "
                                             "its milestones, record it in ROADMAP.md), or say on it what it "
                                             "waits for")
    request = f.kind == "request"
    if f.untriaged:
        return S("untriaged", (OWNER,), "accept it, counter-propose, or close it saying why" if request else
                 "if it's a request, add `request` and `new` and handle it as one; otherwise take it on, answer "
                 "it or close it")
    if request and f.counter:
        return S("counter-proposed", (REQUESTER,), "answer the counter-proposal: take `counter-proposal` off "
                                                   "if you accept it, or put it to the human")
    if request and f.verified:
        return S("verified", (REQUESTER,), "close it")
    if f.open_pr:
        return S("in progress", (PR,))
    if f.rfr:
        return S("ready", (RELEASE,))
    if f.released and not f.not_verified:
        if request:
            return S("released", (REQUESTER,), "verify it against the release (verified, and close it), or add "
                                               "`not-verified` saying what falls short, or a Blocked-by: line "
                                               "naming what you must do first")
        return S("released", (OWNER,), "its release is out: close it")
    if f.merged:
        return S("merged", (OWNER,), "label it ready-for-release, with its CHANGELOG.md entry")
    return S("to do", (OWNER,), "work on it: a pull request with a Refs line, or say what it waits for (a "
                                "Blocked-by: line, Waiting-on:, or needs-human with a decision)", quiet=True)


def pull_state(f):
    """What its author must fix comes first, since nothing else moves until
    they do; then the reviewer the author answered; then CI and the reviews,
    all the reviewers at once; then a merge to schedule, once nothing else
    stands in its way."""
    if not f.open:
        return S("closed")
    if f.hold != "none":
        return held(f.hold)
    if f.wip:
        return S("in draft", (AUTHOR,), "finish it and take WIP off its title, or close it", quiet=True)
    if f.mergeable == "no":
        return S("conflicting", (AUTHOR,), "it conflicts with main: merge main into it and resolve them")
    if f.ci == "failure":
        return S("CI failing", (AUTHOR,), "fix CI on its head")
    if f.ci == "none" and f.ci_late:
        return S("no CI", (AUTHOR,), "CI has never run on its head: its branch probably lacks "
                                     "`.forgejo/workflows/`. Merge `origin/main` into it and push")
    if f.ci == "running" and f.ci_late:
        return S("CI stuck", (AUTHOR,), "CI has been running on its head for over an hour: find out why")
    if f.changes == "at-head":
        return S("changes requested", (AUTHOR,), "make the changes and push, or answer the reviewer")
    if f.changes == "replied":
        return S("author replied", (CHANGES_BY,), "its author answered your request for changes: answer them")
    if f.ci in ("none", "running"):
        return S("CI running", (TIMER,))
    reviewers = ((CONFORMANCE,) if f.conformance == "due" else ()) + \
        ((CHANGES_BY,) if f.changes == "older" else ()) + \
        tuple(r for k, r in (("coordinator", COORDINATOR), ("agent", PENDING), ("human", HUMAN)) if k in f.pending)
    if reviewers:
        timer = (TIMER,) if f.conformance == "settling" else ()
        return S("in review", reviewers + timer, "review its head")
    if f.conformance == "settling":
        return S("settling", (TIMER,))
    if not f.approved:
        return S("unapproved", (AUTHOR,), "nothing has approved its head and no review is pending: request one")
    if f.mergeable == "unknown":
        return S("checking", (TIMER,))
    if not f.scheduled:
        return S("unscheduled", (AUTHOR,), "schedule its merge (`forge pr automerge`), or close it if it's no "
                                           "longer wanted", quiet=True)
    if f.stuck:
        return S("stuck", (AUTHOR,), "everything passed but its scheduled merge hasn't gone through: merge it "
                                     "now (`forge pr merge`)")
    return S("merging", (TIMER,))


def release_state(f):
    if f.main_ci == "failure":
        return S("main failing", (OWNER,), "CI failed on main: fix it before anything else")
    if not f.rfr and f.heading == "none":
        return S("up to date")
    if f.held:
        return S("held", (HELD,))
    if f.main_ci in ("running", "none"):
        if f.main_ci_late:
            return S("main CI stuck", (OWNER,), "CI on main hasn't finished: find out why")
        return S("main CI running", (TIMER,))
    if f.heading == "none":
        return S("no entry", (OWNER,), "work is ready for release, but CHANGELOG.md on main has no entry above "
                                       "the last release: add the one `forge version` gives")
    if f.heading == "wrong":
        return S("wrong entry", (OWNER,), "CHANGELOG.md's newest heading on main isn't the version the release "
                                          "will have: make it the one `forge version` gives")
    if f.level3:
        return S("releasable", (COORDINATOR,), "release it with `forge release`, naming every "
                                               "ready-for-release issue it completes")
    if f.decision:
        return S("with the human", (HUMAN,), "release it with scripts/release.sh")
    return S("releasable", (COORDINATOR,), "it is below level 3: put the release to the human as a "
                                           "`Release <repo>` decision")


def milestone_state(f):
    if f.closed:
        return S("closed")
    if f.open_work:
        return S("in progress", (ISSUES,))
    if not f.had_work:
        return S("empty")
    if f.issue == "none":
        return S("finished", (COORDINATOR,), "its last open issue closed on every repository it spans: open its "
                                             "milestone issue and add milestone-review")
    if f.issue == "closed":
        return S("accepted", (COORDINATOR,), "it is accepted: close it on every repository it spans")
    # Its milestone issue is open work of its own, so this can't be.
    return S("in progress", (ISSUES,))


def kit_state(f):
    if f.milestones == "open-work":
        return S("in progress", (MILESTONES,))
    if f.finished:
        return S("finished")
    if f.milestones == "none-ever":
        return S("not started")
    if f.planning:
        return S("planning", (PLANNING,))
    if f.decision:
        return S("with the human", (HUMAN,), "decide what the next milestone needs")
    return S("what next", (COORDINATOR,), "no milestone has open work: close out any left open, then draft the "
                                          "next one, put to the human what it needs, or record in ROADMAP.md "
                                          "that the project is finished (`Status: finished`)", quiet=True)


STATE_OF = {IssueFacts: issue_state, PullFacts: pull_state, ReleaseFacts: release_state,
            MilestoneFacts: milestone_state, KitFacts: kit_state}


def state(facts):
    return STATE_OF[type(facts)](facts)


# --- Versions -------------------------------------------------------------------------
#
# The gatekeeper's rule (gatekeeper.next_version, which test_dispatcher holds
# this copy to), so that the dispatcher can tell a CHANGELOG.md heading the
# gatekeeper would refuse.

def api_lines(text):
    return {line.rstrip() for line in (text or "").splitlines() if line.strip()}


def next_version(prev, old_lock, new_lock):
    if prev is None:
        return (0, 1, 0)
    old, new = api_lines(old_lock), api_lines(new_lock)
    removed, added = old - new, new - old
    x, y, z = prev
    if x == 0:
        return (0, y + 1, 0) if removed or added else (0, y, z + 1)
    if removed:
        return (x + 1, 0, 0)
    if added:
        return (x, y + 1, 0)
    return (x, y, z + 1)
