"""Vasooli synthetic data foundry.

Three seeded generators — payments, mandates, invoices — plus the shared
manifest/seeding machinery they all use. Same seed + same config = byte-identical
output, always. That contract is what makes every number this project reports
checkable rather than merely asserted.

The generators import the decline taxonomy from `apps/api` rather than keeping
their own copy of the vocabulary: a second list of decline codes would drift, and
a generator emitting a reason the engines cannot classify is a silent
data-quality bug. The path bootstrap below is what makes that import work from a
repo-root CLI invocation and from pytest alike, mirroring
`scripts/core_loop_demo.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_API_DIR = REPO_ROOT / "apps" / "api"

if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

#: Bumped whenever a change alters generated output for an unchanged seed and
#: config. Recorded in every manifest, so a batch that no longer reproduces can
#: be explained rather than merely disbelieved.
GENERATOR_VERSION = "1.0.0"
