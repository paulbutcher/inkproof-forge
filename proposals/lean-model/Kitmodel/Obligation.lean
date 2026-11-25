/-!
# One obligation and its agent: `dispatcher.advance`, abstracted

Each step is one call of `advance` when the obligation is due. The environment
chooses, at every step, whether the agent is busy with other work (queued or
running), and whether a run that woke the agent discharged the obligation (the
unit's state moved on). `tries` stands for `len(OBLIGATION_BACKOFF)`.

Unlike `test_states.py`'s checks, these are statements about every infinite
execution, under any behaviour of the agents and the rest of the kit: an
exhaustive check over a snapshot of facts can't state them.
-/

namespace Kit.Obligation

structure St where
  standing : Bool := true    -- the unit still owes it
  escalated : Bool := false  -- a `Stalled:` decision for the human
  wakes : Nat := 0           -- runs it woke that ended with it standing
  waited : Nat := 0          -- steps it has waited for a busy agent (the fix only)
  deriving Repr

structure Env where
  busy : Bool        -- the agent has other work queued or running
  discharged : Bool  -- the run this step wakes, if any, moves the unit on

/-- Nothing more happens: discharged, or the human's. -/
def St.settled (s : St) : Prop := s.standing = false ∨ s.escalated = true

instance (s : St) : Decidable s.settled := by unfold St.settled; infer_instance

/-- Today's rule (`advance`): a quiet obligation waits while its agent is busy;
otherwise it wakes the agent, or once `tries` runs have ended with it standing,
escalates. -/
def step (tries : Nat) (quiet : Bool) (s : St) (e : Env) : St :=
  if s.settled then s
  else if quiet && e.busy then s
  else if tries ≤ s.wakes then { s with escalated := true }
  else { s with wakes := s.wakes + 1, standing := !e.discharged }

def run (tries : Nat) (quiet : Bool) (env : Nat → Env) : Nat → St
  | 0 => {}
  | n + 1 => step tries quiet (run tries quiet env n) (env n)

/-! ## An ordinary obligation always settles within `tries + 1` steps -/

theorem ordinary_progress (tries : Nat) (env : Nat → Env) :
    ∀ n, n ≤ tries → (run tries false env n).settled ∨ (run tries false env n).wakes = n := by
  intro n
  induction n with
  | zero => intro _; right; rfl
  | succ k ih =>
    intro hk
    rcases ih (by omega) with h | h
    · left
      simp only [run, step, h, ite_true]
    · simp only [run, step]
      by_cases hs : (run tries false env k).settled
      · simp [hs]
      · simp only [hs, ite_false, Bool.false_and]
        have : ¬ tries ≤ (run tries false env k).wakes := by omega
        simp only [this, ite_false]
        right; simp [h]

/-- However the agents behave, an ordinary obligation is discharged, or is the
human's, after at most `tries + 1` steps. -/
theorem ordinary_settles (tries : Nat) (env : Nat → Env) :
    (run tries false env (tries + 1)).settled := by
  rcases ordinary_progress tries env tries (Nat.le_refl _) with h | h
  · simp only [run, step, h, ite_true]
  · simp only [run, step]
    by_cases hs : (run tries false env tries).settled
    · simp [hs]
    · simp only [hs, ite_false, Bool.false_and]
      have : tries ≤ (run tries false env tries).wakes := by omega
      simp only [this, ite_true]
      right; rfl

/-! ## A quiet obligation can starve forever -/

/-- An agent that always has other work: a busy coordinator, say, that each run
leaves with new notices to read. -/
def alwaysBusy : Nat → Env := fun _ => ⟨true, false⟩

/-- Under today's rule, a quiet obligation of an agent that is always busy is
never woken, never counts a wake, and never becomes the human's: a unit left
`to do` or `unscheduled` stalls with no one told. -/
theorem quiet_starves (tries : Nat) :
    ∀ n, ¬ (run tries true alwaysBusy n).settled ∧ (run tries true alwaysBusy n).wakes = 0 := by
  intro n
  induction n with
  | zero => simp [run, St.settled]
  | succ k ih =>
    obtain ⟨hs, hw⟩ := ih
    simp only [run, step, hs, ite_false, alwaysBusy, Bool.true_and, ite_true]
    simp_all

/-! ## A fix: a quiet obligation waits for a busy agent at most `patience` times -/

def stepP (tries patience : Nat) (s : St) (e : Env) : St :=
  if s.settled then s
  else if e.busy && decide (s.waited < patience) then { s with waited := s.waited + 1 }
  else if tries ≤ s.wakes then { s with escalated := true }
  else { s with wakes := s.wakes + 1, waited := 0, standing := !e.discharged }

def runP (tries patience : Nat) (env : Nat → Env) : Nat → St
  | 0 => {}
  | n + 1 => stepP tries patience (runP tries patience env n) (env n)

/-- What is left before it must settle: each step that doesn't settle it uses
up at least one. -/
def budget (tries patience : Nat) (s : St) : Nat :=
  (tries - s.wakes) * (patience + 1) + (patience - s.waited)

theorem patient_progress (tries patience : Nat) (env : Nat → Env) :
    ∀ n, (runP tries patience env n).settled ∨
      ((runP tries patience env n).wakes ≤ tries ∧ (runP tries patience env n).waited ≤ patience ∧
        budget tries patience (runP tries patience env n) + n ≤ (tries + 1) * (patience + 1) - 1) := by
  intro n
  induction n with
  | zero =>
    right
    simp only [runP, budget]
    refine ⟨by omega, by omega, ?_⟩
    simp only [Nat.sub_zero, Nat.add_zero]
    rw [Nat.add_mul, Nat.one_mul]; omega
  | succ k ih =>
    rcases ih with h | ⟨hw, hp, hb⟩
    · left; simp only [runP, stepP, h, ite_true]
    · simp only [runP, stepP]
      by_cases hs : (runP tries patience env k).settled
      · left; simp [hs]
      · simp only [hs, ite_false]
        by_cases hwait : (env k).busy = true ∧ (runP tries patience env k).waited < patience
        · obtain ⟨hb1, hb2⟩ := hwait
          have hgo : ((env k).busy && decide ((runP tries patience env k).waited < patience)) = true := by
            simp [hb1, hb2]
          simp only [hgo, ite_true]
          right
          refine ⟨hw, by first | omega | (simp; omega), ?_⟩
          simp only [budget] at hb ⊢
          omega
        · have hc : ((env k).busy && decide ((runP tries patience env k).waited < patience)) = false := by
            cases hb1 : (env k).busy <;> simp_all
          simp only [hc]
          by_cases ht : tries ≤ (runP tries patience env k).wakes
          · left; simp [ht, St.settled]
          · simp only [ht, ite_false]
            cases hd : (env k).discharged
            · right
              refine ⟨by simp; omega, by simp, ?_⟩
              simp only [budget] at hb ⊢
              have hwl : (runP tries patience env k).wakes < tries := by omega
              have e1 : tries - (runP tries patience env k).wakes =
                  (tries - ((runP tries patience env k).wakes + 1)) + 1 := by omega
              rw [e1, Nat.add_mul, Nat.one_mul] at hb
              simp only [Bool.false_eq_true, ite_false, Bool.not_false]
              omega
            · left; simp [St.settled]

/-- With the fix, a quiet obligation is discharged, or is the human's, within
`(tries + 1) * (patience + 1)` steps, however busy its agent is kept. -/
theorem patient_settles (tries patience : Nat) (env : Nat → Env) :
    (runP tries patience env ((tries + 1) * (patience + 1))).settled := by
  rcases patient_progress tries patience env ((tries + 1) * (patience + 1)) with h | ⟨_, _, hb⟩
  · exact h
  · have : 0 < (tries + 1) * (patience + 1) := Nat.mul_pos (by omega) (by omega)
    omega

end Kit.Obligation
