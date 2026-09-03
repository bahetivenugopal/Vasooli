"""Persisted Engine 1 artefacts: corridor detections and bounded reroutes.

Two tables, both deliberately thin. The audit trail remains the record of what
was *decided*; these hold the two things a decision needs to be inspectable
afterwards — the statistics behind a detection, and the live state of a reroute
that has not yet lapsed.

Nothing here is a metric source. Every reported figure is recomputed from
`audit_entries`, so a stale row in either of these tables can never become a
number anyone quotes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, utcnow


class CorridorDetection(Base):
    """One degradation episode, with its statistics and its diagnosis.

    The diagnosis columns are nullable because a detection exists before it is
    diagnosed: written first, resolved second, exactly like the audit entry it
    accompanies.
    """

    __tablename__ = "corridor_detections"

    detection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    corridor_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    corridor_level: Mapped[str] = mapped_column(String(48), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(16), nullable=True)
    method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    route_id: Mapped[str | None] = mapped_column(String(48), nullable=True)

    window_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    window_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_success_rate: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_success_rate: Mapped[float] = mapped_column(Float, nullable=False)
    p_value: Mapped[float] = mapped_column(Float, nullable=False)
    #: `1 - p_value`. Statistical, never blended with `model_confidence`.
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    affected_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    value_at_risk_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    decline_mix: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    distinct_failing_customers: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- diagnosis, filled in once the reasoning layer has run ---------------
    hypothesis: Mapped[str | None] = mapped_column(String(48), nullable=True)
    determination: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(String(48), nullable=True)
    #: Captured verbatim. The trail should show not just what happened but why
    #: the system believed it.
    diagnosis_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # --- what the policy engine decided about it ----------------------------
    authorised_action: Mapped[str | None] = mapped_column(String(48), nullable=True)
    authorising_rule: Mapped[str | None] = mapped_column(String(96), nullable=True)
    policy_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    overridden_recommendation: Mapped[str | None] = mapped_column(String(48), nullable=True)

    __table_args__ = (Index("ix_detection_batch_corridor", "batch_id", "corridor_key"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<CorridorDetection {self.detection_id} {self.corridor_key} "
            f"{self.observed_success_rate:.2f} vs {self.baseline_success_rate:.2f}>"
        )


class CorridorReroute(Base):
    """A bounded, expiring reroute directive.

    `expires_at` is not optional and is set at authorisation time from
    `corridor-detection:RR1`. There is no code path in this repository that
    creates a reroute without one — a reroute that never lapses is a permanent
    configuration change wearing a recovery action's clothes.
    """

    __tablename__ = "corridor_reroutes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detection_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    corridor_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(24), nullable=False)
    from_route_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    to_route_id: Mapped[str] = mapped_column(String(48), nullable=False)

    effective_from: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    authorising_rule: Mapped[str] = mapped_column(String(96), nullable=False)
    #: The bound that set the expiry, kept beside the rule that authorised the
    #: action so both are visible on the dashboard without a join.
    expiry_rule: Mapped[str] = mapped_column(String(96), nullable=False)
    value_at_risk_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def is_active(self, now: datetime) -> bool:
        """Whether this reroute is in force at `now`. Expiry is not advisory."""
        return self.effective_from <= now < self.expires_at

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<CorridorReroute {self.corridor_key} -> {self.to_route_id} "
            f"until {self.expires_at:%Y-%m-%d %H:%M}>"
        )


# --- API read shapes -------------------------------------------------------


class CorridorDetectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    detection_id: str
    batch_id: str
    corridor_key: str
    corridor_level: str
    issuer: str | None
    method: str | None
    route_id: str | None
    window_start: datetime
    window_end: datetime
    window_attempts: int
    baseline_attempts: int
    observed_success_rate: float
    baseline_success_rate: float
    p_value: float
    confidence: float
    affected_attempts: int
    value_at_risk_paise: int
    decline_mix: dict[str, Any]
    distinct_failing_customers: int
    hypothesis: str | None
    determination: str | None
    recommended_action: str | None
    diagnosis_reasoning: str | None
    model_confidence: float | None
    provenance: dict[str, Any] | None
    authorised_action: str | None
    authorising_rule: str | None
    policy_allowed: bool | None
    overridden_recommendation: str | None


class CorridorRerouteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_id: str
    detection_id: str
    corridor_key: str
    method: str
    from_route_id: str | None
    to_route_id: str
    effective_from: datetime
    expires_at: datetime
    authorising_rule: str
    expiry_rule: str
    value_at_risk_paise: int
