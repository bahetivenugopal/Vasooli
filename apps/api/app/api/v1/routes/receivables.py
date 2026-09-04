"""Engine 3 routes — thin views over the runner and what it wrote.

Every route is a filter plus a call into a service. `POST /runs` delegates the
whole loop to `ReceivablesRunner`: a route that reimplemented any part of
read/rank/gate would be a second decision path with no audit trail behind it.

The route the demo leans on is `GET /runs/{batch_id}/invoices/{invoice_id}`. It
is built to tell a complete story on its own — the reminders that went out, the
customer's reply verbatim, what the model read in it, the promise that came out,
what became of that promise, and the rule behind every step. If a judge picks one
invoice and asks "what happened here, and what stopped it going further?", the
answer is this one response, not a reconstruction from four endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.engines.receivables.config import default_config
from app.engines.receivables.runner import ReceivablesRunner
from app.engines.receivables.schemas import ExtractionScore, RunSummary
from app.models.audit import AuditEntryRead
from app.models.batch import BatchRun, BatchRunRead
from app.models.enums import Engine, EntityType
from app.models.receivables import (
    InvoiceChaseState,
    InvoiceChaseStateRead,
    InvoiceCommunication,
    InvoiceCommunicationRead,
    PromiseToPay,
    PromiseToPayRead,
)
from app.services.audit_trail import AuditTrail

router = APIRouter(prefix="/receivables", tags=["receivables"])

DbDep = Annotated[Session, Depends(get_db)]


class RunRequest(BaseModel):
    """What to run, and against what.

    `now` is exposed for the same reason Engine 2 exposes it, and it bites harder
    here: this engine is *entirely* outreach, so a batch executed outside the
    09:00-21:00 IST window correctly holds every single reminder. That is the
    rule working and it looks like a broken engine. Left unset, the runner
    derives the clock from the latest observed event in the ledger.
    """

    batch_id: str = Field(description="Id for this engine run. Not the dataset's batch id.")
    seed: int = 42
    dataset: str | None = Field(
        default=None, description="Path to an invoices .jsonl. Defaults to the committed sample."
    )
    now: datetime | None = None
    score: bool = Field(
        default=True,
        description=(
            "Compare extractions against the dataset's held-out annotations. "
            "Needs a manifest beside the .jsonl."
        ),
    )


class InvoiceTimeline(BaseModel):
    """One invoice's complete story — the route the demo leans on."""

    invoice_id: str
    batch_id: str
    state: InvoiceChaseStateRead
    #: Every decision about this invoice, oldest first, each naming its rule.
    entries: list[AuditEntryRead]
    #: Every commitment extracted from it, and what became of each.
    promises: list[PromiseToPayRead]
    #: Every message the run drafted for it, in full. None were dispatched.
    communications: list[InvoiceCommunicationRead]
    #: The customer's own words, so the timeline reads as a conversation.
    replies: list[dict[str, Any]]
    #: The one-sentence answer to "what happens next, and why".
    next_step_summary: str


@router.post("/runs", response_model=RunSummary)
def trigger_run(request: RunRequest, db: DbDep) -> RunSummary:
    """Run the full loop over an invoice ledger and return the honest summary."""
    if db.get(BatchRun, request.batch_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"batch {request.batch_id!r} already exists — reusing a batch id "
                "would merge two runs into one set of metrics"
            ),
        )
    try:
        artefacts = ReceivablesRunner(db).run(
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
def list_runs(db: DbDep, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[BatchRunRead]:
    """Every receivables run, newest first. Each carries its seed."""
    return [
        BatchRunRead.model_validate(b)
        for b in AuditTrail(db).list_batches(engine=Engine.RECEIVABLES, limit=limit)
    ]


@router.get("/runs/{batch_id}", response_model=BatchRunRead)
def get_run(batch_id: str, db: DbDep) -> BatchRunRead:
    """One run's record, including the dataset batch id it consumed."""
    return BatchRunRead.model_validate(_require_run(batch_id, db))


@router.get("/runs/{batch_id}/summary", response_model=RunSummary)
def get_run_summary(batch_id: str, db: DbDep) -> RunSummary:
    """The run's own summary — the engine-specific figures the dashboard reads.

    Served from what the run stored rather than recomputed, because the splits in
    it (per-ageing-bucket recovery, the promise register, extraction accuracy)
    need the run's outcomes and the dataset behind them, and nothing serving an
    API request should be re-running an engine.

    The money figures inside it were read off the audit trail at the end of the
    run, and `/audit/batches/{batch_id}/summary` recomputes those independently
    — so the stored copy is checkable against the trail rather than merely
    convenient. Quote that route for a headline; quote this one for the breakdown.
    """
    run = _require_run(batch_id, db)
    stored = (run.notes or {}).get("run_summary")
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"run {batch_id} stored no engine summary — it predates the field, "
                "or the run did not complete"
            ),
        )
    return RunSummary.model_validate(stored)


@router.get("/runs/{batch_id}/extraction", response_model=ExtractionScore)
def get_extraction_score(batch_id: str, db: DbDep) -> ExtractionScore:
    """Extraction accuracy for one run, with its confusion matrix and failures.

    Served from the run's stored summary rather than recomputed, because the
    comparison needs the manifest and the manifest is not something an API
    request should be reaching for. The numbers are the ones the run reported.
    """
    run = _require_run(batch_id, db)
    summary = (run.notes or {}).get("extraction")
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail=f"run {batch_id} recorded no extraction score (was it run with score=false?)",
        )
    return ExtractionScore.model_validate(summary)


@router.get("/runs/{batch_id}/worklist", response_model=list[InvoiceChaseStateRead])
def get_worklist(
    batch_id: str,
    db: DbDep,
    deprioritised: bool | None = Query(
        default=None, description="Only invoices held back by a live promise (policy-bounds:PR2)."
    ),
    ageing_bucket: str | None = None,
    next_step: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[InvoiceChaseStateRead]:
    """The ranked worklist, highest priority first, with every score breakdown.

    Deprioritised invoices stay on the list rather than being filtered out: "we
    chose not to chase this, and here is the rule" is the claim worth making, and
    filtering would make the same decision invisible.
    """
    _require_run(batch_id, db)
    stmt = (
        select(InvoiceChaseState)
        .where(InvoiceChaseState.batch_id == batch_id)
        .order_by(InvoiceChaseState.priority_rank.asc())
        .limit(limit)
    )
    if deprioritised is not None:
        stmt = stmt.where(InvoiceChaseState.deprioritised == deprioritised)
    if ageing_bucket is not None:
        stmt = stmt.where(InvoiceChaseState.ageing_bucket == ageing_bucket)
    if next_step is not None:
        stmt = stmt.where(InvoiceChaseState.next_step == next_step)
    return [InvoiceChaseStateRead.model_validate(row) for row in db.scalars(stmt)]


@router.get("/runs/{batch_id}/promises", response_model=list[PromiseToPayRead])
def list_promises(
    batch_id: str,
    db: DbDep,
    status: str | None = Query(
        default=None, description="`active`, `kept`, `broken` or `superseded`."
    ),
    conditional: bool | None = Query(
        default=None,
        description="Conditional commitments are tracked apart from firm ones (policy-bounds:PP2).",
    ),
    customer_id: str | None = None,
) -> list[PromiseToPayRead]:
    """The promise register: every commitment extracted, and what became of it."""
    _require_run(batch_id, db)
    stmt = (
        select(PromiseToPay)
        .where(PromiseToPay.batch_id == batch_id)
        .order_by(PromiseToPay.invoice_id.asc())
    )
    if status is not None:
        stmt = stmt.where(PromiseToPay.status == status)
    if conditional is not None:
        stmt = stmt.where(PromiseToPay.conditional == conditional)
    if customer_id is not None:
        stmt = stmt.where(PromiseToPay.customer_id == customer_id)
    return [PromiseToPayRead.model_validate(row) for row in db.scalars(stmt)]


@router.get("/runs/{batch_id}/invoices/{invoice_id}", response_model=InvoiceTimeline)
def get_invoice_timeline(batch_id: str, invoice_id: str, db: DbDep) -> InvoiceTimeline:
    """One invoice's full timeline: contacts, reply, reading, promise, decisions.

    Complete on purpose — this is the demo surface. Every rule behind every step
    is in the response, including the ones that refused.
    """
    _require_run(batch_id, db)
    state = db.get(InvoiceChaseState, (batch_id, invoice_id))
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"no invoice {invoice_id} in run {batch_id}"
        )
    trail = AuditTrail(db)
    entries = [
        trail.read(e)
        for e in trail.entity_timeline(EntityType.INVOICE, invoice_id)
        if e.batch_id == batch_id
    ]
    promises = list(
        db.scalars(
            select(PromiseToPay)
            .where(PromiseToPay.batch_id == batch_id)
            .where(PromiseToPay.invoice_id == invoice_id)
            .order_by(PromiseToPay.id.asc())
        )
    )
    communications = list(
        db.scalars(
            select(InvoiceCommunication)
            .where(InvoiceCommunication.batch_id == batch_id)
            .where(InvoiceCommunication.invoice_id == invoice_id)
            .order_by(InvoiceCommunication.id.asc())
        )
    )
    # The customer's own words, pulled off the entry that recorded reading them
    # rather than re-opening the dataset: the timeline should show what the
    # engine actually saw, not what the file says today.
    replies = [
        {
            "reply_id": (e.entry_metadata or {}).get("reply_id"),
            "received_at": e.timestamp,
            "text": (e.entry_metadata or {}).get("reply_text"),
            "read_as": (e.entry_metadata or {}).get("intent"),
            "abstained": (e.entry_metadata or {}).get("abstained"),
        }
        for e in trail.entity_timeline(EntityType.INVOICE, invoice_id)
        if e.batch_id == batch_id and (e.entry_metadata or {}).get("kind") == "reply_understanding"
    ]
    when = f" on {state.scheduled_for:%Y-%m-%d %H:%M} UTC" if state.scheduled_for else ""
    return InvoiceTimeline(
        invoice_id=invoice_id,
        batch_id=batch_id,
        state=InvoiceChaseStateRead.model_validate(state),
        entries=entries,
        promises=[PromiseToPayRead.model_validate(p) for p in promises],
        communications=[InvoiceCommunicationRead.model_validate(c) for c in communications],
        replies=replies,
        next_step_summary=(
            f"{state.next_step}{when}, authorised by {state.authorising_rule}. "
            f"{state.explanation}"
        ),
    )


@router.get("/runs/{batch_id}/communications", response_model=list[InvoiceCommunicationRead])
def list_communications(
    batch_id: str,
    db: DbDep,
    status: str | None = Query(
        default=None,
        description="`marked_for_delivery`, `held` or `suppressed`. Never `sent`: nothing here dispatches.",
    ),
    rung: str | None = None,
) -> list[InvoiceCommunicationRead]:
    """Every reminder the run drafted. **None were sent** — see ADR 0009."""
    _require_run(batch_id, db)
    stmt = (
        select(InvoiceCommunication)
        .where(InvoiceCommunication.batch_id == batch_id)
        .order_by(InvoiceCommunication.id.asc())
    )
    if status is not None:
        stmt = stmt.where(InvoiceCommunication.status == status)
    if rung is not None:
        stmt = stmt.where(InvoiceCommunication.rung == rung)
    return [InvoiceCommunicationRead.model_validate(c) for c in db.scalars(stmt)]


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
        batch_id=batch_id, engine=Engine.RECEIVABLES, limit=limit, offset=offset
    )
    return [trail.read(e) for e in entries]


@router.get("/config")
def get_config() -> dict[str, Any]:
    """The ladder, caps and weights this engine runs with.

    Every value names the rule that argues for it, and says whether the policy
    engine actually enforces it. None of these is regulatory — the receivables
    ladder is entirely product judgment, which is exactly why each number has to
    be citable.
    """
    return default_config().raw


def _require_run(batch_id: str, db: Session) -> BatchRun:
    run = db.get(BatchRun, batch_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {batch_id}")
    return run
