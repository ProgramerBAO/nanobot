"""Built-in report catalog used by Capability Home and compatibility adapters."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

from nanobot.reporting.business_templates import build_business_templates
from nanobot.reporting.contracts import (
    USAGE_METRIC_SEMANTICS,
    MetricDefinition,
    ReportBlock,
    ReportContext,
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportQuery,
    ReportSource,
    ReportWindow,
)
from nanobot.reporting.cube import CubeConnector, CubeCostAccountTemplate, CubeHealthTemplate
from nanobot.reporting.cube_contract_gate import compare_metric_summaries
from nanobot.reporting.grafana import GrafanaConnector
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

_USAGE_METRICS = frozenset({"ai.usage.tokens", "ai.requests", "ai.tpm"})
_USAGE_DIMENSIONS = frozenset({"tenant", "model", "date"})


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
    def __init__(
        self,
        spec: DeclarativeTemplateSpec,
        *,
        connector_ids: frozenset[str] = frozenset({"magik_cube"}),
        semantics_v2: bool = False,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self._spec = spec
        self._semantics_v2 = semantics_v2
        self._timezone = timezone
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
        return (
            ReportQuery(
                connector_id=intent.connector_id,
                metrics=self._spec.metrics,
                dimensions=self._spec.dimensions,
                start_date=intent.start_date,
                end_date=intent.end_date,
                comparison_start=intent.start_date - timedelta(days=days),
                comparison_end=intent.start_date - timedelta(days=1),
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
        if self._semantics_v2:
            return self._analyze_v2(datasets)
        return self._analyze_legacy(datasets)

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
        metric_items = []
        for metric in self._spec.metrics:
            current_value = _aggregate_metric(current, metric)
            baseline_value = _aggregate_metric(comparison, metric)
            metric_items.append(
                {
                    "label": _metric_label(metric),
                    "metric": metric,
                    "value": current_value,
                    "change": _format_metric_change(current_value, baseline_value),
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
        metric_items: list[dict[str, Any]] = []
        for metric in self._spec.metrics:
            current_stat = _usage_metric_stat(current, metric)
            baseline_stat = _usage_metric_stat(comparison, metric)
            semantics = USAGE_METRIC_SEMANTICS[metric]
            current_value = current_stat["value"]
            baseline_value = baseline_stat["value"]
            metric_items.append(
                {
                    "label": "TPM 峰值" if metric == "ai.tpm" else semantics["label"],
                    "metric": metric,
                    "value": _format_usage_value(metric, current_value),
                    "raw_value": current_value,
                    "baseline_value": baseline_value,
                    "baseline": _format_usage_value(metric, baseline_value),
                    "change": _format_metric_change(current_value, baseline_value),
                    "unit": semantics["unit"],
                    "aggregation": semantics["aggregation"],
                    "sample_count": current_stat["sample_count"],
                    "valid_sample_count": current_stat["valid_sample_count"],
                    "source": semantics["source"],
                    "detail": (
                        f"平均日峰值 {_format_usage_value(metric, current_stat['average'])}"
                        if metric == "ai.tpm" and current_stat["average"] is not None
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
                            {"tag": "column", "name": "tpm_peak", "display_name": "TPM 峰值", "data_type": "text"},
                        ],
                        "headers": ["客户", "模型", "Token 总量", "请求总数", "TPM 峰值"],
                        "rows": table,
                        "page_size": 8,
                    },
                )
            )

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
                        "读法：Token 和请求数是窗口总量；TPM 展示日峰值中的窗口峰值，"
                        "不能直接当作窗口平均流量。较基准为当前值相对前一等长窗口的变化。"
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
            "读法：Token 和请求数看窗口总量；TPM 看日峰值中的窗口峰值。",
        ]
        if dataset.warnings:
            fallback_lines.append("数据提示：" + warning_text)
        return ReportDocument(
            title=(
                f"{selected_model} 全部客户{self._spec.display_name}"
                if all_tenants and selected_model
                else self._spec.display_name
            ),
            subtitle="Magik Cube · 前一等长窗口对比",
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
                    ("maxTpm", "date"),
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
        return ReportContext(
            timezone=self._timezone,
            current_window=window("current"),
            baseline_window=window("comparison"),
            baseline_policy="previous_equal_window",
            sources=sources,
            metric_definitions=definitions,
            calculation_version="2",
            quality=dataset.quality,
            quality_reasons=tuple(dataset.warnings),
            freshness=str(dataset.metadata.get("last_sample_at") or ""),
            template_version=self.manifest.version,
        )


def _metric_label(metric: str) -> str:
    return {
        "ai.usage.tokens": "Token 消耗",
        "ai.requests": "请求数",
        "ai.tpm": "TPM 峰值",
    }.get(metric, metric)


def _display_warning(warning: str) -> str:
    code = report_failure_code_from_warning(warning)
    return report_failure_message(code) if code else warning


def _aggregate_metric(rows: list[dict[str, Any]], metric: str) -> int | float | None:
    values = []
    for row in rows:
        if row.get("metric") != metric:
            continue
        try:
            values.append(float(row.get("value")))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    if metric == "ai.tpm":
        return int(max(values))
    return int(sum(values))


def _format_metric_change(
    current: int | float | None,
    baseline: int | float | None,
) -> str:
    if current is None:
        return "当前无数据"
    if baseline is None:
        return "无对比数据"
    if baseline == 0:
        return "新增" if current else "持平"
    change = (current - baseline) / abs(baseline)
    return f"{change:+.1%}"


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
        return {"value": None, "average": None, "sample_count": 0, "valid_sample_count": 0}
    if metric == "ai.tpm":
        return {
            "value": max(values),
            "average": sum(values) / len(values),
            "sample_count": len(values),
            "valid_sample_count": len(values),
        }
    return {
        "value": sum(values),
        "average": sum(values) / len(values),
        "sample_count": len(values),
        "valid_sample_count": len(values),
    }


def _format_usage_value(metric: str, value: float | None) -> str:
    if value is None:
        return "暂无数据"
    if metric in {"ai.usage.tokens", "ai.requests"}:
        return f"{int(value):,}"
    return f"{value:,.0f} tokens/min"


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
    return (
        f"当前窗口：{current_text}\n"
        f"对比基准：{baseline_text}\n"
        f"时区：{context.timezone}\n"
        f"来源：{sources}\n"
        f"口径：{aggregation}"
    )


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
    cube_cost_template_enabled: bool = False,
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
            registry.register_template(
                UsageMatrixTemplate(
                    spec,
                    connector_ids=frozenset({"magik_cube"}),
                    semantics_v2=cube_usage_semantics_v2,
                    timezone=timezone,
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
    if grafana_config and bool(grafana_config.get("enabled", False)):
        try:
            registry.register_connector(GrafanaConnector.from_mapping(grafana_config))
        except Exception as exc:
            registry.load_errors["builtin:grafana"] = type(exc).__name__
    if discover_external:
        registry.discover_entry_points()
    return registry
