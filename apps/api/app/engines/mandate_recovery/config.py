"""Engine 2's configuration, loaded from `config.json`.

The file separates two kinds of number and this module keeps them separate:

- **Regulatory** values (`rbi-mandate-rules` Part A) are resolved from
  `policy_engine.py` and merely *displayed* here. `load_config()` raises if the
  file disagrees with the gate, so a reader who edits the notice window in this
  file finds out immediately that it changed nothing — rather than believing it
  changed something.
- **Vasooli** values are genuinely read from here, each citing its Part B or
  `policy-bounds` rule.

That asymmetry is the whole point. A compliance constant a hackathon demo can
retune from a config file is not a compliance constant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.models.enums import Engine
from app.services.policy_engine import (
    ENGINE_LIMITS,
    NOTICE_MAX_LEAD_HOURS,
    NOTICE_MIN_LEAD_HOURS,
    THRESHOLDS,
)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


class MandateConfigError(RuntimeError):
    """Raised when the config is missing, malformed, or contradicts the gate."""


@dataclass(frozen=True)
class NoticeConfig:
    """The A2 pre-debit notification window, and where to aim a fresh notice."""

    min_lead: timedelta
    max_lead: timedelta
    target_lead: timedelta
    rule: str

    def classify_lead(self, lead_hours: float | None) -> str:
        """Name the notice profile a lead time falls into.

        Derived from timestamps on the record. The generator writes the same
        label into its manifest, which engine code never opens — deriving it
        independently is what makes the two comparable at all.
        """
        if lead_hours is None:
            return "missing"
        if lead_hours < self.min_lead / timedelta(hours=1):
            return "late"
        if lead_hours > self.max_lead / timedelta(hours=1):
            return "stale"
        return "on_time"


@dataclass(frozen=True)
class AttemptConfig:
    """The Part B per-cycle bounds. Enforced by the policy engine, not by this."""

    max_per_cycle: int
    min_spacing: timedelta
    rule: str


@dataclass(frozen=True)
class DunningConfig:
    """Bounds on the drafting task."""

    min_confidence: float
    rule: str


@dataclass(frozen=True)
class MandateConfig:
    notice: NoticeConfig
    attempts: AttemptConfig
    dunning: DunningConfig
    afa_threshold_paise: int
    blind_retry_attempts: int
    version: str
    raw: dict[str, Any]


def load_config(path: Path | str = CONFIG_PATH) -> MandateConfig:
    """Read, validate and resolve the engine config."""
    file = Path(path)
    if not file.exists():
        raise MandateConfigError(
            f"no mandate-recovery config at {file}. Every number this engine uses "
            "is meant to be visible and citable; there is no hardcoded default."
        )
    raw = json.loads(file.read_text(encoding="utf-8"))

    notice = raw["notice_window"]
    _require_matches(
        "notice_window.min_lead_hours", notice["min_lead_hours"], NOTICE_MIN_LEAD_HOURS
    )
    _require_matches(
        "notice_window.max_lead_hours", notice["max_lead_hours"], NOTICE_MAX_LEAD_HOURS
    )
    target = float(notice["target_lead_hours"])
    if not NOTICE_MIN_LEAD_HOURS <= target <= NOTICE_MAX_LEAD_HOURS:
        raise MandateConfigError(
            f"notice_window.target_lead_hours ({target}) is outside the A2 window "
            f"[{NOTICE_MIN_LEAD_HOURS}, {NOTICE_MAX_LEAD_HOURS}]. A notice aimed "
            "outside the window is a violation the scheduler would create itself."
        )

    limits = ENGINE_LIMITS[Engine.MANDATE_RECOVERY]
    attempts = raw["attempts"]
    _require_matches("attempts.max_per_cycle", attempts["max_per_cycle"], limits.max_attempts)
    _require_matches(
        "attempts.min_spacing_hours",
        attempts["min_spacing_hours"],
        limits.min_cooldown / timedelta(hours=1),
    )
    _require_matches(
        "afa.threshold_paise",
        raw["afa"]["threshold_paise"],
        THRESHOLDS["afa_fresh_auth"].value_paise,
    )

    return MandateConfig(
        notice=NoticeConfig(
            min_lead=timedelta(hours=float(notice["min_lead_hours"])),
            max_lead=timedelta(hours=float(notice["max_lead_hours"])),
            target_lead=timedelta(hours=target),
            rule=notice["rule"],
        ),
        attempts=AttemptConfig(
            max_per_cycle=int(attempts["max_per_cycle"]),
            min_spacing=timedelta(hours=float(attempts["min_spacing_hours"])),
            rule=attempts["rule"],
        ),
        dunning=DunningConfig(
            min_confidence=float(raw["dunning"]["min_confidence"]),
            rule=raw["dunning"]["rule"],
        ),
        afa_threshold_paise=int(raw["afa"]["threshold_paise"]),
        blind_retry_attempts=int(raw["blind_retry_baseline"]["attempts_per_cycle"]),
        version=raw["version"],
        raw=raw,
    )


def _require_matches(field: str, configured: float, enforced: float) -> None:
    """A displayed constant that disagrees with the enforced one is a lie.

    Raising rather than preferring one silently: the failure mode this prevents
    is a config file that reads `min_lead_hours: 6` while the gate still enforces
    24, so the engine looks configurable and is not.
    """
    if float(configured) != float(enforced):
        raise MandateConfigError(
            f"config {field} is {configured}, but the policy engine enforces "
            f"{enforced}. This file displays the bound; `policy_engine.py` is the "
            "bound. Change it there, with an ADR, or fix the file."
        )


@lru_cache
def default_config() -> MandateConfig:
    """The shipped config. Cached — it is immutable once read."""
    return load_config()
