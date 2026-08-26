"""Stable data contracts shared by report connectors, templates, and channels."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

DataQuality = Literal["complete", "partial", "missing"]
ReportPeriod = Literal["day", "week", "month", "recent7", "range"]
ModelScope = Literal["summary", "all", "selected"]


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Reference to secret material; the value itself never enters report contracts."""

    provider: Literal["env", "vault", "kubernetes"]
    key: str


@dataclass(frozen=True, slots=True)
class ReportIntent:
    """Validated user intent before it is compiled to a connector query."""

    connector_id: str
    template_id: str
    period: ReportPeriod
    tenant: str = ""
    model_scope: ModelScope | None = None
    models: tuple[str, ...] = ()
    start_date: date | None = None
    end_date: date | None = None
    comparison: str = "previous_period"
    deep_analysis: bool = False


@dataclass(frozen=True, slots=True)
class ReportQuery:
    """Connector-neutral query request using namespaced canonical metrics."""

    connector_id: str
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    start_date: date
    end_date: date
    filters: dict[str, Any] = field(default_factory=dict)
    comparison_start: date | None = None
    comparison_end: date | None = None


@dataclass(frozen=True, slots=True)
class ReportDataset:
    """Normalized rows plus explicit completeness information."""

    rows: tuple[dict[str, Any], ...]
    quality: DataQuality = "complete"
    warnings: tuple[str, ...] = ()
    source: str = ""


@dataclass(frozen=True, slots=True)
class ReportAction:
    """Opaque, server-validated action exposed by a report UI document."""

    action_id: str
    label: str
    style: Literal["default", "primary", "danger"] = "default"
    value: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReportBlock:
    """One channel-neutral UI block."""

    kind: Literal["markdown", "metrics", "table", "note", "actions"]
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReportDocument:
    """Channel-neutral report or interaction document."""

    title: str
    subtitle: str = ""
    blocks: tuple[ReportBlock, ...] = ()
    fallback_text: str = ""
    document_id: str = ""
    version: int = 1

    def to_agent_ui(self) -> dict[str, Any]:
        return {
            "kind": "report_document",
            "version": self.version,
            "document_id": self.document_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "blocks": [asdict(block) for block in self.blocks],
        }


@dataclass(frozen=True, slots=True)
class ReportRunContext:
    """Identity and delivery boundary for one deterministic report run."""

    channel: str
    chat_id: str
    user_id: str
    timezone: str
    trace_id: str
    template_version: str
    idempotency_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
