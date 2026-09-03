"""Engine 1 routes — thin views over the runner and what it wrote.

Everything here is a filter plus a call into a service. The one route that does
work, `POST /runs`, delegates the whole loop to `RootCauseRunner`: a route that
reimplemented any part of detect/diagnose/authorize would be a second decision
path with no audit trail behind it.

Reads come from the tables the run wrote, and the run summary is recomputed from
the audit entries rather than read from the stored snapshot — so a figure served
here and the trail behind it cannot disagree.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.engines.root_cause.config import default_config
from app.engines.root_cause.runner import RootCauseRunner
from app.engines.root_cause.schemas import RunSummary
from app.models.audit import AuditEntryRead
from app.models.batch import BatchRun, BatchRunRead
from app.models.enums import Engine
from app.models.root_cause import (
    CorridorDetection,
    CorridorDetectionRead,
    CorridorReroute,
    CorridorRerouteRead,
)
from app.services.audit_trail import AuditTrail

router = APIRouter(prefix="/root-cause", tags=["root-cause"])

DbDep = Annotated[Session, Depends(get_db)]


class RunRequest(BaseModel):
    """What to run, and against what.

    `now` is exposed because Phase 2's datasets are anchored to a fixed `as_of`
    that is not today. Left unset, the runner derives it from the last attempt in
    the dataset, which is the right default and the one the demo uses.
    """

    batch_id: str = Field(description="Id for this engine run. Not the dataset's batch id.")
    seed: int = 42
    dataset: str | None = Field(
        default=None, description="Path to a payments .jsonl. Defaults to the committed sample."
    )
    now: datetime | None = None
    score: bool = Field(
        default=True,
        description="Score detections against the generator's ground truth manifest.",
    )


@router.post("/runs", response_model=RunSummary)
def trigger_run(request: RunRequest, db: DbDep) -> RunSummary:
    """Run the full loop over a payments batch and return the honest summary."""
    if db.get(BatchRun, request.batch_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"batch {request.batch_id!r} already exists — reusing a batch id "
                "would merge two runs into one set of metrics"
            ),
        )
    try:
        artefacts = RootCauseRunner(db).run(
            batch_id=request.batch_id,
            seed=request.seed,
            dataset=request.dataset,
            now=request.now,
            score=request.score,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return artefacts.summary


@router.get("/runs", response_model=list[BatchRunRead])
def list_runs(
    db: DbDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[BatchRunRead]:
    """Every root-cause run, newest first. Each carries its seed."""
    return [
        BatchRunRead.model_validate(b)
        for b in AuditTrail(db).list_batches(engine=Engine.ROOT_CAUSE, limit=limit)
    ]


@router.get("/runs/{batch_id}", response_model=BatchRunRead)
def get_run(batch_id: str, db: DbDep) -> BatchRunRead:
    """One run's record, including the dataset batch id it consumed."""
    return BatchRunRead.model_validate(_require_run(batch_id, db))


@router.get("/runs/{batch_id}/detections", response_model=list[CorridorDetectionRead])
def list_detections(
    batch_id: str,
    db: DbDep,
    determination: str | None = None,
    allowed: bool | None = Query(
        default=None, description="Filter by whether the policy engine permitted the action."
    ),
) -> list[CorridorDetectionRead]:
    """Every corridor detection in a run, with its diagnosis and the rule that decided it."""
    _require_run(batch_id, db)
    stmt = (
        select(CorridorDetection)
        .where(CorridorDetection.batch_id == batch_id)
        .order_by(CorridorDetection.window_start.asc())
    )
    if determination is not None:
        stmt = stmt.where(CorridorDetection.determination == determination)
    if allowed is not None:
        stmt = stmt.where(CorridorDetection.policy_allowed == allowed)
    return [CorridorDetectionRead.model_validate(d) for d in db.scalars(stmt)]


@router.get("/detections/{detection_id}", response_model=CorridorDetectionRead)
def get_detection(detection_id: str, db: DbDep) -> CorridorDetectionRead:
    """One detection with its full diagnosis, reasoning and provenance."""
    row = db.get(CorridorDetection, detection_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no detection {detection_id}")
    return CorridorDetectionRead.model_validate(row)


@router.get("/runs/{batch_id}/actions", response_model=list[AuditEntryRead])
def list_actions(
    batch_id: str,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditEntryRead]:
    """Every action the run took or refused, each naming its authorising rule."""
    _require_run(batch_id, db)
    trail = AuditTrail(db)
    entries = trail.query(
        batch_id=batch_id, engine=Engine.ROOT_CAUSE, limit=limit, offset=offset
    )
    return [trail.read(e) for e in entries]


@router.get("/runs/{batch_id}/reroutes", response_model=list[CorridorRerouteRead])
def list_reroutes(
    batch_id: str,
    db: DbDep,
    active_at: datetime | None = Query(
        default=None,
        description="Return only reroutes in force at this instant. Expiry is not advisory.",
    ),
) -> list[CorridorRerouteRead]:
    """Reroutes the run authorised, each with the moment it lapses."""
    _require_run(batch_id, db)
    rows = list(
        db.scalars(select(CorridorReroute).where(CorridorReroute.batch_id == batch_id))
    )
    if active_at is not None:
        rows = [r for r in rows if r.is_active(active_at)]
    return [CorridorRerouteRead.model_validate(r) for r in rows]


@router.get("/config")
def get_config() -> dict[str, object]:
    """The corridor definition and thresholds this engine is running with.

    Exposed because "our detection thresholds are principled" should be checkable
    by anyone reading the API, not only by anyone reading the config file. Every
    value names the rule in the `corridor-detection` skill that argues for it.
    """
    return default_config().raw


def _require_run(batch_id: str, db: Session) -> BatchRun:
    run = db.get(BatchRun, batch_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {batch_id}")
    return run
