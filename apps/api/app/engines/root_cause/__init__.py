"""Engine 1 — Root-Cause Recovery.

Watches payment attempts, and when a corridor (issuing bank, method, route)
degrades, works out whether this is *one customer's problem* or a *corridor-wide
problem*. The same decline code appears in both cases and demands opposite
responses, which is the judgment this engine exists to make.

The split that defines it:

- **Detection is deterministic and statistical** (`detection.py`). Reproducible,
  testable, and explainable to a judge without hand-waving.
- **Diagnosis is model-driven** (`diagnosis.py`). This is the ambiguous call, and
  it is where a reasoning layer genuinely earns its place.
- **Permission is neither.** Every action goes through `policy_engine.py` first,
  and the model can only ever narrow what it permits.

See ADR 0006 for why that boundary was drawn there, and ADR 0007 for the corridor
definition and the minimum-volume threshold.
"""

from app.engines.root_cause.config import RootCauseConfig, default_config, load_config
from app.engines.root_cause.detection import CorridorDetector
from app.engines.root_cause.diagnosis import (
    TASK_DIAGNOSE_CORRIDOR,
    register_root_cause_tasks,
)
from app.engines.root_cause.runner import RootCauseRunner, RunArtefacts
from app.engines.root_cause.schemas import (
    Corridor,
    CorridorDiagnosis,
    Detection,
    DetectionScore,
    PaymentAttempt,
    RunSummary,
)

__all__ = [
    "TASK_DIAGNOSE_CORRIDOR",
    "Corridor",
    "CorridorDetector",
    "CorridorDiagnosis",
    "Detection",
    "DetectionScore",
    "PaymentAttempt",
    "RootCauseConfig",
    "RootCauseRunner",
    "RunArtefacts",
    "RunSummary",
    "default_config",
    "load_config",
    "register_root_cause_tasks",
]
