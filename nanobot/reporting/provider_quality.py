"""Read-only Cube provider quality connector and deterministic template."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.agent.tools.magik_cube import MagikCubeClient, MagikCubeToolConfig, _as_int, _pick
from nanobot.reporting.contracts import (
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
from nanobot.reporting.interactions import ReportInteraction
from nanobot.reporting.registry import (
    ConnectorCapabilities,
    ConnectorManifest,
    ConnectorPlugin,
    TemplateManifest,
    TemplatePlugin,
)
from nanobot.utils.report_failures import classify_report_failure

_QUALITY_METRICS = frozenset(
    {
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
    }
)
_QUALITY_DIMENSIONS = frozenset({"provider", "model", "endpoint", "cluster", "date", "hour"})
_ALLOWED_FILTERS = frozenset(
    {
        "provider_quality",
        "provider",
        "providers",
        "provider_id",
        "model",
        "endpoint",
        "include_empty",
        "period",
        "start_date",
        "end_date",
    }
)
_PERFORMANCE_METRICS = {
    "ai.provider.throughput": "PROVIDER_METRIC_THROUGHPUT",
    "ai.provider.latency": "PROVIDER_METRIC_LATENCY",
    "ai.provider.error_rate": "PROVIDER_METRIC_ERROR_RATE",
    "ai.provider.tpm": "PROVIDER_METRIC_TPM",
}
_METRIC_LABELS = {
    "ai.provider.throughput": ("吞吐", "tokens/s", "P99/时间桶值"),
    "ai.provider.latency": ("E2E 延迟", "ms", "P99/时间桶值"),
    "ai.provider.error_rate": ("错误率", "ratio", "P99/时间桶值"),
    "ai.provider.tpm": ("TPM", "tokens/minute", "时间桶峰值"),
    "ai.provider.tokens": ("Token 流量", "tokens", "窗口总和"),
    "ai.provider.traffic_ratio": ("流量占比", "ratio", "时间桶峰值"),
    "ai.provider.requests": ("请求数", "requests", "详情快照"),
    "ai.provider.actual_tpm": ("实际 TPM", "tokens/minute", "详情快照"),
    "ai.provider.avg_latency": ("平均延迟", "ms", "详情快照平均值"),
    "ai.provider.avg_ttft": ("平均 TTFT", "ms", "详情快照平均值"),
    "ai.provider.test_score": ("测试得分", "score", "同测试集原值"),
    "ai.provider.input_price": ("输入单价", "currency/token", "配置快照"),
    "ai.provider.output_price": ("输出单价", "currency/token", "配置快照"),
}


class CubeProviderQualityConnector(ConnectorPlugin):
    """Fetch approved provider catalog and fixed provider quality contracts."""

    manifest = ConnectorManifest(
        connector_id="cube_provider_quality",
        display_name="Cube 供应商质量",
        version="1.0",
        auth_methods=("bearer", "password"),
        capabilities=ConnectorCapabilities(
            metrics=_QUALITY_METRICS,
            dimensions=_QUALITY_DIMENSIONS,
            max_window_days=90,
            supports_bulk_dimensions=True,
            read_only=True,
            supports_catalog_discovery=True,
        ),
        secret_fields=frozenset({"access_token", "password", "api_key"}),
        allowed_hosts=(),
    )

    def __init__(
        self,
        config: MagikCubeToolConfig,
        *,
        transport: Any = None,
        include_details: bool = False,
    ) -> None:
        self._config = config
        self._transport = transport
        self._include_details = include_details
        self._tz = ZoneInfo("Asia/Shanghai")

    async def health_check(self) -> dict[str, Any]:
        configured = bool(
            self._config.base_url
            and (self._config.access_token or (self._config.account and self._config.password))
        )
        return {"status": "configured" if configured else "unconfigured", "connector": self.manifest.connector_id}

    async def discover_catalog(self) -> dict[str, list[str]]:
        return {"metrics": sorted(_QUALITY_METRICS), "dimensions": sorted(_QUALITY_DIMENSIONS)}

    async def list_provider_catalog(self) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        """Return the sanitized live provider catalog for report selectors."""

        warnings: list[str] = []
        async with MagikCubeClient(self._config, transport=self._transport) as client:
            catalog = await self._list_providers(client, {}, warnings)
        return catalog, tuple(dict.fromkeys(warnings))

    async def query(self, query: ReportQuery) -> ReportDataset:
        if query.connector_id != self.manifest.connector_id:
            raise ValueError("provider quality connector received another connector query")
        if set(query.metrics) - _QUALITY_METRICS:
            raise ValueError("provider quality query contains unsupported metrics")
        if set(query.dimensions) - _QUALITY_DIMENSIONS:
            raise ValueError("provider quality query contains unsupported dimensions")
        if set(query.filters) - _ALLOWED_FILTERS:
            raise ValueError("provider quality query contains unsupported filters")

        current_window = (
            (query.start_time, query.end_time)
            if query.start_time is not None and query.end_time is not None
            else self._date_window(query.start_date, query.end_date)
        )
        windows = [("current", current_window)]
        if query.comparison_start_time is not None and query.comparison_end_time is not None:
            windows.append(("comparison", (query.comparison_start_time, query.comparison_end_time)))
        elif query.comparison_start and query.comparison_end:
            windows.append(("comparison", self._date_window(query.comparison_start, query.comparison_end)))
        warnings: list[str] = []
        rows: list[dict[str, Any]] = []
        query_windows: list[dict[str, str]] = []
        successful_queries = 0
        failed_queries = 0
        async with MagikCubeClient(self._config, transport=self._transport) as client:
            catalog = await self._list_providers(client, query.filters, warnings)
            if not catalog:
                return ReportDataset(
                    rows=(), quality="missing", warnings=("provider_catalog no_data",),
                    source=self.manifest.connector_id,
                    metadata={"provider_catalog": (), "quality_reasons": ("provider_catalog no_data",)},
                )
            selected = self._select_catalog(catalog, query.filters)
            if not selected:
                return ReportDataset(
                    rows=(), quality="missing", warnings=("provider_not_found",),
                    source=self.manifest.connector_id,
                    metadata={"provider_catalog": tuple(catalog), "quality_reasons": ("provider_not_found",)},
                )
            provider_names = tuple(sorted({str(item.get("provider") or "").strip() for item in selected if item.get("provider")}))
            for period, (start, end) in windows:
                query_windows.append({"period": period, "start": start.isoformat(), "end": end.isoformat()})
                if await self._query_performance(client, rows, warnings, period, start, end, provider_names, query):
                    successful_queries += 1
                else:
                    failed_queries += 1
                if await self._query_traffic(client, rows, warnings, period, start, end, provider_names, query):
                    successful_queries += 1
                else:
                    failed_queries += 1
            if self._include_details:
                await self._query_details(client, rows, warnings, selected)

        unique_warnings = tuple(dict.fromkeys(warnings))
        if failed_queries and rows:
            quality = "partial"
        elif failed_queries:
            quality = "missing"
        elif successful_queries:
            # A successful empty response is a valid no-usage result, not a
            # failed query and not an artificial zero-valued dataset.
            quality = "complete"
        else:
            unique_warnings = tuple(dict.fromkeys((*unique_warnings, "provider_quality no_data")))
            quality = "missing"
        rows.sort(key=lambda row: (str(row.get("period")), str(row.get("provider")), str(row.get("model")), str(row.get("metric")), str(row.get("timestamp"))))
        return ReportDataset(
            rows=tuple(rows), quality=quality, warnings=unique_warnings,
            source=self.manifest.connector_id,
            metadata={
                "provider_catalog": tuple(selected),
                "query_windows": tuple(query_windows),
                "filters": {
                    "provider": str(query.filters.get("provider") or ""),
                    "providers": tuple(str(item) for item in query.filters.get("providers") or []),
                    "provider_id": str(query.filters.get("provider_id") or ""),
                    "model": str(query.filters.get("model") or ""),
                    "endpoint": str(query.filters.get("endpoint") or ""),
                    "include_empty": bool(query.filters.get("include_empty")),
                    "start_date": str(query.filters.get("start_date") or ""),
                    "end_date": str(query.filters.get("end_date") or ""),
                },
                "query_success_count": successful_queries,
                "query_failure_count": failed_queries,
                "source_refs": (
                    {"system": "Cube Admin", "route": "providers/list", "fields": ("id", "name", "provider", "modelName", "modelEndpoint", "modelInstance", "cluster", "enabled", "lastProbeStatus")},
                    {"system": "Cube Admin", "route": "analysis/provider-performance/query", "fields": ("provider", "metric", "percentile", "timestamp", "value")},
                    {"system": "Cube Admin", "route": "analysis/provider-daily-traffic/query", "fields": ("provider", "model", "totalTokens", "tpm", "trafficRatio")},
                    {"system": "Cube Admin", "route": "providers/detail", "fields": ("realtime", "tests", "lastProbeAt", "lastProbeStatus", "lastProbeMsg")},
                ),
                "quality_reasons": unique_warnings,
            },
        )

    @staticmethod
    def _date_window(start: date, end: date) -> tuple[datetime, datetime]:
        return (
            datetime.combine(start, time.min, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime.combine(end + timedelta(days=1), time.min, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    async def _list_providers(
        self,
        client: MagikCubeClient,
        filters: dict[str, Any],
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        del filters
        values: list[dict[str, Any]] = []
        page_size = 100
        max_pages = max(1, min(int(self._config.max_pages), 20))
        total = 0
        try:
            for page in range(1, max_pages + 1):
                data = await client.request(
                    "POST",
                    "providers/list",
                    params={"page_num": page, "page_size": page_size},
                    json_body={},
                )
                total = max(total, _as_int(data.get("total")))
                page_items = data.get("list") or []
                for item in page_items:
                    if not isinstance(item, dict):
                        continue
                    values.append({
                        "id": str(_pick(item, "id", default="") or ""),
                        "name": str(_pick(item, "name", default="") or ""),
                        "provider": str(_pick(item, "provider", default="") or ""),
                        "model": str(_pick(item, "modelName", "model_name", default="") or ""),
                        "endpoint": str(_pick(item, "modelEndpoint", "model_endpoint", default="") or ""),
                        "instance": str(_pick(item, "modelInstance", "model_instance", default="") or ""),
                        "cluster": str(_pick(item, "cluster", default="") or ""),
                        "enabled": bool(_pick(item, "enabled", default=False)),
                        "last_probe_at": str(_pick(item, "lastProbeAt", "last_probe_at", default="") or ""),
                        "last_probe_status": str(_pick(item, "lastProbeStatus", "last_probe_status", default="") or ""),
                "tpm_quota": self._number(_pick(item, "tpmQuota", "tpm_quota", default=None)),
                        "input_price": self._number(_pick(item, "inputPrice", "input_price", default=None)),
                        "output_price": self._number(_pick(item, "outputPrice", "output_price", default=None)),
                    })
                if not page_items or len(page_items) < page_size or len(values) >= total:
                    break
        except Exception as exc:
            warnings.append(f"{classify_report_failure(exc)}: provider_catalog query failed")
            return []
        if total > len(values):
            warnings.append("provider_catalog pagination limited")
        return values

    @staticmethod
    def _select_catalog(catalog: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
        provider_id = str(filters.get("provider_id") or "").strip()
        providers = {str(value).strip().casefold() for value in filters.get("providers") or [] if str(value).strip()}
        provider = str(filters.get("provider") or "").strip().casefold()
        model = str(filters.get("model") or "").strip().casefold()
        endpoint = str(filters.get("endpoint") or "").strip().casefold()
        result = [item for item in catalog if not provider_id or item["id"] == provider_id]
        if providers:
            result = [item for item in result if item["provider"].casefold() in providers]
        elif provider:
            result = [item for item in result if item["provider"].casefold() == provider]
        if model:
            result = [item for item in result if item["model"].casefold() == model]
        if endpoint:
            result = [item for item in result if item["endpoint"].casefold() == endpoint]
        return result

    async def _query_performance(self, client: MagikCubeClient, rows: list[dict[str, Any]], warnings: list[str], period: str, start: datetime, end: datetime, providers: tuple[str, ...], query: ReportQuery) -> bool:
        succeeded = False
        for metric, metric_name in _PERFORMANCE_METRICS.items():
            for percentile in ("PERCENTILE_P50", "PERCENTILE_P99"):
                try:
                    data = await client.request("POST", "analysis/provider-performance/query", json_body={
                        "timeLevel": "TIME_LEVEL_HOUR" if (end - start).days == 0 else "TIME_LEVEL_DAY",
                        "percentile": percentile,
                        "metric": metric_name,
                        "provider": list(providers),
                        "model": str(query.filters.get("model") or ""),
                        "endpoint": str(query.filters.get("endpoint") or ""),
                        "startTime": start.isoformat(),
                        "endTime": end.isoformat(),
                    })
                    succeeded = True
                    for item in data.get("series") or []:
                        if not isinstance(item, dict):
                            continue
                        provider = str(item.get("provider") or "").strip()
                        if provider not in providers:
                            continue
                        for point in item.get("points") or []:
                            if not isinstance(point, dict):
                                continue
                            value = self._number(point.get("value"))
                            if not provider or value is None:
                                continue
                            rows.append({"metric": metric, "value": value, "unit": _METRIC_LABELS[metric][1], "provider": provider, "period": period, "percentile": percentile[-3:].lower(), "timestamp": str(point.get("timestamp") or ""), "aggregation": "time_bucket_value", "source": "Cube Admin / analysis/provider-performance/query"})
                except Exception as exc:
                    warnings.append(
                        f"{classify_report_failure(exc)}: {period} {metric} "
                        f"{percentile.lower()} query failed"
                    )
        return succeeded

    async def _query_traffic(self, client: MagikCubeClient, rows: list[dict[str, Any]], warnings: list[str], period: str, start: datetime, end: datetime, providers: tuple[str, ...], query: ReportQuery) -> bool:
        try:
            data = await client.request("POST", "analysis/provider-daily-traffic/query", json_body={
                "startTime": start.isoformat(), "endTime": end.isoformat(),
                "timeLevel": "TIME_LEVEL_HOUR" if (end - start).days == 0 else "TIME_LEVEL_DAY",
                "provider": "" if len(providers) != 1 else providers[0],
                "model": str(query.filters.get("model") or ""),
                "endpoint": str(query.filters.get("endpoint") or ""),
            })
            for item in data.get("items") or []:
                if not isinstance(item, dict):
                    continue
                provider = str(item.get("provider") or "").strip()
                model = str(item.get("model") or "").strip()
                if provider not in providers:
                    continue
                for point in item.get("points") or []:
                    if not isinstance(point, dict) or not provider:
                        continue
                    timestamp = str(point.get("timestamp") or "")
                    for metric, key, unit in (("ai.provider.tokens", "totalTokens", "tokens"), ("ai.provider.tpm", "tpm", "tokens/minute"), ("ai.provider.traffic_ratio", "trafficRatio", "ratio")):
                        value = self._number(point.get(key))
                        if value is not None:
                            rows.append({"metric": metric, "value": value, "unit": unit, "provider": provider, "model": model, "period": period, "timestamp": timestamp, "aggregation": "time_bucket_value" if metric != "ai.provider.tokens" else "window_sum_candidate", "source": "Cube Admin / analysis/provider-daily-traffic/query"})
            return True
        except Exception as exc:
            warnings.append(
                f"{classify_report_failure(exc)}: {period} provider-daily-traffic query failed"
            )
            return False

    async def _query_details(self, client: MagikCubeClient, rows: list[dict[str, Any]], warnings: list[str], catalog: list[dict[str, Any]]) -> None:
        async def one(item: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
            if not item.get("id"):
                return [], "provider detail missing id"
            try:
                data = await client.request("POST", "providers/detail", json_body={"id": item["id"]})
                result: list[dict[str, Any]] = []
                realtime = data.get("realtime") if isinstance(data.get("realtime"), dict) else {}
                for metric, key, unit in (("ai.provider.requests", "totalRequests", "requests"), ("ai.provider.tokens", "totalTokens", "tokens"), ("ai.provider.actual_tpm", "actualTpm", "tokens/minute"), ("ai.provider.avg_latency", "avgLatencyMs", "ms"), ("ai.provider.avg_ttft", "avgTtftMs", "ms")):
                    value = self._number(_pick(realtime, key, key[0].lower() + key[1:], default=None))
                    if value is not None:
                        result.append({"metric": metric, "value": value, "unit": unit, "provider": item["provider"], "model": item["model"], "endpoint": item["endpoint"], "cluster": item["cluster"], "period": "current", "aggregation": "detail_snapshot", "source": "Cube Admin / providers/detail"})
                for test in data.get("tests") or []:
                    if isinstance(test, dict):
                        value = self._number(test.get("score"))
                        if value is not None:
                            result.append({"metric": "ai.provider.test_score", "value": value, "unit": "score", "provider": item["provider"], "model": item["model"], "endpoint": item["endpoint"], "period": "current", "aggregation": "same_dataset_raw", "test_type": str(test.get("type") or ""), "dataset": str(test.get("dataset") or ""), "source": "Cube Admin / providers/detail"})
                return result, None
            except Exception as exc:
                return [], f"{classify_report_failure(exc)}: provider detail query failed"

        results = await asyncio.gather(*(one(item) for item in catalog[:50]))
        if len(catalog) > 50:
            warnings.append("provider detail limited to 50")
        for values, warning in results:
            rows.extend(values)
            if warning:
                warnings.append(warning)

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(str(value)) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None


class ProviderQualityTemplate(TemplatePlugin):
    """Explainable provider quality summary with one stable comparison table."""

    manifest = TemplateManifest(
        template_id="provider_quality",
        display_name="Cube 供应商质量",
        version="1.0",
        category="provider_quality",
        periods=frozenset({"recent15m", "day", "week", "range"}),
        required_metrics=_QUALITY_METRICS,
        required_dimensions=_QUALITY_DIMENSIONS,
        connector_ids=frozenset({"cube_provider_quality"}),
        description="供应商在线稳定性、延迟、吞吐、探测、测试和成本辅助分析",
    )

    def __init__(self, *, timezone: str = "Asia/Shanghai") -> None:
        self.timezone = timezone

    def plan(self, intent: ReportIntent) -> tuple[ReportQuery, ...]:
        if intent.tenant or intent.project:
            raise ValueError("provider quality reports do not accept tenant or project")
        if intent.start_date is None or intent.end_date is None:
            raise ValueError("provider quality planning requires concrete dates")
        days = (intent.end_date - intent.start_date).days + 1
        filters = {
            "provider_quality": True,
            "provider": intent.provider,
            "providers": list(intent.filters.get("providers") or []),
            "provider_id": str(intent.filters.get("provider_id") or ""),
            "model": intent.models[0] if intent.models else str(intent.filters.get("model") or ""),
            "endpoint": intent.endpoint,
            "include_empty": bool(intent.filters.get("include_empty")),
            "period": intent.period,
            "start_date": str(intent.filters.get("start_date") or ""),
            "end_date": str(intent.filters.get("end_date") or ""),
        }
        return (ReportQuery(
            connector_id=intent.connector_id,
            metrics=tuple(sorted(_QUALITY_METRICS)),
            dimensions=tuple(sorted(_QUALITY_DIMENSIONS)),
            start_date=intent.start_date,
            end_date=intent.end_date,
            start_time=intent.start_time,
            end_time=intent.end_time,
            comparison_start=intent.start_date - timedelta(days=days),
            comparison_end=intent.start_date - timedelta(days=1),
            comparison_start_time=intent.comparison_start_time,
            comparison_end_time=intent.comparison_end_time,
            filters=filters,
        ),)

    def analyze(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        dataset = datasets[0]
        catalog = list(dataset.metadata.get("provider_catalog") or [])
        filters = dataset.metadata.get("filters") or {}
        explicit_provider_scope = bool(
            str(filters.get("provider") or "").strip()
            or filters.get("providers")
            or str(filters.get("provider_id") or "").strip()
        )
        include_empty = bool(filters.get("include_empty"))
        current = [row for row in dataset.rows if row.get("period", "current") == "current"]
        baseline = [row for row in dataset.rows if row.get("period") == "comparison"]
        grouped = self._group(current)
        baseline_grouped = self._group(baseline)
        catalog_by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in catalog:
            catalog_by_provider[str(item.get("provider") or "-")].append(item)
        providers = sorted(set(grouped) | set(catalog_by_provider), key=str.casefold)
        provider_rows: list[dict[str, Any]] = []
        drilldown_rows: list[dict[str, Any]] = []
        no_usage_rows: list[dict[str, Any]] = []
        abnormal = 0
        insufficient = 0
        unavailable = 0
        active_count = 0
        for provider in providers:
            value = grouped.get(provider, {})
            details = catalog_by_provider.get(provider) or [{}]
            request_count = int(value.get("ai.provider.requests") or 0)
            representative = details[0]
            probe = str(representative.get("last_probe_status") or "未知")
            query_failed = bool(dataset.metadata.get("query_failure_count")) and not value
            usage_state = (
                "unavailable" if query_failed else "no_usage" if not self._has_usage(value) else
                "partial" if dataset.quality == "partial" else "active"
            )
            status = self._provider_status(
                value, request_count, probe, dataset.quality, usage_state=usage_state
            )
            models = sorted({str(item.get("model") or "").strip() for item in details if item.get("model")})
            endpoints = sorted({str(item.get("endpoint") or "").strip() for item in details if item.get("endpoint")})
            common = {
                "provider": provider,
                "model": models[0] if len(models) == 1 else f"{len(models)} 个模型",
                "endpoint": endpoints[0] if len(endpoints) == 1 else f"{len(endpoints)} 个 Endpoint",
                "status": status,
                "usage_state": usage_state,
                "error_rate": self._format("ai.provider.error_rate", value.get("ai.provider.error_rate")),
                "latency": self._format("ai.provider.latency", value.get("ai.provider.latency")),
                "throughput": self._format("ai.provider.throughput", value.get("ai.provider.throughput")),
                "tpm": self._format("ai.provider.tpm", value.get("ai.provider.tpm")),
                "traffic_ratio": self._format("ai.provider.traffic_ratio", value.get("ai.provider.traffic_ratio")),
                "requests": str(request_count) if request_count else "暂无样本",
                "probe": probe,
                "test_score": self._format("ai.provider.test_score", value.get("ai.provider.test_score")),
                "input_price": self._format("ai.provider.input_price", representative.get("input_price")),
                "output_price": self._format("ai.provider.output_price", representative.get("output_price")),
                "change": self._change(
                    value.get("ai.provider.error_rate"),
                    baseline_grouped.get(provider, {}).get("ai.provider.error_rate"),
                ),
            }
            if usage_state == "active":
                active_count += 1
            if status == "异常":
                abnormal += 1
            if status == "数据不足":
                insufficient += 1
            if usage_state == "unavailable":
                unavailable += 1
            if usage_state == "no_usage" and not explicit_provider_scope and not include_empty:
                no_usage_rows.append(common)
            else:
                provider_rows.append(common)
            for detail in details:
                drilldown_rows.append({
                    **common,
                    "model": detail.get("model") or "-",
                    "endpoint": detail.get("endpoint") or "-",
                    "instance": detail.get("instance") or "-",
                    "cluster": detail.get("cluster") or "-",
                })
        priority = {"查询失败": 0, "异常": 1, "关注": 2, "部分数据": 3, "数据不足": 4, "暂无用量": 5, "正常": 6}
        provider_rows.sort(key=lambda row: (priority.get(row["status"], 9), row["provider"].casefold()))
        no_usage_rows.sort(key=lambda row: row["provider"].casefold())
        drilldown_rows.sort(key=lambda row: (priority.get(row["status"], 9), row["provider"].casefold(), row["model"].casefold(), row["endpoint"].casefold()))
        quality = dataset.quality
        if quality == "missing" or (providers and active_count == 0 and unavailable):
            status = "数据不足"
        elif abnormal:
            status = "异常"
        elif any(row["status"] == "关注" for row in provider_rows):
            status = "关注"
        elif active_count == 0 and no_usage_rows:
            status = "暂无用量"
        elif quality != "complete":
            status = "数据不足"
        else:
            status = "正常"
        metrics = [
            {"label": "供应商数", "value": str(len(providers)), "metric": "provider_count"},
            {"label": "有用量供应商", "value": str(active_count), "metric": "provider_active"},
            {"label": "查询异常", "value": str(unavailable), "metric": "provider_unavailable"},
            {"label": "无用量供应商", "value": str(len(no_usage_rows)), "metric": "provider_no_usage"},
            {"label": "样本不足", "value": str(insufficient), "metric": "provider_insufficient"},
        ]
        context = self._context(dataset)
        source_text = "；".join(f"{source.system} / {source.route}" for source in context.sources)
        notes = [
            f"状态：{status}；数据质量：{dataset.quality}",
            "质量等级只依据线上错误率、延迟和主动探测；测试分与价格仅作辅助证据。",
            "阈值：错误率 2%/5%，E2E 延迟 1000/3000ms；请求样本少于 20 时为数据不足。",
            f"供应商状态：有用量 {active_count}；查询异常 {unavailable}；无用量 {len(no_usage_rows)}。全部供应商模式下无用量项默认收起。",
            f"来源：{source_text}",
            "读法：错误率和延迟越低越好；吞吐、TPM 和流量占比描述承载情况，不单独代表故障。",
        ]
        fallback = "\n".join([
            f"Cube 供应商质量｜{status}",
            self._window_text(context),
            f"供应商数：{len(providers)}；有用量：{active_count}；查询异常：{unavailable}；无用量：{len(no_usage_rows)}",
            "供应商排行：" + ("\n".join(f"• {row['provider']}｜{row['status']}｜错误率 {row['error_rate']}｜延迟 {row['latency']}" for row in provider_rows[:20]) or "暂无有用量供应商"),
            "说明：" + "；".join(notes[1:]),
        ])
        columns = [
            {"tag": "column", "name": key, "display_name": label, "data_type": "text"}
            for key, label in (
                ("provider", "供应商"), ("model", "模型概览"), ("endpoint", "Endpoint 概览"),
                ("status", "状态"), ("error_rate", "错误率"), ("latency", "E2E 延迟"),
                ("throughput", "吞吐"), ("tpm", "TPM"), ("requests", "请求数"),
                ("probe", "探测"), ("change", "较基准"),
            )
        ]
        drilldown_columns = [
            {"tag": "column", "name": key, "display_name": label, "data_type": "text"}
            for key, label in (
                ("provider", "供应商"), ("model", "模型"), ("endpoint", "Endpoint"),
                ("instance", "实例"), ("cluster", "集群"), ("status", "状态"),
                ("error_rate", "错误率"), ("latency", "E2E 延迟"), ("requests", "请求数"),
            )
        ]
        blocks = [
            ReportBlock("metrics", {"items": metrics}),
            ReportBlock("table", {"title": "供应商质量排行：按状态、错误率和延迟排序", "columns": columns, "rows": provider_rows[:20], "page_size": 10}),
        ]
        if no_usage_rows:
            blocks.append(ReportBlock("table", {"title": f"无用量供应商：{len(no_usage_rows)} 个", "collapsed_label": f"无用量供应商：{len(no_usage_rows)} 个（点击展开）", "collapsed": True, "columns": columns, "rows": no_usage_rows[:50], "page_size": 10}))
            if not explicit_provider_scope and not include_empty:
                period = str(filters.get("period") or "recent15m")
                period_label = {"recent15m": "近15分钟", "day": "昨日", "week": "上一完整周", "range": "本次"}.get(period, "本次")
                command = f"查看{period_label}供应商无用量"
                if period == "range" and filters.get("start_date") and filters.get("end_date"):
                    command = (
                        f"查看自定义区间供应商无用量 {filters['start_date']} "
                        f"至 {filters['end_date']}"
                    )
                blocks.append(ReportBlock("actions", {"actions": [{
                    "action_id": "provider_quality_show_empty",
                    "label": "查看无用量供应商",
                    "style": "default",
                    "command": command,
                    "tool_name": "report_center",
                    "params": {
                        "action": "provider_quality_report",
                        "period": period,
                        "model": str(filters.get("model") or ""),
                        "endpoint": str(filters.get("endpoint") or ""),
                        "start_date": str(filters.get("start_date") or ""),
                        "end_date": str(filters.get("end_date") or ""),
                        "selection_confirmed": True,
                        "include_empty": True,
                    },
                    "content": "查看本次报告中的无用量供应商",
                }]}))
        if drilldown_rows:
            blocks.append(ReportBlock("table", {"title": "供应商下钻：按 provider / model / Endpoint 展示", "columns": drilldown_columns, "rows": drilldown_rows[:50], "page_size": 10}))
        blocks.append(
            ReportBlock("note", {"content": "\n".join(notes), "severity": "warning" if quality != "complete" or status in {"异常", "数据不足"} else "info"}),
        )
        return ReportDocument(title="Cube 供应商质量报告", subtitle="在线质量总览 · provider/model/endpoint 下钻", document_id=self.manifest.template_id, fallback_text=fallback, blocks=tuple(blocks), quality=quality, warnings=dataset.warnings, context=context)

    @staticmethod
    def _group(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
        grouped: dict[str, dict[str, float]] = defaultdict(dict)
        for row in rows:
            provider = str(row.get("provider") or "").strip()
            metric = str(row.get("metric") or "")
            if not provider or not metric:
                continue
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError):
                continue
            if metric == "ai.provider.tokens":
                grouped[provider][metric] = grouped[provider].get(metric, 0.0) + value
            elif metric in {"ai.provider.latency", "ai.provider.error_rate"}:
                if str(row.get("percentile")) == "p99":
                    grouped[provider][metric] = max(grouped[provider].get(metric, 0.0), value)
            else:
                grouped[provider][metric] = max(grouped[provider].get(metric, 0.0), value)
        return grouped

    @staticmethod
    def _provider_status(
        value: dict[str, float],
        request_count: int,
        probe: str,
        quality: str,
        *,
        usage_state: str,
    ) -> str:
        if usage_state == "unavailable":
            return "查询失败"
        if usage_state == "no_usage":
            return "暂无用量"
        if usage_state == "partial":
            return "部分数据"
        if quality == "missing" or request_count < 20 or not value:
            return "数据不足"
        if probe.casefold() in {"failed", "failure", "error", "unhealthy", "失败"}:
            return "异常"
        if value.get("ai.provider.error_rate", 0) >= 0.05 or value.get("ai.provider.latency", 0) >= 3000:
            return "异常"
        if value.get("ai.provider.error_rate", 0) >= 0.02 or value.get("ai.provider.latency", 0) >= 1000:
            return "关注"
        return "正常"

    @staticmethod
    def _has_usage(value: dict[str, float]) -> bool:
        return any(
            float(value.get(metric) or 0) > 0
            for metric in (
                "ai.provider.tokens",
                "ai.provider.requests",
                "ai.provider.throughput",
                "ai.provider.tpm",
            )
        )

    def _context(self, dataset: ReportDataset) -> ReportContext:
        windows = dataset.metadata.get("query_windows") or []
        current = next((item for item in windows if item.get("period") == "current"), None)
        baseline = next((item for item in windows if item.get("period") == "comparison"), None)
        return ReportContext(
            timezone=self.timezone,
            current_window=ReportWindow(str(current.get("start")), str(current.get("end")), "current") if current else None,
            baseline_window=ReportWindow(str(baseline.get("start")), str(baseline.get("end")), "comparison") if baseline else None,
            sources=tuple(ReportSource(str(item["system"]), str(item["route"]), tuple(item.get("fields") or ())) for item in dataset.metadata.get("source_refs") or []),
            metric_definitions=tuple(MetricDefinition(metric, label, unit, aggregation, "Cube Admin / provider quality contract", "lower_is_better" if metric in {"ai.provider.error_rate", "ai.provider.latency"} else "informational") for metric, (label, unit, aggregation) in _METRIC_LABELS.items()),
            calculation_version="1",
            quality=dataset.quality,
            quality_reasons=tuple(dataset.warnings),
            template_version=self.manifest.version,
        )

    @staticmethod
    def _format(metric: str, value: float | None) -> str:
        if value is None:
            return "暂无数据"
        if metric == "ai.provider.error_rate":
            return f"{value:.2%}"
        if metric == "ai.provider.latency":
            return f"{value:.0f} ms"
        if metric == "ai.provider.traffic_ratio":
            return f"{value:.2%}"
        return f"{value:.2f}"

    @classmethod
    def _change(cls, value: float | None, baseline: float | None) -> str:
        if value is None or baseline is None:
            return "暂无基准"
        if baseline == 0:
            return "新增" if value else "持平"
        return f"{(value - baseline) / abs(baseline):+.1%}"

    @staticmethod
    def _window_text(context: ReportContext) -> str:
        current = context.current_window
        baseline = context.baseline_window
        current_text = f"{current.start} - {current.end}" if current else "暂无"
        baseline_text = f"{baseline.start} - {baseline.end}" if baseline else "暂无可比基准"
        return f"统计窗口：{current_text}；对比基准：{baseline_text}；时区：{context.timezone}"


def provider_quality_selector_document(
    interaction: ReportInteraction,
    catalog: list[dict[str, Any]],
    *,
    timezone: str,
    warnings: tuple[str, ...] = (),
) -> ReportDocument:
    """Build a channel-neutral provider selector from sanitized catalog data."""

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in catalog:
        provider = str(item.get("provider") or "").strip()
        if provider:
            grouped[provider].append(item)
    options: list[dict[str, Any]] = []
    for provider in sorted(grouped, key=str.casefold):
        token = next(
            key for key, value in interaction.options.items() if value == provider
        )
        records = grouped[provider]
        names = sorted({str(item.get("name") or "").strip() for item in records if item.get("name")})
        label = names[0] if names else provider
        model_count = len({str(item.get("model") or "").strip() for item in records if item.get("model")})
        options.append(
            {
                "token": token,
                "label": label,
                "description": f"{provider} · {model_count} 个模型",
                "enabled": any(bool(item.get("enabled")) for item in records),
            }
        )
    quality = "partial" if warnings else "complete"
    fallback_lines = [
        "Cube 供应商质量报告",
        "请选择一个或多个供应商，然后生成报告。默认范围：全部供应商。",
        f"可选供应商：{len(options)} 个；时区：{timezone}",
        "说明：全部供应商模式会折叠无用量供应商；查询失败的供应商仍会单独展示。",
    ]
    if warnings:
        fallback_lines.append("数据提示：" + "；".join(warnings[:3]))
    return ReportDocument(
        title="Cube 供应商质量报告",
        subtitle="选择供应商与统计周期",
        document_id="provider_quality_selector",
        fallback_text="\n".join(fallback_lines),
        quality=quality,
        warnings=warnings,
        blocks=(
            ReportBlock(
                "selector",
                {
                    "selector_id": "provider_quality",
                    "interaction_id": interaction.interaction_id,
                    "mode": "multi",
                    "options": options,
                    "all_option": {
                        "token": interaction.all_option,
                        "label": "全部供应商",
                        "description": f"包含 {len(options)} 个供应商",
                    },
                    "default_options": [interaction.all_option],
                    "periods": [
                        {"value": "recent15m", "label": "近 15 分钟"},
                        {"value": "day", "label": "昨日"},
                        {"value": "week", "label": "上一完整周"},
                        {"value": "range", "label": "自定义区间"},
                    ],
                    "default_period": "recent15m",
                    "submit_token": interaction.submit_token,
                },
            ),
            ReportBlock(
                "note",
                {
                    "content": "供应商按 provider 聚合，模型和 Endpoint 会在报告中继续下钻。选择结果只用于固定 Cube 查询，不支持任意 URL、SQL 或 PromQL。",
                    "severity": "warning" if warnings else "info",
                },
            ),
        ),
    )
