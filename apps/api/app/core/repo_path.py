"""Make the repo-root `data` package importable from inside the API process.

All three engines sample their outcomes from `data.generators.retry_model`. That
is deliberate — the retry-success probabilities are a documented *data*
assumption rather than engine logic, so they live with the generators that
produced them (Phase 2, `data/DATA_CARD.md` §9) and no engine keeps a second
copy of the numbers.

The cost is that `apps/api` code depends on a package at the repo root. The demo
scripts have always handled this by putting the root on `sys.path` before
importing anything, so the engines worked from the command line. The API did
not, and `POST /runs` therefore failed with `ModuleNotFoundError: No module
named 'data'` for every engine — a route that was only ever exercised from
tests, where pytest's rootdir put the path there for free.

Phase 6 found it, because the dashboard's run trigger is the first thing that
calls those routes for real.

So the bootstrap happens once, from the app's lifespan, and the engines' lazy
imports keep working exactly as they did. `app` still imports cleanly without
this being called — nothing at module scope reaches for `data`.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: `apps/api/app/core/repo_path.py` -> the repo root, four levels up.
REPO_ROOT = Path(__file__).resolve().parents[4]


def ensure_repo_root_importable() -> Path:
    """Put the repo root on `sys.path` if it is not already there.

    Idempotent, and returns the path so a caller can log or assert on it.
    """
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT
