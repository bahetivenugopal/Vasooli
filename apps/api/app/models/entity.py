"""The recoverable entity — what the three engines have in common.

A failed payment, a failed mandate debit and an overdue invoice are different
things, but recovery treats them identically: there is an amount at risk, a
bounded number of attempts, a last attempt, and a terminal flag that ends it.

Only genuinely shared fields live here. Payments, mandates and invoices get
their own models in their own phases — pulling engine specifics up into this
class is how a "shared" core turns into a union of three special cases.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import UTCDateTime
from app.models.enums import EntityType


class RecoverableEntityMixin:
    """SQLAlchemy mixin for any entity a recovery engine can act on."""

    entity_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # Terminal means terminal: no code path may schedule further action on it.
    # The policy engine's hard-stop rules key off this flag.
    is_terminal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    terminal_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RecoverableEntity(BaseModel):
    """The read/transport shape of the same concept.

    The policy engine takes this rather than an ORM object, so a decision can be
    evaluated against a hypothetical state — which is what makes the rules
    testable without a database.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: str
    entity_type: EntityType
    amount_paise: int = Field(ge=0, description="Integer paise. Never a float.")
    currency: str = "INR"
    status: str = "open"
    attempt_count: int = Field(default=0, ge=0)
    last_attempt_at: datetime | None = None
    is_terminal: bool = False
    terminal_reason: str | None = None
