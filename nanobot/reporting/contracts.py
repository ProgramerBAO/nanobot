"""Stable data contracts shared by report connectors, templates, and channels."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

DataQuality = Literal["complete", "partial", "missing"]
ReportPeriod = Literal["day", "week", "month", "recent7", "range"]
ModelScope = Literal["summary", "all", "selected"]

# Canonical names are the only metric/dimension vocabulary exposed to templates.
# Connector-specific names must be mapped before a ReportDataset is returned.
CANONICAL_METRICS = frozenset(
    {
        "ai.usage.tokens",
        "ai.requests",
        "ai.rpm",
        "ai.tpm",
        "ai.error_rate",
        "ai.http_4xx_rate",
        "ai.http_5xx_rate",
        "ai.interface_delay",
        "ai.ttft",
        "ai.cost",
        "ai.balance",
        "ai.unbilled_amount",
        "ai.gpu_hours",
        "ai.capacity_utilization",
    }
)

CANONICAL_DIMENSIONS = frozenset(
    {
        "tenant",
        "project",
        "model",
        "endpoint",
        "provider",
        "cluster",
        "date",
        "hour",
    }
)

_UNSAFE_INTENT_FILTER_KEYS = frozenset(
    {
        "url", "uri", "sql", "query", "promql", "expr", "expression", "token",
        "password", "secret", "authorization", "api_key", "access_token",
    }
)


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
    project: str = ""
    endpoint: str = ""
    environment: str = ""
    provider: str = ""
    model_scope: ModelScope | None = None
    models: tuple[str, ...] = ()
    start_date: date | None = None
    end_date: date | None = None
    comparison: str = "previous_period"
    deep_analysis: bool = False
    filters: dict[str, Any] = field(default_factory=dict)


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
    query_id: str = ""
    step_seconds: int | None = None


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
    quality: DataQuality | None = None
    warnings: tuple[str, ...] = ()

    def to_agent_ui(self) -> dict[str, Any]:
        return {
            "kind": "report_document",
            "version": self.version,
            "document_id": self.document_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "quality": self.quality,
            "warnings": list(self.warnings),
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


def validate_report_intent(intent: ReportIntent) -> None:
    """Reject execution controls and credential material from a user Intent."""

    unsafe = _UNSAFE_INTENT_FILTER_KEYS.intersection(
        str(key).casefold() for key in intent.filters
    )
    if unsafe:
        raise ValueError("report Intent contains an unsafe filter")


def validate_report_query(query: ReportQuery) -> None:
    """Require connectors and templates to exchange canonical vocabulary only."""

    if not set(query.metrics) <= CANONICAL_METRICS:
        raise ValueError("report query contains a non-canonical metric")
    if not set(query.dimensions) <= CANONICAL_DIMENSIONS:
        raise ValueError("report query contains a non-canonical dimension")
    if query.end_date < query.start_date:
        raise ValueError("report query end_date must not be earlier than start_date")
