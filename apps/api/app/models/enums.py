"""Controlled vocabularies shared by every engine.

These enums are the contract between the audit trail, the policy engine and the
three engines. They come from the `audit-schema` and `decline-taxonomy` skills —
adding a value here means adding it to the skill first, not the other way round.
"""

from __future__ import annotations

from enum import StrEnum


class Engine(StrEnum):
    """Which engine acted. Mirrors `apps/api/app/engines/<name>/`."""

    ROOT_CAUSE = "root_cause"
    MANDATE_RECOVERY = "mandate_recovery"
    RECEIVABLES = "receivables"
    # The shared core itself acts before any engine exists — batch bookkeeping,
    # provider degradation notices. Kept distinct so per-engine metrics stay clean.
    CORE = "core"


class EntityType(StrEnum):
    """What was acted on. `audit-schema` -> Required fields."""

    PAYMENT = "payment"
    MANDATE = "mandate"
    INVOICE = "invoice"
    CORRIDOR = "corridor"
    BATCH = "batch"


class Action(StrEnum):
    """What was done, or refused. `audit-schema` -> `action` values.

    `block_attempt` and `halt_schedule` are the compliance-evidence actions: a
    refusal recorded with the rule that refused it is the proof that the
    stopping rules are real, not decorative.
    """

    CLASSIFY_DECLINE = "classify_decline"
    DIAGNOSE_ROOT_CAUSE = "diagnose_root_cause"
    SCHEDULE_RETRY = "schedule_retry"
    ATTEMPT_CHARGE = "attempt_charge"
    SEND_PRE_DEBIT_NOTICE = "send_pre_debit_notice"
    REROUTE_TRAFFIC = "reroute_traffic"
    SEND_DUNNING = "send_dunning"
    SEND_REMINDER = "send_reminder"
    RECORD_PROMISE_TO_PAY = "record_promise_to_pay"
    ESCALATE = "escalate"
    BLOCK_ATTEMPT = "block_attempt"
    HALT_SCHEDULE = "halt_schedule"
    # Shared-core bookkeeping: an external API call and its result.
    API_CALL = "api_call"


class Outcome(StrEnum):
    """How it turned out. `audit-schema` -> `outcome` values."""

    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"
    SCHEDULED = "scheduled"
    SKIPPED = "skipped"
    PENDING = "pending"
    ESCALATED = "escalated"
    HALTED = "halted"


#: Outcomes that mean the action is finished. `PENDING` is the only one the
#: audit trail is allowed to move away from — see `audit_trail.resolve()`.
TERMINAL_OUTCOMES: frozenset[Outcome] = frozenset(
    o for o in Outcome if o is not Outcome.PENDING
)


class ProvenanceSource(StrEnum):
    """Was this judgment *reasoned* or *ruled*?

    There is no third value. A lookup-table decision with no model involved is
    `deterministic`, because it was ruled. See `audit-schema` -> Provenance.
    """

    MODEL = "model"
    DETERMINISTIC = "deterministic"


class DeclineClass(StrEnum):
    """`decline-taxonomy` classes. Retry budgets attach to these, not to codes."""

    SOFT = "SOFT"
    HARD = "HARD"
    TERMINAL_MANDATE = "TERMINAL_MANDATE"
    AMBIGUOUS = "AMBIGUOUS"
    POLICY_BLOCK = "POLICY_BLOCK"
    UNKNOWN = "UNKNOWN"


class CorridorDetermination(StrEnum):
    """Engine 1's central judgment: whose problem is this?

    The same decline code means opposite things depending on this answer, which
    is why it is a first-class value rather than a boolean. `INSUFFICIENT_EVIDENCE`
    is deliberately one of the three: a diagnosis layer that always produces a
    confident answer is one that will confidently be wrong, and abstention here
    routes the corridor to a human under `policy-bounds:HE1`.
    """

    SYSTEMIC = "systemic"
    INDIVIDUAL = "individual"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EscalationTrigger(StrEnum):
    """Why an invoice left the ladder. Engine 3's `policy-bounds:RL5` evidence.

    The rule is that escalation ascends on evidence and never on a timer, which
    is only checkable if "what was the evidence?" is a value rather than a
    narrative. `None` on an escalation request is therefore not a missing field —
    it is the refusal case.
    """

    #: A commitment passed its date plus the PP1 grace with no payment.
    BROKEN_PROMISE = "broken_promise"
    #: Every rung of the RL1 ladder has been spent.
    LADDER_EXHAUSTED = "ladder_exhausted"
    #: A reply was classified as a dispute (RL4). Chasing stops, a human takes it.
    DISPUTE = "dispute"
    #: A reasoning task declined to answer (HE1).
    ABSTENTION = "abstention"
    #: Above the `high_value_review` threshold (HE3).
    HIGH_VALUE = "high_value"


class BatchStatus(StrEnum):
    """Lifecycle of one reproducible batch run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RuleSource(StrEnum):
    """Where a policy rule's authority comes from.

    Every rule in the registry cites one of these. `PRODUCT_DECISION` is a
    deliberate escape hatch, but it still requires a named entry in the
    `policy-bounds` skill — it is not a licence to invent a number inline.
    """

    DECLINE_TAXONOMY = "decline-taxonomy"
    RBI_MANDATE_RULES = "rbi-mandate-rules"
    POLICY_BOUNDS = "policy-bounds"
    CORRIDOR_DETECTION = "corridor-detection"
    PRODUCT_DECISION = "product-decision"
