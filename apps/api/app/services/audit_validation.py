"""`/audit-check`, as code rather than as a checklist somebody walks.

The command file in `.claude/commands/audit-check.md` specifies twelve
validations over a batch's audit trail. Until now they were performed by reading
the trail and checking by hand, which is fine once and unreliable every time
after — a validation that is skipped silently is worse than one that fails
loudly, and a hand-walked checklist has no way to say which of the twelve it
skipped.

So the twelve live here, executable, and three callers share them: the
`scripts/audit_check.py` CLI, the metric-integrity audit in
`services/integrity.py`, and the test suite. One implementation means the
command, the consolidated report and the tests cannot disagree about whether a
run is clean.

**This module never mutates anything.** It reads `audit_entries` and reports. A
violation it finds is a defect to fix in a separate change, exactly as the
command file says.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditEntry
from app.models.enums import (
    Action,
    DeclineClass,
    Engine,
    EntityType,
    Outcome,
    ProvenanceSource,
)
from app.services.decline_taxonomy import TAXONOMY, UNKNOWN_FAIL_SAFE_RULE
from app.services.policy_engine import RULE_REGISTRY
from app.services.razorpay_client import API_CALL_RULE

#: Citations that are real named rules but deliberately live outside
#: `RULE_REGISTRY`, which holds policy-engine rules only.
#:
#: Phase 5 recorded the reason this list exists rather than the taxonomy codes
#: being registered: `decline-taxonomy:SOFT.INSUFFICIENT_FUNDS` is the documented
#: citation format from `audit-schema` and is generated per code by
#: `decline_taxonomy.py`. Adding those to the policy registry would make the
#: registry claim to own rules it does not own.
_TAXONOMY_RULE_IDS: frozenset[str] = frozenset(
    {spec.rule_id for spec in TAXONOMY.values()}
    | {f"decline-taxonomy:budget.{cls.value}" for cls in DeclineClass}
    | {UNKNOWN_FAIL_SAFE_RULE}
)

_EXTERNAL_RULE_IDS: frozenset[str] = frozenset({API_CALL_RULE})

#: Actions that speak to a customer. Their budget is the contact cap (QH2/RL3),
#: never the debit budget — see `budget_kind`.
_OUTREACH_ACTIONS: frozenset[str] = frozenset(
    {
        Action.SEND_DUNNING.value,
        Action.SEND_REMINDER.value,
        Action.SEND_PRE_DEBIT_NOTICE.value,
    }
)

#: Key material must never reach the trail. Substrings, matched case-insensitively
#: across every text field. `rzp_live_` is here because a live key in the trail is
#: the single worst thing that could be in this repository.
_SECRET_MARKERS: tuple[str, ...] = (
    "rzp_live_",
    "rzp_test_",
    "key_secret",
    "api_key",
    "authorization:",
    "bearer ",
    "aizasy",  # Google API key prefix, lowercased
)


class Violation(BaseModel):
    """One failed validation, named by the check that caught it."""

    check: str
    entry_id: int | None
    entity_id: str | None
    detail: str

    def __str__(self) -> str:  # pragma: no cover - console rendering
        where = f"entry {self.entry_id}" if self.entry_id is not None else "batch"
        return f"{where} — {self.check} — {self.detail}"


class AuditCheckReport(BaseModel):
    """The result of running every validation over one or more batches."""

    batch_ids: list[str]
    entries: int
    passed: bool
    violations: list[Violation] = Field(default_factory=list)

    #: Citation -> count. The evidence that rules are cited, and which ones.
    rule_citations: dict[str, int] = Field(default_factory=dict)
    provenance: dict[str, int] = Field(default_factory=dict)
    prompt_versions: dict[str, int] = Field(default_factory=dict)
    outcomes: dict[str, int] = Field(default_factory=dict)
    actions: dict[str, int] = Field(default_factory=dict)

    #: Not a violation, but the command file is explicit that a batch with no
    #: refusals at all is suspicious rather than clean, and says to say so.
    suspicious: list[str] = Field(default_factory=list)

    @property
    def violations_by_check(self) -> dict[str, int]:
        return dict(Counter(v.check for v in self.violations))


def rule_exists(citation: str) -> bool:
    """Does this citation resolve to a rule that actually exists?

    Validation 4. A citation pointing at nothing is worse than no citation,
    because it looks like rigour.
    """
    return (
        citation in RULE_REGISTRY
        or citation in _TAXONOMY_RULE_IDS
        or citation in _EXTERNAL_RULE_IDS
    )


def load_entries(db: Session, batch_ids: list[str]) -> list[AuditEntry]:
    """Every entry across the given batches, in timeline order."""
    if not batch_ids:
        return []
    rows = list(db.scalars(select(AuditEntry).where(AuditEntry.batch_id.in_(batch_ids))))
    rows.sort(key=lambda e: (e.timestamp, e.id))
    return rows


def check_audit(db: Session, batch_ids: list[str]) -> AuditCheckReport:
    """Run all twelve validations over the given batches."""
    entries = load_entries(db, batch_ids)
    violations: list[Violation] = []

    for entry in entries:
        violations.extend(_check_entry(entry))

    violations.extend(_check_timelines(entries))
    violations.extend(_check_bounds(entries))
    violations.extend(_check_terminality(entries))

    return AuditCheckReport(
        batch_ids=list(batch_ids),
        entries=len(entries),
        passed=not violations,
        violations=violations,
        rule_citations=dict(
            sorted(Counter(e.authorising_rule for e in entries).items())
        ),
        provenance=_provenance_counts(entries),
        prompt_versions=dict(
            sorted(
                Counter(
                    str(e.provenance.get("prompt_version"))
                    for e in entries
                    if e.provenance.get("prompt_version")
                ).items()
            )
        ),
        outcomes=dict(sorted(Counter(e.outcome for e in entries).items())),
        actions=dict(sorted(Counter(e.action for e in entries).items())),
        suspicious=_suspicions(entries),
    )


# ---------------------------------------------------------------------------
# Per-entry validations (1-9)
# ---------------------------------------------------------------------------


def _check_entry(entry: AuditEntry) -> list[Violation]:
    out: list[Violation] = []

    def fail(check: str, detail: str) -> None:
        out.append(
            Violation(
                check=check, entry_id=entry.id, entity_id=entry.entity_id, detail=detail
            )
        )

    # 1. Required fields present.
    for field in (
        "timestamp",
        "batch_id",
        "engine",
        "entity_type",
        "entity_id",
        "action",
        "outcome",
        "reason_code",
        "authorising_rule",
        "provenance",
        "rationale",
    ):
        value = getattr(entry, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            fail("required-fields", f"{field} is missing or blank")

    # 2. reason_code non-empty. (Its *vocabulary* is engine-specific — the
    #    taxonomy's codes plus each engine's policy reason codes — so the check
    #    is presence, and validation 4 is what makes the citation resolvable.)
    if not (entry.reason_code or "").strip():
        fail("reason-code", "reason_code is empty")

    # 3. authorising_rule matches <skill-or-module>:<rule-id>.
    citation = entry.authorising_rule or ""
    if ":" not in citation or citation.startswith(":") or citation.endswith(":"):
        fail("citation-format", f"{citation!r} is not <skill>:<rule-id>")
    # 4. The cited rule exists.
    elif not rule_exists(citation):
        fail("citation-resolves", f"{citation!r} resolves to no rule that exists")

    # 5. action and outcome are allowed values.
    if entry.action not in {a.value for a in Action}:
        fail("action-vocabulary", f"{entry.action!r} is not an audit-schema action")
    if entry.outcome not in {o.value for o in Outcome}:
        fail("outcome-vocabulary", f"{entry.outcome!r} is not an audit-schema outcome")
    if entry.engine not in {e.value for e in Engine}:
        fail("engine-vocabulary", f"{entry.engine!r} is not a known engine")
    if entry.entity_type not in {t.value for t in EntityType}:
        fail("entity-vocabulary", f"{entry.entity_type!r} is not a known entity type")

    # 6. Provenance complete and internally consistent.
    out.extend(_check_provenance(entry))

    # 7. Timestamps tz-aware UTC. (Ordering is checked per entity, below.)
    if entry.timestamp is None or entry.timestamp.tzinfo is None:
        fail("timestamp-aware", "timestamp is naive; UTC offset required")

    # 8. Money is integer paise.
    for field in ("amount_at_risk_paise", "amount_recovered_paise"):
        value = getattr(entry, field)
        if isinstance(value, bool) or not isinstance(value, int):
            fail("money-integer", f"{field} is {type(value).__name__}, not int")
        elif value < 0:
            fail("money-integer", f"{field} is negative ({value})")

    # 9. No secrets anywhere in the trail.
    haystack = " ".join(
        str(part)
        for part in (
            entry.rationale,
            entry.reason_code,
            entry.entity_id,
            entry.entry_metadata,
            entry.provenance,
        )
        if part
    ).lower()
    for marker in _SECRET_MARKERS:
        if marker in haystack:
            fail("no-secrets", f"text containing {marker!r} reached the trail")

    return out


def _check_provenance(entry: AuditEntry) -> list[Violation]:
    provenance: dict[str, Any] = entry.provenance or {}
    out: list[Violation] = []

    def fail(detail: str) -> None:
        out.append(
            Violation(
                check="provenance",
                entry_id=entry.id,
                entity_id=entry.entity_id,
                detail=detail,
            )
        )

    source = provenance.get("source")
    if source not in {s.value for s in ProvenanceSource}:
        fail(f"source is {source!r}, not model or deterministic")
        return out

    for field in ("provider", "cache_hit", "prompt_version", "abstained"):
        if field not in provenance:
            fail(f"{field} absent")

    if source == ProvenanceSource.DETERMINISTIC.value:
        for field in ("latency_ms", "tokens", "model", "prompt_version"):
            if provenance.get(field) is not None:
                fail(f"deterministic entry carries {field}={provenance[field]!r}")
    else:
        for field in ("model", "prompt_version"):
            if not provenance.get(field):
                fail(f"model entry has no {field}")
        if entry.model_confidence is None:
            fail("model entry has no model_confidence")

    if provenance.get("abstained"):
        if entry.action != Action.ESCALATE.value:
            fail(f"abstained entry has action {entry.action!r}, expected escalate")
        if entry.outcome != Outcome.ESCALATED.value:
            fail(f"abstained entry has outcome {entry.outcome!r}, expected escalated")

    return out


# ---------------------------------------------------------------------------
# Cross-entry validations (7 ordering, 11 bounds, 12 terminality)
# ---------------------------------------------------------------------------


def _by_entity(entries: list[AuditEntry]) -> dict[tuple[str, str], list[AuditEntry]]:
    """Entries grouped per entity **per batch**.

    Per batch matters: one dataset feeds many runs, so the same `entity_id`
    legitimately appears in every run of an engine. Grouping across batches would
    interleave three runs' timelines and report ordering violations that are
    artefacts of the grouping rather than defects in the trail.
    """
    grouped: dict[tuple[str, str], list[AuditEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.batch_id, entry.entity_id)].append(entry)
    return grouped


def _check_timelines(entries: list[AuditEntry]) -> list[Violation]:
    """Validation 7's second half — non-decreasing within an entity's timeline."""
    out: list[Violation] = []
    for (batch_id, entity_id), rows in _by_entity(entries).items():
        ordered = sorted(rows, key=lambda e: e.id)
        for previous, current in pairwise(ordered):
            if previous.timestamp is None or current.timestamp is None:
                continue
            if current.timestamp < previous.timestamp:
                out.append(
                    Violation(
                        check="timeline-order",
                        entry_id=current.id,
                        entity_id=entity_id,
                        detail=(
                            f"{current.action} at {current.timestamp.isoformat()} "
                            f"precedes entry {previous.id} ({previous.action}) at "
                            f"{previous.timestamp.isoformat()} in batch {batch_id}"
                        ),
                    )
                )
    return out


def budget_kind(entry: AuditEntry) -> str:
    """Which budget this entry's `attempts_remaining` is counting down.

    An entity has **two** attempt budgets, not one, and they share a column.
    Phase 4 found this the expensive way: `PolicyEngine.evaluate()` applies the
    attempt cap and the cooldown to whatever action is proposed, so a dunning
    message gated against the debit budget is refused for a charge the engine
    never wanted to make. The fix was `outreach_entity()`, which presents the
    *message* count as the entity's attempt count — and the consequence is that
    one entity's timeline legitimately shows `attempts_remaining` going 0, 3:
    the debit budget is spent, the outreach budget is untouched.

    Checking monotonicity across both would report that as a budget refilling,
    which it is not. So the budgets are separated here, using the marker each
    decision already carries: a charge-path decision records the
    `proposed_action` it was gating, a communication records its `kind`. Every
    entry in the trail that reports a budget carries exactly one of the two, and
    a test pins that.

    The underlying wrinkle — one column, two budgets — is real and is recorded in
    Phase 5's "what the shared core made awkward". This function does not hide
    it; it just refuses to mislabel it as a violation.
    """
    metadata = entry.entry_metadata or {}
    if entry.action in _OUTREACH_ACTIONS or metadata.get("kind"):
        return "outreach"
    return "charge"


def _check_bounds(entries: list[AuditEntry]) -> list[Violation]:
    """Validation 11 — `attempts_remaining` never negative, never increasing.

    Per entity **per budget** — see `budget_kind`.
    """
    out: list[Violation] = []
    grouped: dict[tuple[str, str, str], list[AuditEntry]] = defaultdict(list)
    for entry in entries:
        if entry.attempts_remaining is not None:
            grouped[(entry.batch_id, entry.entity_id, budget_kind(entry))].append(entry)

    for (_batch_id, entity_id, kind), rows in grouped.items():
        previous: int | None = None
        for entry in sorted(rows, key=lambda e: e.id):
            remaining = entry.attempts_remaining
            if remaining is None:  # pragma: no cover - filtered above
                continue
            if remaining < 0:
                out.append(
                    Violation(
                        check="attempt-budget",
                        entry_id=entry.id,
                        entity_id=entity_id,
                        detail=f"{kind} attempts_remaining is {remaining}",
                    )
                )
            if previous is not None and remaining > previous:
                out.append(
                    Violation(
                        check="attempt-budget",
                        entry_id=entry.id,
                        entity_id=entity_id,
                        detail=(
                            f"{kind} attempts_remaining rose from {previous} to "
                            f"{remaining} — a budget that refills is not a budget"
                        ),
                    )
                )
            previous = remaining
    return out


def _check_terminality(entries: list[AuditEntry]) -> list[Violation]:
    """Validation 12 — terminal means terminal.

    After a `halt_schedule`, no later `attempt_charge` for that entity. This is
    the `MANDATE_REVOKED` hard stop (`rbi-mandate-rules:A4`), checked across every
    code path rather than the obvious one.
    """
    out: list[Violation] = []
    for (_batch_id, entity_id), rows in _by_entity(entries).items():
        halted_at: int | None = None
        for entry in sorted(rows, key=lambda e: e.id):
            if entry.action == Action.HALT_SCHEDULE.value:
                halted_at = entry.id
            elif (
                halted_at is not None
                and entry.action == Action.ATTEMPT_CHARGE.value
                and entry.outcome
                in {Outcome.SUCCESS.value, Outcome.FAILURE.value, Outcome.PENDING.value}
            ):
                out.append(
                    Violation(
                        check="terminal-is-terminal",
                        entry_id=entry.id,
                        entity_id=entity_id,
                        detail=(
                            f"attempt_charge after halt_schedule (entry {halted_at}) — "
                            "a terminal entity was charged anyway"
                        ),
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def _provenance_counts(entries: list[AuditEntry]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for entry in entries:
        provenance = entry.provenance or {}
        source = str(provenance.get("source", "unknown"))
        counts[source] += 1
        if source == ProvenanceSource.MODEL.value:
            counts["model.cache_hit" if provenance.get("cache_hit") else "model.live"] += 1
        if provenance.get("abstained"):
            counts["abstained"] += 1
    return dict(sorted(counts.items()))


def _suspicions(entries: list[AuditEntry]) -> list[str]:
    """Things that are not violations but should be said out loud."""
    out: list[str] = []
    if not entries:
        return ["the batch has no entries at all — nothing was checked"]

    refusals = sum(
        1
        for e in entries
        if e.outcome in {Outcome.BLOCKED.value, Outcome.HALTED.value}
    )
    if refusals == 0:
        out.append(
            "zero blocked/halted entries across the run — either the batch has no "
            "edge cases or a gate is not firing"
        )

    return out


__all__ = [
    "AuditCheckReport",
    "Violation",
    "check_audit",
    "load_entries",
    "rule_exists",
]
