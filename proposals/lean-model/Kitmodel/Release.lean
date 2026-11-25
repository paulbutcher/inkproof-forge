/-!
# A repository's release: `states.py`'s `release_state`, and `test_states.py`'s
`release_steps`, ported directly.

The three properties `test_states.py` checks exhaustively are proved here. Over
a finite space of facts, an exhaustive check and a proof by `decide` establish
the same thing; the point of this file is that the port is direct, not that it
adds assurance.
-/

namespace Kit.Release

inductive Ci | success | failure | running | none
  deriving DecidableEq, Repr

inductive Heading | none | wrong | right
  deriving DecidableEq, Repr

inductive Role | owner | coordinator | human | timer | held
  deriving DecidableEq, Repr

structure Facts where
  mainCi : Ci := .success
  mainCiLate : Bool := false
  rfr : Bool := false
  heading : Heading := .none
  held : Bool := false
  level3 : Bool := false
  decision : Bool := false
  deriving DecidableEq, Repr

inductive Name
  | mainFailing | upToDate | held | mainCiStuck | mainCiRunning | noEntry | wrongEntry
  | releasable | withHuman
  deriving DecidableEq, Repr

structure State where
  name : Name
  holders : List Role
  deriving DecidableEq, Repr

def State.terminal (s : State) : Bool := s.holders.isEmpty

/-- `states.release_state`, line for line. -/
def state (f : Facts) : State :=
  if f.mainCi = .failure then ⟨.mainFailing, [.owner]⟩
  else if !f.rfr && f.heading = .none then ⟨.upToDate, []⟩
  else if f.held then ⟨.held, [.held]⟩
  else if f.mainCi = .running || f.mainCi = .none then
    if f.mainCiLate then ⟨.mainCiStuck, [.owner]⟩ else ⟨.mainCiRunning, [.timer]⟩
  else if f.heading = .none then ⟨.noEntry, [.owner]⟩
  else if f.heading = .wrong then ⟨.wrongEntry, [.owner]⟩
  else if f.level3 then ⟨.releasable, [.coordinator]⟩
  else if f.decision then ⟨.withHuman, [.human]⟩
  else ⟨.releasable, [.coordinator]⟩

/-- `test_states.release_steps`: what the holders (and the world) may do next. -/
def steps (f : Facts) : List Facts :=
  let released := { f with rfr := false, heading := .none, mainCi := .success, mainCiLate := false }
  match (state f).name with
  | .mainFailing => [{ f with mainCi := .running, mainCiLate := false }]
  | .held => [{ f with held := false }]
  | .mainCiStuck => [{ f with mainCi := .running, mainCiLate := false }]
  | .mainCiRunning => [{ f with mainCi := .success, mainCiLate := false },
                       { f with mainCi := .failure, mainCiLate := false },
                       { f with mainCiLate := true }]
  | .noEntry | .wrongEntry => [{ f with heading := .right, mainCi := .running, mainCiLate := false }]
  | .releasable => if f.level3 then [released] else [{ f with decision := true }]
  | .withHuman => [released]
  | .upToDate => []

def allCi : List Ci := [.success, .failure, .running, .none]
def allHeading : List Heading := [.none, .wrong, .right]
def bools : List Bool := [false, true]

/-- Every combination of the facts: `ReleaseFacts.every()`. -/
def space : List Facts := Id.run do
  let mut out := []
  for c in allCi do for l in bools do for r in bools do for h in allHeading do
    for he in bools do for l3 in bools do for d in bools do
      out := ⟨c, l, r, h, he, l3, d⟩ :: out
  return out

/-- Check 1: a state is finished exactly when it is `up to date`. -/
theorem finished_iff : space.all (fun f => (state f).terminal == ((state f).name == .upToDate)) := by
  decide +kernel

/-- Check 2: every holder's step moves the unit to another state or other holders. -/
theorem every_step_moves_on :
    space.all (fun f => (state f).terminal || (steps f).all (fun g => state g != state f)) := by
  decide +kernel

/-- Finished within `n` steps, by some path. -/
def canFinish : Nat → Facts → Bool
  | 0, f => (state f).terminal
  | n + 1, f => (state f).terminal || (steps f).any (canFinish n)

/-- Check 3: from every combination, a finished state can be reached, within 12 steps (6 is too few). -/
theorem every_combination_can_finish : space.all (canFinish 12) := by
  decide +kernel


end Kit.Release
