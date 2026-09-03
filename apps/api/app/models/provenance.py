"""The provenance object attached to every judgment in the system.

> Nothing model-derived may appear anywhere in the system without this object
> attached — not in an audit entry, not in an API response, not on the
> dashboard, not in a reported metric.

Contract defined in the `llm-provider` and `audit-schema` skills; those two
files and this module must agree exactly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ProvenanceSource

#: The `provider` value used for anything not produced by a model. There is no
#: third source: a lookup-table decision was *ruled*, so it is deterministic.
DETERMINISTIC_PROVIDER = "deterministic"


class Provenance(BaseModel):
    """How a judgment was produced.

    Frozen on purpose. Provenance describes something that already happened; a
    caller that can edit it after the fact can misreport how a number was made.
    """

    model_config = ConfigDict(frozen=True)

    source: ProvenanceSource
    provider: str
    model: str | None = None
    cache_hit: bool = False
    prompt_version: str | None = None
    abstained: bool = False
    latency_ms: int | None = None
    tokens: int | None = None

    @model_validator(mode="after")
    def _enforce_source_contract(self) -> Provenance:
        """Deterministic results carry no model, prompt, latency or token counts.

        Letting a fallback quietly report a model name would make a rule-derived
        figure look reasoned, which is exactly the blending the reporting rule
        forbids.
        """
        if self.source is ProvenanceSource.DETERMINISTIC:
            bad = [
                name
                for name in ("model", "prompt_version", "latency_ms", "tokens")
                if getattr(self, name) is not None
            ]
            if bad:
                raise ValueError(
                    f"deterministic provenance must leave {bad} null — "
                    "a rule-derived result must never look model-derived"
                )
            if self.cache_hit:
                raise ValueError("deterministic provenance cannot be a cache hit")
        elif not self.model:
            raise ValueError("model-derived provenance must name the model that produced it")
        return self

    @classmethod
    def deterministic(cls, *, abstained: bool = False) -> Provenance:
        """Provenance for a ruled decision — a fallback, or a table lookup."""
        return cls(
            source=ProvenanceSource.DETERMINISTIC,
            provider=DETERMINISTIC_PROVIDER,
            abstained=abstained,
        )

    @classmethod
    def from_model(
        cls,
        *,
        provider: str,
        model: str,
        prompt_version: str,
        cache_hit: bool = False,
        latency_ms: int | None = None,
        tokens: int | None = None,
    ) -> Provenance:
        """Provenance for a reasoned decision. A cache hit is still `model`."""
        return cls(
            source=ProvenanceSource.MODEL,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            cache_hit=cache_hit,
            latency_ms=latency_ms,
            tokens=tokens,
        )


class ProvenanceField(BaseModel):
    """Marker base for any payload that carries model-derived content.

    Inheriting from this is how a schema states, structurally, that it cannot be
    serialised without saying where it came from.
    """

    provenance: Provenance = Field(..., description="How this result was produced")
