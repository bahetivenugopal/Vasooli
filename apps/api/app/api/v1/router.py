"""v1 API router.

Engine routers are mounted here as each phase lands. Each router carries its own
prefix, so mounting one is a single line and nothing else in the app moves:

    api_router.include_router(receivables.router)
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    audit,
    health,
    mandate_recovery,
    overview,
    receivables,
    root_cause,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(audit.router)
api_router.include_router(overview.router)
api_router.include_router(root_cause.router)
api_router.include_router(mandate_recovery.router)
api_router.include_router(receivables.router)
