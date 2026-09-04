"""The policy engine — the bounded, gated guarantee made concrete.

It answers exactly one question: *given this situation, what is the system
permitted to do right now, and which rule says so?*

Three properties this module exists to guarantee:

1. **Rules are declarative and centrally registered.** Every rule in
   `RULE_REGISTRY` has a stable id, a description, and a citation back to
   `decline-taxonomy`, `rbi-mandate-rules` or `policy-bounds`. A rule with no
   traceable justification does not exist here.
2. **The policy engine has the final say.** `llm_agent.py` supplies judgment;
   this module supplies permission. A recommendation can narrow an envelope and
   can never widen one — see `authorize()`, and ADR 0003.
3. **Fail closed.** A precondition that cannot be evaluated blocks the action.
   The safe direction of error is always "do less".

Every decision returns a `PolicyDecision`, whose fields map straight onto audit
trail columns. That is deliberate: a decision the trail cannot record is a
decision nobody can check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field

from app.models.entity import RecoverableEntity
from app.models.enums import (
    Action,
    CorridorDetermination,
    DeclineClass,
    Engine,
    EscalationTrigger,
    Outcome,
    RuleSource,
)
from app.models.provenance import Provenance
from app.services.decline_taxonomy import (
    BUDGET_RULE_ID,
    RETRY_BUDGET,
    DeclineCode,
    DeclineSpec,
    Retryability,
    classify,
)

# ---------------------------------------------------------------------------
# The rule registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRule:
    """One declaratively registered rule.

    `rule_id` is the citation string written to the audit trail, in the
    `<skill-or-module>:<rule-id>` form the `audit-schema` skill specifies.
    """

    rule_id: str
    category: str
    description: str
    source: RuleSource
    #: Where a reader goes to argue with this rule.
    citation: str


def _rule(
    rule_id: str, category: str, description: str, source: RuleSource, citation: str
) -> PolicyRule:
    return PolicyRule(
        rule_id=rule_id,
        category=category,
        description=description,
        source=source,
        citation=citation,
    )


#: Every rule the engine can cite. Nothing outside this dict may appear as an
#: `authorising_rule`, which is what makes "every action cites a rule" checkable
#: rather than merely intended.
RULE_REGISTRY: dict[str, PolicyRule] = {
    r.rule_id: r
    for r in (
        # --- Hard stops (evaluated first) ---------------------------------
        _rule(
            "policy-bounds:HS1",
            "hard_stop",
            "A terminal entity is terminal, irreversibly — no retry, no reminder, "
            "no escalation, in any code path.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> HS1",
        ),
        _rule(
            "policy-bounds:HS2",
            "hard_stop",
            "Terminal decline classes cancel the schedule, not just the attempt.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> HS2",
        ),
        _rule(
            "rbi-mandate-rules:A4",
            "hard_stop",
            "A revoked mandate is an immediate hard stop and is never retryable. "
            "No grace attempt, no exception. Regulatory.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part A, A4",
        ),
        _rule(
            "rbi-mandate-rules:PartB.paused",
            "hard_stop",
            "A paused mandate accepts no debit. Reversible, unlike revocation, so "
            "it blocks the attempt rather than terminating the mandate.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part B, paused",
        ),
        # --- Mandate debit preconditions (Engine 2) ------------------------
        _rule(
            "rbi-mandate-rules:A1",
            "mandate_precondition",
            "A mandate requires one-time AFA at registration. Until it succeeds "
            "there is no mandate and no debit is permissible. Regulatory.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part A, A1",
        ),
        _rule(
            "rbi-mandate-rules:A2",
            "mandate_precondition",
            "A pre-debit notification must reach the customer 24-48 hours before "
            "every scheduled debit, retries included. Regulatory.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part A, A2",
        ),
        _rule(
            "rbi-mandate-rules:A5",
            "mandate_precondition",
            "A debit declined for missing AFA is its own failure mode. Retrying it "
            "without obtaining authentication reproduces the decline forever. "
            "Regulatory.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part A, A5",
        ),
        _rule(
            "rbi-mandate-rules:PartB.notice_staleness",
            "mandate_precondition",
            "A pre-debit notice older than 48 hours is stale. Re-notify rather "
            "than debit against it.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part B, notice_staleness",
        ),
        _rule(
            "rbi-mandate-rules:PartB.mandate_cap",
            "mandate_precondition",
            "A debit above the amount authorised at registration is not covered by "
            "the mandate, whatever else holds.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part B, mandate_cap",
        ),
        # --- Attempt caps --------------------------------------------------
        _rule(
            "policy-bounds:AC1",
            "attempt_cap",
            "Hard ceiling of 4 recovery attempts per entity. Nothing may raise it.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> AC1",
        ),
        _rule(
            "policy-bounds:AC2",
            "attempt_cap",
            "A configured per-engine cap above the ceiling is clamped down, never honoured.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> AC2",
        ),
        _rule(
            "rbi-mandate-rules:PartB.max_retries",
            "attempt_cap",
            "Mandate flows are capped at 3 attempts per billing cycle — below the "
            "taxonomy's SOFT budget of 4, because recurring debits are more constrained.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part B",
        ),
        *(
            _rule(
                BUDGET_RULE_ID[cls],
                "attempt_cap",
                f"Retry budget for {cls.value} declines: {budget} attempt(s) including the original.",
                RuleSource.DECLINE_TAXONOMY,
                "decline-taxonomy -> Retry budgets by class",
            )
            for cls, budget in RETRY_BUDGET.items()
        ),
        # --- Cooldown / backoff --------------------------------------------
        _rule(
            "policy-bounds:CD1",
            "cooldown",
            "Minimum 6 hours between attempts on the same entity.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> CD1",
        ),
        _rule(
            "policy-bounds:CD2",
            "cooldown",
            "Escalating backoff: 6h doubling per attempt, capped at 72h.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> CD2",
        ),
        _rule(
            "rbi-mandate-rules:PartB.min_spacing",
            "cooldown",
            "Mandate debits are spaced at least 24 hours apart — anything shorter "
            "cannot satisfy the A2 pre-debit notification window.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part B",
        ),
        # --- Quiet hours / communication limits -----------------------------
        _rule(
            "policy-bounds:QH1",
            "quiet_hours",
            "Customer-facing outreach only between 09:00 and 21:00 IST. Outside the "
            "window an attempt is blocked and rescheduled, never dropped.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> QH1",
        ),
        _rule(
            "policy-bounds:QH2",
            "quiet_hours",
            "At most 3 customer messages per entity per rolling 7 days, across all channels.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> QH2",
        ),
        _rule(
            "policy-bounds:QH3",
            "quiet_hours",
            "The escalation ladder has a final rung; past it the entity goes to a human.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> QH3",
        ),
        # --- Amount thresholds ----------------------------------------------
        # --- Tone constraints on generated messages -------------------------
        _rule(
            "policy-bounds:TN1",
            "tone",
            "A generated message may not threaten, imply a consequence the system "
            "will not carry out, or manufacture a deadline no rule imposes.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> TN1",
        ),
        _rule(
            "policy-bounds:TN2",
            "tone",
            "A generated message must state the real failure and a remedy the "
            "customer can actually complete.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> TN2",
        ),
        _rule(
            "policy-bounds:TN3",
            "tone",
            "A draft failing tone validation is discarded in favour of the "
            "deterministic template, and the rejection is audited.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> TN3",
        ),
        _rule(
            "policy-bounds:AT1",
            "amount_threshold",
            "Value-dependent branching is permitted only against a registered, "
            "named threshold. Anonymous magic numbers are forbidden.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> AT1",
        ),
        _rule(
            "rbi-mandate-rules:A3",
            "amount_threshold",
            "Recurring debits up to Rs 15,000 need no fresh OTP once registered; "
            "above it, fresh AFA is required each cycle. Regulatory.",
            RuleSource.RBI_MANDATE_RULES,
            "rbi-mandate-rules -> Part A, A3",
        ),
        # --- Human escalation -------------------------------------------------
        _rule(
            "policy-bounds:HE1",
            "human_escalation",
            "An abstaining reasoning task routes the entity to human review.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> HE1",
        ),
        _rule(
            "policy-bounds:HE2",
            "human_escalation",
            "An entity whose budget is spent without recovery is handed to a human, "
            "not silently abandoned.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> HE2",
        ),
        _rule(
            "policy-bounds:HE3",
            "human_escalation",
            "Entities above Rs 50,000 escalate to a human on the first hard-class "
            "failure rather than running the full ladder.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> HE3",
        ),
        _rule(
            "policy-bounds:HE4",
            "human_escalation",
            "FRAUD_SUSPECTED is never automated. Human review only.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> HE4",
        ),
        # --- Corridor recovery bounds (Engine 1) ---------------------------
        _rule(
            "corridor-detection:RR1",
            "hard_stop",
            "Every reroute expires. Maximum duration 6 hours, and a configured "
            "duration above it is clamped — a reroute that never lapses is a "
            "permanent config change wearing a recovery action's clothes.",
            RuleSource.CORRIDOR_DETECTION,
            "corridor-detection -> RR1",
        ),
        _rule(
            "corridor-detection:RR2",
            "authority",
            "Rerouting a corridor requires a systemic determination. An individual or "
            "insufficient-evidence diagnosis can never authorise one.",
            RuleSource.CORRIDOR_DETECTION,
            "corridor-detection -> RR2",
        ),
        _rule(
            "corridor-detection:RR3",
            "hard_stop",
            "A reroute needs an alternate route to exist. Where none does, the "
            "precondition is unsatisfiable and the action fails closed.",
            RuleSource.CORRIDOR_DETECTION,
            "corridor-detection -> RR3",
        ),
        _rule(
            "corridor-detection:ES1",
            "amount_threshold",
            "A corridor-level action above Rs 2,00,000 of value at risk is escalated "
            "for human confirmation rather than executed autonomously.",
            RuleSource.CORRIDOR_DETECTION,
            "corridor-detection -> ES1",
        ),
        # --- Receivables ladder bounds (Engine 3) ---------------------------
        _rule(
            "policy-bounds:RL1",
            "quiet_hours",
            "The chase ladder has exactly three rungs — gentle reminder, firm "
            "follow-up, formal notice. Past the third the invoice goes to a human.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> RL1",
        ),
        _rule(
            "policy-bounds:RL2",
            "cooldown",
            "No rung fires less than 72 hours after the previous contact on that "
            "invoice. Three rungs at that spacing fit inside the QH2 window.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> RL2",
        ),
        _rule(
            "policy-bounds:RL3",
            "quiet_hours",
            "At most 4 messages per customer per rolling 7 days, counted across "
            "every invoice they owe. QH2 bounds the conversation; this bounds the "
            "recipient.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> RL3",
        ),
        _rule(
            "policy-bounds:RL4",
            "hard_stop",
            "A disputed invoice is never chased again automatically. Chasing stops "
            "immediately and the invoice routes to human review.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> RL4",
        ),
        _rule(
            "policy-bounds:RL5",
            "human_escalation",
            "The ladder ascends on evidence — a broken promise or ladder "
            "exhaustion — never on elapsed time alone.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> RL5",
        ),
        # --- Promise-to-pay bounds (Engine 3) -------------------------------
        _rule(
            "policy-bounds:PP1",
            "cooldown",
            "A promise is broken only once 48 hours have passed beyond the "
            "committed date with no payment. Settlement lag is not a broken promise.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> PP1",
        ),
        _rule(
            "policy-bounds:PP2",
            "human_escalation",
            "A conditional promise is never scored kept or broken against a date it "
            "did not give, and cannot trigger an RL5 escalation.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> PP2",
        ),
        _rule(
            "policy-bounds:PP3",
            "cooldown",
            "A promise with no extractable date deprioritises its invoice for 7 "
            "days from the reply, then the ladder resumes. A pause, never a stop.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> PP3",
        ),
        # --- Prioritisation (Engine 3) ---------------------------------------
        _rule(
            "policy-bounds:PR1",
            "authority",
            "The worklist is a deterministic weighted score with declared weights. "
            "Ranking decides the order of work, never permission.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> PR1",
        ),
        _rule(
            "policy-bounds:PR2",
            "quiet_hours",
            "An invoice with a live, unbroken promise is deprioritised and no "
            "reminder is authorised for it.",
            RuleSource.POLICY_BOUNDS,
            "policy-bounds -> PR2",
        ),
        # --- The decision-authority boundary -----------------------------------
        _rule(
            "policy_engine:llm_authority.denied",
            "authority",
            "A model recommendation cannot authorise an action the rules forbid. "
            "The denial stands and the recommendation is recorded as overridden.",
            RuleSource.PRODUCT_DECISION,
            "docs/adr/0003-policy-engine-final-authority.md",
        ),
        _rule(
            "policy_engine:llm_authority.out_of_envelope",
            "authority",
            "A model may only recommend an action inside the permitted envelope. "
            "Anything outside it is refused, not negotiated.",
            RuleSource.PRODUCT_DECISION,
            "docs/adr/0003-policy-engine-final-authority.md",
        ),
        _rule(
            "policy_engine:permitted",
            "authority",
            "No stopping rule fired; the action is inside every applicable bound.",
            RuleSource.PRODUCT_DECISION,
            "docs/adr/0003-policy-engine-final-authority.md",
        ),
        _rule(
            "policy_engine:fail_closed",
            "authority",
            "A precondition that cannot be evaluated blocks the action.",
            RuleSource.PRODUCT_DECISION,
            "CLAUDE.md -> Non-negotiables #3",
        ),
    )
}


# ---------------------------------------------------------------------------
# Named thresholds (policy-bounds AT1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AmountThreshold:
    """A named, cited money threshold. Integer paise, never a float."""

    name: str
    value_paise: int
    rule_id: str
    description: str


THRESHOLDS: dict[str, AmountThreshold] = {
    t.name: t
    for t in (
        AmountThreshold(
            name="afa_fresh_auth",
            value_paise=1_500_000,  # Rs 15,000
            rule_id="rbi-mandate-rules:A3",
            description="Above this, a recurring debit needs fresh AFA each cycle. Regulatory.",
        ),
        AmountThreshold(
            name="corridor_autonomous_action",
            value_paise=20_000_000,  # Rs 2,00,000
            rule_id="corridor-detection:ES1",
            description=(
                "Above this value at risk, a corridor-level action needs human "
                "confirmation. A bulk action deserves a higher bar than one account."
            ),
        ),
        AmountThreshold(
            name="high_value_review",
            value_paise=5_000_000,  # Rs 50,000
            rule_id="policy-bounds:HE3",
            description="Above this, a hard-class failure escalates to a human immediately.",
        ),
    )
}


def exceeds_threshold(name: str, amount_paise: int) -> bool:
    """Compare against a registered threshold.

    Raises on an unknown name rather than returning False: a silently absent
    threshold would turn a value-dependent rule into a no-op that still looks
    like it fired.
    """
    if name not in THRESHOLDS:
        raise PolicyConfigurationError(
            f"unknown amount threshold {name!r}. Register it in THRESHOLDS with a "
            "citation first (policy-bounds -> AT1)."
        )
    return amount_paise > THRESHOLDS[name].value_paise


class PolicyConfigurationError(RuntimeError):
    """Raised when the engine is asked to cite a rule or threshold it does not have."""


# ---------------------------------------------------------------------------
# Bounds (values live in the skills; these are the resolved constants)
# ---------------------------------------------------------------------------

#: policy-bounds:AC1 — nothing raises this.
HARD_ATTEMPT_CEILING = 4

#: policy-bounds:CD1 / CD2
BASE_COOLDOWN = timedelta(hours=6)
MAX_COOLDOWN = timedelta(hours=72)

#: policy-bounds:QH1 — the payer's timezone. All Vasooli entities are INR/domestic.
#: A fixed offset rather than `ZoneInfo("Asia/Kolkata")`: IST has observed no DST
#: since 1945, so the offset is exact, and this avoids depending on a system tz
#: database that is absent on a bare Windows Python.
OUTREACH_TZ = timezone(timedelta(hours=5, minutes=30), "IST")
OUTREACH_OPEN_HOUR = 9
OUTREACH_CLOSE_HOUR = 21

#: policy-bounds:QH2
MAX_MESSAGES_PER_WINDOW = 3
CONTACT_WINDOW = timedelta(days=7)

#: policy-bounds:RL3 — the *recipient* cap, counted across every invoice a
#: customer owes. Deliberately one above the per-entity QH2 cap: one whole
#: ladder plus room to raise a second invoice, and no more.
MAX_MESSAGES_PER_CUSTOMER = 4

#: policy-bounds:RL1 — the chase ladder's rungs, in order. The list is the rule:
#: its length is the final rung QH3 refers to, so a fourth rung cannot be added
#: by editing engine code.
LADDER_RUNGS: tuple[str, ...] = ("gentle_reminder", "firm_follow_up", "formal_notice")

#: policy-bounds:RL2 — minimum spacing between two rungs on the same invoice.
LADDER_RUNG_INTERVAL = timedelta(hours=72)

#: policy-bounds:PP1 — grace beyond a committed date before a promise is broken.
PROMISE_GRACE = timedelta(hours=48)

#: policy-bounds:PP3 — how long an undateable or conditional promise parks its
#: invoice before the ladder resumes.
UNDATEABLE_PROMISE_HORIZON = timedelta(days=7)

#: rbi-mandate-rules:A2 — regulatory, not ours. A pre-debit notification reaches
#: the customer 24-48h before every scheduled debit, retries included. The upper
#: bound is restated as `PartB.notice_staleness`, which is where the decision to
#: treat "too early" as a violation rather than a curiosity is argued.
NOTICE_MIN_LEAD_HOURS = 24.0
NOTICE_MAX_LEAD_HOURS = 48.0


@dataclass(frozen=True)
class EngineLimits:
    """Per-engine bounds. Every value carries the rule that set it."""

    max_attempts: int
    max_attempts_rule: str
    min_cooldown: timedelta
    cooldown_rule: str


ENGINE_LIMITS: dict[Engine, EngineLimits] = {
    Engine.ROOT_CAUSE: EngineLimits(
        max_attempts=HARD_ATTEMPT_CEILING,
        max_attempts_rule="policy-bounds:AC1",
        min_cooldown=BASE_COOLDOWN,
        cooldown_rule="policy-bounds:CD1",
    ),
    Engine.MANDATE_RECOVERY: EngineLimits(
        # Stricter than the ceiling, because recurring debits are more constrained.
        max_attempts=3,
        max_attempts_rule="rbi-mandate-rules:PartB.max_retries",
        min_cooldown=timedelta(hours=24),
        cooldown_rule="rbi-mandate-rules:PartB.min_spacing",
    ),
    Engine.RECEIVABLES: EngineLimits(
        # The ladder's final rung. `RL1` sets the rung count and is deliberately
        # the same number as the QH2 contact cap, so the two bounds cannot
        # disagree about how many times a customer hears from us about one
        # invoice. The cap is the length of `LADDER_RUNGS`, not a literal —
        # adding a rung to that tuple is the only way to change it.
        max_attempts=len(LADDER_RUNGS),
        max_attempts_rule="policy-bounds:RL1",
        # 72h between rungs, not the generic 6h floor: a B2B accounts-payable
        # cycle does not move in hours, and CD2's doubling clamps at 72h anyway,
        # so every rung is spaced exactly RL2's interval.
        min_cooldown=LADDER_RUNG_INTERVAL,
        cooldown_rule="policy-bounds:RL2",
    ),
    Engine.CORE: EngineLimits(
        max_attempts=HARD_ATTEMPT_CEILING,
        max_attempts_rule="policy-bounds:AC1",
        min_cooldown=BASE_COOLDOWN,
        cooldown_rule="policy-bounds:CD1",
    ),
}


# ---------------------------------------------------------------------------
# Request / decision objects
# ---------------------------------------------------------------------------


class PolicyRequest(BaseModel):
    """Everything the engine needs to decide, and nothing it can mutate.

    Frozen and database-free on purpose: a rule that can only be evaluated
    against live rows is a rule that is awkward to test, and these are the rules
    the submission's credibility rests on.
    """

    model_config = ConfigDict(frozen=True)

    engine: Engine
    entity: RecoverableEntity
    proposed_action: Action
    now: datetime

    #: The normalized decline that prompted this, if any.
    decline_code: DeclineCode | None = None

    #: True when a human receives the result — a message, a call, a notification.
    #: Silent server-side actions are not outreach and skip the QH1 gate.
    is_outreach: bool = False
    messages_sent_in_window: int = Field(default=0, ge=0)

    #: Per-engine override. Clamped to the ceiling by AC2 — never honoured above it.
    max_attempts_override: int | None = None

    #: Set when a reasoning task declined to answer. Routes to a human (HE1).
    abstained: bool = False

    #: Set when a precondition could not be evaluated at all. Fails closed.
    unevaluable_precondition: str | None = None

    #: Engine 1 only. The diagnosis layer's determination about the corridor this
    #: action concerns. Required for `reroute_traffic`, which `corridor-detection:RR2`
    #: permits only on a systemic determination — the rule that stops a
    #: concentration of insufficient-funds declines from rerouting live traffic.
    corridor_determination: CorridorDetermination | None = None

    #: Engine 1 only. Whether an alternate route actually exists for this
    #: corridor's method. `None` means "not established", which denies under
    #: `corridor-detection:RR3` exactly as `False` does — a reroute to nowhere is
    #: an audit entry claiming an action that cannot have happened.
    alternate_route_available: bool | None = None

    # --- Engine 2: the mandate debit preconditions ------------------------
    #
    # These live on the request, not inside the engine, for the same reason the
    # corridor fields do: a precondition an engine is trusted to check itself is
    # an intention, and an engine can always forget. Here they are gates.
    # All four are consulted only for `attempt_charge` on `mandate_recovery`.

    #: A pause is a customer instruction to stop collecting. Reversible, so it
    #: blocks the debit rather than terminating the mandate
    #: (`rbi-mandate-rules:PartB.paused`).
    mandate_paused: bool = False

    #: Whether the mandate ever completed one-time AFA registration
    #: (`rbi-mandate-rules:A1`). `None` means "not established" and denies
    #: exactly as `False` does — an unevaluable precondition is not a green light.
    afa_registered: bool | None = None

    #: Hours between the pre-debit notice and the scheduled debit
    #: (`rbi-mandate-rules:A2`). `None` means no notice was sent at all.
    notice_lead_hours: float | None = None

    #: The maximum the customer authorised at registration
    #: (`rbi-mandate-rules:PartB.mandate_cap`). `None` means "not established".
    mandate_cap_paise: int | None = None

    #: Whether fresh per-cycle AFA has been completed for this debit. Consulted
    #: only above the `afa_fresh_auth` threshold, where `rbi-mandate-rules:A3`
    #: requires it. Below the threshold a registered mandate needs no fresh OTP.
    fresh_afa_completed: bool = False

    # --- Engine 3: the receivables ladder gates ---------------------------
    #
    # Same shape and same reasoning as the two blocks above. Engine 3 is entirely
    # outreach, so every one of these bounds a *message*; leaving any of them to
    # the engine to remember would make it an intention rather than a gate.

    #: A reply on this invoice was classified as a dispute
    #: (`policy-bounds:RL4`). Evaluated with the hard stops: automated chasing
    #: stops immediately and the invoice routes to a human.
    dispute_frozen: bool = False

    #: Messages already sent to this *customer* in the rolling window, across
    #: every invoice they owe (`policy-bounds:RL3`). Distinct from
    #: `messages_sent_in_window`, which is per invoice under QH2.
    customer_messages_in_window: int = Field(default=0, ge=0)

    #: True while a promise on this invoice is `active` and inside its window
    #: (PP1) or its horizon (PP3). Suppresses outreach under
    #: `policy-bounds:PR2` — chasing someone who already committed is the
    #: behaviour that makes automated collections feel like harassment.
    active_promise: bool = False

    #: The evidence for an escalation (`policy-bounds:RL5`). `None` on a
    #: receivables `escalate` is the refusal case, not a missing field: the
    #: ladder ascends on evidence and never on a timer.
    escalation_trigger: EscalationTrigger | None = None


class PolicyDecision(BaseModel):
    """The engine's answer. Maps field-for-field onto the audit trail.

    Frozen. A decision that can be edited after the fact is not a decision, and
    the audit entry written from it would be describing something that never
    happened.
    """

    model_config = ConfigDict(frozen=True)

    allowed: bool
    #: What the system may actually do — the proposed action when allowed, or the
    #: refusal (`block_attempt`, `halt_schedule`, `escalate`) when not.
    action: Action
    rule_id: str
    reason_code: str
    rationale: str
    outcome: Outcome

    attempts_remaining: int | None = None
    #: The earliest moment this action becomes permissible, when it is only
    #: blocked on timing. None when timing is not the blocker.
    earliest_next_attempt_at: datetime | None = None
    requires_human: bool = False
    terminal: bool = False

    #: The envelope a recommendation must stay inside. Empty when denied.
    permitted_actions: frozenset[Action] = frozenset()

    #: Recorded when a model recommendation was refused. Kept so the trail shows
    #: what the model wanted as well as what the rules allowed.
    overridden_recommendation: str | None = None

    #: The authority rule that governed the override, when a recommendation was
    #: overruled. Separate from `rule_id`, which keeps naming the rule that
    #: actually refused — `policy-bounds:HE1`, `corridor-detection:RR3`, an
    #: attempt cap. Collapsing the two would leave every denial in a batch citing
    #: the same generic authority rule, and "which rule stopped this?" is the
    #: question the audit trail exists to answer.
    authority_rule_id: str | None = None

    def audit_fields(self) -> dict[str, object]:
        """The subset that goes straight into `audit_trail.record()`."""
        return {
            "action": self.action,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "authorising_rule": self.rule_id,
            "rationale": self.rationale,
            "attempts_remaining": self.attempts_remaining,
        }

    def override_metadata(self) -> dict[str, object]:
        """Audit metadata describing an overruled recommendation, if there was one.

        Kept beside `audit_fields()` rather than folded into it: the citation
        names the refusing rule, and this says the model was overruled while
        doing so. Two facts, two fields.
        """
        if self.overridden_recommendation is None:
            return {}
        return {
            "overridden_recommendation": self.overridden_recommendation,
            "authority_rule": self.authority_rule_id,
        }


class LLMRecommendation(BaseModel):
    """What the reasoning layer thinks should happen.

    A recommendation, never an authorisation. It carries provenance because
    nothing model-derived may exist in this system without it.
    """

    model_config = ConfigDict(frozen=True)

    action: Action
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: Provenance
    #: A model may propose waiting *longer* than the policy floor. It may never
    #: propose waiting less; `authorize()` takes the later of the two.
    proposed_next_attempt_at: datetime | None = None


# ---------------------------------------------------------------------------
# Decision construction — the only places `allowed` is ever set
# ---------------------------------------------------------------------------


def _require_rule(rule_id: str) -> PolicyRule:
    if rule_id not in RULE_REGISTRY:
        raise PolicyConfigurationError(
            f"rule {rule_id!r} is not registered. Add it to RULE_REGISTRY with a "
            "description and a citation — a rule with no traceable justification "
            "should not exist (CLAUDE.md -> Non-negotiables #1)."
        )
    return RULE_REGISTRY[rule_id]


def _deny(
    *,
    rule_id: str,
    reason_code: str,
    rationale: str,
    action: Action = Action.BLOCK_ATTEMPT,
    outcome: Outcome = Outcome.BLOCKED,
    attempts_remaining: int | None = None,
    earliest_next_attempt_at: datetime | None = None,
    requires_human: bool = False,
    terminal: bool = False,
) -> PolicyDecision:
    _require_rule(rule_id)
    return PolicyDecision(
        allowed=False,
        action=action,
        rule_id=rule_id,
        reason_code=reason_code,
        rationale=rationale,
        outcome=outcome,
        attempts_remaining=attempts_remaining,
        earliest_next_attempt_at=earliest_next_attempt_at,
        requires_human=requires_human,
        terminal=terminal,
        permitted_actions=frozenset(),
    )


def _permit(
    *,
    action: Action,
    rule_id: str,
    reason_code: str,
    rationale: str,
    attempts_remaining: int,
    earliest_next_attempt_at: datetime | None,
    permitted_actions: frozenset[Action],
) -> PolicyDecision:
    """The **only** constructor of an allowed decision in this codebase.

    Module-private, and never reachable from `authorize()`. That is what makes
    "a model cannot widen a bound" a structural property rather than a
    convention someone has to remember.
    """
    _require_rule(rule_id)
    return PolicyDecision(
        allowed=True,
        action=action,
        rule_id=rule_id,
        reason_code=reason_code,
        rationale=rationale,
        outcome=Outcome.PENDING,
        attempts_remaining=attempts_remaining,
        earliest_next_attempt_at=earliest_next_attempt_at,
        permitted_actions=permitted_actions,
    )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class PolicyEngine:
    """Bounded decisions with a named rule behind every one.

    Stateless — it takes the world as a `PolicyRequest` and returns a judgement
    about it. That is what lets every rule be tested without a database, and it
    is why the boundary cases in the test suite are cheap to write.
    """

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        """Decide what is permitted right now, and say which rule decided it.

        Evaluation order is load-bearing: hard stops first, then the things that
        end a schedule, then caps, then timing, then communication limits. A
        revoked mandate short-circuits every other consideration.
        """
        entity = request.entity
        limits = ENGINE_LIMITS[request.engine]

        # 0. Fail closed. An unevaluable precondition is not a green light.
        if request.unevaluable_precondition:
            return _deny(
                rule_id="policy_engine:fail_closed",
                reason_code="PRECONDITION_UNEVALUABLE",
                rationale=(
                    f"Precondition '{request.unevaluable_precondition}' could not be "
                    "evaluated, so the action is blocked. The safe direction of error "
                    "is to do less."
                ),
                requires_human=True,
            )

        # 1. Hard stops (policy-bounds HS1/HS2, rbi-mandate-rules A4).
        if entity.is_terminal:
            reason = entity.terminal_reason or "ENTITY_TERMINAL"
            rule_id = (
                "rbi-mandate-rules:A4"
                if reason
                in {DeclineCode.MANDATE_REVOKED.value, DeclineCode.MANDATE_EXPIRED.value}
                else "policy-bounds:HS1"
            )
            return _deny(
                rule_id=rule_id,
                reason_code=reason,
                rationale=(
                    f"Entity {entity.entity_id} is terminal ({reason}). No further "
                    "action of any kind is permitted, in any code path."
                ),
                action=Action.HALT_SCHEDULE,
                outcome=Outcome.HALTED,
                attempts_remaining=0,
                terminal=True,
            )

        # 1b. Dispute freeze (policy-bounds:RL4). Evaluated with the hard stops
        #     rather than among the ladder rules: once a customer says an invoice
        #     is wrong, *nothing* automated may touch it, and a rule that only
        #     applied to reminders would leave escalation and payment links
        #     running against an invoice we cannot adjudicate. Unlike HS1 this is
        #     not terminality — a human may resume it — so the invoice is
        #     escalated rather than halted.
        if request.dispute_frozen:
            return _deny(
                rule_id="policy-bounds:RL4",
                reason_code="DISPUTE_FREEZE",
                rationale=(
                    f"Invoice {entity.entity_id} is disputed. Automated chasing "
                    "stops immediately and it goes to a human: the system has no "
                    "way to adjudicate a disputed amount, and continuing to chase "
                    "one is both bad practice and a compliance risk."
                ),
                action=Action.ESCALATE,
                outcome=Outcome.ESCALATED,
                attempts_remaining=0,
                requires_human=True,
            )

        spec = classify(request.decline_code) if request.decline_code else None

        if spec is not None and spec.retryability is Retryability.NEVER:
            # These do not merely fail the attempt — they end the schedule.
            rule_id = (
                "rbi-mandate-rules:A4"
                if spec.decline_class is DeclineClass.TERMINAL_MANDATE
                else "policy-bounds:HS2"
            )
            return _deny(
                rule_id=rule_id,
                reason_code=spec.code.value,
                rationale=f"{spec.code.value}: {spec.default_action}",
                action=Action.HALT_SCHEDULE,
                outcome=Outcome.HALTED,
                attempts_remaining=0,
                requires_human=spec.human_review,
                terminal=True,
            )

        # 2. Human-escalation triggers that pre-empt any further automation.
        if request.abstained:
            return _deny(
                rule_id="policy-bounds:HE1",
                reason_code="ABSTAINED",
                rationale=(
                    "The reasoning task declined to answer rather than guess. Routing "
                    "to human review — abstention is only a successful outcome if "
                    "something picks the entity up."
                ),
                action=Action.ESCALATE,
                outcome=Outcome.ESCALATED,
                requires_human=True,
            )

        if (
            spec is not None
            and spec.decline_class is DeclineClass.HARD
            and exceeds_threshold("high_value_review", entity.amount_paise)
        ):
            return _deny(
                rule_id="policy-bounds:HE3",
                reason_code=spec.code.value,
                rationale=(
                    f"{_rupees(entity.amount_paise)} is above the high-value review "
                    f"threshold of {_rupees(THRESHOLDS['high_value_review'].value_paise)} "
                    "and this is a hard-class failure. A human looks at it before the "
                    "ladder runs."
                ),
                action=Action.ESCALATE,
                outcome=Outcome.ESCALATED,
                requires_human=True,
            )

        # 2b. Corridor-level reroute preconditions (Engine 1).
        #
        # These sit above the attempt caps deliberately: a reroute that should
        # never have been proposed must be refused for the *right* reason, not
        # incidentally by a budget check that happens to fire first.
        if request.proposed_action is Action.REROUTE_TRAFFIC:
            if request.corridor_determination is not CorridorDetermination.SYSTEMIC:
                stated = (
                    request.corridor_determination.value
                    if request.corridor_determination
                    else "none stated"
                )
                return _deny(
                    rule_id="corridor-detection:RR2",
                    reason_code="REROUTE_REQUIRES_SYSTEMIC",
                    rationale=(
                        f"Corridor {entity.entity_id} was diagnosed '{stated}'. Only a "
                        "systemic determination may reroute live traffic — a "
                        "concentration of customer-side declines is not a corridor "
                        "outage, and rerouting on one is the expensive mistake."
                    ),
                    requires_human=True,
                )
            if exceeds_threshold("corridor_autonomous_action", entity.amount_paise):
                return _deny(
                    rule_id="corridor-detection:ES1",
                    reason_code="CORRIDOR_VALUE_AT_RISK",
                    rationale=(
                        f"{_rupees(entity.amount_paise)} at risk is above the corridor "
                        "autonomous-action ceiling of "
                        f"{_rupees(THRESHOLDS['corridor_autonomous_action'].value_paise)}. "
                        "A bulk action at this value is confirmed by a human first."
                    ),
                    action=Action.ESCALATE,
                    outcome=Outcome.ESCALATED,
                    requires_human=True,
                )
            if request.alternate_route_available is not True:
                return _deny(
                    rule_id="corridor-detection:RR3",
                    reason_code="NO_ALTERNATE_ROUTE",
                    rationale=(
                        f"No alternate route is available for corridor "
                        f"{entity.entity_id}, so the reroute has nowhere to send "
                        "traffic. Failing closed to human review rather than "
                        "recording an action that cannot have happened."
                    ),
                    action=Action.ESCALATE,
                    outcome=Outcome.ESCALATED,
                    requires_human=True,
                )

        # 2bb. Escalation evidence (Engine 3, policy-bounds:RL5).
        #
        # Above the caps for the same reason the reroute preconditions are: an
        # escalation with no evidence behind it must be refused for *that*
        # reason, not incidentally by a budget check. This is the rule behind the
        # engine's central claim — the system does not chase people who are
        # cooperating, it chases the ones who committed and did not follow
        # through — so "which rule let this escalate?" has to have an answer.
        if (
            request.engine is Engine.RECEIVABLES
            and request.proposed_action is Action.ESCALATE
            and request.escalation_trigger is None
        ):
            return _deny(
                rule_id="policy-bounds:RL5",
                reason_code="NO_ESCALATION_EVIDENCE",
                rationale=(
                    f"Nothing has escalated invoice {entity.entity_id}: no promise "
                    "was broken and the ladder is not exhausted. Elapsed time is "
                    "not evidence, so the ladder stays where it is."
                ),
                action=Action.BLOCK_ATTEMPT,
                outcome=Outcome.BLOCKED,
            )

        # 2bc. A live promise suppresses the chase (Engine 3, policy-bounds:PR2).
        #
        # Above the caps and the cooldown deliberately. All three would refuse
        # this reminder, but only one of them says the *true* thing: an invoice
        # whose customer has already committed is not "out of budget" or "too
        # soon", it is an invoice with nothing to say. Found on the first live
        # run, where six deprioritised invoices were being refused by the RL2
        # rung interval and the trail could not show that the promise was the
        # reason.
        if request.is_outreach and request.active_promise:
            return _deny(
                rule_id="policy-bounds:PR2",
                reason_code="ACTIVE_PROMISE",
                rationale=(
                    f"{entity.entity_id} carries a live, unbroken promise to pay. "
                    "Chasing a customer who has already committed is the behaviour "
                    "that makes automated collections feel like harassment, so the "
                    "invoice is deprioritised until the promise is kept or broken."
                ),
                outcome=Outcome.SKIPPED,
            )

        # 2c. Mandate debit preconditions (Engine 2).
        #
        # Ordered exactly as `rbi-mandate-rules` -> Preconditions states them, and
        # placed above the attempt caps deliberately: an AFA or notice failure has
        # to be refused for the reason that actually applies. `AFA_REQUIRED` is
        # POLICY_BLOCK with a budget of 0, so a cap check running first would
        # refuse every one of them citing a budget rule and the trail would never
        # say that authentication was the problem.
        if (
            request.engine is Engine.MANDATE_RECOVERY
            and request.proposed_action is Action.ATTEMPT_CHARGE
        ):
            refusal = self._mandate_debit_preconditions(request)
            if refusal is not None:
                return refusal

        # 3. Attempt caps. The effective cap is the strictest applicable bound.
        cap, cap_rule = self._effective_cap(request, limits, spec)
        attempts_remaining = max(cap - entity.attempt_count, 0)
        if attempts_remaining <= 0:
            return _deny(
                rule_id=cap_rule,
                reason_code="ATTEMPT_BUDGET_EXHAUSTED",
                rationale=(
                    f"{entity.attempt_count} attempts made against a permitted {cap}. The "
                    "budget is spent, so the entity is handed to a human rather than "
                    "silently abandoned (policy-bounds:HE2)."
                ),
                action=Action.ESCALATE,
                outcome=Outcome.ESCALATED,
                attempts_remaining=0,
                requires_human=True,
            )

        # 4. Cooldown / escalating backoff.
        earliest = self._earliest_next_attempt(request, limits)
        if earliest is not None and request.now < earliest:
            return _deny(
                rule_id=limits.cooldown_rule,
                reason_code="COOLDOWN_NOT_ELAPSED",
                rationale=(
                    f"Last attempt was at {entity.last_attempt_at:%Y-%m-%d %H:%M} UTC; "
                    f"the next is not permitted before {earliest:%Y-%m-%d %H:%M} UTC "
                    "under escalating backoff."
                ),
                attempts_remaining=attempts_remaining,
                earliest_next_attempt_at=earliest,
            )

        # 5. Communication limits — outreach only.
        if request.is_outreach:
            # 5a. The recipient cap (policy-bounds:RL3), checked before the
            #     per-entity one: a customer at their weekly limit is at it
            #     whatever this particular invoice's ladder says.
            if request.customer_messages_in_window >= MAX_MESSAGES_PER_CUSTOMER:
                return _deny(
                    rule_id="policy-bounds:RL3",
                    reason_code="CUSTOMER_CONTACT_CAP_REACHED",
                    rationale=(
                        f"{request.customer_messages_in_window} messages already sent "
                        f"to this customer across all their invoices in the rolling "
                        f"{CONTACT_WINDOW.days}-day window (cap "
                        f"{MAX_MESSAGES_PER_CUSTOMER}). QH2 bounds the conversation; "
                        "this bounds the recipient — eight overdue invoices are not "
                        "eight licences to write."
                    ),
                    attempts_remaining=attempts_remaining,
                    requires_human=True,
                )

            if request.messages_sent_in_window >= MAX_MESSAGES_PER_WINDOW:
                return _deny(
                    rule_id="policy-bounds:QH2",
                    reason_code="CONTACT_CAP_REACHED",
                    rationale=(
                        f"{request.messages_sent_in_window} messages already sent to this "
                        f"entity in the rolling {CONTACT_WINDOW.days}-day window "
                        f"(cap {MAX_MESSAGES_PER_WINDOW}). Chasing further would be "
                        "harassment, not recovery."
                    ),
                    attempts_remaining=attempts_remaining,
                    requires_human=True,
                )
            if not _within_outreach_window(request.now):
                reopen = _next_outreach_open(request.now)
                return _deny(
                    rule_id="policy-bounds:QH1",
                    reason_code="QUIET_HOURS",
                    rationale=(
                        f"{request.now.astimezone(OUTREACH_TZ):%H:%M} IST is outside the "
                        f"{OUTREACH_OPEN_HOUR:02d}:00-{OUTREACH_CLOSE_HOUR:02d}:00 outreach "
                        "window. Rescheduled rather than dropped — the timing was wrong, "
                        "not the recovery."
                    ),
                    attempts_remaining=attempts_remaining,
                    earliest_next_attempt_at=reopen,
                )

        # 6. Nothing fired. Permitted, inside a stated envelope.
        #
        # A permitted reroute cites the corridor rule that let it through rather
        # than the generic one, so the trail names the specific bound the action
        # satisfied — RR2 is the rule doing the work in both directions.
        # A permitted action cites the specific rule that let it through where
        # one exists, rather than the generic permission. RR2 governs a reroute
        # in both directions; RL5 governs a receivables escalation in both
        # directions. "Which rule let this happen?" should have the same quality
        # of answer as "which rule stopped it?".
        permit_rule = "policy_engine:permitted"
        if request.proposed_action is Action.REROUTE_TRAFFIC:
            permit_rule = "corridor-detection:RR2"
        elif (
            request.engine is Engine.RECEIVABLES
            and request.proposed_action is Action.ESCALATE
        ):
            permit_rule = "policy-bounds:RL5"
        return _permit(
            action=request.proposed_action,
            rule_id=permit_rule,
            reason_code=spec.code.value if spec else "NO_STOPPING_RULE",
            rationale=(
                f"No stopping rule fired. {attempts_remaining} of {cap} attempts remain "
                f"under {cap_rule}."
            ),
            attempts_remaining=attempts_remaining,
            earliest_next_attempt_at=earliest,
            permitted_actions=self._envelope(request, spec),
        )

    def authorize(
        self, request: PolicyRequest, recommendation: LLMRecommendation
    ) -> PolicyDecision:
        """Apply a model recommendation *inside* the policy envelope.

        The ordering here is the entire safety story of the product, so it is
        built to be structurally impossible to invert:

        - The envelope is computed by `evaluate()` **before** the recommendation
          is looked at.
        - A denial is returned as a denial. The recommendation is recorded on it,
          never applied to it.
        - `authorize()` has no access to `_permit()`; the only allowed decision
          it can return is the one `evaluate()` already produced.

        A model can therefore narrow an envelope — pick a different permitted
        action, propose a *later* retry — and can never widen one.
        """
        base = self.evaluate(request)

        if not base.allowed:
            # The refusal stands, and it keeps citing the rule that actually
            # refused. Recording what the model wanted is the point: a trail
            # showing the gate overruling a recommendation is the evidence that
            # the gate is real — but the citation has to stay specific, or every
            # overruled denial in a batch cites the same generic authority rule
            # and "which rule stopped this?" stops having an answer.
            _require_rule("policy_engine:llm_authority.denied")
            return base.model_copy(
                update={
                    "authority_rule_id": "policy_engine:llm_authority.denied",
                    "rationale": (
                        f"{base.rationale} The reasoning layer recommended "
                        f"'{recommendation.action.value}'; a recommendation cannot "
                        f"authorise what {base.rule_id} forbids."
                    ),
                    "overridden_recommendation": recommendation.action.value,
                }
            )

        if recommendation.action not in base.permitted_actions:
            refusal = _deny(
                rule_id="policy_engine:llm_authority.out_of_envelope",
                reason_code=base.reason_code,
                rationale=(
                    f"The reasoning layer recommended '{recommendation.action.value}', "
                    f"which is outside the permitted envelope "
                    f"{sorted(a.value for a in base.permitted_actions)}. Refused."
                ),
                attempts_remaining=base.attempts_remaining,
                earliest_next_attempt_at=base.earliest_next_attempt_at,
            )
            # Here the authority rule *is* the refusing rule: nothing else
            # stopped the action, the recommendation itself was inadmissible.
            return refusal.model_copy(
                update={
                    "authority_rule_id": "policy_engine:llm_authority.out_of_envelope",
                    "overridden_recommendation": recommendation.action.value,
                }
            )

        # The recommendation is admissible. It may only make the decision
        # narrower: a later retry time is accepted, an earlier one is discarded.
        earliest = base.earliest_next_attempt_at
        proposed = recommendation.proposed_next_attempt_at
        if proposed is not None and (earliest is None or proposed > earliest):
            earliest = proposed

        return base.model_copy(
            update={
                "action": recommendation.action,
                "rationale": recommendation.rationale,
                "earliest_next_attempt_at": earliest,
            }
        )

    # --- internals ---------------------------------------------------------

    def _mandate_debit_preconditions(self, request: PolicyRequest) -> PolicyDecision | None:
        """The `rbi-mandate-rules` gate every mandate debit passes through.

        Returns a denial, or `None` when every precondition holds. Revocation
        (A4) is not here — it is checked first, in `evaluate()`, because it
        short-circuits every other consideration including these.

        Each branch fails **closed**: `None` on a precondition means "not
        established", which denies exactly as `False` does. A debit permitted
        because nobody supplied the notice timestamp would be the whole gate
        quietly turned off.
        """
        entity = request.entity

        # 1. Mandate is active. (Part B, paused)
        if request.mandate_paused:
            return _deny(
                rule_id="rbi-mandate-rules:PartB.paused",
                reason_code="MANDATE_PAUSED",
                rationale=(
                    f"Mandate {entity.entity_id} is paused. A pause is a customer "
                    "instruction to stop collecting, so no debit fires — but it is "
                    "reversible, so the schedule is suspended rather than cancelled."
                ),
                attempts_remaining=0,
            )

        # 2. Pre-debit notice was sent, and the timing is right. (A2)
        lead = request.notice_lead_hours
        if lead is None or lead < NOTICE_MIN_LEAD_HOURS:
            stated = "no notice was sent" if lead is None else f"only {lead:.1f}h ahead"
            return _deny(
                rule_id="rbi-mandate-rules:A2",
                reason_code=DeclineCode.PRE_DEBIT_NOTICE_MISSING.value,
                rationale=(
                    f"A pre-debit notification must reach the customer "
                    f"{NOTICE_MIN_LEAD_HOURS:.0f}-{NOTICE_MAX_LEAD_HOURS:.0f}h before "
                    f"every debit, retries included; {stated}. Send the notice and "
                    "reschedule the debit beyond the window — never attempt against "
                    "a notice that was not given."
                ),
                attempts_remaining=None,
            )
        if lead > NOTICE_MAX_LEAD_HOURS:
            return _deny(
                rule_id="rbi-mandate-rules:PartB.notice_staleness",
                reason_code=DeclineCode.PRE_DEBIT_NOTICE_MISSING.value,
                rationale=(
                    f"The pre-debit notice went out {lead:.1f}h ahead of this debit, "
                    f"past the {NOTICE_MAX_LEAD_HOURS:.0f}h staleness ceiling. A notice "
                    "that old is not the notice A2 requires — re-notify, then debit."
                ),
                attempts_remaining=None,
            )

        # 3. Amount is within the registered mandate cap. (Part B, mandate_cap)
        cap_paise = request.mandate_cap_paise
        if cap_paise is None:
            return _deny(
                rule_id="rbi-mandate-rules:PartB.mandate_cap",
                reason_code="MANDATE_CAP_UNKNOWN",
                rationale=(
                    f"The registered cap for mandate {entity.entity_id} could not be "
                    "established, so there is nothing to check this debit against. "
                    "Failing closed."
                ),
                requires_human=True,
            )
        if entity.amount_paise > cap_paise:
            return _deny(
                rule_id="rbi-mandate-rules:PartB.mandate_cap",
                reason_code="MANDATE_CAP_EXCEEDED",
                rationale=(
                    f"{_rupees(entity.amount_paise)} is above the "
                    f"{_rupees(cap_paise)} the customer authorised at registration. "
                    "A debit outside the mandate is not covered by what they agreed "
                    "to, so it is refused rather than attempted."
                ),
                requires_human=True,
            )

        # 4. AFA status matches the amount. (A1, then A3/A5)
        if request.afa_registered is not True:
            stated = "never completed" if request.afa_registered is False else "not established"
            return _deny(
                rule_id="rbi-mandate-rules:A1",
                reason_code=DeclineCode.AFA_REQUIRED.value,
                rationale=(
                    f"One-time AFA registration for mandate {entity.entity_id} is "
                    f"{stated}. Until registration succeeds there is no mandate to "
                    "debit against, and every retry reproduces the same decline."
                ),
                attempts_remaining=0,
            )
        if (
            request.decline_code is DeclineCode.AFA_REQUIRED
            and not request.fresh_afa_completed
        ):
            # More specific than A3, and the reason this branch exists at all: the
            # issuer already asked for authentication we did not supply. A3 says
            # when fresh AFA is *required*; A5 says what retrying without it costs.
            return _deny(
                rule_id="rbi-mandate-rules:A5",
                reason_code=DeclineCode.AFA_REQUIRED.value,
                rationale=(
                    f"The last debit on mandate {entity.entity_id} was declined for "
                    "missing AFA and no authentication has happened since. Retrying "
                    "reproduces the identical decline forever, so the budget is spent "
                    "for a guaranteed-zero return. Route to customer authentication."
                ),
                attempts_remaining=0,
            )
        if (
            exceeds_threshold("afa_fresh_auth", entity.amount_paise)
            and not request.fresh_afa_completed
        ):
            return _deny(
                rule_id="rbi-mandate-rules:A3",
                reason_code=DeclineCode.AFA_REQUIRED.value,
                rationale=(
                    f"{_rupees(entity.amount_paise)} is above the "
                    f"{_rupees(THRESHOLDS['afa_fresh_auth'].value_paise)} per-transaction "
                    "threshold, so this cycle needs fresh AFA. It cannot be silently "
                    "auto-debited; the customer has to authenticate first (A5: a "
                    "silent retry reproduces this decline forever)."
                ),
                attempts_remaining=0,
            )

        return None

    def _effective_cap(
        self,
        request: PolicyRequest,
        limits: EngineLimits,
        spec: DeclineSpec | None,
    ) -> tuple[int, str]:
        """The strictest applicable attempt cap, and the rule that set it.

        Three bounds compete: the hard ceiling (AC1), the engine's own cap, and
        the decline class's retry budget. The smallest wins — which is why an
        override above the ceiling is clamped rather than obeyed (AC2).
        """
        candidates: list[tuple[int, str]] = [
            (HARD_ATTEMPT_CEILING, "policy-bounds:AC1"),
            (limits.max_attempts, limits.max_attempts_rule),
        ]
        if request.max_attempts_override is not None:
            if request.max_attempts_override > HARD_ATTEMPT_CEILING:
                # Clamped, not honoured, not an error. AC2.
                candidates.append((HARD_ATTEMPT_CEILING, "policy-bounds:AC2"))
            else:
                candidates.append((request.max_attempts_override, "policy-bounds:AC2"))
        if spec is not None:
            candidates.append(
                (RETRY_BUDGET[spec.decline_class], BUDGET_RULE_ID[spec.decline_class])
            )
        return min(candidates, key=lambda c: c[0])

    def _earliest_next_attempt(
        self, request: PolicyRequest, limits: EngineLimits
    ) -> datetime | None:
        """When the next attempt becomes permissible under CD1/CD2.

        Returns None when there is no previous attempt to space away from — a
        first attempt is not waiting on anything.
        """
        last = request.entity.last_attempt_at
        if last is None:
            return None
        return last + _backoff(request.entity.attempt_count, limits.min_cooldown)

    def _envelope(self, request: PolicyRequest, spec: DeclineSpec | None) -> frozenset[Action]:
        """Which actions a recommendation may choose between.

        Always includes the proposed action plus the three exits that are never
        forbidden — escalating to a human, stopping the schedule, and simply not
        acting. A model is always allowed to choose to do *less*, and Engine 3's
        reply understanding needs that explicitly: "this reply says pause, do not
        send the reminder" is the correct answer to a credible promise, and it
        should be admissible rather than refused as out-of-envelope.
        """
        envelope = {
            request.proposed_action,
            Action.ESCALATE,
            Action.HALT_SCHEDULE,
            Action.BLOCK_ATTEMPT,
        }
        if spec is not None:
            if spec.retryability is Retryability.YES:
                envelope.add(Action.SCHEDULE_RETRY)
            if spec.retryability in {Retryability.CUSTOMER_PRESENT, Retryability.NO}:
                # A dead or customer-blocked instrument cannot be silently
                # re-charged, but it can still be dunned for a new one.
                envelope.add(Action.SEND_DUNNING)
        return frozenset(envelope)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Enough doublings to reach MAX_COOLDOWN from any sane floor, and no more.
_MAX_DOUBLINGS = 16


def _backoff(attempt_count: int, floor: timedelta) -> timedelta:
    """Escalating backoff per policy-bounds CD2, floored by the engine's CD1.

    Repeated failure is evidence the cause is not transient, so each attempt
    waits longer — capped, so a schedule never runs past a working week.
    """
    if attempt_count <= 0:
        return floor
    # The exponent is clamped before it is used. Attempt counts are capped at 4
    # by AC1, but a corrupted counter should produce the ceiling, not an
    # overflow that takes the batch down.
    doublings = min(attempt_count - 1, _MAX_DOUBLINGS)
    wait = floor * (2**doublings)
    return min(max(wait, floor), MAX_COOLDOWN)


def _within_outreach_window(now: datetime) -> bool:
    """policy-bounds QH1, evaluated in the payer's timezone."""
    local = now.astimezone(OUTREACH_TZ)
    return OUTREACH_OPEN_HOUR <= local.hour < OUTREACH_CLOSE_HOUR


def _next_outreach_open(now: datetime) -> datetime:
    """The next moment outreach becomes permissible, in UTC."""
    local = now.astimezone(OUTREACH_TZ)
    opening = local.replace(hour=OUTREACH_OPEN_HOUR, minute=0, second=0, microsecond=0)
    if local.hour >= OUTREACH_CLOSE_HOUR:
        opening = opening + timedelta(days=1)
    return opening.astimezone(UTC)


def _rupees(paise: int) -> str:
    """Display only. Money stays integer paise everywhere else."""
    return f"Rs {paise / 100:,.2f}"


def registered_rules() -> list[PolicyRule]:
    """Every rule the engine can cite — used by the API and by `/audit-check`."""
    return sorted(RULE_REGISTRY.values(), key=lambda r: (r.category, r.rule_id))
