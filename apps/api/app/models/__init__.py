"""Shared data models.

Every ORM model must be imported here: `Base.metadata.create_all()` only creates
tables for models that have been imported by the time it runs, and a silently
missing table surfaces as a confusing runtime error much later.
"""

from app.models.audit import AuditEntry, AuditEntryRead, BatchSummary, SourceBreakdown
from app.models.batch import BatchRun, BatchRunRead
from app.models.entity import RecoverableEntity, RecoverableEntityMixin
from app.models.enums import (
    TERMINAL_OUTCOMES,
    Action,
    BatchStatus,
    CorridorDetermination,
    DeclineClass,
    Engine,
    EntityType,
    EscalationTrigger,
    Outcome,
    ProvenanceSource,
    RuleSource,
)
from app.models.mandate import (
    MandateCommunication,
    MandateCommunicationRead,
    MandateRecoveryState,
    MandateRecoveryStateRead,
)
from app.models.overview import (
    EngineContribution,
    OverviewSummary,
    TrustMetric,
)
from app.models.provenance import Provenance
from app.models.receivables import (
    InvoiceChaseState,
    InvoiceChaseStateRead,
    InvoiceCommunication,
    InvoiceCommunicationRead,
    PromiseToPay,
    PromiseToPayRead,
)
from app.models.root_cause import (
    CorridorDetection,
    CorridorDetectionRead,
    CorridorReroute,
    CorridorRerouteRead,
)

__all__ = [
    "TERMINAL_OUTCOMES",
    "Action",
    "AuditEntry",
    "AuditEntryRead",
    "BatchRun",
    "BatchRunRead",
    "BatchStatus",
    "BatchSummary",
    "CorridorDetection",
    "CorridorDetectionRead",
    "CorridorDetermination",
    "CorridorReroute",
    "CorridorRerouteRead",
    "DeclineClass",
    "Engine",
    "EngineContribution",
    "EntityType",
    "EscalationTrigger",
    "InvoiceChaseState",
    "InvoiceChaseStateRead",
    "InvoiceCommunication",
    "InvoiceCommunicationRead",
    "MandateCommunication",
    "MandateCommunicationRead",
    "MandateRecoveryState",
    "MandateRecoveryStateRead",
    "Outcome",
    "OverviewSummary",
    "PromiseToPay",
    "PromiseToPayRead",
    "Provenance",
    "ProvenanceSource",
    "RecoverableEntity",
    "RecoverableEntityMixin",
    "RuleSource",
    "SourceBreakdown",
    "TrustMetric",
]
