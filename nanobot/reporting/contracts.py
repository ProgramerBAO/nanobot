"""Stable data contracts shared by report connectors, templates, and channels."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

DataQuality = Literal["complete", "partial", "missing"]
ReportPeriod = Literal["day", "week", "month", "recent7", "recent15m", "range"]
ModelScope = Literal["summary", "all", "selected"]
TenantScope = Literal["key_accounts", "all", "selected"]
BaselinePolicy = Literal["previous_equal_window"]

# Canonical names are the only metric/dimension vocabulary exposed to templates.
# Connector-specific names must be mapped before a ReportDataset is returned.
CANONICAL_METRICS = frozenset(
    {
        "ai.usage.tokens",
        "ai.requests",
        "ai.rpm",
        "ai.tpm",
        "ai.tpm.avg",
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
        "ai.provider.throughput",
        "ai.provider.latency",
        "ai.provider.error_rate",
        "ai.provider.tpm",
        "ai.provider.traffic_ratio",
        "ai.provider.tokens",
        "ai.provider.requests",
        "ai.provider.actual_tpm",
        "ai.provider.avg_latency",
        "ai.provider.avg_ttft",
        "ai.provider.test_score",
        "ai.provider.input_price",
        "ai.provider.output_price",
        "ai.machine.tpm_per_machine",
        "ai.machine.total_tokens",
        "ai.machine.count",
        "ai.machine.gpu_count",
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
        "gpu_product",
    }
)

# Shared usage semantics. Connectors may provide richer metadata, but templates
# must use these names when presenting the standard Cube usage report.
USAGE_METRIC_SEMANTICS: dict[str, dict[str, str]] = {
    "ai.usage.tokens": {
        "label": "Token 消耗",
        "unit": "tokens",
        "aggregation": "窗口总和",
        "source": "Cube Admin / analysis/active-tenant-daily-usage/query",
        "direction": "informational",
    },
    "ai.requests": {
        "label": "请求数",
        "unit": "requests",
        "aggregation": "窗口总和",
        "source": "Cube Admin / analysis/active-tenant-daily-usage/query",
        "direction": "informational",
    },
    "ai.tpm": {
        "label": "峰值 TPM",
        "unit": "tokens/minute",
        "aggregation": "单 Endpoint 日峰值的窗口最大值",
        "source": "Cube Admin / analysis/endpoint-max-tpm/daily/query",
        "direction": "informational",
    },
    "ai.tpm.avg": {
        "label": "平均 TPM",
        "unit": "tokens/minute",
        "aggregation": "同一 Endpoint 有效日 avgTpm 的算术平均",
        "source": "Cube Admin / analysis/endpoint-max-tpm/daily/query",
        "direction": "informational",
    },
}

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
    tenant_scope: TenantScope | None = None
    tenants: tuple[str, ...] = ()
    project: str = ""
    endpoint: str = ""
    environment: str = ""
    provider: str = ""
    model_scope: ModelScope | None = None
    models: tuple[str, ...] = ()
    start_date: date | None = None
    end_date: date | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    comparison_start_time: datetime | None = None
    comparison_end_time: datetime | None = None
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
    start_time: datetime | None = None
    end_time: datetime | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    comparison_start: date | None = None
    comparison_end: date | None = None
    comparison_start_time: datetime | None = None
    comparison_end_time: datetime | None = None
    additional_comparisons: tuple[ReportQueryComparison, ...] = ()
    query_id: str = ""
    step_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ReportDataset:
    """Normalized rows plus explicit completeness information."""

    rows: tuple[dict[str, Any], ...]
    quality: DataQuality = "complete"
    warnings: tuple[str, ...] = ()
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReportWindow:
    """A user-visible reporting interval represented without runtime objects."""

    start: str
    end: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class ReportQueryComparison:
    """An additional named date window fetched by a deterministic query plan."""

    key: str
    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True)
class ReportComparisonWindow:
    """A named comparison window shown consistently by every renderer."""

    key: str
    label: str
    window: ReportWindow


@dataclass(frozen=True, slots=True)
class ReportSource:
    """Safe logical source reference; credentials and hostnames are excluded."""

    system: str
    route: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """Human-readable metric semantics shared by card and text renderers."""

    metric: str
    label: str
    unit: str
    aggregation: str
    source: str
    direction: Literal["lower_is_better", "higher_is_better", "informational"] = (
        "informational"
    )


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Explainable report scope and calculation metadata."""

    timezone: str
    current_window: ReportWindow | None = None
    baseline_window: ReportWindow | None = None
    baseline_policy: BaselinePolicy = "previous_equal_window"
    comparison_windows: tuple[ReportComparisonWindow, ...] = ()
    sources: tuple[ReportSource, ...] = ()
    metric_definitions: tuple[MetricDefinition, ...] = ()
    calculation_version: str = "1"
    quality: DataQuality | None = None
    quality_reasons: tuple[str, ...] = ()
    freshness: str = ""
    template_version: str = ""


@dataclass(frozen=True, slots=True)
class ReportAction:
    """Opaque, server-validated action exposed by a report UI document."""

    action_id: str
    label: str
    style: Literal["default", "primary", "danger"] = "default"
    value: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReportBlock:
    """One channel-neutral UI block.

    Note blocks may request a closed disclosure with ``collapsed`` and opt in
    to co-locating report context or warnings. Renderers must preserve the
    content when their channel lacks an interactive disclosure component.
    """

    kind: Literal[
        "markdown", "metrics", "grouped_metrics", "table", "note", "actions", "selector"
    ]
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
    context: ReportContext | None = None

    def to_agent_ui(self) -> dict[str, Any]:
        return {
            "kind": "report_document",
            "version": self.version,
            "document_id": self.document_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "quality": self.quality,
            "warnings": list(self.warnings),
            "context": asdict(self.context) if self.context is not None else None,
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
    all_tenants = intent.filters.get("all_tenants", False)
    if not isinstance(all_tenants, bool):
        raise ValueError("report Intent all_tenants must be boolean")
    requested_tenants = tuple(str(item).strip() for item in intent.tenants if str(item).strip())
    if len(requested_tenants) > 20:
        raise ValueError("report Intent supports at most 20 selected tenants")
    if intent.tenant and requested_tenants:
        raise ValueError("report Intent cannot combine tenant and tenants")
    if all_tenants:
        if intent.tenant or requested_tenants or str(intent.filters.get("tenant") or "").strip():
            raise ValueError("all_tenants report cannot select an individual tenant")
    if len(intent.models) > 20:
        raise ValueError("report Intent supports at most 20 selected models")
    times = (intent.start_time, intent.end_time)
    if any(value is not None for value in times):
        if not all(value is not None for value in times):
            raise ValueError("report Intent start_time and end_time must be provided together")
        if intent.start_time.tzinfo is None or intent.end_time.tzinfo is None:
            raise ValueError("report Intent times must be timezone-aware")
        if intent.end_time <= intent.start_time:
            raise ValueError("report Intent end_time must be later than start_time")
        if intent.period == "recent15m" and intent.end_time - intent.start_time > timedelta(minutes=15):
            raise ValueError("recent15m report window must not exceed 15 minutes")
    comparison_times = (intent.comparison_start_time, intent.comparison_end_time)
    if any(value is not None for value in comparison_times):
        if not all(value is not None for value in comparison_times):
            raise ValueError(
                "report Intent comparison_start_time and comparison_end_time must be provided together"
            )
        if (
            intent.comparison_start_time.tzinfo is None
            or intent.comparison_end_time.tzinfo is None
        ):
            raise ValueError("report Intent comparison times must be timezone-aware")
        if intent.comparison_end_time <= intent.comparison_start_time:
            raise ValueError("report Intent comparison_end_time must be later than comparison_start_time")


def validate_report_query(query: ReportQuery) -> None:
    """Require connectors and templates to exchange canonical vocabulary only."""

    if not set(query.metrics) <= CANONICAL_METRICS:
        raise ValueError("report query contains a non-canonical metric")
    if not set(query.dimensions) <= CANONICAL_DIMENSIONS:
        raise ValueError("report query contains a non-canonical dimension")
    if query.end_date < query.start_date:
        raise ValueError("report query end_date must not be earlier than start_date")
    comparison_keys: set[str] = set()
    for comparison in query.additional_comparisons:
        key = comparison.key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise ValueError("report query comparison key must be an identifier")
        if key in comparison_keys:
            raise ValueError("report query comparison keys must be unique")
        if comparison.end_date < comparison.start_date:
            raise ValueError("report query comparison end_date must not precede start_date")
        comparison_keys.add(key)
    times = (query.start_time, query.end_time)
    if any(value is not None for value in times):
        if not all(value is not None for value in times):
            raise ValueError("report query start_time and end_time must be provided together")
        if query.start_time.tzinfo is None or query.end_time.tzinfo is None:
            raise ValueError("report query times must be timezone-aware")
        if query.end_time <= query.start_time:
            raise ValueError("report query end_time must be later than start_time")
    comparison_times = (query.comparison_start_time, query.comparison_end_time)
    if any(value is not None for value in comparison_times):
        if not all(value is not None for value in comparison_times):
            raise ValueError(
                "report query comparison_start_time and comparison_end_time must be provided together"
            )
        if (
            query.comparison_start_time.tzinfo is None
            or query.comparison_end_time.tzinfo is None
        ):
            raise ValueError("report query comparison times must be timezone-aware")
        if query.comparison_end_time <= query.comparison_start_time:
            raise ValueError(
                "report query comparison_end_time must be later than comparison_start_time"
            )
