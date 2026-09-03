"""Versioned prompt files.

Prompts are **files**, not Python strings. Two reasons, both practical:

- A prompt edit silently changes results that were already reported. Carrying a
  version identifier into provenance means any metric traces back to the exact
  prompt that produced it.
- A prompt buried in a call site is invisible to review. One in a file shows up
  in a diff.

Format is a small frontmatter block followed by the template:

    ---
    version: v1
    task: classify_unknown_decline
    ---
    ...template text with {placeholders}...
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent
_FRONTMATTER_FENCE = "---"


class PromptError(RuntimeError):
    """Raised when a prompt file is missing, malformed, or unversioned."""


@dataclass(frozen=True)
class Prompt:
    """One versioned prompt template."""

    name: str
    version: str
    template: str

    def render(self, **context: object) -> str:
        """Fill the template.

        A missing placeholder raises rather than rendering a prompt with a
        literal `{customer_name}` in it — a malformed prompt produces a
        confidently wrong answer, which is the expensive kind.
        """
        try:
            return self.template.format(**context)
        except KeyError as exc:
            raise PromptError(
                f"prompt {self.name!r} ({self.version}) needs context key {exc.args[0]!r}"
            ) from exc


def _parse(path: Path) -> Prompt:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith(_FRONTMATTER_FENCE):
        raise PromptError(
            f"{path.name} has no frontmatter. Every prompt declares its version — "
            "an unversioned prompt makes its results untraceable."
        )
    _, frontmatter, body = raw.split(_FRONTMATTER_FENCE, 2)
    meta: dict[str, str] = {}
    for line in frontmatter.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    if "version" not in meta:
        raise PromptError(f"{path.name} frontmatter is missing `version`")
    return Prompt(
        name=meta.get("task", path.stem),
        version=meta["version"],
        template=body.strip(),
    )


@lru_cache
def load_prompt(name: str) -> Prompt:
    """Load `<name>.md` from this directory. Cached — prompts are immutable."""
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise PromptError(
            f"no prompt file for task {name!r} at {path}. Prompts live here as "
            "version-controlled files, never inline at a call site."
        )
    return _parse(path)


def available_prompts() -> list[str]:
    return sorted(p.stem for p in PROMPT_DIR.glob("*.md"))
