"""SQLAlchemy engine and session wiring.

SQLite by default — deliberately zero-setup for the hackathon. Swapping
`DATABASE_URL` is all that is needed to move to Postgres later.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

_database_url = settings.resolved_database_url
_is_sqlite = _database_url.startswith("sqlite")

engine = create_engine(
    _database_url,
    # SQLite's default thread check rejects the connection reuse FastAPI does
    # across its threadpool. Not needed for other backends.
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    echo=False,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session that is always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
