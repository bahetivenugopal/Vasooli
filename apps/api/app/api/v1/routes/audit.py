"""Read-only audit routes.

Read-only is the point. The audit trail is written by services, through
`AuditTrail`, and nothing else — exposing a write endpoint would create a path
that produces entries without a decision behind them.

Routes stay thin: every one of them is a filter plus a call into the service.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.audit import AuditEntryRead, BatchSummary
from app.models.batch import BatchRunRead
from app.models.enums import Action, Engine, EntityType, Outcome, ProvenanceSource, RuleSource
from app.services.audit_trail import AuditTrail
from app.services.policy_engine import registered_rules

router = APIRouter(prefix="/audit", tags=["audit"])


def get_audit_trail(db: Annotated[Session, Depends(get_db)]) -> AuditTrail:
    """Request-scoped audit trail bound to the request's session."""
    return AuditTrail(db)


TrailDep = Annotated[AuditTrail, Depends(get_audit_trail)]


class PolicyRuleRead(BaseModel):
    """One registered rule, as the dashboard and `/audit-check` see it."""

    rule_id: str
    category: str
    description: str
    source: RuleSource
    citation: str


@router.get("/entries", response_model=list[AuditEntryRead])
def list_entries(
    trail: TrailDep,
    batch_id: str | None = None,
    engine: Engine | None = None,
    entity_type: EntityType | None = None,
    entity_id: str | None = None,
    action: Action | None = None,
    outcome: Outcome | None = None,
    source: Annotated[
        ProvenanceSource | None,
        Query(description="Filter by provenance: was this judgment reasoned or ruled?"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditEntryRead]:
    """List audit entries, newest first."""
    entries = trail.query(
        batch_id=batch_id,
        engine=engine,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        outcome=outcome,
        source=source,
        limit=limit,
        offset=offset,
    )
    return [trail.read(e) for e in entries]


@router.get("/entries/{entry_id}", response_model=AuditEntryRead)
def get_entry(entry_id: int, trail: TrailDep) -> AuditEntryRead:
    """One entry with its full reasoning, citation and provenance."""
    entry = trail.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no audit entry {entry_id}")
    return trail.read(entry)


@router.get("/batches", response_model=list[BatchRunRead])
def list_batches(
    trail: TrailDep,
    engine: Engine | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[BatchRunRead]:
    """Every batch run, newest first. Each carries its seed."""
    return [BatchRunRead.model_validate(b) for b in trail.list_batches(engine=engine, limit=limit)]


@router.get("/batches/{batch_id}/summary", response_model=BatchSummary)
def batch_summary(batch_id: str, trail: TrailDep) -> BatchSummary:
    """The headline metrics for one batch, recomputed from its entries.

    Recomputed rather than read from the batch's stored snapshot, so the number
    reported here and the trail behind it cannot disagree.
    """
    if trail.get_batch(batch_id) is None:
        raise HTTPException(status_code=404, detail=f"no batch {batch_id}")
    return trail.batch_summary(batch_id)


@router.get("/entities/{entity_type}/{entity_id}/timeline", response_model=list[AuditEntryRead])
def entity_timeline(
    entity_type: EntityType, entity_id: str, trail: TrailDep
) -> list[AuditEntryRead]:
    """Every decision about one entity, oldest first."""
    return [trail.read(e) for e in trail.entity_timeline(entity_type, entity_id)]


@router.get("/rules", response_model=list[PolicyRuleRead])
def list_rules() -> list[PolicyRuleRead]:
    """Every rule the policy engine can cite.

    Exposed because "every rule has a description and a traceable source" should
    be checkable by anyone reading the API, not just by anyone reading the code.
    """
    return [
        PolicyRuleRead(
            rule_id=r.rule_id,
            category=r.category,
            description=r.description,
            source=r.source,
            citation=r.citation,
        )
        for r in registered_rules()
    ]
