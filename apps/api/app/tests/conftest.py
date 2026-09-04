"""Shared fixtures.

Two rules hold across this whole suite:

- **No live network calls.** The provider is always mocked; the Razorpay client
  always runs in simulated mode or against fixtures.
- **No shared database.** Each test gets its own in-memory SQLite, so a test
  that writes audit entries cannot influence one that counts them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import (  # noqa: F401 - registers the tables
    AuditEntry,
    BatchRun,
    CorridorDetection,
    CorridorReroute,
    MandateCommunication,
    MandateRecoveryState,
)
from app.models.entity import RecoverableEntity
from app.models.enums import EntityType
from app.services.audit_trail import AuditTrail
from app.services.policy_engine import PolicyEngine

#: A fixed, tz-aware instant inside the outreach window (14:30 IST). Rules that
#: depend on wall-clock time are tested against explicit times, never `now()` —
#: a suite that passes only between 09:00 and 21:00 is not a suite.
NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)  # 14:30 IST


@pytest.fixture
def db() -> Iterator[Session]:
    """A private in-memory database per test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def trail(db: Session) -> AuditTrail:
    return AuditTrail(db)


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def entity() -> RecoverableEntity:
    """A plain, healthy entity: one attempt made, nothing terminal."""
    return RecoverableEntity(
        entity_id="pay_test_0001",
        entity_type=EntityType.PAYMENT,
        amount_paise=250_000,  # Rs 2,500
        attempt_count=0,
    )
