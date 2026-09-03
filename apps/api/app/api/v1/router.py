"""v1 API router.

Engine routers are mounted here as each phase lands:

    api_router.include_router(root_cause.router, prefix="/root-cause")
    api_router.include_router(mandate_recovery.router, prefix="/mandate-recovery")
    api_router.include_router(receivables.router, prefix="/receivables")
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import audit, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(audit.router)
