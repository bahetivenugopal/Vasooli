"""Declarative base for all Vasooli ORM models."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class every ORM model inherits from.

    Model modules live in `app/models/` and must be imported before
    `Base.metadata.create_all()` runs, or their tables will not be created.
    """
