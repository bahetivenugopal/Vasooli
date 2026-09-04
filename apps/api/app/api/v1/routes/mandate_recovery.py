"""Engine 2 routes — thin views over the runner and what it wrote.

Every route is a filter plus a call into a service. `POST /runs` delegates the
whole loop to `MandateRunner`: a route that reimplemented any part of
classify/schedule/authorize would be a second decision path with no audit trail
behind it.

The route the demo leans on is `GET /runs/{batch_id}/mandates/{mandate_id}` —
one mandate's complete history, in order, with the rule behind every step and
the text of every message. It is built to be read aloud, so it returns the
narrative rather than three joins the caller has to assemble.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.engines.mandate_recovery.config import default_config
from app.engines.mandate_recovery.runner import MandateRunner
from app.engines.mandate_recovery.schemas import RunSummary
from app.models.audit import AuditEntryRead
from app.models.batch import BatchRun, BatchRunRead
from app.models.enums import Engine, EntityType
from app.models.mandate import (
    MandateCommunication,
    MandateCommunicationRead,
    MandateRecoveryState,
    MandateRecoveryStateRead,
)
from app.services.audit_trail import AuditTrail

router = APIRouter(prefix="/mandate-recovery", tags=["mandate-recovery"])

DbDep = Annotated[Session, Depends(get_db)]


class RunRequest(BaseModel):
    """What to run, and against what.

    `now` is exposed because the mandate book is anchored to a fixed `as_of` that
    is not today, and because the outreach window makes the run clock genuinely
    load-bearing: a batch executed at 03:00 IST correctly holds every customer
    message, which is right and looks wrong. Left unset, the runner derives the
    clock from the latest debit actually attempted in the book.
    """

    batch_id: str = Field(description="Id for this engine run. Not the dataset's batch id.")
    seed: int = 42
    dataset: str | None = Field(
        default=None, description="Path to a mandates .jsonl. Defaults to the committed sample."
    )
    now: datetime | None = None


class MandateTimeline(BaseModel):
    """One mandate's complete history — the route the demo leans on."""

    mandate_id: str
    batch_id: str
    state: MandateRecoveryStateRead
    #: Every decision about this mandate, oldest first, each naming its rule.
    entries: list[AuditEntryRead]
    #: Every message the run drafted for it, in full. None were dispatched.
    communications: list[MandateCommunicationRead]
    #: The one-sentence answer to "what happens next, when, and why".
    next_step_summary: str


@router.post("/runs", response_model=RunSummary)
def trigger_run(request: RunRequest, db: DbDep) -> RunSummary:
    """Run the full loop over a mandate book and return the honest summary."""
    if db.get(BatchRun, request.batch_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"batch {request.batch_id!r} already exists — reusing a batch id "
                "would merge two runs into one set of metrics"
            ),
        )
    try:
        artefacts = MandateRunner(db).run(
            batch_id=request.batch_id,
            seed=request.seed,
            dataset=request.dataset,
            now=request.now,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return artefacts.summary


@router.get("/runs", response_model=list[BatchRunRead])
def list_runs(db: DbDep, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[BatchRunRead]:
    """Every mandate-recovery run, newest first. Each carries its seed."""
    return [
        BatchRunRead.model_validate(b)
        for b in AuditTrail(db).list_batches(engine=Engine.MANDATE_RECOVERY, limit=limit)
    ]


@router.get("/runs/{batch_id}", response_model=BatchRunRead)
def get_run(batch_id: str, db: DbDep) -> BatchRunRead:
    """One run's record, including the dataset batch id it consumed."""
    return BatchRunRead.model_validate(_require_run(batch_id, db))


@router.get("/runs/{batch_id}/mandates", response_model=list[MandateRecoveryStateRead])
def list_mandates(
    batch_id: str,
    db: DbDep,
    route: str | None = Query(default=None, description="Filter by derived failure route."),
    next_step: str | None = Query(default=None, description="Filter by the decided next step."),
    afa_side: str | None = Query(
        default=None, description="`above` or `at_or_below` the Rs 15,000 AFA threshold."
    ),
    compliance_blocked: bool | None = Query(
        default=None, description="Only mandates a compliance gate refused."
    ),
) -> list[MandateRecoveryStateRead]:
    """Every mandate in a run with its current recovery state and next step."""
    _require_run(batch_id, db)
    stmt = (
        select(MandateRecoveryState)
        .where(MandateRecoveryState.batch_id == batch_id)
        .order_by(MandateRecoveryState.mandate_id.asc())
    )
    if route is not None:
        stmt = stmt.where(MandateRecoveryState.failure_route == route)
    if next_step is not None:
        stmt = stmt.where(MandateRecoveryState.next_step == next_step)
    if afa_side is not None:
        stmt = stmt.where(MandateRecoveryState.afa_side == afa_side)
    if compliance_blocked is not None:
        stmt = stmt.where(MandateRecoveryState.compliance_blocked == compliance_blocked)
    return [MandateRecoveryStateRead.model_validate(row) for row in db.scalars(stmt)]


@router.get("/runs/{batch_id}/mandates/{mandate_id}", response_model=MandateTimeline)
def get_mandate_timeline(batch_id: str, mandate_id: str, db: DbDep) -> MandateTimeline:
    """One mandate's full timeline: every attempt, decision, rule and message.

    Complete on purpose. If a judge picks a mandate and asks "why did it do that,
    and what stopped it going further?", the answer is this one response — not a
    reconstruction from three endpoints.
    """
    _require_run(batch_id, db)
    state = db.get(MandateRecoveryState, (batch_id, mandate_id))
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"no mandate {mandate_id} in run {batch_id}"
        )
    trail = AuditTrail(db)
    entries = [
        trail.read(e)
        for e in trail.entity_timeline(EntityType.MANDATE, mandate_id)
        if e.batch_id == batch_id
    ]
    communications = list(
        db.scalars(
            select(MandateCommunication)
            .where(MandateCommunication.batch_id == batch_id)
            .where(MandateCommunication.mandate_id == mandate_id)
            .order_by(MandateCommunication.id.asc())
        )
    )
    when = (
        f" on {state.scheduled_for:%Y-%m-%d %H:%M} UTC" if state.scheduled_for else ""
    )
    return MandateTimeline(
        mandate_id=mandate_id,
        batch_id=batch_id,
        state=MandateRecoveryStateRead.model_validate(state),
        entries=entries,
        communications=[MandateCommunicationRead.model_validate(c) for c in communications],
        next_step_summary=(
            f"{state.next_step}{when}, authorised by {state.authorising_rule}. "
            f"{state.explanation}"
        ),
    )


@router.get("/runs/{batch_id}/communications", response_model=list[MandateCommunicationRead])
def list_communications(
    batch_id: str,
    db: DbDep,
    status: str | None = Query(
        default=None,
        description="`marked_for_delivery`, `held` or `suppressed`. Never `sent`: nothing here dispatches.",
    ),
    kind: str | None = None,
) -> list[MandateCommunicationRead]:
    """Every message the run drafted. **None were sent** — see ADR 0009."""
    _require_run(batch_id, db)
    stmt = (
        select(MandateCommunication)
        .where(MandateCommunication.batch_id == batch_id)
        .order_by(MandateCommunication.id.asc())
    )
    if status is not None:
        stmt = stmt.where(MandateCommunication.status == status)
    if kind is not None:
        stmt = stmt.where(MandateCommunication.kind == kind)
    return [MandateCommunicationRead.model_validate(c) for c in db.scalars(stmt)]


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
        batch_id=batch_id, engine=Engine.MANDATE_RECOVERY, limit=limit, offset=offset
    )
    return [trail.read(e) for e in entries]


@router.get("/config")
def get_config() -> dict[str, Any]:
    """The compliance windows and bounds this engine runs with.

    Every value names the rule that argues for it, and says whether that rule is
    regulatory or a Vasooli product decision. The distinction is the point: a
    reader should be able to tell at a glance which numbers we chose.
    """
    return default_config().raw


def _require_run(batch_id: str, db: Session) -> BatchRun:
    run = db.get(BatchRun, batch_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {batch_id}")
    return run
