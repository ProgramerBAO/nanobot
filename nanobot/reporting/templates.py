"""Safe loader for declarative report templates."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_ALLOWED_PERIODS = frozenset({"day", "week", "month", "recent7", "range"})
_ALLOWED_METRICS = frozenset({"ai.usage.tokens", "ai.requests", "ai.tpm", "ai.tpm.avg"})
_ALLOWED_DIMENSIONS = frozenset({"tenant", "model", "endpoint", "date"})
_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "template_id",
        "display_name",
        "version",
        "category",
        "period",
        "comparison",
        "metrics",
        "dimensions",
        "description",
    }
)


@dataclass(frozen=True, slots=True)
class DeclarativeTemplateSpec:
    template_id: str
    display_name: str
    version: str
    category: str
    period: str
    comparison: str
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    description: str


def parse_template_spec(raw: dict[str, Any]) -> DeclarativeTemplateSpec:
    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unsupported template fields: {', '.join(sorted(unknown))}")
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported template schema version")
    template_id = str(raw.get("template_id") or "")
    period = str(raw.get("period") or "")
    metrics = tuple(str(item) for item in raw.get("metrics") or ())
    dimensions = tuple(str(item) for item in raw.get("dimensions") or ())
    if not _ID_RE.fullmatch(template_id):
        raise ValueError("invalid template_id")
    if period not in _ALLOWED_PERIODS:
        raise ValueError("invalid template period")
    if not metrics or not set(metrics) <= _ALLOWED_METRICS:
        raise ValueError("template requests unsupported metrics")
    if not dimensions or not set(dimensions) <= _ALLOWED_DIMENSIONS:
        raise ValueError("template requests unsupported dimensions")
    display_name = str(raw.get("display_name") or "").strip()
    version = str(raw.get("version") or "").strip()
    if not display_name or not version:
        raise ValueError("template display_name and version are required")
    return DeclarativeTemplateSpec(
        template_id=template_id,
        display_name=display_name,
        version=version,
        category=str(raw.get("category") or "report"),
        period=period,
        comparison=str(raw.get("comparison") or "previous_period"),
        metrics=metrics,
        dimensions=dimensions,
        description=str(raw.get("description") or ""),
    )


def load_builtin_template_specs() -> tuple[DeclarativeTemplateSpec, ...]:
    root = files("nanobot.reporting.template_specs")
    specs = []
    for resource in sorted(root.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".json"):
            continue
        raw = json.loads(resource.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"template {resource.name} must be an object")
        specs.append(parse_template_spec(raw))
    return tuple(specs)
