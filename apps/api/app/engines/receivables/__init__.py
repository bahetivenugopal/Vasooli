"""Engine 3 — B2B Receivables Chaser & Promise-to-Pay Tracker.

Chases overdue invoices on a bounded, escalating ladder; reads free-text customer
replies to find the commitments in them; and escalates only the commitments that
actually break. The claim worth stating out loud is a negative one: *the system
does not chase people who are cooperating.*

The split that matters:

- **Prioritisation is deterministic** (`prioritization.py`). A transparent
  weighted score with declared weights, shown per invoice. There is no judgment
  here a weighted sum does not capture (`policy-bounds:PR1`).
- **Only reply understanding is model-driven** (`understanding.py`). Reading a
  hedged, code-mixed human reply and deciding whether it constitutes a commitment
  is the one task in this engine no rule engine could do — and it is the one task
  in the project whose fallback **abstains** rather than guessing, because a
  fabricated promise is more expensive than no promise at all.
- **The ladder is bounded by rules, not by the engine** (`ladder.py`). The
  dispute freeze, the per-customer contact cap, the live-promise suppression and
  the escalation-evidence requirement all live in `policy_engine.py`, where an
  engine cannot forget to check them.
- **Promise bookkeeping is arithmetic with citations** (`promises.py`). Kept,
  broken, active, superseded — each an explicit check returning a named rule.
- **Accuracy is measured, not asserted** (`scoring.py`, the only module here that
  opens a manifest).
- **Nothing is dispatched.** Reminders are drafted, gated, logged and rendered.
  See ADR 0009.

ADR 0010 argues why escalation is gated on broken promises rather than elapsed
time; ADR 0011 explains how the extraction measurement is built and why its
ground truth is trustworthy.
"""

from app.engines.receivables.config import (
    ReceivablesConfig,
    default_config,
    load_config,
)
from app.engines.receivables.dataset import (
    dataset_anchor,
    dataset_batch_id,
    load_invoices,
)
from app.engines.receivables.ladder import (
    ChaseLadder,
    ContactCounts,
    escalation_evidence,
    invoice_entity,
)
from app.engines.receivables.prioritization import build_worklist, score_invoice
from app.engines.receivables.promises import assess, reliability, supersede
from app.engines.receivables.recovery import InvoiceOutcome, ReceivablesService
from app.engines.receivables.runner import ReceivablesRunner, RunArtefacts
from app.engines.receivables.schemas import (
    ChasePlan,
    ChaseStep,
    ExtractionResult,
    ExtractionScore,
    Invoice,
    PriorityScore,
    PromiseAssessment,
    PromiseStatus,
    ReplyIntent,
    ReplyUnderstanding,
    RunSummary,
)
from app.engines.receivables.scoring import score_extractions
from app.engines.receivables.understanding import (
    TASK_UNDERSTAND_REPLY,
    register_receivables_tasks,
    understand_reply,
)

__all__ = [
    "TASK_UNDERSTAND_REPLY",
    "ChaseLadder",
    "ChasePlan",
    "ChaseStep",
    "ContactCounts",
    "ExtractionResult",
    "ExtractionScore",
    "Invoice",
    "InvoiceOutcome",
    "PriorityScore",
    "PromiseAssessment",
    "PromiseStatus",
    "ReceivablesConfig",
    "ReceivablesRunner",
    "ReceivablesService",
    "ReplyIntent",
    "ReplyUnderstanding",
    "RunArtefacts",
    "RunSummary",
    "assess",
    "build_worklist",
    "dataset_anchor",
    "dataset_batch_id",
    "default_config",
    "escalation_evidence",
    "invoice_entity",
    "load_config",
    "load_invoices",
    "register_receivables_tasks",
    "reliability",
    "score_extractions",
    "score_invoice",
    "supersede",
    "understand_reply",
]
