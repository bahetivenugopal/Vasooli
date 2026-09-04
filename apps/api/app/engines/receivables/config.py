"""Engine 3's configuration, loaded from `config.json`.

Same discipline as Engine 2's config module, for the same reason: values the
policy engine actually enforces are *displayed* here and resolved from
`policy_engine.py`, and `load_config()` raises if the file disagrees. A reader
who edits the contact cap in this file should find out immediately that it
changed nothing, rather than believing it changed something.

The difference from Engine 2 is that **none** of these numbers is regulatory.
Engine 3's ladder is entirely product judgment, which is precisely why every one
of them has to name the `policy-bounds` rule that argues for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.engines.receivables.schemas import ReminderCallToAction, ReminderChannel
from app.models.enums import Engine
from app.services.policy_engine import (
    CONTACT_WINDOW,
    ENGINE_LIMITS,
    LADDER_RUNG_INTERVAL,
    LADDER_RUNGS,
    MAX_MESSAGES_PER_CUSTOMER,
    MAX_MESSAGES_PER_WINDOW,
    PROMISE_GRACE,
    UNDATEABLE_PROMISE_HORIZON,
)

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


class ReceivablesConfigError(RuntimeError):
    """Raised when the config is missing, malformed, or contradicts the gate."""


@dataclass(frozen=True)
class Rung:
    """One rung of the RL1 ladder. Name and bounds come from the policy engine."""

    name: str
    number: int
    tone: str
    channel: ReminderChannel
    call_to_action: ReminderCallToAction


@dataclass(frozen=True)
class LadderConfig:
    rungs: tuple[Rung, ...]
    min_interval: timedelta
    rule: str

    def rung_for(self, rungs_used: int) -> Rung | None:
        """The next rung after `rungs_used` contacts, or None past the last one.

        `None` is the RL1/QH3 exhaustion case and the caller must treat it as
        such — it is not "send the last rung again".
        """
        if rungs_used < 0 or rungs_used >= len(self.rungs):
            return None
        return self.rungs[rungs_used]


@dataclass(frozen=True)
class ContactCapConfig:
    per_invoice: int
    per_customer: int
    window: timedelta
    rule: str


@dataclass(frozen=True)
class PromiseConfig:
    grace: timedelta
    undateable_horizon: timedelta
    rule: str


@dataclass(frozen=True)
class PrioritizationConfig:
    weights: dict[str, float]
    days_overdue_cap: int
    unknown_reliability: float
    rule: str


@dataclass(frozen=True)
class AgeingBucket:
    name: str
    min_days: int
    max_days: int


@dataclass(frozen=True)
class ReceivablesConfig:
    ladder: LadderConfig
    contact_caps: ContactCapConfig
    promises: PromiseConfig
    prioritization: PrioritizationConfig
    ageing_buckets: tuple[AgeingBucket, ...]
    understanding_min_confidence: float
    payment_link_expiry: timedelta
    version: str
    raw: dict[str, Any]

    def ageing_bucket(self, days_late: int) -> str:
        """Which bucket a given lateness falls into. Derived, never read."""
        for bucket in self.ageing_buckets:
            if bucket.min_days <= days_late <= bucket.max_days:
                return bucket.name
        # Unreachable with the shipped config, which covers the whole line.
        raise ReceivablesConfigError(
            f"no ageing bucket covers {days_late} days late. The configured "
            "buckets must partition the number line, or an invoice silently "
            "disappears from every per-bucket figure."
        )


def load_config(path: Path | str = CONFIG_PATH) -> ReceivablesConfig:
    """Read, validate and resolve the engine config."""
    file = Path(path)
    if not file.exists():
        raise ReceivablesConfigError(
            f"no receivables config at {file}. Every number this engine uses is "
            "meant to be visible and citable; there is no hardcoded default."
        )
    raw = json.loads(file.read_text(encoding="utf-8"))

    ladder_raw = raw["ladder"]
    names = tuple(r["name"] for r in ladder_raw["rungs"])
    _require_matches("ladder.rungs (names)", names, LADDER_RUNGS)
    limits = ENGINE_LIMITS[Engine.RECEIVABLES]
    _require_matches("ladder.rungs (count)", len(names), limits.max_attempts)
    _require_matches(
        "ladder.min_interval_hours",
        ladder_raw["min_interval_hours"],
        LADDER_RUNG_INTERVAL / timedelta(hours=1),
    )

    caps = raw["contact_caps"]
    _require_matches(
        "contact_caps.per_invoice_per_window",
        caps["per_invoice_per_window"],
        MAX_MESSAGES_PER_WINDOW,
    )
    _require_matches(
        "contact_caps.per_customer_per_window",
        caps["per_customer_per_window"],
        MAX_MESSAGES_PER_CUSTOMER,
    )
    _require_matches("contact_caps.window_days", caps["window_days"], CONTACT_WINDOW.days)

    promises = raw["promises"]
    _require_matches(
        "promises.grace_hours", promises["grace_hours"], PROMISE_GRACE / timedelta(hours=1)
    )
    _require_matches(
        "promises.undateable_horizon_days",
        promises["undateable_horizon_days"],
        UNDATEABLE_PROMISE_HORIZON.days,
    )

    priority = raw["prioritization"]
    weights = {k: float(v) for k, v in priority["weights"].items()}
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-9:
        raise ReceivablesConfigError(
            f"prioritization weights sum to {total}, not 1.0. A score whose weights "
            "do not normalise is not comparable across invoices, and its breakdown "
            "cannot be read as shares of a whole."
        )

    ageing = raw["ageing_buckets"]
    buckets = tuple(
        AgeingBucket(name=name, min_days=int(spec["min_days"]), max_days=int(spec["max_days"]))
        for name, spec in ageing.items()
        if isinstance(spec, dict)
    )

    return ReceivablesConfig(
        ladder=LadderConfig(
            rungs=tuple(
                Rung(
                    name=r["name"],
                    number=index,
                    tone=r["tone"],
                    channel=ReminderChannel(r["channel"]),
                    call_to_action=ReminderCallToAction(r["call_to_action"]),
                )
                for index, r in enumerate(ladder_raw["rungs"], start=1)
            ),
            min_interval=timedelta(hours=float(ladder_raw["min_interval_hours"])),
            rule=ladder_raw["rule"],
        ),
        contact_caps=ContactCapConfig(
            per_invoice=int(caps["per_invoice_per_window"]),
            per_customer=int(caps["per_customer_per_window"]),
            window=timedelta(days=int(caps["window_days"])),
            rule=caps["rule"],
        ),
        promises=PromiseConfig(
            grace=timedelta(hours=float(promises["grace_hours"])),
            undateable_horizon=timedelta(days=int(promises["undateable_horizon_days"])),
            rule=promises["rule"],
        ),
        prioritization=PrioritizationConfig(
            weights=weights,
            days_overdue_cap=int(priority["days_overdue_cap"]),
            unknown_reliability=float(priority["unknown_reliability"]),
            rule=priority["rule"],
        ),
        ageing_buckets=buckets,
        understanding_min_confidence=float(raw["understanding"]["min_confidence"]),
        payment_link_expiry=timedelta(days=int(raw["payment_link"]["expiry_days"])),
        version=raw["version"],
        raw=raw,
    )


def _require_matches(field: str, configured: Any, enforced: Any) -> None:
    """A displayed constant that disagrees with the enforced one is a lie.

    Same guard Engine 2 uses. The failure it prevents is a config file reading
    `per_customer_per_window: 12` while the gate still enforces 4 — the engine
    looks configurable and is not.
    """
    left = float(configured) if isinstance(configured, int | float) else configured
    right = float(enforced) if isinstance(enforced, int | float) else enforced
    if left != right:
        raise ReceivablesConfigError(
            f"config {field} is {configured!r}, but the policy engine enforces "
            f"{enforced!r}. This file displays the bound; `policy_engine.py` is the "
            "bound. Change it there, with an ADR, or fix the file."
        )


@lru_cache
def default_config() -> ReceivablesConfig:
    """The shipped config. Cached — it is immutable once read."""
    return load_config()


__all__ = [
    "AgeingBucket",
    "ContactCapConfig",
    "LadderConfig",
    "PrioritizationConfig",
    "PromiseConfig",
    "ReceivablesConfig",
    "ReceivablesConfigError",
    "Rung",
    "default_config",
    "load_config",
]
