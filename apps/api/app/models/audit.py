"""The audit entry — the single most important artifact in the project.

One table, one schema, all three engines. If a judge picks any action out of a
batch run and asks "why did it do that, and what stopped it going further?", the
answer comes out of this table in one query.

Schema source of truth: the `audit-schema` skill. This module and that file must
agree; refining one means refining both.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, utcnow
from app.models.enums import Action, Engine, EntityType, Outcome
from app.models.provenance import Provenance


class AuditEntry(Base):
    """One recorded decision.

    Append-only. The single permitted mutation is resolving a `pending` outcome,
    and only through `audit_trail.resolve_outcome()` — there is no generic
    update on the service, deliberately.
    """

    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    action: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    # The citation. `audit-schema` -> "Rule citation format": <skill>:<rule-id>.
    # An entry without one is a bug, not a gap — enforced in the service.
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    authorising_rule: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)

    # How the judgment was produced. Replaces the old `decision_source` field —
    # one field, not both. Shape enforced by the `Provenance` model on write.
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Money is integer paise, always. `at_risk` is what this action concerns;
    # `recovered` is what it actually brought back, and stays 0 until it does.
    amount_at_risk_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    amount_recovered_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Required whenever provenance.source is `model`. Describes the *answer*,
    # not how it was produced — which is why it is not inside provenance.
    model_confidence: Mapped[float | None] = mapped_column(nullable=True)

    # `metadata` is reserved on SQLAlchemy's declarative base, so the attribute
    # is renamed and the column keeps the name the skill specifies.
    entry_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSON, nullable=True
    )

    __table_args__ = (
        Index("ix_audit_batch_engine", "batch_id", "engine"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<AuditEntry id={self.id} batch={self.batch_id} {self.engine}"
            f" {self.action}->{self.outcome} rule={self.authorising_rule}>"
        )


# --- API / service schemas -------------------------------------------------


class AuditEntryRead(BaseModel):
    """Read shape for the API and the dashboard.

    `provenance` is re-validated on the way out, so a hand-edited row cannot
    present itself as something the provenance contract would have rejected.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    timestamp: datetime
    batch_id: str
    engine: Engine
    entity_type: EntityType
    entity_id: str
    action: Action
    outcome: Outcome
    reason_code: str
    authorising_rule: str
    rationale: str
    provenance: Provenance
    amount_at_risk_paise: int
    amount_recovered_paise: int
    currency: str
    attempt_number: int | None = None
    attempts_remaining: int | None = None
    model_confidence: float | None = None
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="entry_metadata")


class SourceBreakdown(BaseModel):
    """Per-source figures.

    Metrics are reported *per source*, never blended: a recovery rate where half
    the decisions came from static templates is not false, but as one figure it
    implies more than it delivers.
    """

    entries: int = 0
    amount_at_risk_paise: int = 0
    amount_recovered_paise: int = 0
    recovery_rate: float = 0.0


class BatchSummary(BaseModel):
    """The headline number, computed honestly from the log.

    Never from a separately maintained counter — a counter can drift from the
    trail it claims to summarise, and then the demo's headline figure and its
    evidence disagree.
    """

    batch_id: str
    entries: int
    amount_at_risk_paise: int
    amount_recovered_paise: int
    recovery_rate: float
    action_counts: dict[str, int]
    outcome_counts: dict[str, int]
    engine_counts: dict[str, int]
    by_source: dict[str, SourceBreakdown]
    #: Entries that failed the schema's own rules — a missing citation, a model
    #: result with no confidence. Should be zero; if it isn't, the trail says so.
    policy_violations: int
    #: Refusals. A run with none means the gate never fired, which is worth
    #: investigating rather than celebrating.
    blocked_count: int
    halted_count: int
    escalated_count: int
    abstained_count: int
