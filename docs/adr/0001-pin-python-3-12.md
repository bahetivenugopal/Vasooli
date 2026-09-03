# ADR 0001 — Pin Python to 3.12

**Status:** Accepted
**Date:** 2026-09-03

## Context

The original project brief specified **Python 3.11** in the tech stack table, and
required that any deviation be recorded in an ADR.

The build machine has **Python 3.12.3** installed, and 3.11 is not present. The
initial scaffold hedged with `requires-python = ">=3.11"`, which left the actual
interpreter version ambiguous — a range that resolves differently on different
machines is exactly the kind of thing that produces a "works here, not there"
failure at the worst moment before a deadline.

## Decision

Pin the project to **Python 3.12**, expressed in one fixed way everywhere:

| Location | Value |
| --- | --- |
| `.python-version` (repo root, committed) | `3.12` |
| `apps/api/pyproject.toml` | `requires-python = ">=3.12,<3.13"` |
| `apps/api/pyproject.toml` | `target-version = "py312"` (Ruff) |
| `apps/api/requirements.txt` header | Python 3.12 |
| `README.md` / `.claude/CLAUDE.md` stack tables | Python 3.12 |

`>=3.12,<3.13` rather than a bare `>=3.12`: it fixes the minor version while
still allowing patch upgrades, so a future 3.13 cannot silently be picked up.

## Consequences

- One interpreter version, stated identically in six places. No 3.11-or-greater
  ambiguity left in the repo.
- Every pinned dependency in `requirements.txt` installed and ran cleanly on
  3.12.3 — verified, not assumed: the API imports, the health route returns 200,
  and Ruff passes with `target-version = "py312"`.
- Deployment (a stretch goal only) must use a 3.12 runtime image.
- Nothing in the stack required 3.11 specifically, so this costs nothing. It is
  a deviation on paper only.
