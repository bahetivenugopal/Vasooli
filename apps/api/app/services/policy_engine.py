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
        # The ladder's final rung, aligned with the QH2 contact cap so the two
        # bounds cannot disagree about how many times a customer hears from us.
        max_attempts=MAX_MESSAGES_PER_WINDOW,
        max_attempts_rule="policy-bounds:QH3",
        min_cooldown=BASE_COOLDOWN,
        cooldown_rule="policy-bounds:CD1",
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
        permit_rule = (
            "corridor-detection:RR2"
            if request.proposed_action is Action.REROUTE_TRAFFIC
            else "policy_engine:permitted"
        )
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

        Always includes the proposed action plus the two exits that are never
        forbidden — escalating to a human, and stopping. A model is always
        allowed to choose to do *less*.
        """
        envelope = {request.proposed_action, Action.ESCALATE, Action.HALT_SCHEDULE}
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
