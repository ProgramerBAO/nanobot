"""Built-in report catalog used by Capability Home and compatibility adapters."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from nanobot.reporting.business_templates import build_business_templates
from nanobot.reporting.contracts import (
    USAGE_METRIC_SEMANTICS,
    MetricDefinition,
    ReportBlock,
    ReportComparisonWindow,
    ReportContext,
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportQuery,
    ReportQueryComparison,
    ReportSource,
    ReportWindow,
)
from nanobot.reporting.cube import CubeConnector, CubeCostAccountTemplate, CubeHealthTemplate
from nanobot.reporting.cube_contract_gate import compare_metric_summaries
from nanobot.reporting.grafana import GrafanaConnector
from nanobot.reporting.provider_quality import CubeProviderQualityConnector, ProviderQualityTemplate
from nanobot.reporting.registry import (
    ConnectorCapabilities,
    ConnectorManifest,
    ConnectorPlugin,
    ReportPluginRegistry,
    TemplateManifest,
    TemplatePlugin,
)
from nanobot.reporting.renderer import (
    DingTalkReportRenderer,
    FeishuReportRenderer,
    TextChannelRenderer,
    WeComReportRenderer,
)
from nanobot.reporting.templates import DeclarativeTemplateSpec, load_builtin_template_specs
from nanobot.utils.report_failures import (
    report_failure_code_from_warning,
    report_failure_message,
)

_USAGE_METRICS = frozenset({"ai.usage.tokens", "ai.requests", "ai.tpm", "ai.tpm.avg"})
_USAGE_DIMENSIONS = frozenset({"tenant", "model", "endpoint", "date"})


class MagikCubeConnector(ConnectorPlugin):
    """Manifest adapter for the existing, separately executed read-only Tool."""

    manifest = ConnectorManifest(
        connector_id="magik_cube",
        display_name="Magik Cube",
        version="1.0",
        auth_methods=("bearer", "password"),
        capabilities=ConnectorCapabilities(
            metrics=_USAGE_METRICS,
            dimensions=_USAGE_DIMENSIONS,
            max_window_days=90,
        ),
        secret_fields=frozenset({"token", "password"}),
        allowed_hosts=("www.magikcloud.cn",),
    )

    async def health_check(self) -> dict[str, Any]:
        return {"status": "delegated", "tool": "magik_cube_daily_report"}

    async def discover_catalog(self) -> dict[str, list[str]]:
        return {"status": ["delegated_to_magik_cube_tool"]}

    async def query(self, query: ReportQuery) -> ReportDataset:
        raise RuntimeError("Magik Cube queries are executed through the compatibility Tool adapter")


class UsageMatrixTemplate(TemplatePlugin):
    """Build Cube usage reports with shared calculations and selectable presentation depth."""

    def __init__(
        self,
        spec: DeclarativeTemplateSpec,
        *,
        connector_ids: frozenset[str] = frozenset({"magik_cube"}),
        semantics_v2: bool = False,
        timezone: str = "Asia/Shanghai",
        presentation: str = "matrix",
    ) -> None:
        if presentation not in {"matrix", "brief"}:
            raise ValueError("usage presentation must be matrix or brief")
        self._spec = spec
        self._semantics_v2 = semantics_v2
        self._timezone = timezone
        self._presentation = presentation
        self.manifest = TemplateManifest(
            template_id=spec.template_id,
            display_name=spec.display_name,
            version=spec.version,
            category=spec.category,
            periods=frozenset({spec.period}),
            required_metrics=frozenset(spec.metrics),
            required_dimensions=frozenset(spec.dimensions),
            connector_ids=connector_ids,
            description=spec.description,
        )
        if semantics_v2:
            self.manifest = replace(
                self.manifest,
                version="2.0",
                description=f"{spec.description} 包含统计窗口、基准、来源和聚合口径。",
            )

    def plan(self, intent: ReportIntent) -> tuple[ReportQuery, ...]:
        if intent.start_date is None or intent.end_date is None:
            raise ValueError("template planning requires concrete dates")
        days = (intent.end_date - intent.start_date).days + 1
        additional_comparisons = (
            (
                ReportQueryComparison(
                    key="previous_week_same_day",
                    start_date=intent.start_date - timedelta(days=7),
                    end_date=intent.end_date - timedelta(days=7),
                ),
            )
            if self._spec.period == "day"
            else ()
        )
        return (
            ReportQuery(
                connector_id=intent.connector_id,
                metrics=self._spec.metrics,
                dimensions=self._spec.dimensions,
                start_date=intent.start_date,
                end_date=intent.end_date,
                comparison_start=intent.start_date - timedelta(days=days),
                comparison_end=intent.start_date - timedelta(days=1),
                additional_comparisons=additional_comparisons,
                filters={
                    "tenant": intent.tenant,
                    "model_scope": intent.model_scope,
                    "models": list(intent.models),
                    "project": intent.project,
                    "endpoint": intent.endpoint,
                    "provider": intent.provider,
                    "all_tenants": intent.filters.get("all_tenants", False),
                },
            ),
        )

    def analyze(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        if self._presentation == "brief":
            return self._analyze_brief(datasets)
        if self._semantics_v2:
            return self._analyze_v2(datasets)
        return self._analyze_legacy(datasets)

    def _analyze_brief(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        """Render only period KPIs while retaining full comparison provenance in context."""

        if len(datasets) != 1:
            raise ValueError("usage brief expects one normalized dataset")
        dataset = datasets[0]
        current = [row for row in dataset.rows if row.get("period", "current") == "current"]
        previous_period = [row for row in dataset.rows if row.get("period") == "comparison"]
        previous_week = [
            row for row in dataset.rows if row.get("period") == "previous_week_same_day"
        ]
        metric_items: list[dict[str, Any]] = []
        for metric in self._spec.metrics:
            current_stat = _usage_metric_stat(current, metric)
            previous_stat = _usage_metric_stat(previous_period, metric)
            weekly_stat = _usage_metric_stat(previous_week, metric)
            semantics = USAGE_METRIC_SEMANTICS[metric]
            comparisons = self._brief_comparisons(
                metric,
                current_stat,
                previous_stat,
                weekly_stat,
            )
            metric_items.append(
                {
                    "label": _usage_metric_display_label(metric, current_stat),
                    "metric": metric,
                    "value": _usage_stat_value(metric, current_stat),
                    "raw_value": current_stat["value"],
                    "baseline_value": previous_stat["value"],
                    "change": comparisons[0]["change"] if comparisons else "",
                    "comparisons": comparisons,
                    "unit": semantics["unit"],
                    "aggregation": semantics["aggregation"],
                    "sample_count": current_stat["sample_count"],
                    "valid_sample_count": current_stat["valid_sample_count"],
                    "source": semantics["source"],
                }
            )
        context = self._usage_context(dataset, metric_items)
        scope = dataset.metadata.get("scope")
        scope = scope if isinstance(scope, Mapping) else {}
        selected_models = [str(item) for item in scope.get("models", ()) if str(item).strip()]
        selected_model = selected_models[0] if len(selected_models) == 1 else ""
        all_tenants = bool(scope.get("all_tenants"))
        title = (
            f"{selected_model} 全部客户{self._spec.display_name}"
            if all_tenants and selected_model
            else self._spec.display_name
        )
        warning_text = "；".join(_display_warning(item) for item in dataset.warnings[:5])
        quality_text = f"数据质量：{dataset.quality}"
        if warning_text:
            quality_text += f"；{warning_text}"
        blocks: list[ReportBlock] = [ReportBlock("metrics", {"items": metric_items})]
        blocks.append(
            ReportBlock(
                "note",
                {
                    "content": quality_text,
                    "severity": "warning" if dataset.quality != "complete" else "info",
                },
            )
        )
        blocks.append(
            ReportBlock(
                "note",
                {
                    "content": (
                        "来源：Cube Admin。Token 和请求数为周期求和；平均 TPM 仅在同一"
                        "客户、模型、Endpoint 序列内计算；峰值 TPM 为最高 Endpoint 峰值。"
                    )
                },
            )
        )
        blocks.append(
            ReportBlock(
                "actions",
                {
                    "actions": [
                        {
                            "action_id": "usage_further_analysis",
                            "label": "进一步分析",
                            "style": "primary",
                            "tool_name": "report_center",
                            "params": self._further_analysis_params(dataset),
                            "command": self._further_analysis_command(dataset),
                        }
                    ]
                },
            )
        )
        summary = "\n".join(
            f"- {item['label']}：{item['value']}"
            + "".join(
                f"｜{comparison['label']}：{comparison['change']}"
                for comparison in item["comparisons"]
            )
            for item in metric_items
        )
        return ReportDocument(
            title=title,
            subtitle="Magik Cube · 简报",
            document_id=self._spec.template_id,
            fallback_text=f"{title}\n{summary}\n{quality_text}\n{_brief_context_text(context)}",
            blocks=tuple(blocks),
            quality=dataset.quality,
            warnings=dataset.warnings,
            context=context,
            version=2,
        )

    def _brief_comparisons(
        self,
        metric: str,
        current_stat: Mapping[str, Any],
        previous_stat: Mapping[str, Any],
        weekly_stat: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply business labels without changing the underlying comparison windows."""

        if metric == "ai.tpm.avg" and current_stat.get("reason") == "multiple_series":
            return []
        current_value = current_stat.get("value")
        if self._spec.period == "day":
            return [
                {
                    "key": "previous_week_same_day",
                    "label": "同比",
                    "baseline_value": weekly_stat.get("value"),
                    "change": _format_metric_change(current_value, weekly_stat.get("value")),
                },
                {
                    "key": "previous_period",
                    "label": "环比",
                    "baseline_value": previous_stat.get("value"),
                    "change": _format_metric_change(current_value, previous_stat.get("value")),
                },
            ]
        return [
            {
                "key": "previous_period",
                "label": "环比",
                "baseline_value": previous_stat.get("value"),
                "change": _format_metric_change(current_value, previous_stat.get("value")),
            }
        ]

    def _further_analysis_params(self, dataset: ReportDataset) -> dict[str, Any]:
        """Carry only validated scope and window values into the opaque detail action."""

        scope = dataset.metadata.get("scope")
        scope = scope if isinstance(scope, Mapping) else {}
        windows = [
            item
            for item in dataset.metadata.get("query_windows") or ()
            if isinstance(item, dict) and item.get("period") == "current"
        ]
        start = min((str(item.get("start") or "")[:10] for item in windows), default="")
        end_exclusive = max((str(item.get("end") or "")[:10] for item in windows), default="")
        try:
            inclusive_end = (date.fromisoformat(end_exclusive) - timedelta(days=1)).isoformat()
        except ValueError:
            inclusive_end = start
        models = [str(item) for item in scope.get("models", ()) if str(item).strip()]
        tenant = str(scope.get("tenant") or "")
        model_scope = str(scope.get("model_scope") or "summary")
        params: dict[str, Any] = {
            "action": "cube_report",
            "period": self._spec.period,
            "report_template": "matrix_card",
            "tenant_query": tenant,
            "models": models,
            "all_tenants": bool(scope.get("all_tenants")),
            "breakdown": "model" if model_scope in {"all", "selected"} else "summary",
            "start_date": start,
            "end_date": inclusive_end,
            "interactive": False,
        }
        if tenant:
            params["report_selections"] = [
                {
                    "tenant_query": tenant,
                    "model_scope": model_scope,
                    "models": models,
                }
            ]
        return params

    def _further_analysis_command(self, dataset: ReportDataset) -> str:
        """Build a deterministic WebUI command that preserves the visible report scope."""

        params = self._further_analysis_params(dataset)
        tenant = (
            "全部客户"
            if params.get("all_tenants")
            else str(params.get("tenant_query") or "").strip() or "默认客户范围"
        )
        selections = params.get("report_selections") or ()
        model_scope = str(selections[0].get("model_scope") or "") if selections else ""
        models = [str(model) for model in params.get("models") or ()]
        model_text = "全部模型" if model_scope == "all" else "、".join(models) or "汇总"
        start = str(params.get("start_date") or "")
        end = str(params.get("end_date") or "")
        period_name = {"day": "日报", "week": "周报", "month": "月报"}.get(
            self._spec.period, "区间报表"
        )
        return (
            f"进一步分析（{period_name}）：客户 {tenant}，模型 {model_text}，"
            f"日期 {start} 至 {end}"
        )

    def shadow_summary(self, datasets: tuple[ReportDataset, ...]) -> dict[str, Any]:
        """Compare legacy and v2 aggregates on one fetched dataset without exposing values."""

        if not self._semantics_v2 or len(datasets) != 1:
            return {"status": "not_applicable"}
        rows = list(datasets[0].rows)
        current = [row for row in rows if row.get("period", "current") == "current"]
        comparison = [row for row in rows if row.get("period") == "comparison"]
        legacy = {
            metric: (_aggregate_metric(current, metric), _aggregate_metric(comparison, metric))
            for metric in self._spec.metrics
        }
        candidate = {
            metric: (
                _usage_metric_stat(current, metric)["value"],
                _usage_metric_stat(comparison, metric)["value"],
            )
            for metric in self._spec.metrics
        }
        return compare_metric_summaries(legacy, candidate)

    def _analyze_legacy(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        if len(datasets) != 1:
            raise ValueError("usage matrix expects one normalized dataset")
        dataset = datasets[0]
        rows = list(dataset.rows)
        current = [row for row in rows if row.get("period", "current") == "current"]
        comparison = [row for row in rows if row.get("period") == "comparison"]
        previous_week = [
            row for row in rows if row.get("period") == "previous_week_same_day"
        ]
        comparison_labels = self._metric_comparison_labels(dataset)
        metric_items = []
        for metric in self._spec.metrics:
            current_stat = _usage_metric_stat(current, metric)
            baseline_stat = _usage_metric_stat(comparison, metric)
            weekly_stat = _usage_metric_stat(previous_week, metric)
            current_value = current_stat["value"]
            comparisons = self._metric_comparisons_for_stats(
                metric,
                current_stat,
                baseline_stat,
                weekly_stat,
                comparison_labels=comparison_labels,
            )
            metric_items.append(
                {
                    "label": _usage_metric_display_label(metric, current_stat),
                    "metric": metric,
                    "value": _usage_stat_value(metric, current_stat),
                    "raw_value": current_value,
                    "baseline_value": baseline_stat["value"],
                    "change": "；".join(
                        f"{item['label']} {item['change']}" for item in comparisons
                    ),
                    "comparisons": comparisons,
                }
            )
        table = _usage_table(current)
        blocks = [ReportBlock("metrics", {"items": metric_items})]
        if table:
            blocks.append(
                ReportBlock(
                    "table",
                    {
                        "columns": [
                            {
                                "tag": "column",
                                "name": "tenant",
                                "display_name": "客户",
                                "data_type": "text",
                            },
                            {
                                "tag": "column",
                                "name": "model",
                                "display_name": "模型",
                                "data_type": "text",
                            },
                            {
                                "tag": "column",
                                "name": "tokens",
                                "display_name": "Token",
                                "data_type": "text",
                            },
                            {
                                "tag": "column",
                                "name": "requests",
                                "display_name": "请求数",
                                "data_type": "text",
                            },
                            {
                                "tag": "column",
                                "name": "tpm",
                                "display_name": "TPM",
                                "data_type": "text",
                            },
                        ],
                        "headers": ["客户", "模型", "Token", "请求数", "TPM"],
                        "rows": [
                            {
                                "tenant": row[0],
                                "model": row[1],
                                "tokens": row[2],
                                "requests": row[3],
                                "tpm": row[4],
                            }
                            for row in table
                        ],
                        "page_size": 8,
                    },
                )
            )
        endpoint_tpm_rows = _usage_endpoint_tpm_table(current)
        if endpoint_tpm_rows:
            blocks.append(_usage_endpoint_tpm_block(endpoint_tpm_rows))
        warning_text = "；".join(_display_warning(item) for item in dataset.warnings)
        if warning_text:
            blocks.append(
                ReportBlock(
                    "note",
                    {
                        "content": f"数据质量：{dataset.quality}；{warning_text}",
                        "severity": "warning",
                    },
                )
            )
        content = "\n".join(
            f"- {item['label']}：{item['value']}（{item['change']}）"
            for item in metric_items
        )
        if endpoint_tpm_rows:
            content += "\n" + _usage_endpoint_tpm_fallback(endpoint_tpm_rows)
        content += (
            "\n来源：Cube Admin / analysis/endpoint-max-tpm/daily/query；"
            "avgTpm 仅在单客户、单模型、单 Endpoint 序列内按有效日期取平均。"
        )
        if warning_text:
            content = f"{content}\n数据提示：{warning_text}"
        return ReportDocument(
            title=self._spec.display_name,
            subtitle="Magik Cube · 固定口径",
            document_id=self._spec.template_id,
            fallback_text=f"{self._spec.display_name}\n{content}",
            blocks=tuple(blocks),
            quality=dataset.quality,
            warnings=dataset.warnings,
        )

    def _analyze_v2(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        if len(datasets) != 1:
            raise ValueError("usage matrix expects one normalized dataset")
        dataset = datasets[0]
        current = [row for row in dataset.rows if row.get("period", "current") == "current"]
        comparison = [row for row in dataset.rows if row.get("period") == "comparison"]
        previous_week = [
            row for row in dataset.rows if row.get("period") == "previous_week_same_day"
        ]
        comparison_labels = self._metric_comparison_labels(dataset)
        metric_items: list[dict[str, Any]] = []
        for metric in self._spec.metrics:
            current_stat = _usage_metric_stat(current, metric)
            baseline_stat = _usage_metric_stat(comparison, metric)
            weekly_stat = _usage_metric_stat(previous_week, metric)
            semantics = USAGE_METRIC_SEMANTICS[metric]
            current_value = current_stat["value"]
            baseline_value = baseline_stat["value"]
            comparisons = self._metric_comparisons_for_stats(
                metric,
                current_stat,
                baseline_stat,
                weekly_stat,
                comparison_labels=comparison_labels,
            )
            metric_items.append(
                {
                    "label": _usage_metric_display_label(metric, current_stat),
                    "metric": metric,
                    "value": _usage_stat_value(metric, current_stat),
                    "raw_value": current_value,
                    "baseline_value": baseline_value,
                    "baseline": _format_usage_value(metric, baseline_value),
                    "change": _format_metric_change(current_value, baseline_value),
                    "comparisons": comparisons,
                    "unit": semantics["unit"],
                    "aggregation": semantics["aggregation"],
                    "sample_count": current_stat["sample_count"],
                    "valid_sample_count": current_stat["valid_sample_count"],
                    "source": semantics["source"],
                    "detail": (
                        f"有效样本日 {current_stat['valid_sample_count']}"
                        if metric == "ai.tpm.avg"
                        and current_stat["reason"] != "multiple_series"
                        else ""
                    ),
                }
            )

        table = _usage_table_v2(current)
        scope = dataset.metadata.get("scope")
        all_tenants = bool(isinstance(scope, Mapping) and scope.get("all_tenants"))
        scoped_models = (
            tuple(str(item) for item in scope.get("models", ()) if str(item).strip())
            if isinstance(scope, Mapping)
            else ()
        )
        selected_model = scoped_models[0] if len(scoped_models) == 1 else ""
        table_title = (
            f"客户用量排行：{selected_model}，按 Token 总量降序"
            if all_tenants and selected_model
            else "模型用量排行：按 Token 总量降序"
        )
        blocks: list[ReportBlock] = [ReportBlock("metrics", {"items": metric_items})]
        if all_tenants and selected_model:
            blocks.append(
                ReportBlock(
                    "note",
                    {
                        "content": (
                            f"查询范围：模型 {selected_model} 的全部 Cube 客户用量；"
                            "客户清单来自 Cube Admin / tenants。"
                        )
                    },
                )
            )
        if table:
            blocks.append(
                ReportBlock(
                    "table",
                    {
                        "title": table_title,
                        "columns": [
                            {"tag": "column", "name": "tenant", "display_name": "客户", "data_type": "text"},
                            {"tag": "column", "name": "model", "display_name": "模型", "data_type": "text"},
                            {"tag": "column", "name": "tokens", "display_name": "Token 总量", "data_type": "text"},
                            {"tag": "column", "name": "requests", "display_name": "请求总数", "data_type": "text"},
                            {"tag": "column", "name": "tpm_peak", "display_name": "最高 Endpoint 峰值 TPM", "data_type": "text"},
                        ],
                        "headers": ["客户", "模型", "Token 总量", "请求总数", "最高 Endpoint 峰值 TPM"],
                        "rows": table,
                        "page_size": 8,
                    },
                )
            )
        endpoint_tpm_rows = _usage_endpoint_tpm_table(current)
        if endpoint_tpm_rows:
            blocks.append(_usage_endpoint_tpm_block(endpoint_tpm_rows))

        context = self._usage_context(dataset, metric_items)
        warning_text = (
            "；".join(_display_warning(item) for item in dataset.warnings[:5])
            if dataset.warnings
            else "无"
        )
        blocks.append(
            ReportBlock(
                "note",
                {
                    "content": (
                        f"数据质量：{dataset.quality}；失败或缺失接口：{warning_text}"
                    ),
                    "severity": "warning" if dataset.quality != "complete" else "info",
                },
            )
        )
        blocks.append(
            ReportBlock(
                "note",
                {
                    "content": (
                        "来源：Cube Admin / analysis/endpoint-max-tpm/daily/query。"
                        "字段：avgTpm 为单 Endpoint 日平均 TPM，maxTpm 为单 Endpoint "
                        "日峰值。聚合：平均 TPM 不跨 Endpoint 或客户汇总。"
                        "变化：仅展示相对基准的百分比，不展示绝对增减值。"
                    )
                },
            )
        )
        summary = "；".join(
            f"{item['label']}：{item['value']}（{item['change']}）"
            for item in metric_items
        )
        fallback_lines = [
            self._spec.display_name,
            summary,
            _usage_context_text(context),
            (
                "读法：Token 和请求数看窗口总量；平均 TPM 只比较同一 Endpoint 序列，"
                "最高 Endpoint 峰值 TPM 只表示窗口内单 Endpoint 峰值。"
            ),
        ]
        if endpoint_tpm_rows:
            fallback_lines.append(_usage_endpoint_tpm_fallback(endpoint_tpm_rows))
        if dataset.warnings:
            fallback_lines.append("数据提示：" + warning_text)
        return ReportDocument(
            title=(
                f"{selected_model} 全部客户{self._spec.display_name}"
                if all_tenants and selected_model
                else self._spec.display_name
            ),
            subtitle=(
                "Magik Cube · 前一日 / 上周同期对比"
                if self._spec.period == "day"
                else f"Magik Cube · {self._comparison_label()}对比"
            ),
            document_id=self._spec.template_id,
            fallback_text="\n".join(fallback_lines),
            blocks=tuple(blocks),
            quality=dataset.quality,
            warnings=dataset.warnings,
            context=context,
            version=2,
        )

    def _usage_context(
        self, dataset: ReportDataset, metric_items: list[dict[str, Any]]
    ) -> ReportContext:
        query_windows = dataset.metadata.get("query_windows") or []

        def window(period: str) -> ReportWindow | None:
            values = [
                item
                for item in query_windows
                if isinstance(item, dict) and item.get("period") == period
            ]
            if not values:
                dates = sorted(
                    str(row.get("date"))
                    for row in dataset.rows
                    if row.get("period") == period and row.get("date")
                )
                if not dates:
                    return None
                return ReportWindow(
                    start=f"{dates[0]} 00:00",
                    end=f"{dates[-1]} 24:00",
                    label=period,
                )
            starts = [str(item.get("start") or "") for item in values if item.get("start")]
            ends = [str(item.get("end") or "") for item in values if item.get("end")]
            return ReportWindow(
                start=min(starts) if starts else "",
                end=max(ends) if ends else "",
                label=period,
            )

        sources = tuple(
            ReportSource(
                str(item.get("system") or "Cube Admin"),
                str(item.get("route") or ""),
                tuple(str(field) for field in item.get("fields") or ()),
            )
            for item in dataset.metadata.get("source_refs") or ()
            if isinstance(item, dict) and item.get("route")
        )
        if not sources:
            sources = (
                ReportSource(
                    "Cube Admin",
                    "analysis/active-tenant-daily-usage/query",
                    ("totalTokens", "requestCount", "date"),
                ),
                ReportSource(
                    "Cube Admin",
                    "analysis/endpoint-max-tpm/daily/query",
                    ("avgTpm", "maxTpm", "date", "model", "endpoint"),
                ),
            )
        definitions = tuple(
            MetricDefinition(
                metric=item["metric"],
                label=item["label"],
                unit=item["unit"],
                aggregation=item["aggregation"],
                source=item["source"],
                direction="informational",
            )
            for item in metric_items
        )
        baseline_window = window("comparison")
        comparison_windows: list[ReportComparisonWindow] = []
        if baseline_window is not None:
            comparison_windows.append(
                ReportComparisonWindow(
                    key="previous_period",
                    label=self._comparison_label(),
                    window=baseline_window,
                )
            )
        previous_week_window = window("previous_week_same_day")
        if previous_week_window is not None:
            comparison_windows.append(
                ReportComparisonWindow(
                    key="previous_week_same_day",
                    label="上周同期",
                    window=previous_week_window,
                )
            )
        return ReportContext(
            timezone=self._timezone,
            current_window=window("current"),
            baseline_window=baseline_window,
            baseline_policy="previous_equal_window",
            comparison_windows=tuple(comparison_windows),
            sources=sources,
            metric_definitions=definitions,
            calculation_version="2",
            quality=dataset.quality,
            quality_reasons=tuple(dataset.warnings),
            freshness=str(dataset.metadata.get("last_sample_at") or ""),
            template_version=self.manifest.version,
        )

    def _comparison_label(self) -> str:
        return {
            "day": "前一日",
            "week": "上上周",
            "month": "前一月",
        }.get(self._spec.period, "前一等长周期")

    def _metric_comparisons_for_stats(
        self,
        metric: str,
        current_stat: Mapping[str, Any],
        baseline_stat: Mapping[str, Any],
        weekly_stat: Mapping[str, Any],
        *,
        comparison_labels: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        if metric == "ai.tpm.avg" and current_stat.get("reason") == "multiple_series":
            return []
        current_value = current_stat.get("value")
        baseline_value = baseline_stat.get("value")
        weekly_value = weekly_stat.get("value")
        comparisons = [
            {
                "key": "previous_period",
                "label": comparison_labels["previous_period"],
                "baseline_value": baseline_value,
                "baseline": _format_usage_value(metric, baseline_value),
                "change": _format_metric_change(current_value, baseline_value),
            }
        ]
        if self._spec.period == "day":
            comparisons.append(
                {
                    "key": "previous_week_same_day",
                    "label": comparison_labels["previous_week_same_day"],
                    "baseline_value": weekly_value,
                    "baseline": _format_usage_value(metric, weekly_value),
                    "change": _format_metric_change(current_value, weekly_value),
                }
            )
        return comparisons

    def _metric_comparison_labels(self, dataset: ReportDataset) -> dict[str, str]:
        """Attach exact dates to user-visible comparisons without changing raw windows."""

        windows = {
            str(item.get("period")): item
            for item in dataset.metadata.get("query_windows") or ()
            if isinstance(item, dict) and item.get("period")
        }

        def label(period: str, default: str) -> str:
            window = windows.get(period, {})
            start = str(window.get("start") or "")
            end = str(window.get("end") or "")
            if not start:
                return default
            try:
                start_date = date.fromisoformat(start[:10])
                end_date = date.fromisoformat(end[:10]) if end else start_date
                inclusive_end = end_date - timedelta(days=1) if end_date > start_date else end_date
                date_text = (
                    start_date.isoformat()
                    if inclusive_end == start_date
                    else f"{start_date.isoformat()} - {inclusive_end.isoformat()}"
                )
            except ValueError:
                date_text = start if not end or start == end else f"{start} - {end}"
            return f"{default}（{date_text}）"

        return {
            "previous_period": label("comparison", f"较{self._comparison_label()}"),
            "previous_week_same_day": label(
                "previous_week_same_day", "较上周同期"
            ),
        }


def _usage_metric_display_label(metric: str, stat: Mapping[str, Any]) -> str:
    """Name peak TPM precisely when the scope contains multiple endpoint series."""

    if metric == "ai.tpm" and int(stat.get("series_count") or 0) > 1:
        return "最高 Endpoint 峰值 TPM"
    return _metric_label(metric)


def _metric_label(metric: str) -> str:
    return {
        "ai.usage.tokens": "Token 消耗",
        "ai.requests": "请求数",
        "ai.tpm.avg": "平均 TPM",
        "ai.tpm": "峰值 TPM",
    }.get(metric, metric)


def _display_warning(warning: str) -> str:
    code = report_failure_code_from_warning(warning)
    return report_failure_message(code) if code else warning


def _aggregate_metric(rows: list[dict[str, Any]], metric: str) -> int | float | None:
    return _usage_metric_stat(rows, metric)["value"]


def _format_metric_change(
    current: int | float | None,
    baseline: int | float | None,
) -> str:
    if current is None:
        return "当前无数据"
    if baseline is None:
        return "暂无可比基准"
    if baseline == 0:
        return "新增" if current else "无变化"
    change = (current - baseline) / baseline * 100
    if change > 0:
        return f"↑{change:.1f}%"
    if change < 0:
        return f"↓{abs(change):.1f}%"
    return "0.0%"


def _usage_table(rows: list[dict[str, Any]]) -> list[list[Any]]:
    grouped: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for row in rows:
        tenant = str(row.get("tenant") or "-")
        model = str(row.get("model") or "汇总")
        metric = str(row.get("metric") or "")
        try:
            value = float(row.get("value"))
            current = grouped[(tenant, model)].get(metric, 0)
            grouped[(tenant, model)][metric] = (
                max(current, value)
                if metric == "ai.tpm"
                else current + value
            )
        except (TypeError, ValueError):
            continue
    ordered = sorted(
        grouped.items(),
        key=lambda item: item[1].get("ai.usage.tokens", 0),
        reverse=True,
    )
    return [
        [
            tenant,
            model,
            int(values.get("ai.usage.tokens", 0)),
            int(values.get("ai.requests", 0)),
            int(values.get("ai.tpm", 0)),
        ]
        for (tenant, model), values in ordered[:20]
    ]


def _usage_metric_stat(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    values: list[float] = []
    for row in rows:
        if row.get("metric") != metric:
            continue
        try:
            values.append(float(row.get("value")))
        except (TypeError, ValueError):
            continue
    if not values:
        return {
            "value": None,
            "average": None,
            "sample_count": 0,
            "valid_sample_count": 0,
            "series_count": 0,
            "reason": "no_data",
        }
    series = {
        (
            str(row.get("tenant") or ""),
            str(row.get("model") or ""),
            str(row.get("endpoint") or ""),
        )
        for row in rows
        if row.get("metric") == metric
    }
    if metric == "ai.tpm.avg" and len(series) != 1:
        return {
            "value": None,
            "average": None,
            "sample_count": len(values),
            "valid_sample_count": len(values),
            "series_count": len(series),
            "reason": "multiple_series",
        }
    if metric == "ai.tpm":
        return {
            "value": max(values),
            "average": sum(values) / len(values),
            "sample_count": len(values),
            "valid_sample_count": len(values),
            "series_count": len(series),
            "reason": "",
        }
    if metric == "ai.tpm.avg":
        average = sum(values) / len(values)
        return {
            "value": average,
            "average": average,
            "sample_count": len(values),
            "valid_sample_count": len(values),
            "series_count": 1,
            "reason": "",
        }
    return {
        "value": sum(values),
        "average": sum(values) / len(values),
        "sample_count": len(values),
        "valid_sample_count": len(values),
        "series_count": len(series),
        "reason": "",
    }


def _format_usage_value(metric: str, value: float | None) -> str:
    if value is None:
        return "暂无数据"
    if metric in {"ai.usage.tokens", "ai.requests"}:
        return f"{int(value):,}"
    return f"{value:,.0f} tokens/min"


def _usage_stat_value(metric: str, stat: Mapping[str, Any]) -> str:
    """Format one usage statistic while preserving the no-cross-series rule."""

    if metric == "ai.tpm.avg" and stat.get("reason") == "multiple_series":
        return "多 Endpoint/客户，不汇总"
    return _format_usage_value(metric, stat.get("value"))


def _usage_endpoint_tpm_table(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Aggregate avgTpm only within one tenant/model/Endpoint series."""

    grouped: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"avg": [], "peak": [], "dates": set()}
    )
    for row in rows:
        metric = str(row.get("metric") or "")
        if metric not in {"ai.tpm", "ai.tpm.avg"}:
            continue
        key = (
            str(row.get("tenant") or "-"),
            str(row.get("model") or "-"),
            str(row.get("endpoint") or "-"),
        )
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        if metric == "ai.tpm.avg":
            grouped[key]["avg"].append(value)
            grouped[key]["dates"].add(str(row.get("date") or ""))
        else:
            grouped[key]["peak"].append(value)

    result: list[dict[str, str]] = []
    sortable: list[tuple[float, str, str, str, dict[str, str]]] = []
    for (tenant, model, endpoint), values in grouped.items():
        average = sum(values["avg"]) / len(values["avg"]) if values["avg"] else None
        peak = max(values["peak"]) if values["peak"] else None
        samples = len(values["dates"])
        row = {
            "tenant": tenant,
            "model": model,
            "endpoint": endpoint,
            "avg_tpm": _format_usage_value("ai.tpm.avg", average),
            "peak_tpm": _format_usage_value("ai.tpm", peak),
            "samples": str(samples),
            "quality": "完整" if average is not None else "平均 TPM 暂不可用",
        }
        sort_value = average if average is not None else -1.0
        sortable.append((-sort_value, tenant.casefold(), model.casefold(), endpoint.casefold(), row))
    for _average, _tenant, _model, _endpoint, row in sorted(sortable):
        result.append(row)
    return result


def _usage_endpoint_tpm_block(rows: list[dict[str, str]]) -> ReportBlock:
    """Build the shared endpoint detail block without exposing raw Cube responses."""

    columns = [
        {"tag": "column", "name": "tenant", "display_name": "客户", "data_type": "text"},
        {"tag": "column", "name": "model", "display_name": "模型", "data_type": "text"},
        {
            "tag": "column",
            "name": "endpoint",
            "display_name": "Endpoint",
            "data_type": "text",
        },
        {
            "tag": "column",
            "name": "avg_tpm",
            "display_name": "平均 TPM",
            "data_type": "text",
        },
        {
            "tag": "column",
            "name": "peak_tpm",
            "display_name": "峰值 TPM",
            "data_type": "text",
        },
        {
            "tag": "column",
            "name": "samples",
            "display_name": "有效样本日数",
            "data_type": "text",
        },
        {
            "tag": "column",
            "name": "quality",
            "display_name": "数据质量",
            "data_type": "text",
        },
    ]
    return ReportBlock(
        "table",
        {
            "title": "Endpoint TPM 明细：按平均 TPM 降序",
            "columns": columns,
            "headers": [item["display_name"] for item in columns],
            "rows": rows,
            "page_size": 8,
        },
    )


def _usage_endpoint_tpm_fallback(rows: list[dict[str, str]]) -> str:
    """Keep text-only channels semantically aligned with structured report cards."""

    lines = ["Endpoint TPM 明细：按平均 TPM 降序"]
    lines.extend(
        (
            f"- {row['tenant']} / {row['model']} / {row['endpoint']}："
            f"平均 {row['avg_tpm']}，峰值 {row['peak_tpm']}，"
            f"有效样本日 {row['samples']}，{row['quality']}"
        )
        for row in rows[:20]
    )
    return "\n".join(lines)


def _usage_table_v2(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        tenant = str(row.get("tenant") or "-")
        model = str(row.get("model") or "汇总")
        metric = str(row.get("metric") or "")
        if metric not in USAGE_METRIC_SEMANTICS:
            continue
        try:
            grouped[(tenant, model)][metric].append(float(row.get("value")))
        except (TypeError, ValueError):
            continue
    ordered = sorted(
        grouped.items(),
        key=lambda item: (
            -sum(item[1].get("ai.usage.tokens", ())),
            item[0][0].casefold(),
            item[0][1].casefold(),
        ),
    )
    result: list[dict[str, Any]] = []
    for (tenant, model), values in ordered[:20]:
        token_total = sum(values.get("ai.usage.tokens", ()))
        request_total = sum(values.get("ai.requests", ()))
        tpm_values = values.get("ai.tpm", ())
        tpm_peak = max(tpm_values) if tpm_values else None
        result.append(
            {
                "tenant": tenant,
                "model": model,
                "tokens": f"{int(token_total):,}",
                "requests": f"{int(request_total):,}",
                "tpm_peak": _format_usage_value("ai.tpm", tpm_peak),
            }
        )
    return result


def _usage_context_text(context: ReportContext) -> str:
    current = context.current_window
    baseline = context.baseline_window
    current_text = f"{current.start} - {current.end}" if current else "暂无"
    baseline_text = f"{baseline.start} - {baseline.end}" if baseline else "暂无可比基准"
    sources = "；".join(
        dict.fromkeys(f"{item.system} / {item.route}" for item in context.sources)
    ) or "已配置报表数据源"
    aggregation = "、".join(
        dict.fromkeys(item.aggregation for item in context.metric_definitions)
    ) or "按指标定义聚合"
    comparison_lines = (
        [
            f"对比（{item.label}）：{item.window.start} - {item.window.end}"
            for item in context.comparison_windows
        ]
        if context.comparison_windows
        else [f"对比基准：{baseline_text}"]
    )
    return (
        f"当前窗口：{current_text}\n"
        + "\n".join(comparison_lines)
        + "\n"
        f"时区：{context.timezone}\n"
        f"来源：{sources}\n"
        f"口径：{aggregation}"
    )


def _brief_context_text(context: ReportContext) -> str:
    """Keep named brief baselines traceable without restoring a comparison section."""

    named = {item.key: item for item in context.comparison_windows}
    previous = named.get("previous_period")
    weekly = named.get("previous_week_same_day")
    lines: list[str] = []
    if weekly is not None:
        lines.append(f"同比基准：上周同期 {weekly.window.start} - {weekly.window.end}")
    if previous is not None:
        prefix = "前一日" if weekly is not None else "前一等长周期"
        lines.append(f"环比基准：{prefix} {previous.window.start} - {previous.window.end}")
    if not lines:
        lines.append("环比基准：暂无可比基准")
    lines.append(f"时区：{context.timezone}")
    sources = "；".join(dict.fromkeys(item.system for item in context.sources)) or "Cube Admin"
    lines.append(f"来源：{sources}")
    return "\n".join(lines)


def build_default_registry(
    *,
    discover_external: bool = True,
    magik_enabled: bool = True,
    grafana_config: Mapping[str, Any] | None = None,
    cube_config: Any | None = None,
    cube_templates_enabled: bool = True,
    cube_health_template_enabled: bool = False,
    cube_health_semantics_v2: bool = False,
    cube_health_card_v2: bool = False,
    cube_ttft_detail_enabled: bool = False,
    cube_usage_semantics_v2: bool = False,
    cube_usage_brief_template_enabled: bool = True,
    cube_cost_template_enabled: bool = False,
    cube_provider_quality_connector_enabled: bool = False,
    cube_provider_quality_template_enabled: bool = False,
    cube_provider_quality_detail_enabled: bool = False,
    timezone: str = "Asia/Shanghai",
    health_thresholds: Mapping[str, Any] | None = None,
    wecom_renderer_enabled: bool = True,
    dingtalk_renderer_enabled: bool = True,
) -> ReportPluginRegistry:
    registry = ReportPluginRegistry()
    registry.register_renderer(TextChannelRenderer())
    registry.register_renderer(FeishuReportRenderer())
    if wecom_renderer_enabled:
        registry.register_renderer(WeComReportRenderer())
    if dingtalk_renderer_enabled:
        registry.register_renderer(DingTalkReportRenderer())
    if magik_enabled:
        registry.register_connector(
            CubeConnector(
                cube_config,
                ttft_detail_enabled=cube_ttft_detail_enabled,
            )
            if cube_config is not None
            else MagikCubeConnector()
        )
    cube_health_active = cube_health_template_enabled and isinstance(
        registry.connector("magik_cube"), CubeConnector
    )
    for template in build_business_templates():
        if cube_health_active and template.manifest.template_id == "health_sre":
            continue
        registry.register_template(template)
    if cube_templates_enabled:
        for spec in load_builtin_template_specs():
            is_brief = spec.template_id.endswith("_brief")
            if is_brief and not cube_usage_brief_template_enabled:
                continue
            registry.register_template(
                UsageMatrixTemplate(
                    spec,
                    connector_ids=frozenset({"magik_cube"}),
                    semantics_v2=(cube_usage_semantics_v2 or is_brief),
                    timezone=timezone,
                    presentation="brief" if is_brief else "matrix",
                )
            )
    if cube_health_active:
        registry.register_template(
            CubeHealthTemplate(
                thresholds=health_thresholds,
                semantics_v2=cube_health_semantics_v2,
                presentation_v2=cube_health_card_v2,
                ttft_detail_enabled=cube_ttft_detail_enabled,
                timezone=timezone,
            )
        )
    if (
        cube_cost_template_enabled
        and isinstance(registry.connector("magik_cube"), CubeConnector)
        and registry.connector("magik_cube").account_configured
    ):
        registry.register_template(CubeCostAccountTemplate(timezone=timezone))
    if cube_provider_quality_connector_enabled and cube_config is not None:
        try:
            registry.register_connector(
                CubeProviderQualityConnector(
                    cube_config,
                    include_details=cube_provider_quality_detail_enabled,
                )
            )
        except Exception as exc:
            registry.load_errors["builtin:cube_provider_quality"] = type(exc).__name__
    if cube_provider_quality_template_enabled and registry.connector("cube_provider_quality") is not None:
        registry.register_template(ProviderQualityTemplate(timezone=timezone))
    if grafana_config and bool(grafana_config.get("enabled", False)):
        try:
            registry.register_connector(GrafanaConnector.from_mapping(grafana_config))
        except Exception as exc:
            registry.load_errors["builtin:grafana"] = type(exc).__name__
    if discover_external:
        registry.discover_entry_points()
    return registry
