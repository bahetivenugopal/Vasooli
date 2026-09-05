"""Generate the frontend's TypeScript types from the API's own OpenAPI schema.

    python scripts/generate_api_types.py

Writes two files into `packages/shared-types/`:

- `openapi.json` — the schema itself, committed so a reviewer can diff what the
  API promised against what the dashboard consumed.
- `src/api.ts` — one exported type per component schema, plus friendlier aliases
  for the names FastAPI disambiguates by module path.

**Why generated rather than hand-written.** Phase 6 §5.6: "Types come from the
backend. Do not hand-write drifting duplicates." A hand-aligned type is correct
on the day it is written and wrong the first time a Pydantic field is renamed,
and the failure mode is a dashboard that renders `undefined` at runtime rather
than one that fails to compile.

**Why not `openapi-typescript`.** It would work, and it would add a Node
toolchain dependency to a Python repo for a schema of 40 flat models. This is
~200 lines, has no install step, and runs from the same interpreter the API does.
If the schema ever grows shapes this cannot express — discriminated unions,
recursive refs — swap it out rather than bolting more cases on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
OUT_DIR = ROOT / "packages" / "shared-types"

#: FastAPI disambiguates same-named models by module path. Those names are
#: accurate and unreadable; the dashboard imports these instead.
ALIASES: dict[str, str] = {
    "app__engines__root_cause__schemas__RunSummary": "RootCauseRunSummary",
    "app__engines__mandate_recovery__schemas__RunSummary": "MandateRunSummary",
    "app__engines__receivables__schemas__RunSummary": "ReceivablesRunSummary",
    "app__api__v1__routes__root_cause__RunRequest": "RootCauseRunRequest",
    "app__api__v1__routes__mandate_recovery__RunRequest": "MandateRunRequest",
    "app__api__v1__routes__receivables__RunRequest": "ReceivablesRunRequest",
}

HEADER = """/**
 * Generated from the Vasooli API's OpenAPI schema. Do not edit by hand.
 *
 *     python scripts/generate_api_types.py
 *
 * Every type here is the shape the backend actually serialises. Hand-editing one
 * makes the dashboard compile against a promise the API never made.
 */

"""


def ts_name(name: str) -> str:
    return ALIASES.get(name, name)


def ref_name(ref: str) -> str:
    return ts_name(ref.rsplit("/", 1)[-1])


def render_type(schema: dict[str, Any] | bool, indent: int = 0) -> str:
    """One JSON-Schema node -> one TypeScript type expression.

    Deliberately narrow. It covers refs, enums, the primitives, arrays, free-form
    and typed records, and the `anyOf` shape Pydantic emits for `X | None`.
    Anything else becomes `unknown`, which fails loudly at the use site rather
    than quietly typing a field as `any`.
    """
    if schema is True or schema == {}:
        return "unknown"
    if schema is False:
        return "never"
    if "$ref" in schema:
        return ref_name(schema["$ref"])
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(v) for v in schema["enum"])

    if "anyOf" in schema or "oneOf" in schema:
        members = schema.get("anyOf") or schema.get("oneOf")
        rendered = [render_type(m, indent) for m in members]
        # Pydantic's `X | None` arrives as anyOf[X, null]; collapse the duplicate
        # `null` members so the union reads as one nullable type.
        seen: list[str] = []
        for item in rendered:
            if item not in seen:
                seen.append(item)
        return " | ".join(seen)
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return render_type(schema["allOf"][0], indent)

    kind = schema.get("type")
    if kind == "null":
        return "null"
    if kind == "string":
        return "string"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "array":
        return f"Array<{render_type(schema.get('items', {}), indent)}>"
    if kind == "object" or "properties" in schema:
        if "properties" in schema:
            return render_object(schema, indent)
        values = schema.get("additionalProperties", True)
        return f"Record<string, {render_type(values, indent)}>"
    return "unknown"


def render_object(schema: dict[str, Any], indent: int) -> str:
    """An inline object literal, for the rare nested shape."""
    pad = "  " * (indent + 1)
    required = set(schema.get("required", []))
    lines = ["{"]
    for prop, sub in schema.get("properties", {}).items():
        optional = "" if prop in required else "?"
        lines.append(f"{pad}{json.dumps(prop)}{optional}: {render_type(sub, indent + 1)};")
    lines.append("  " * indent + "}")
    return "\n".join(lines)


def render_declaration(name: str, schema: dict[str, Any]) -> str:
    """One top-level schema -> an exported `type` or `interface`."""
    doc = schema.get("description", "").strip()
    comment = ""
    if doc:
        body = "\n".join(f" * {line}".rstrip() for line in doc.splitlines())
        comment = f"/**\n{body}\n */\n"

    if "enum" in schema:
        union = " | ".join(json.dumps(v) for v in schema["enum"])
        return f"{comment}export type {ts_name(name)} = {union};\n"

    if "properties" not in schema:
        return f"{comment}export type {ts_name(name)} = {render_type(schema)};\n"

    required = set(schema.get("required", []))
    lines = [f"{comment}export interface {ts_name(name)} {{"]
    for prop, sub in schema["properties"].items():
        prop_doc = (sub.get("description") or "").strip()
        if prop_doc:
            lines.append(f"  /** {' '.join(prop_doc.split())} */")
        rendered = render_type(sub, 1)
        # A field with a default is present in every response FastAPI builds, but
        # marking it optional lets a caller construct a partial object. Required
        # in the schema means required here; everything else is optional.
        optional = "" if prop in required else "?"
        lines.append(f"  {prop}{optional}: {rendered};")
    lines.append("}\n")
    return "\n".join(lines)


def load_spec() -> dict[str, Any]:
    """Import the app and ask it for its own schema."""
    sys.path.insert(0, str(API_DIR))
    from app.main import app

    return app.openapi()


def main() -> int:
    spec = load_spec()
    schemas = spec["components"]["schemas"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "src").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "openapi.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    parts = [HEADER]
    for name in sorted(schemas, key=lambda n: ts_name(n).lower()):
        parts.append(render_declaration(name, schemas[name]))

    parts.append(
        "/** Every path the API serves, so a typo in a URL is a compile error. */\n"
        "export const API_PATHS = "
        + json.dumps(sorted(spec["paths"]), indent=2)
        + " as const;\n"
    )

    target = OUT_DIR / "src" / "api.ts"
    target.write_text("\n".join(parts), encoding="utf-8")

    # Prettier owns formatting in `apps/web`; running it here keeps the generated
    # file from being the one thing the lint step always fails on.
    prettier = ROOT / "apps" / "web" / "node_modules" / ".bin" / "prettier"
    if prettier.exists() or (prettier.with_suffix(".cmd")).exists():
        subprocess.run(
            ["npx", "--no-install", "prettier", "--write", str(target)],
            cwd=ROOT / "apps" / "web",
            check=False,
            shell=sys.platform == "win32",
        )

    print(f"wrote {target.relative_to(ROOT)} ({len(schemas)} schemas)")
    print(f"wrote {(OUT_DIR / 'openapi.json').relative_to(ROOT)} ({len(spec['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
