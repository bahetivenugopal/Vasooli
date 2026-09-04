"""Engine 2 — Mandate & Subscription Recovery.

Recovers failed recurring charges without ever stepping outside RBI's e-mandate
constraints. The distinction that defines it is not "retry harder": it is knowing
which failures are worth retrying, which need the customer, which need a notice
we failed to send, and which must never be touched again.

The split that matters:

- **Classification reuses the shared taxonomy** (`classification.py`). What it
  adds is the routing a recurring payment needs and a one-off does not —
  authentication, compliance, and the absolute mandate hard stop.
- **Scheduling is deterministic and compliance-gated** (`scheduler.py`). Every
  decision names the rule that permits it, and states what happens next and when.
- **Only the customer message is model-driven** (`dunning.py`). That is the one
  judgment here worth a reasoning call, and even it is validated for tone before
  the policy engine is asked whether it may go out at all.
- **Nothing is dispatched.** Messages are drafted, gated, logged and rendered.
  See ADR 0009.

ADR 0008 explains how the RBI constraints are encoded as policy rules and which
timings are regulation rather than product judgment.
"""

from app.engines.mandate_recovery.classification import (
    assess_notice,
    classify_failure,
    mandate_state,
)
from app.engines.mandate_recovery.config import MandateConfig, default_config, load_config
from app.engines.mandate_recovery.dunning import (
    TASK_DRAFT_DUNNING,
    register_mandate_tasks,
    validate_tone,
)
from app.engines.mandate_recovery.recovery import MandateOutcome, MandateRecoveryService
from app.engines.mandate_recovery.runner import MandateRunner, RunArtefacts
from app.engines.mandate_recovery.scheduler import RetryScheduler, ScheduleDecision
from app.engines.mandate_recovery.schemas import (
    DunningDraft,
    FailureClassification,
    FailureRoute,
    Mandate,
    NextStep,
    NoticeAssessment,
    RunSummary,
    ScheduleExplanation,
)

__all__ = [
    "TASK_DRAFT_DUNNING",
    "DunningDraft",
    "FailureClassification",
    "FailureRoute",
    "Mandate",
    "MandateConfig",
    "MandateOutcome",
    "MandateRecoveryService",
    "MandateRunner",
    "NextStep",
    "NoticeAssessment",
    "RetryScheduler",
    "RunArtefacts",
    "RunSummary",
    "ScheduleDecision",
    "ScheduleExplanation",
    "assess_notice",
    "classify_failure",
    "default_config",
    "load_config",
    "mandate_state",
    "register_mandate_tasks",
    "validate_tone",
]
