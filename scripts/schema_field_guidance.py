"""
schema_field_guidance.py — Single renderer: JSON schema → natural language field guidance.

This is the ONLY place that converts schema property descriptions into
human-readable field guidance text. All consumers (jingu_model.py tool
description, phase_prompt.py step guidance) derive from this renderer.

Contract: schema properties.*.description is the single source of truth
for field semantics. This module renders, never invents.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _unwrap_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Best-effort unwrap for common schema wrapper shapes."""
    if not isinstance(schema, dict):
        return {}
    if "type" in schema or "properties" in schema:
        return schema
    if isinstance(schema.get("schema"), dict):
        return _unwrap_schema(schema["schema"])
    json_schema = schema.get("json_schema")
    if isinstance(json_schema, dict) and isinstance(json_schema.get("schema"), dict):
        return _unwrap_schema(json_schema["schema"])
    return schema


def _render_field_type(field_schema: dict[str, Any]) -> str:
    """Render a short human-readable type summary."""
    if not isinstance(field_schema, dict):
        return "value"
    if "enum" in field_schema and isinstance(field_schema["enum"], list):
        vals = ", ".join(repr(v) for v in field_schema["enum"] if v is not None)
        return f"enum({vals})"
    t = field_schema.get("type")
    if isinstance(t, list):
        return " | ".join(str(x) for x in t)
    if isinstance(t, str):
        if t == "array":
            items = field_schema.get("items", {})
            item_type = _render_field_type(items) if isinstance(items, dict) else "value"
            return f"array[{item_type}]"
        return t
    any_of = field_schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        return " | ".join(_render_field_type(x) for x in any_of if isinstance(x, dict))
    return "value"


def _render_field_line(
    field_name: str,
    field_schema: dict[str, Any],
    required_fields: set[str],
) -> str:
    """Render one field as: - field_name [required, type]: description."""
    status = "required" if field_name in required_fields else "optional"
    type_summary = _render_field_type(field_schema)
    description = ""
    if isinstance(field_schema, dict):
        description = (field_schema.get("description") or "").strip()
    if not description:
        description = f"(no description in schema for '{field_name}')"
        logger.warning("Schema field '%s' is missing description.", field_name)
    return f"- {field_name} [{status}, {type_summary}]: {description}"


def render_schema_field_guidance(
    schema: dict[str, Any],
    *,
    phase: str | None = None,
) -> str:
    """Turn a JSON schema into a deterministic field-guidance block.

    This is the single renderer that both jingu_model.py (tool description)
    and phase_prompt.py (step guidance) use. No other code should generate
    field guidance from schema descriptions.

    Returns empty string if schema has no properties (safe degradation).
    """
    unwrapped = _unwrap_schema(schema)
    properties = unwrapped.get("properties", {})
    required_fields = set(unwrapped.get("required", []))

    if not isinstance(properties, dict) or not properties:
        if phase:
            logger.warning("No schema properties for phase=%s", phase)
        return ""

    title = (phase or "submission").upper()
    lines = [f"For {title}, provide the following fields:"]
    for field_name, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            field_schema = {}
        lines.append(_render_field_line(field_name, field_schema, required_fields))

    return "\n".join(lines)


def validate_schema_descriptions(
    schema: dict[str, Any],
    *,
    phase: str,
) -> list[str]:
    """Check that every schema property has a non-empty description.

    Returns list of missing-description warnings. Empty list = all good.
    Called at bundle compile / startup time to catch incomplete schemas.
    """
    unwrapped = _unwrap_schema(schema)
    properties = unwrapped.get("properties", {})
    missing = []

    if not isinstance(properties, dict):
        return [f"{phase}: schema has no properties"]

    for field_name, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            missing.append(f"{phase}: properties.{field_name} is not a dict")
            continue
        desc = (field_schema.get("description") or "").strip()
        if not desc:
            missing.append(f"{phase}: properties.{field_name}.description missing")

    return missing
