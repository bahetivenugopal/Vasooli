"""Vasooli API entrypoint.

One FastAPI app. The three engines are separated by folder, not by service —
splitting them into microservices would be premature complexity with no payoff
at this scale.

Run from `apps/api/`:

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.db.base import Base
from app.db.session import engine

# Imported for the side effect of registering ORM models on `Base` before
# `create_all()` runs. A table missing because its module was never imported
# surfaces as a baffling error much later.
from app.engines.mandate_recovery import register_mandate_tasks
from app.engines.root_cause import register_root_cause_tasks
from app.models import (  # noqa: F401
    AuditEntry,
    BatchRun,
    CorridorDetection,
    CorridorReroute,
    MandateCommunication,
    MandateRecoveryState,
)
from app.services.llm_agent import REGISTRY
from app.services.reasoning_tasks import register_core_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables, then prove every reasoning task has a way to degrade.

    The fallback check is deliberately a **startup** failure: discovering a
    missing fallback mid-demo, at the moment the quota runs out, is exactly the
    failure this design exists to prevent (ADR 0002).

    Table creation is fine for SQLite at hackathon scale. A real migration tool
    is the answer once the schema has to survive its own history — not now.
    """
    Base.metadata.create_all(bind=engine)
    register_core_tasks()
    register_root_cause_tasks()
    register_mandate_tasks()
    REGISTRY.verify_all_have_fallbacks()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="AI-first revenue recovery — one policy layer, one audit trail, three engines.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {
        "service": settings.app_name,
        "version": settings.version,
        "docs": "/docs",
        "health": "/api/v1/health",
    }
