"""Namespace package so `data.generators` imports from the repo root.

The generators deliberately live outside `apps/api/` — they are the evidence
base, not part of the service — but they still need to be importable by the
pytest suite and by a CLI run from the repo root. One `__init__.py` is the whole
mechanism; there is no build step and nothing to install.
"""
