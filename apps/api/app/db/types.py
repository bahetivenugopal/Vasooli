"""Column types that keep the repo's two data invariants true in the database.

SQLite stores no timezone, so a naive datetime written today comes back naive
and silently becomes "local time" to whatever reads it. `UTCDateTime` closes
that hole: everything crossing the boundary is tz-aware UTC, per `CLAUDE.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, TypeDecorator


class UTCDateTime(TypeDecorator):
    """A datetime column that is tz-aware UTC on the way in and on the way out.

    Naive values are rejected rather than assumed: guessing that a naive
    timestamp meant UTC is how a retry ends up scheduled five and a half hours
    early.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime rejected — timestamps are tz-aware UTC everywhere "
                "(see CLAUDE.md). Use datetime.now(UTC)."
            )
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    """The one way this codebase asks for the current time."""
    return datetime.now(UTC)
