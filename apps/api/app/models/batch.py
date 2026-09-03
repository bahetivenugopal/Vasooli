"""The batch run — the unit of reproducibility.

Every reported recovery number travels with its `batch_id` and its `seed`. That
is what makes a figure verifiable rather than cherry-picked, and it is why the
seed is stored next to the metrics rather than left in a shell history.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, utcnow
from app.models.enums import BatchStatus, Engine


class BatchRun(Base):
    """One reproducible run of one engine (or of the shared core)."""

    __tablename__ = "batch_runs"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=BatchStatus.RUNNING, nullable=False)

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # Snapshot of the summary at completion. Convenience for the dashboard only —
    # the authoritative figures are always recomputed from the audit entries, so
    # a stale snapshot can never become the number anyone reports.
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    notes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BatchRun {self.batch_id} {self.engine} {self.status} seed={self.seed}>"


class BatchRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    batch_id: str
    engine: Engine
    seed: int
    status: BatchStatus
    started_at: datetime
    completed_at: datetime | None = None
    summary: dict[str, Any] | None = None
    notes: dict[str, Any] | None = None
