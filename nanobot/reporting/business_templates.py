"""Small deterministic templates for connector-backed operational reports."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from nanobot.reporting.contracts import (
    ReportBlock,
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportQuery,
)
from nanobot.reporting.registry import TemplateManifest, TemplatePlugin


class FixedMetricTemplate(TemplatePlugin):
    """Summarize a fixed metric set without allowing caller-supplied expressions."""

    def __init__(
        self,
        *,
        template_id: str,
        display_name: str,
        metrics: tuple[str, ...],
        dimensions: tuple[str, ...],
        periods: frozenset[str],
        description: str,
        aggregations: dict[str, str] | None = None,
    ) -> None:
        self._metrics = metrics
        self._dimensions = dimensions
        self._aggregations = aggregations or {}
        self.manifest = TemplateManifest(
            template_id=template_id,
            display_name=display_name,
            version="1.0",
            category="operations",
            periods=periods,
            required_metrics=frozenset(metrics),
            required_dimensions=frozenset(dimensions),
            connector_ids=frozenset({"grafana"}),
            description=description,
        )

    def plan(self, intent: ReportIntent) -> tuple[ReportQuery, ...]:
        if intent.start_date is None or intent.end_date is None:
            raise ValueError("template planning requires concrete dates")
        days = (intent.end_date - intent.start_date).days + 1
        filters = dict(intent.filters)
        filters.update(
            {
                "tenant": intent.tenant,
                "project": intent.project,
                "endpoint": intent.endpoint,
                "environment": intent.environment,
                "provider": intent.provider,
                "model_scope": intent.model_scope,
                "models": list(intent.models),
            }
        )
        filters = {key: value for key, value in filters.items() if value not in (None, "", [])}
        return (
            ReportQuery(
                connector_id=intent.connector_id,
                metrics=self._metrics,
                dimensions=self._dimensions,
                start_date=intent.start_date,
                end_date=intent.end_date,
                filters=filters,
                comparison_start=intent.start_date - timedelta(days=days),
                comparison_end=intent.start_date - timedelta(days=1),
            ),
        )

    def analyze(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        rows = [row for dataset in datasets for row in dataset.rows]
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            metric = str(row.get("metric") or "")
            if metric not in self._metrics:
                continue
            try:
                grouped[metric].append(float(row.get("value")))
            except (TypeError, ValueError):
                continue
        items = [
            {
                "label": _METRIC_LABELS.get(metric, metric),
                "metric": metric,
                "value": _aggregate(metric, values, self._aggregations.get(metric, "avg")),
            }
            for metric in self._metrics
            for values in [grouped.get(metric, [])]
        ]
        endpoint_rows = _top_dimension_rows(rows, "endpoint")
        blocks = [ReportBlock("metrics", {"items": items})]
        if endpoint_rows:
            blocks.append(
                ReportBlock(
                    "table",
                    {"headers": ["Endpoint", "Value"], "rows": endpoint_rows},
                )
            )
        content = "\n".join(f"- {_METRIC_LABELS.get(item['metric'], item['metric'])}: {item['value']}" for item in items)
        return ReportDocument(
            title=self.manifest.display_name,
            subtitle="固定指标 · 确定性计算",
            document_id=self.manifest.template_id,
            fallback_text=f"{self.manifest.display_name}\n{content}",
            blocks=tuple(blocks),
        )


_METRIC_LABELS = {
    "ai.usage.tokens": "Token 消耗",
    "ai.requests": "请求数",
    "ai.rpm": "RPM",
    "ai.tpm": "TPM",
    "ai.error_rate": "错误率",
    "ai.http_4xx_rate": "HTTP 4xx",
    "ai.http_5xx_rate": "HTTP 5xx",
    "ai.interface_delay": "接口延迟",
    "ai.ttft": "TTFT",
    "ai.cost": "成本",
    "ai.balance": "余额",
    "ai.unbilled_amount": "未结算金额",
    "ai.gpu_hours": "GPU Hours",
    "ai.capacity_utilization": "容量使用率",
}


def _aggregate(metric: str, values: list[float], operation: str) -> float | None:
    if not values:
        return None
    if operation == "sum":
        return round(sum(values), 6)
    if operation == "max":
        return round(max(values), 6)
    if operation == "last":
        return round(values[-1], 6)
    return round(sum(values) / len(values), 6)


def _top_dimension_rows(rows: list[dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
    values: dict[str, float] = defaultdict(float)
    for row in rows:
        key = str(row.get(dimension) or "").strip()
        if not key:
            continue
        try:
            values[key] = max(values[key], float(row.get("value")))
        except (TypeError, ValueError):
            continue
    return [
        {"Endpoint": key, "Value": round(value, 6)}
        for key, value in sorted(values.items(), key=lambda item: item[1], reverse=True)[:10]
    ]


def build_business_templates() -> tuple[FixedMetricTemplate, ...]:
    return (
        FixedMetricTemplate(
            template_id="health_sre",
            display_name="SRE 健康报告",
            metrics=(
                "ai.error_rate", "ai.http_4xx_rate", "ai.http_5xx_rate", "ai.interface_delay",
                "ai.ttft", "ai.rpm", "ai.tpm",
            ),
            dimensions=("date", "hour", "model", "endpoint"),
            periods=frozenset({"day", "week", "recent7", "range"}),
            description="固定错误率、延迟、TTFT、RPM 和 TPM 健康摘要。",
            aggregations={
                "ai.http_5xx_rate": "max",
                "ai.error_rate": "max",
                "ai.tpm": "max",
                "ai.rpm": "max",
            },
        ),
        FixedMetricTemplate(
            template_id="cost_summary",
            display_name="成本与余额报告",
            metrics=("ai.usage.tokens", "ai.requests", "ai.cost", "ai.balance", "ai.unbilled_amount"),
            dimensions=("tenant", "project", "model", "date"),
            periods=frozenset({"day", "week", "month", "recent7", "range"}),
            description="固定 Token、请求、成本、余额和未结算金额摘要。",
            aggregations={"ai.usage.tokens": "sum", "ai.requests": "sum", "ai.cost": "sum"},
        ),
        FixedMetricTemplate(
            template_id="capacity_summary",
            display_name="容量与 GPU 报告",
            metrics=("ai.tpm", "ai.capacity_utilization", "ai.gpu_hours"),
            dimensions=("cluster", "model", "date", "hour"),
            periods=frozenset({"day", "week", "month", "recent7", "range"}),
            description="固定 TPM、容量使用率和 GPU Hours 摘要。",
            aggregations={"ai.tpm": "max", "ai.capacity_utilization": "max", "ai.gpu_hours": "sum"},
        ),
    )
