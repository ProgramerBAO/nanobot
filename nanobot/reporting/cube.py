"""Magik Cube connector for the connector-neutral reporting runner."""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from nanobot.agent.tools.magik_cube import (
    MagikCubeApiError,
    MagikCubeClient,
    MagikCubeTenantResolutionError,
    MagikCubeToolConfig,
    _as_int,
    _match_catalog_tenants,
    _pick,
)
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
from nanobot.reporting.cube_contract_gate import compare_metric_summaries
from nanobot.reporting.registry import (
    ConnectorCapabilities,
    ConnectorManifest,
    ConnectorPlugin,
    TemplateManifest,
    TemplatePlugin,
)
from nanobot.utils.report_failures import classify_report_failure

_CUBE_HEALTH_METRICS = frozenset(
    {
        "ai.error_rate",
        "ai.http_4xx_rate",
        "ai.http_5xx_rate",
        "ai.interface_delay",
        "ai.ttft",
        "ai.rpm",
        "ai.tpm",
        "ai.capacity_utilization",
    }
)
_CUBE_ACCOUNT_METRICS = frozenset({"ai.cost", "ai.balance", "ai.unbilled_amount"})
_CUBE_METRICS = (
    frozenset({"ai.usage.tokens", "ai.requests"})
    | _CUBE_HEALTH_METRICS
    | _CUBE_ACCOUNT_METRICS
)
_CUBE_DIMENSIONS = frozenset(
    {"tenant", "project", "model", "endpoint", "provider", "date", "hour"}
)
_ALLOWED_FILTERS = frozenset(
    {
        "tenant",
        "model",
        "models",
        "model_scope",
        "endpoint",
        "provider",
        "project",
        "all_tenants",
    }
)


@dataclass(frozen=True, slots=True)
class _CubeTenant:
    tenant_id: str
    name: str
    tags: tuple[str, ...] = ()


class CubeConnector(ConnectorPlugin):
    """Read-only Cube usage connector using the existing authenticated client."""

    manifest = ConnectorManifest(
        connector_id="magik_cube",
        display_name="Magik Cube",
        version="1.3",
        auth_methods=("bearer", "password", "tokenapi_bearer"),
        capabilities=ConnectorCapabilities(
            metrics=_CUBE_METRICS,
            dimensions=_CUBE_DIMENSIONS,
            max_window_days=90,
            supports_bulk_dimensions=True,
            read_only=True,
        ),
        secret_fields=frozenset({"access_token", "password", "token_api.access_token"}),
        allowed_hosts=(),
    )

    def __init__(
        self,
        config: MagikCubeToolConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        ttft_detail_enabled: bool = False,
    ) -> None:
        self._config = config
        self._transport = transport
        self._tz = ZoneInfo("Asia/Shanghai")
        self._ttft_detail_enabled = ttft_detail_enabled

    @property
    def account_configured(self) -> bool:
        """Whether the independent Casbin-scoped TokenAPI path is usable."""

        return self._config.token_api.configured

    async def health_check(self) -> dict[str, Any]:
        configured = bool(
            self._config.base_url
            and (
                self._config.access_token
                or (self._config.account and self._config.password)
            )
        )
        return {
            "status": "configured" if configured else "unconfigured",
            "connector": self.manifest.connector_id,
        }

    async def discover_catalog(self) -> dict[str, list[str]]:
        return {
            "metrics": sorted(_CUBE_METRICS),
            "dimensions": sorted(_CUBE_DIMENSIONS),
        }

    async def query(self, query: ReportQuery) -> ReportDataset:
        if query.connector_id != self.manifest.connector_id:
            raise ValueError("Cube connector received a query for another connector")
        unsupported_metrics = set(query.metrics) - _CUBE_METRICS
        if unsupported_metrics:
            raise ValueError(f"Cube connector does not support metrics: {sorted(unsupported_metrics)}")
        unsupported_dimensions = set(query.dimensions) - _CUBE_DIMENSIONS
        if unsupported_dimensions:
            raise ValueError(
                f"Cube connector does not support dimensions: {sorted(unsupported_dimensions)}"
            )
        unsupported_filters = set(query.filters) - _ALLOWED_FILTERS
        if unsupported_filters:
            raise ValueError(
                f"Cube connector received unsupported filters: {sorted(unsupported_filters)}"
            )
        all_tenants = query.filters.get("all_tenants", False)
        if not isinstance(all_tenants, bool):
            raise ValueError("Cube all_tenants filter must be boolean")
        selected_models = self._selected_model_values(query.filters)
        if all_tenants:
            if str(query.filters.get("tenant") or "").strip():
                raise ValueError("Cube all_tenants query cannot select an individual tenant")
            if len(selected_models) != 1:
                raise ValueError("Cube all_tenants query requires exactly one selected model")
        account_metrics = set(query.metrics).intersection(_CUBE_ACCOUNT_METRICS)
        if str(query.filters.get("project") or "").strip() and not account_metrics:
            raise ValueError(
                "Cube usage contract does not support project filtering yet; use tenant/model/endpoint/provider"
            )
        health_metrics = set(query.metrics).intersection(_CUBE_HEALTH_METRICS)
        health_only_metrics = health_metrics - {"ai.tpm"}
        usage_metrics = set(query.metrics).intersection({"ai.usage.tokens", "ai.requests"})
        if health_only_metrics and (usage_metrics or account_metrics):
            raise ValueError("Cube health and usage metrics must use separate query plans")
        if health_only_metrics:
            return await self._query_health(query)
        if account_metrics:
            return await self._query_account(query)

        warnings: list[str] = []
        rows: list[dict[str, Any]] = []
        query_windows = [
            {
                "period": "current",
                "start": query.start_date.isoformat() + " 00:00",
                "end": (query.end_date + timedelta(days=1)).isoformat() + " 00:00",
            }
        ]
        if query.comparison_start is not None and query.comparison_end is not None:
            query_windows.append(
                {
                    "period": "comparison",
                    "start": query.comparison_start.isoformat() + " 00:00",
                    "end": (query.comparison_end + timedelta(days=1)).isoformat() + " 00:00",
                }
            )
        async with MagikCubeClient(self._config, transport=self._transport) as client:
            tenants = await self._resolve_tenants(client, query.filters)
            windows = [("current", query.start_date, query.end_date)]
            if query.comparison_start is not None and query.comparison_end is not None:
                windows.append(("comparison", query.comparison_start, query.comparison_end))
            semaphore = asyncio.Semaphore(max(1, min(self._config.max_concurrency, 16)))
            results = await asyncio.gather(
                *(
                    self._query_tenant(
                        client,
                        tenant,
                        windows,
                        query,
                        semaphore,
                    )
                    for tenant in tenants
                )
            )
            for tenant_rows, tenant_warnings in results:
                rows.extend(tenant_rows)
                warnings.extend(tenant_warnings)

        unique_warnings = tuple(dict.fromkeys(warnings))
        if unique_warnings and rows:
            quality = "partial"
        elif unique_warnings:
            quality = "missing"
        else:
            quality = "complete"
        rows.sort(
            key=lambda row: (
                str(row.get("period")),
                str(row.get("date")),
                str(row.get("tenant")),
                str(row.get("model")),
                str(row.get("metric")),
            )
        )
        if not rows and (
            not unique_warnings or all(warning.endswith("no_data") for warning in unique_warnings)
        ):
            unique_warnings = ("no_business_data",)
            quality = "missing"
        last_sample_at = max(
            (str(row.get("date") or "") for row in rows if row.get("date")),
            default="",
        )
        return ReportDataset(
            rows=tuple(rows),
            quality=quality,
            warnings=unique_warnings,
            source=self.manifest.connector_id,
            metadata={
                "query_windows": query_windows,
                "source_refs": (
                    {
                        "system": "Cube Admin",
                        "route": "analysis/active-tenant-daily-usage/query",
                        "fields": ("totalTokens", "requestCount", "date"),
                    },
                    {
                        "system": "Cube Admin",
                        "route": "analysis/endpoint-max-tpm/daily/query",
                        "fields": ("maxTpm", "date"),
                    },
                ),
                "last_sample_at": last_sample_at,
                "scope": {
                    "tenant_catalog": (
                        "all_tenants"
                        if all_tenants
                        else "selected_tenant"
                        if str(query.filters.get("tenant") or "").strip()
                        else "key_accounts"
                    ),
                    "all_tenants": all_tenants,
                    "tenant_count": len(tenants),
                    "models": list(selected_models),
                },
            },
        )

    async def _query_account(self, query: ReportQuery) -> ReportDataset:
        """Fetch fixed TokenAPI account data without falling back to Admin credentials."""

        if not self.account_configured:
            return ReportDataset(
                rows=(),
                quality="missing",
                warnings=("token_api_not_configured",),
                source=self.manifest.connector_id,
            )
        tenant = self._account_tenant(str(query.filters.get("tenant") or ""))
        if not tenant:
            return ReportDataset(
                rows=(),
                quality="missing",
                warnings=("account_report_requires_tenant",),
                source=self.manifest.connector_id,
            )

        windows = [("current", query.start_date, query.end_date)]
        if query.comparison_start is not None and query.comparison_end is not None:
            windows.append(("comparison", query.comparison_start, query.comparison_end))
        warnings: list[str] = []
        rows: list[dict[str, Any]] = []
        project = str(query.filters.get("project") or "").strip()
        async with MagikCubeClient(self._config.token_api, transport=self._transport) as client:
            for period, start, end in windows:
                try:
                    bills = await self._token_api_pages(
                        client,
                        "bills",
                        {"tenant_id": tenant, "period": start.strftime("%Y-%m")},
                    )
                    bill_rows = self._account_bill_rows(bills, tenant, period, start, end)
                    rows.extend(bill_rows)
                    if not bill_rows:
                        warnings.append(f"{period} bills no_data")
                except Exception as exc:
                    warnings.append(f"{period} bills query failed: {type(exc).__name__}")
                if project:
                    try:
                        usage = await client.request(
                            "GET",
                            "usages/token",
                            params=self._token_usage_params(query, start, end, project),
                        )
                        usage_rows = self._account_usage_rows(usage, tenant, period, start, end)
                        rows.extend(usage_rows)
                        if not usage_rows:
                            warnings.append(f"{period} token usage no_data")
                    except Exception as exc:
                        warnings.append(
                            f"{period} token usage query failed: {type(exc).__name__}"
                        )
                else:
                    warnings.append(f"{period} token usage skipped: project_required")
            try:
                wallet = await client.request(
                    "GET", "wallets/balance", params={"tenant_id": tenant}
                )
                wallet_rows = self._account_wallet_rows(wallet, tenant, query.end_date)
                rows.extend(wallet_rows)
                if not wallet_rows:
                    warnings.append("wallet balance no_data")
            except Exception as exc:
                warnings.append(f"wallet balance query failed: {type(exc).__name__}")

        unique_warnings = tuple(dict.fromkeys(warnings))
        quality = "complete" if rows and not unique_warnings else "partial" if rows else "missing"
        rows.sort(
            key=lambda row: (
                str(row.get("period") or ""),
                str(row.get("metric") or ""),
                str(row.get("date") or ""),
            )
        )
        return ReportDataset(
            rows=tuple(rows),
            quality=quality,
            warnings=unique_warnings,
            source=self.manifest.connector_id,
            metadata={
                "query_windows": [
                    {
                        "period": period,
                        "start": start.isoformat() + " 00:00",
                        "end": (end + timedelta(days=1)).isoformat() + " 00:00",
                    }
                    for period, start, end in windows
                ],
                "source_refs": (
                    {
                        "system": "Cube TokenAPI",
                        "route": "bills",
                        "fields": ("payableAmount", "period", "requestCount"),
                    },
                    {
                        "system": "Cube TokenAPI",
                        "route": "wallets/balance",
                        "fields": ("balance", "unbilledAmount"),
                    },
                    {
                        "system": "Cube TokenAPI",
                        "route": "usages/token",
                        "fields": ("totalTokens", "requestCount", "timeSeries"),
                    },
                ),
                "last_sample_at": query.end_date.isoformat(),
            },
        )

    def _account_tenant(self, value: str) -> str:
        return value.strip()

    async def _token_api_pages(
        self, client: MagikCubeClient, path: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        first = await client.request(
            "GET", path, params={**params, "page_num": 1, "page_size": 100}
        )
        values = [item for item in first.get("list") or [] if isinstance(item, dict)]
        total = _as_int(first.get("total"))
        pages = min(max(1, math.ceil(total / 100)) if total else 1, self._config.token_api.max_pages)
        if pages > 1:
            remaining = await asyncio.gather(
                *(
                    client.request(
                        "GET", path, params={**params, "page_num": page, "page_size": 100}
                    )
                    for page in range(2, pages + 1)
                )
            )
            values.extend(
                item for data in remaining for item in data.get("list") or [] if isinstance(item, dict)
            )
        return values

    @staticmethod
    def _token_usage_params(
        query: ReportQuery, start: date, end: date, project: str
    ) -> dict[str, Any]:
        tz = ZoneInfo("Asia/Shanghai")
        params: dict[str, Any] = {
            "time_range.start_time": int(datetime.combine(start, time.min, tzinfo=tz).timestamp()),
            "time_range.end_time": int(
                datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz).timestamp()
            ),
            "project_id": project,
            "aggregation": "AGGREGATION_DAILY",
        }
        for source, target in (("model", "model"), ("endpoint", "endpoint")):
            value = str(query.filters.get(source) or "").strip()
            if value:
                params[target] = value
        return params

    @staticmethod
    def _account_bill_rows(
        items: list[dict[str, Any]], tenant: str, period: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        amounts = [
            parsed
            for item in items
            if (parsed := CubeConnector._decimal_value(
                _pick(item, "payableAmount", "payable_amount", default=None)
            )) is not None
        ]
        if not amounts:
            return []
        return [
            {
                "tenant": tenant,
                "period": period,
                "date": end.isoformat(),
                "metric": "ai.cost",
                "value": sum(amounts),
                "unit": "currency",
                "aggregation": "billing_period_sum",
                "sample_count": len(items),
                "valid_sample_count": len(items),
                "source": "Cube TokenAPI / bills.payableAmount",
                "billing_period": start.strftime("%Y-%m"),
            }
        ]

    @staticmethod
    def _account_usage_rows(
        data: dict[str, Any], tenant: str, period: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        totals = {
            "ai.usage.tokens": _pick(data, "totalTokens", "total_tokens", default=None),
            "ai.requests": _pick(data, "requestCount", "request_count", default=None),
        }
        return [
            {
                "tenant": tenant,
                "period": period,
                "date": end.isoformat(),
                "metric": metric,
                "value": _as_int(value),
                "unit": "tokens" if metric == "ai.usage.tokens" else "requests",
                "aggregation": "window_sum",
                "sample_count": 1,
                "valid_sample_count": 1,
                "source": "Cube TokenAPI / usages/token",
            }
            for metric, value in totals.items()
            if value is not None
        ]

    @staticmethod
    def _account_wallet_rows(data: dict[str, Any], tenant: str, report_date: date) -> list[dict[str, Any]]:
        values = {
            "ai.balance": _pick(data, "balance", default=None),
            "ai.unbilled_amount": _pick(data, "unbilledAmount", "unbilled_amount", default=None),
        }
        return [
            {
                "tenant": tenant,
                "period": "snapshot",
                "date": report_date.isoformat(),
                "metric": metric,
                "value": parsed,
                "unit": "currency",
                "aggregation": "current_snapshot",
                "sample_count": 1,
                "valid_sample_count": 1,
                "source": "Cube TokenAPI / wallets/balance",
            }
            for metric, value in values.items()
            if value is not None and (parsed := CubeConnector._decimal_value(value)) is not None
        ]

    @staticmethod
    def _decimal_value(value: Any) -> float | None:
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    async def _query_health(self, query: ReportQuery) -> ReportDataset:
        """Fetch the fixed Cube health contract and normalize every response row."""

        windows = self._health_windows(query)
        if any(end - start > timedelta(minutes=15) for _, start, end in windows if query.start_time):
            raise ValueError("Cube realtime health window must not exceed 15 minutes")
        warnings: list[str] = []
        optional_warnings: list[str] = []
        rows: list[dict[str, Any]] = []
        realtime = query.start_time is not None
        interval_minutes = 1 if realtime else 60
        query_windows: list[dict[str, Any]] = []
        async with MagikCubeClient(self._config, transport=self._transport) as client:
            endpoints = await self._health_endpoints(client, query, warnings)
            endpoint_names = tuple(item[0] for item in endpoints)
            endpoint_models = {name: model for name, model, _tpm in endpoints}
            for period, start, end in windows:
                query_windows.append(
                    {
                        "period": period,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "interval_minutes": interval_minutes,
                    }
                )
                if "ai.capacity_utilization" in query.metrics:
                    try:
                        path = (
                            "analysis/token-utilization/query"
                            if realtime
                            else "analysis/token-utilization/daily/query"
                        )
                        data = await client.request(
                            "POST",
                            path,
                            json_body=(
                                self._health_realtime_body(start, end, query)
                                if realtime
                                else self._health_daily_body(start, end, query)
                            ),
                        )
                        capacity_rows = (
                            self._health_realtime_capacity_rows(data, period, end)
                            if realtime
                            else self._health_daily_capacity_rows(data, period, start, end)
                        )
                        rows.extend(capacity_rows)
                        if not capacity_rows:
                            warnings.append(f"{period} capacity_utilization no_data")
                    except Exception as exc:
                        warnings.append(
                            f"{period} capacity_utilization query failed: {type(exc).__name__}"
                        )

                performance_metrics = set(query.metrics) - {"ai.capacity_utilization"}
                for metric in sorted(performance_metrics):
                    try:
                        data = await client.request(
                            "POST",
                            "analysis/model-performance/query",
                            json_body=self._health_performance_body(
                                start,
                                end,
                                endpoint_names,
                                metric,
                                interval_minutes,
                            ),
                        )
                        metric_rows = self._health_performance_rows(
                            data,
                            metric,
                            period,
                            start,
                            end,
                            endpoint_models,
                        )
                        rows.extend(metric_rows)
                        if not metric_rows:
                            warnings.append(f"{period} {metric} no_data")
                    except Exception as exc:
                        warnings.append(
                            f"{period} {metric} query failed: {type(exc).__name__}"
                        )

                if self._ttft_detail_enabled and "ai.ttft" in query.metrics:
                    detail_rows, detail_warnings = await self._query_ttft_details(
                        client,
                        period=period,
                        start=start,
                        end=end,
                        endpoints=endpoints,
                    )
                    rows.extend(detail_rows)
                    optional_warnings.extend(detail_warnings)

                if not realtime and "ai.tpm" in query.metrics:
                    try:
                        data = await client.request(
                            "POST",
                            "analysis/endpoint-tpm-trend/query",
                            json_body=self._health_daily_body(start, end, query),
                        )
                        trend_rows = self._health_endpoint_tpm_rows(
                            data, period, start, end, endpoint_models
                        )
                        rows.extend(trend_rows)
                        if not trend_rows:
                            warnings.append(f"{period} endpoint_tpm_trend no_data")
                    except Exception as exc:
                        warnings.append(
                            f"{period} endpoint_tpm_trend query failed: {type(exc).__name__}"
                        )

        unique_warnings = tuple(dict.fromkeys(warnings))
        if (unique_warnings or optional_warnings) and rows:
            quality = "partial"
        elif unique_warnings or optional_warnings:
            quality = "missing"
        else:
            quality = "complete"
        if not rows and not unique_warnings:
            unique_warnings = ("no_data",)
            quality = "missing"
        rows.sort(
            key=lambda row: (
                str(row.get("period")),
                str(row.get("timestamp") or row.get("date")),
                str(row.get("endpoint")),
                str(row.get("model")),
                str(row.get("metric")),
            )
        )
        quality_reasons = tuple(dict.fromkeys(unique_warnings + tuple(optional_warnings)))
        last_sample_at = max(
            (
                str(row.get("timestamp") or row.get("date") or "")
                for row in rows
                if row.get("timestamp") or row.get("date")
            ),
            default="",
        )
        return ReportDataset(
            rows=tuple(rows),
            quality=quality,
            warnings=unique_warnings,
            source=self.manifest.connector_id,
            metadata={
                "query_windows": query_windows,
                "ttft_detail_enabled": self._ttft_detail_enabled,
                "optional_warnings": tuple(dict.fromkeys(optional_warnings)),
                "quality_reasons": quality_reasons,
                "last_sample_at": last_sample_at,
            },
        )

    def _health_windows(
        self, query: ReportQuery
    ) -> list[tuple[str, datetime, datetime]]:
        def date_window(start: date, end: date) -> tuple[datetime, datetime]:
            return (
                datetime.combine(start, time.min, tzinfo=self._tz),
                datetime.combine(end + timedelta(days=1), time.min, tzinfo=self._tz),
            )

        if query.start_time is not None and query.end_time is not None:
            current = (query.start_time, query.end_time)
        else:
            current = date_window(query.start_date, query.end_date)
        windows = [("current", current[0], current[1])]
        if query.comparison_start_time is not None and query.comparison_end_time is not None:
            windows.append(
                ("comparison", query.comparison_start_time, query.comparison_end_time)
            )
        elif query.comparison_start is not None and query.comparison_end is not None:
            comparison = date_window(query.comparison_start, query.comparison_end)
            windows.append(("comparison", comparison[0], comparison[1]))
        return windows

    async def _health_endpoints(
        self,
        client: MagikCubeClient,
        query: ReportQuery,
        warnings: list[str],
    ) -> list[tuple[str, str, float]]:
        requested = str(query.filters.get("endpoint") or "").strip()
        if requested:
            model = str(query.filters.get("model") or "").strip()
            return [(requested, model, 0.0)]
        try:
            data = await client.request(
                "POST",
                "analysis/performance-endpoints/query",
                json_body={},
            )
        except Exception as exc:
            warnings.append(f"performance-endpoints query failed: {type(exc).__name__}")
            return []
        values: list[tuple[str, str, float]] = []
        items = data.get("items") or data.get("list") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            endpoint = str(
                _pick(item, "endpoint", "endpointName", "endpointId", "endpoint_id", default="")
            ).strip()
            if not endpoint:
                continue
            model = str(_pick(item, "model", "modelName", "model_name", default="")).strip()
            tpm = self._as_float(_pick(item, "tpm", "tpmLimit", "tpm_limit", default=0))
            values.append((endpoint, model, tpm))
        values.sort(key=lambda item: (-item[2], item[0].casefold(), item[1].casefold()))
        if len(values) > 20:
            warnings.append("performance-endpoints limited to 20")
        return values[:20]

    def _health_realtime_body(
        self, start: datetime, end: datetime, query: ReportQuery
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "startTime": start.isoformat(),
            "endTime": end.isoformat(),
        }
        self._add_health_filters(body, query)
        return body

    def _health_daily_body(
        self, start: datetime, end: datetime, query: ReportQuery
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "startDate": start.date().isoformat(),
            "endDate": (end - timedelta(days=1)).date().isoformat(),
        }
        self._add_health_filters(body, query)
        return body

    @staticmethod
    def _add_health_filters(body: dict[str, Any], query: ReportQuery) -> None:
        endpoint = str(query.filters.get("endpoint") or "").strip()
        if endpoint:
            body["endpoint"] = endpoint

    @staticmethod
    def _health_performance_body(
        start: datetime,
        end: datetime,
        endpoints: tuple[str, ...],
        metric: str,
        interval_minutes: int,
    ) -> dict[str, Any]:
        metric_name = {
            "ai.rpm": "TIMESERIES_METRIC_RPM",
            "ai.tpm": "TIMESERIES_METRIC_TPM",
            "ai.interface_delay": "TIMESERIES_METRIC_INTERFACE_DELAY",
            "ai.error_rate": "TIMESERIES_METRIC_ERROR_RATE",
            "ai.http_4xx_rate": "TIMESERIES_METRIC_HTTP_4XX_RATE",
            "ai.http_5xx_rate": "TIMESERIES_METRIC_HTTP_5XX_RATE",
            "ai.ttft": "TIMESERIES_METRIC_FIRST_TOKEN_DELAY",
        }[metric]
        return {
            "endpoints": list(endpoints),
            "metric": metric_name,
            "startTime": start.isoformat(),
            "endTime": end.isoformat(),
            "intervalMinutes": interval_minutes,
        }

    def _health_performance_rows(
        self,
        data: dict[str, Any],
        metric: str,
        period: str,
        start: datetime,
        end: datetime,
        endpoint_models: dict[str, str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        series = data.get("series") or data.get("list") or data.get("items") or []
        for item in series:
            if not isinstance(item, dict):
                continue
            endpoint = str(
                _pick(item, "endpoint", "endpointName", "endpointId", "endpoint_id", default="")
            ).strip()
            model = str(
                _pick(item, "model", "modelName", "model_name", default="")
                or endpoint_models.get(endpoint, "")
            ).strip()
            for point in item.get("points") or []:
                if not isinstance(point, dict):
                    continue
                timestamp = self._parse_timestamp(
                    _pick(point, "timestamp", "time", "ts", default=None)
                )
                if timestamp is None or timestamp < start or timestamp >= end:
                    continue
                value = self._health_value(metric, _pick(point, "value", default=None))
                if value is None:
                    continue
                rows.append(
                    {
                        "metric": metric,
                        "value": value,
                        "unit": self._health_unit(metric),
                        "timestamp": timestamp.isoformat(),
                        "date": timestamp.date().isoformat(),
                        "endpoint": endpoint,
                        "model": model,
                        "period": period,
                        "aggregation": "time_bucket_value",
                        "source": "Cube Admin / analysis/model-performance/query",
                    }
                )
        return rows

    async def _query_ttft_details(
        self,
        client: MagikCubeClient,
        *,
        period: str,
        start: datetime,
        end: datetime,
        endpoints: list[tuple[str, str, float]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Read bounded request metadata and emit percentile-only TTFT rows."""

        endpoint_values: dict[str, list[float]] = defaultdict(list)
        endpoint_counts: dict[str, tuple[int, int]] = {}
        warnings: list[str] = []
        for endpoint, model, _tpm in endpoints:
            try:
                items, truncated = await self._gateway_usage_pages(
                    client, endpoint=endpoint, model=model, start=start, end=end
                )
                if truncated:
                    warnings.append(f"{period} ttft detail pagination truncated: {endpoint}")
                eligible = 0
                values: list[float] = []
                for item in items:
                    if not self._eligible_ttft_usage(item):
                        continue
                    eligible += 1
                    value = self._as_float(_pick(item, "ttft", default=None))
                    if value is not None and value > 0:
                        values.append(value)
                endpoint_values[endpoint].extend(values)
                endpoint_counts[endpoint] = (eligible, len(values))
            except Exception as exc:
                warnings.append(
                    f"{period} ttft detail query failed: {endpoint}:{type(exc).__name__}"
                )

        all_values = [value for values in endpoint_values.values() for value in values]
        all_count = (
            sum(counts[0] for counts in endpoint_counts.values()),
            len(all_values),
        )
        rows: list[dict[str, Any]] = []
        rows.extend(
            self._ttft_stat_rows(
                values=all_values,
                request_count=all_count[0],
                valid_sample_count=all_count[1],
                endpoint="",
                model="平台聚合",
                period=period,
                timestamp=end,
            )
        )
        endpoint_models = {endpoint: model for endpoint, model, _tpm in endpoints}
        for endpoint in sorted(endpoint_values, key=str.casefold):
            eligible, valid = endpoint_counts.get(endpoint, (0, 0))
            rows.extend(
                self._ttft_stat_rows(
                    values=endpoint_values[endpoint],
                    request_count=eligible,
                    valid_sample_count=valid,
                    endpoint=endpoint,
                    model=endpoint_models.get(endpoint, ""),
                    period=period,
                    timestamp=end,
                )
            )
        if not rows:
            warnings.append(f"{period} ttft detail no_data")
        return rows, warnings

    async def _gateway_usage_pages(
        self,
        client: MagikCubeClient,
        *,
        endpoint: str,
        model: str,
        start: datetime,
        end: datetime,
    ) -> tuple[list[dict[str, Any]], bool]:
        params: dict[str, Any] = {
            "endpoint": endpoint,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "page_num": 1,
            "page_size": 500,
            "stream": "true",
            "stream_done": "true",
        }
        if model:
            params["model"] = model
        first = await client.request("GET", "gateway/usages", params=params)
        items = [item for item in first.get("list") or [] if isinstance(item, dict)]
        total = _as_int(first.get("total"))
        required_pages = max(1, math.ceil(total / 500)) if total else 1
        page_count = min(required_pages, self._config.max_pages)
        if page_count > 1:
            pages = await asyncio.gather(
                *(
                    client.request(
                        "GET",
                        "gateway/usages",
                        params={**params, "page_num": page},
                    )
                    for page in range(2, page_count + 1)
                )
            )
            for page in pages:
                items.extend(item for item in page.get("list") or [] if isinstance(item, dict))
        return items, required_pages > self._config.max_pages

    @staticmethod
    def _eligible_ttft_usage(item: dict[str, Any]) -> bool:
        if item.get("stream") is False or item.get("streamDone", item.get("stream_done")) is False:
            return False
        response_code = _as_int(_pick(item, "respCode", "resp_code", default=None))
        return response_code is None or 200 <= response_code < 300

    @classmethod
    def _ttft_stat_rows(
        cls,
        *,
        values: list[float],
        request_count: int,
        valid_sample_count: int,
        endpoint: str,
        model: str,
        period: str,
        timestamp: datetime,
    ) -> list[dict[str, Any]]:
        if not values:
            return []
        stats = {
            "request_p50": cls._percentile(values, 0.50),
            "request_p95": cls._percentile(values, 0.95),
            "request_p99": cls._percentile(values, 0.99),
            "request_max": max(values),
        }
        return [
            {
                "metric": "ai.ttft",
                "value": value,
                "unit": "ms",
                "timestamp": timestamp.isoformat(),
                "date": timestamp.date().isoformat(),
                "endpoint": endpoint,
                "model": model,
                "period": period,
                "aggregation": aggregation,
                "sample_count": request_count,
                "valid_sample_count": valid_sample_count,
                "source": "Cube Admin / gateway/usages.ttft",
            }
            for aggregation, value in stats.items()
            if value is not None
        ]

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * quantile
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * weight

    def _health_realtime_capacity_rows(
        self, data: dict[str, Any], period: str, timestamp: datetime
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            endpoint = str(_pick(item, "endpoint", "endpointName", default="")).strip()
            model = str(_pick(item, "model", "modelName", default="")).strip()
            actual_tokens = _as_int(_pick(item, "actualTokens", "actual_tokens", default=0))
            tpm_limit = _as_int(_pick(item, "tpmLimit", "tpm_limit", default=0))
            value = self._rate_value(_pick(item, "utilizationRate", "utilization_rate", default=None))
            if value is None:
                continue
            rows.append(
                {
                    "metric": "ai.capacity_utilization",
                    "value": value,
                    "unit": "ratio",
                    "timestamp": timestamp.isoformat(),
                    "date": timestamp.date().isoformat(),
                    "endpoint": endpoint,
                    "model": model,
                    "actual_tokens": actual_tokens,
                    "tpm_limit": tpm_limit,
                    "period": period,
                    "aggregation": "current_snapshot",
                    "source": "Cube Admin / analysis/token-utilization/query",
                }
            )
        return rows

    def _health_daily_capacity_rows(
        self, data: dict[str, Any], period: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for point in data.get("points") or []:
            if not isinstance(point, dict):
                continue
            day = str(point.get("date") or "")[:10]
            if not day:
                continue
            value = self._rate_value(_pick(point, "utilizationRate", "utilization_rate", default=None))
            if value is None:
                continue
            rows.append(
                {
                    "metric": "ai.capacity_utilization",
                    "value": value,
                    "unit": "ratio",
                    "date": day,
                    "timestamp": datetime.combine(
                        date.fromisoformat(day), time.min, tzinfo=self._tz
                    ).isoformat(),
                    "endpoint": "",
                    "model": "",
                    "period": period,
                    "aggregation": "daily_value",
                    "source": "Cube Admin / analysis/token-utilization/daily/query",
                }
            )
        return [
            row
            for row in rows
            if start.date().isoformat() <= row["date"] < end.date().isoformat()
        ]

    def _health_endpoint_tpm_rows(
        self,
        data: dict[str, Any],
        period: str,
        start: datetime,
        end: datetime,
        endpoint_models: dict[str, str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            endpoint = str(item.get("endpoint") or "").strip()
            model = str(item.get("model") or endpoint_models.get(endpoint, "")).strip()
            for point in item.get("points") or []:
                day = str(point.get("date") or "")[:10]
                if not day or not (start.date().isoformat() <= day < end.date().isoformat()):
                    continue
                value = self._as_float(_pick(point, "maxTpm", "max_tpm", default=None))
                if value is None:
                    continue
                rows.append(
                    {
                        "metric": "ai.tpm",
                        "value": value,
                        "unit": "tokens/minute",
                        "date": day,
                        "timestamp": datetime.combine(
                            date.fromisoformat(day), time.min, tzinfo=self._tz
                        ).isoformat(),
                        "endpoint": endpoint,
                        "model": model,
                        "period": period,
                        "aggregation": "daily_peak",
                        "source": "Cube Admin / analysis/endpoint-tpm-trend/query",
                    }
                )
        return rows

    @staticmethod
    def _health_unit(metric: str) -> str:
        if metric in {"ai.error_rate", "ai.http_4xx_rate", "ai.http_5xx_rate"}:
            return "ratio"
        if metric in {"ai.interface_delay", "ai.ttft"}:
            return "ms"
        if metric == "ai.rpm":
            return "requests/minute"
        return "tokens/minute"

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            if value is None or isinstance(value, bool):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _rate_value(cls, value: Any) -> float | None:
        if isinstance(value, str) and value.strip().endswith("%"):
            parsed = cls._as_float(value.strip()[:-1])
            return parsed / 100 if parsed is not None else None
        parsed = cls._as_float(value)
        return parsed / 100 if parsed is not None and parsed > 1 else parsed

    @classmethod
    def _health_value(cls, metric: str, value: Any) -> float | None:
        if metric in {"ai.error_rate", "ai.http_4xx_rate", "ai.http_5xx_rate"}:
            return cls._rate_value(value)
        return cls._as_float(value)

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, dict):
            seconds = CubeConnector._as_float(value.get("seconds"))
            nanos = CubeConnector._as_float(value.get("nanos")) or 0
            if seconds is None:
                return None
            parsed = datetime.fromtimestamp(seconds + nanos / 1_000_000_000, tz=timezone.utc)
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed

    async def _resolve_tenants(
        self,
        client: MagikCubeClient,
        filters: dict[str, Any],
    ) -> list[_CubeTenant]:
        requested = str(filters.get("tenant") or "").strip()
        # Cube tenant catalog is the source of truth. Local display aliases
        # may not construct a tenant that is absent from this API response.
        params = (
            None
            if requested or filters.get("all_tenants") is True
            else {"isKeyAccount": "true"}
        )
        items = await self._get_pages(client, "tenants", params=params, label="Cube tenant catalog")
        tenants = [self._tenant(item) for item in items]
        tenants = [item for item in tenants if item is not None]
        if not requested:
            return tenants

        matches = _match_catalog_tenants(
            tenants,
            requested,
            self._config.tenant_mappings,
        )
        if not matches:
            raise MagikCubeTenantResolutionError(
                f"Cube tenant was not found: {requested}",
                failure_code="tenant_not_found",
            )
        if len(matches) > 1:
            raise MagikCubeTenantResolutionError(
                f"Cube tenant selection was ambiguous: {requested}",
                failure_code="tenant_ambiguous",
            )
        return matches

    async def _get_pages(
        self,
        client: MagikCubeClient,
        path: str,
        *,
        params: dict[str, Any] | None,
        label: str,
    ) -> list[dict[str, Any]]:
        base = dict(params or {})
        first = await client.request(
            "GET",
            path,
            params={**base, "page_num": 1, "page_size": 500},
        )
        first_items = [item for item in first.get("list") or [] if isinstance(item, dict)]
        total = _as_int(first.get("total"))
        required_pages = max(1, (total + 499) // 500) if total else 1
        page_count = min(required_pages, self._config.max_pages)
        pages = await asyncio.gather(
            *(
                client.request(
                    "GET",
                    path,
                    params={**base, "page_num": page, "page_size": 500},
                )
                for page in range(2, page_count + 1)
            )
        )
        items = list(first_items)
        for page in pages:
            items.extend(item for item in page.get("list") or [] if isinstance(item, dict))
        if required_pages > self._config.max_pages:
            raise MagikCubeApiError(
                f"{label} pagination exceeded max_pages={self._config.max_pages}"
            )
        return items

    @staticmethod
    def _tenant(item: dict[str, Any]) -> _CubeTenant | None:
        tenant_id = str(_pick(item, "tenantId", "tenant_id", "id", default="")).strip()
        if not tenant_id:
            return None
        return _CubeTenant(
            tenant_id=tenant_id,
            name=str(_pick(item, "tenantName", "tenant_name", "name", default=tenant_id)),
            tags=tuple(
                str(tag)
                for tag in (_pick(item, "tenantTags", "tenant_tags", "tags", default=[]) or [])
            ),
        )

    async def _query_tenant(
        self,
        client: MagikCubeClient,
        tenant: _CubeTenant,
        windows: list[tuple[str, date, date]],
        query: ReportQuery,
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        for period, start_date, end_date in windows:
            async with semaphore:
                try:
                    if {"ai.usage.tokens", "ai.requests"}.intersection(query.metrics):
                        model_values = self._selected_model_values(query.filters) or (None,)
                        for model in model_values:
                            data = await client.request(
                                "POST",
                                "analysis/active-tenant-daily-usage/query",
                                json_body=self._usage_body(
                                    tenant, start_date, end_date, query, model=model
                                ),
                            )
                            usage_rows = self._usage_rows(
                                data, tenant, period, start_date, end_date, query, model=model
                            )
                            rows.extend(usage_rows)
                            if not usage_rows:
                                label = f" model={model}" if model else ""
                                warnings.append(f"{tenant.name} {period} usage{label} no_data")
                except Exception as exc:
                    warnings.append(
                        f"{classify_report_failure(exc)}:"
                        f"{tenant.name} {period} usage query failed"
                    )
                if "ai.tpm" in query.metrics:
                    try:
                        model_values = self._selected_model_values(query.filters) or (None,)
                        for model in model_values:
                            data = await client.request(
                                "POST",
                                "analysis/endpoint-max-tpm/daily/query",
                                json_body=self._tpm_body(
                                    tenant, start_date, end_date, query, model=model
                                ),
                            )
                            tpm_rows = self._tpm_rows(
                                data, tenant, period, start_date, end_date, query, model=model
                            )
                            rows.extend(tpm_rows)
                            if not tpm_rows:
                                label = f" model={model}" if model else ""
                                warnings.append(f"{tenant.name} {period} TPM{label} no_data")
                    except Exception as exc:
                        warnings.append(
                            f"{classify_report_failure(exc)}:"
                            f"{tenant.name} {period} TPM query failed"
                        )
        return rows, warnings

    def _usage_body(
        self,
        tenant: _CubeTenant,
        start_date: date,
        end_date: date,
        query: ReportQuery,
        model: str | None = None,
    ) -> dict[str, Any]:
        start = datetime.combine(start_date, time.min, tzinfo=self._tz).isoformat()
        end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=self._tz).isoformat()
        body: dict[str, Any] = {
            "startTime": start,
            "endTime": end,
            "tenantId": tenant.tenant_id,
            "topN": 0,
            "timeLevel": "TIME_LEVEL_DAY",
        }
        model_value = str(model or query.filters.get("model") or "").strip()
        if model_value:
            body["model"] = model_value
        endpoint = str(query.filters.get("endpoint") or "").strip()
        if endpoint:
            body["endpoint"] = endpoint
        provider = str(query.filters.get("provider") or "").strip()
        if provider:
            body["provider"] = provider
        return body

    def _tpm_body(
        self,
        tenant: _CubeTenant,
        start_date: date,
        end_date: date,
        query: ReportQuery,
        model: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "tenantId": tenant.tenant_id,
        }
        model_value = str(model or query.filters.get("model") or "").strip()
        if model_value:
            body["model"] = model_value
        endpoint = str(query.filters.get("endpoint") or "").strip()
        if endpoint:
            body["endpoint"] = endpoint
        provider = str(query.filters.get("provider") or "").strip()
        if provider:
            body["provider"] = provider
        return body

    @staticmethod
    def _selected_models(filters: dict[str, Any]) -> set[str]:
        models = filters.get("models") or []
        if isinstance(models, str):
            models = [models]
        return {str(item).strip().casefold() for item in models if str(item).strip()}

    @staticmethod
    def _selected_model_values(filters: dict[str, Any]) -> tuple[str, ...]:
        models = filters.get("models") or []
        if isinstance(models, str):
            models = [models]
        return tuple(
            dict.fromkeys(str(item).strip() for item in models if str(item).strip())
        )

    def _usage_rows(
        self,
        data: dict[str, Any],
        tenant: _CubeTenant,
        period: str,
        start_date: date,
        end_date: date,
        query: ReportQuery,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        selected = self._selected_models(query.filters)
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(data.get("items") or []):
            row_model = str(
                _pick(item, "model", "modelName", "model_name", default="")
                or model
                or query.filters.get("model")
                or ""
            ).strip()
            endpoint = str(_pick(item, "endpoint", "endpointName", default="")).strip()
            if selected and row_model.casefold() not in selected:
                continue
            for point in item.get("points") or []:
                day = str(point.get("date") or "")[:10]
                if not day or day < start_date.isoformat() or day > end_date.isoformat():
                    continue
                common = {
                    "tenant": tenant.name,
                    "model": row_model,
                    "endpoint": endpoint,
                    "date": day,
                    "period": period,
                }
                rows.append(
                    {
                        **common,
                        "metric": "ai.usage.tokens",
                        "value": _as_int(_pick(point, "totalTokens", "total_tokens", default=0)),
                        "unit": "tokens",
                        "aggregation": "window_sum",
                        "source": (
                            "Cube Admin / analysis/active-tenant-daily-usage/query"
                        ),
                    }
                )
                rows.append(
                    {
                        **common,
                        "metric": "ai.requests",
                        "value": _as_int(_pick(point, "requestCount", "request_count", default=0)),
                        "unit": "requests",
                        "aggregation": "window_sum",
                        "source": (
                            "Cube Admin / analysis/active-tenant-daily-usage/query"
                        ),
                    }
                )
            if not item.get("points") and index == 0:
                continue
        return rows

    def _tpm_rows(
        self,
        data: dict[str, Any],
        tenant: _CubeTenant,
        period: str,
        start_date: date,
        end_date: date,
        query: ReportQuery,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        selected = self._selected_models(query.filters)
        rows: list[dict[str, Any]] = []
        for item in data.get("items") or []:
            row_model = str(
                _pick(item, "model", "modelName", "model_name", default="")
                or model
                or query.filters.get("model")
                or ""
            ).strip()
            endpoint = str(item.get("endpoint") or "").strip()
            if selected and row_model.casefold() not in selected:
                continue
            for point in item.get("points") or []:
                day = str(point.get("date") or "")[:10]
                if not day or day < start_date.isoformat() or day > end_date.isoformat():
                    continue
                rows.append(
                    {
                        "tenant": tenant.name,
                        "model": row_model,
                        "endpoint": endpoint,
                        "date": day,
                        "period": period,
                        "metric": "ai.tpm",
                        "value": _as_int(_pick(point, "maxTpm", "max_tpm", default=0)),
                        "unit": "tokens/minute",
                        "aggregation": "daily_peak",
                        "source": "Cube Admin / analysis/endpoint-max-tpm/daily/query",
                    }
                )
        return rows


class CubeCostAccountTemplate(TemplatePlugin):
    """Deterministic monthly Cube bill and wallet report through TokenAPI."""

    manifest = TemplateManifest(
        template_id="cost_account",
        display_name="Cube 成本与账户报表",
        version="1.0",
        category="cost",
        periods=frozenset({"month"}),
        required_metrics=frozenset(
            {"ai.usage.tokens", "ai.requests", "ai.cost", "ai.balance", "ai.unbilled_amount"}
        ),
        required_dimensions=frozenset({"tenant", "project", "model", "endpoint", "date"}),
        connector_ids=frozenset({"magik_cube"}),
        description="按账单归属月对比应付金额，并展示当前钱包余额和未结算金额",
    )

    def __init__(self, *, timezone: str = "Asia/Shanghai") -> None:
        self._timezone = timezone

    def plan(self, intent: ReportIntent) -> tuple[ReportQuery, ...]:
        if intent.period != "month":
            raise ValueError("Cube cost reports support month only")
        if not intent.tenant.strip():
            raise ValueError("Cube cost reports require an authorized tenant")
        if intent.start_date is None or intent.end_date is None:
            raise ValueError("Cube cost reports require a concrete monthly window")
        previous_end = intent.start_date - timedelta(days=1)
        previous_start = previous_end.replace(day=1)
        return (
            ReportQuery(
                connector_id=intent.connector_id,
                metrics=(
                    "ai.usage.tokens",
                    "ai.requests",
                    "ai.cost",
                    "ai.balance",
                    "ai.unbilled_amount",
                ),
                dimensions=("tenant", "project", "model", "endpoint", "date"),
                start_date=intent.start_date,
                end_date=intent.end_date,
                comparison_start=previous_start,
                comparison_end=previous_end,
                filters={
                    "tenant": intent.tenant,
                    "project": intent.project,
                    "model": intent.models[0] if len(intent.models) == 1 else "",
                    "endpoint": intent.endpoint,
                },
            ),
        )

    def analyze(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        if len(datasets) != 1:
            raise ValueError("Cube cost report expects one normalized dataset")
        dataset = datasets[0]
        current = [row for row in dataset.rows if row.get("period") == "current"]
        baseline = [row for row in dataset.rows if row.get("period") == "comparison"]
        snapshots = [row for row in dataset.rows if row.get("period") == "snapshot"]
        metric_specs = (
            ("ai.cost", "应付金额", "账单归属月总和", "Cube TokenAPI / bills.payableAmount"),
            ("ai.usage.tokens", "Token 消耗", "窗口总和", "Cube TokenAPI / usages/token.totalTokens"),
            ("ai.requests", "请求数", "窗口总和", "Cube TokenAPI / usages/token.requestCount"),
        )
        items: list[dict[str, Any]] = []
        for metric, label, aggregation, source in metric_specs:
            value = self._value(current, metric)
            prior = self._value(baseline, metric)
            items.append(
                {
                    "metric": metric,
                    "label": label,
                    "value": self._format(metric, value),
                    "baseline": self._format(metric, prior),
                    "baseline_value": prior,
                    "change": self._change(value, prior),
                    "unit": self._unit(metric),
                    "aggregation": aggregation,
                    "sample_count": self._sample_count(current, metric),
                    "valid_sample_count": self._sample_count(current, metric),
                    "source": source,
                }
            )
        for metric, label in (("ai.balance", "当前余额"), ("ai.unbilled_amount", "未结算金额")):
            value = self._value(snapshots, metric)
            items.append(
                {
                    "metric": metric,
                    "label": label,
                    "value": self._format(metric, value),
                    "baseline": "不适用",
                    "baseline_value": None,
                    "change": "当前钱包快照",
                    "unit": "amount",
                    "aggregation": "当前快照",
                    "sample_count": self._sample_count(snapshots, metric),
                    "valid_sample_count": self._sample_count(snapshots, metric),
                    "source": "Cube TokenAPI / wallets/balance",
                }
            )

        tenant = str(next((row.get("tenant") for row in dataset.rows if row.get("tenant")), ""))
        context = self._context(dataset, items)
        warnings = "；".join(dataset.warnings[:5]) if dataset.warnings else "无"
        report_params = {
            "tenant_query": tenant,
            "report_family": "cost",
            "subscription_period": "month",
        }
        blocks = [ReportBlock("metrics", {"items": items})]
        blocks.extend(
            (
                ReportBlock(
                    "note",
                    {
                        "content": f"数据质量：{dataset.quality}；失败或不可用接口：{warnings}",
                        "severity": "warning" if dataset.quality != "complete" else "info",
                    },
                ),
                ReportBlock(
                    "note",
                    {
                        "content": (
                            "读法：应付金额按 Cube 账单归属月汇总，当前月与前一自然月比较；"
                            "余额和未结算金额为生成时的钱包快照，不参与环比。"
                        )
                    },
                ),
                ReportBlock(
                    "actions",
                    {
                        "actions": [
                            {
                                "action_id": "subscription_setup:cost",
                                "label": "订阅月度成本报表",
                                "tool_name": "report_center",
                                "params": {
                                    "action": "subscription_setup",
                                    "period": "month",
                                    "report_family": "cost",
                                    "report_params": report_params,
                                },
                            }
                        ]
                    },
                ),
            )
        )
        summary = "；".join(
            f"{item['label']}：{item['value']}（{item['change']}）" for item in items
        )
        return ReportDocument(
            title=self.manifest.display_name,
            subtitle="Magik Cube · TokenAPI 只读账务口径",
            document_id=self.manifest.template_id,
            fallback_text=(
                f"{self.manifest.display_name}\n{summary}\n"
                f"当前窗口：{self._window_text(context.current_window)}\n"
                f"对比基准：{self._window_text(context.baseline_window, '暂无可比基准')}\n"
                "读法：账单金额按归属月比较；余额和未结算金额是当前快照。"
            ),
            blocks=tuple(blocks),
            quality=dataset.quality,
            warnings=dataset.warnings,
            context=context,
            version=2,
        )

    def _context(self, dataset: ReportDataset, items: list[dict[str, Any]]) -> ReportContext:
        windows = dataset.metadata.get("query_windows") or []

        def window(period: str) -> ReportWindow | None:
            item = next(
                (value for value in windows if isinstance(value, dict) and value.get("period") == period),
                None,
            )
            if item is None:
                return None
            return ReportWindow(str(item.get("start") or ""), str(item.get("end") or ""), period)

        sources = tuple(
            ReportSource(
                str(item.get("system") or "Cube TokenAPI"),
                str(item.get("route") or ""),
                tuple(str(field) for field in item.get("fields") or ()),
            )
            for item in dataset.metadata.get("source_refs") or ()
            if isinstance(item, dict) and item.get("route")
        )
        definitions = tuple(
            MetricDefinition(
                metric=str(item["metric"]),
                label=str(item["label"]),
                unit=str(item["unit"]),
                aggregation=str(item["aggregation"]),
                source=str(item["source"]),
                direction="informational",
            )
            for item in items
        )
        return ReportContext(
            timezone=self._timezone,
            current_window=window("current"),
            baseline_window=window("comparison"),
            baseline_policy="previous_equal_window",
            sources=sources,
            metric_definitions=definitions,
            calculation_version="1",
            quality=dataset.quality,
            quality_reasons=tuple(dataset.warnings),
            freshness=str(dataset.metadata.get("last_sample_at") or ""),
            template_version=self.manifest.version,
        )

    @staticmethod
    def _value(rows: list[dict[str, Any]], metric: str) -> float | None:
        values = []
        for row in rows:
            if row.get("metric") != metric:
                continue
            try:
                values.append(float(row["value"]))
            except (KeyError, TypeError, ValueError):
                continue
        return sum(values) if values else None

    @staticmethod
    def _sample_count(rows: list[dict[str, Any]], metric: str) -> int:
        return sum(1 for row in rows if row.get("metric") == metric)

    @staticmethod
    def _unit(metric: str) -> str:
        return "amount" if metric == "ai.cost" else "tokens" if metric == "ai.usage.tokens" else "requests"

    @staticmethod
    def _format(metric: str, value: float | None) -> str:
        if value is None:
            return "暂不可用"
        if metric in {"ai.usage.tokens", "ai.requests"}:
            return f"{int(value):,}"
        return f"{value:,.2f}"

    @staticmethod
    def _change(value: float | None, baseline: float | None) -> str:
        if value is None:
            return "当前无数据"
        if baseline is None:
            return "暂无可比基准"
        if baseline == 0:
            return "新增" if value else "持平"
        return f"{(value - baseline) / abs(baseline):+.1%}"

    @staticmethod
    def _window_text(value: ReportWindow | None, fallback: str = "暂无") -> str:
        return f"{value.start} - {value.end}" if value else fallback


DEFAULT_HEALTH_THRESHOLDS: dict[str, dict[str, float]] = {
    "ai.error_rate": {"attention": 0.02, "critical": 0.05},
    "ai.http_4xx_rate": {"attention": 0.05, "critical": 0.10},
    "ai.http_5xx_rate": {"attention": 0.01, "critical": 0.03},
    "ai.interface_delay": {"attention": 1000.0, "critical": 3000.0},
    "ai.ttft": {"attention": 1000.0, "critical": 3000.0},
    "ai.capacity_utilization": {"attention": 0.80, "critical": 0.90},
}

_HEALTH_STATUS_METRICS = frozenset(
    {
        "ai.error_rate",
        "ai.http_4xx_rate",
        "ai.http_5xx_rate",
        "ai.interface_delay",
        "ai.rpm",
        "ai.tpm",
    }
)


def normalize_health_thresholds(value: Mapping[str, Any] | None) -> dict[str, dict[str, float]]:
    """Validate configurable thresholds without allowing unbounded report values."""

    source = value or DEFAULT_HEALTH_THRESHOLDS
    result: dict[str, dict[str, float]] = {}
    for metric, defaults in DEFAULT_HEALTH_THRESHOLDS.items():
        raw = source.get(metric, defaults) if isinstance(source, Mapping) else defaults
        if not isinstance(raw, Mapping):
            raise ValueError(f"health threshold for {metric} must be an object")
        try:
            attention = float(raw.get("attention", defaults["attention"]))
            critical = float(raw.get("critical", defaults["critical"]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"health threshold for {metric} must be numeric") from exc
        upper_bound = 1.0 if metric.endswith("rate") or metric == "ai.capacity_utilization" else 1_000_000.0
        if not 0 <= attention <= critical <= upper_bound:
            raise ValueError(f"health threshold for {metric} is outside the allowed range")
        result[metric] = {"attention": attention, "critical": critical}
    unknown = set(source) - set(DEFAULT_HEALTH_THRESHOLDS) if isinstance(source, Mapping) else set()
    if unknown:
        raise ValueError(f"unsupported health threshold metrics: {sorted(unknown)}")
    return result


class CubeHealthTemplate(TemplatePlugin):
    """Deterministic platform-level Cube health report."""

    manifest = TemplateManifest(
        template_id="health_sre",
        display_name="Cube 健康报告",
        version="1.0",
        category="health",
        periods=frozenset({"recent15m", "day", "week"}),
        required_metrics=_CUBE_HEALTH_METRICS,
        required_dimensions=frozenset({"model", "endpoint", "date", "hour"}),
        connector_ids=frozenset({"magik_cube"}),
        description="平台级错误率、延迟、吞吐和容量健康快照及趋势",
    )

    def __init__(
        self,
        *,
        thresholds: Mapping[str, Any] | None = None,
        max_items: int = 10,
        semantics_v2: bool = False,
        presentation_v2: bool = False,
        ttft_detail_enabled: bool = False,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self.thresholds = normalize_health_thresholds(thresholds)
        self.max_items = max(1, min(int(max_items), 20))
        self.semantics_v2 = semantics_v2
        self.presentation_v2 = presentation_v2
        self.ttft_detail_enabled = ttft_detail_enabled
        self.timezone = timezone
        if semantics_v2:
            self.manifest = replace(
                self.manifest,
                version="2.0",
                description="可解释的 Cube 平台健康报告，包含基准、来源、口径和 TTFT 分位数",
            )

    def plan(self, intent: ReportIntent) -> tuple[ReportQuery, ...]:
        if intent.tenant or intent.project or intent.models or intent.provider:
            raise ValueError("Cube health reports are platform-level and do not accept scope filters")
        if intent.period == "recent15m":
            if intent.start_time is None or intent.end_time is None:
                raise ValueError("recent15m health planning requires start_time and end_time")
            start_date = intent.start_time.date()
            end_date = intent.end_time.date()
            comparison_start = intent.comparison_start_time
            comparison_end = intent.comparison_end_time
            return (
                ReportQuery(
                    connector_id=intent.connector_id,
                    metrics=tuple(sorted(_CUBE_HEALTH_METRICS)),
                    dimensions=("model", "endpoint", "date", "hour"),
                    start_date=start_date,
                    end_date=end_date,
                    start_time=intent.start_time,
                    end_time=intent.end_time,
                    comparison_start_time=comparison_start,
                    comparison_end_time=comparison_end,
                    filters={},
                    step_seconds=60,
                ),
            )
        if intent.period not in {"day", "week"}:
            raise ValueError("Cube health supports recent15m, day, and week")
        if intent.start_date is None or intent.end_date is None:
            raise ValueError("health trend planning requires concrete dates")
        days = (intent.end_date - intent.start_date).days + 1
        return (
            ReportQuery(
                connector_id=intent.connector_id,
                metrics=tuple(sorted(_CUBE_HEALTH_METRICS)),
                dimensions=("model", "endpoint", "date", "hour"),
                start_date=intent.start_date,
                end_date=intent.end_date,
                comparison_start=intent.start_date - timedelta(days=days),
                comparison_end=intent.start_date - timedelta(days=1),
                filters={},
                step_seconds=3600,
            ),
        )

    def analyze(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        if self.semantics_v2:
            return self._analyze_v2(datasets)
        return self._analyze_legacy(datasets)

    def shadow_summary(self, datasets: tuple[ReportDataset, ...]) -> dict[str, Any]:
        """Compare legacy maxima with v2 semantics using the same normalized dataset."""

        if not self.semantics_v2 or len(datasets) != 1:
            return {"status": "not_applicable"}
        rows = list(datasets[0].rows)
        current = [row for row in rows if row.get("period", "current") == "current"]
        comparison = [row for row in rows if row.get("period") == "comparison"]
        metrics = (
            "ai.error_rate",
            "ai.http_4xx_rate",
            "ai.http_5xx_rate",
            "ai.interface_delay",
            "ai.ttft",
            "ai.rpm",
            "ai.tpm",
            "ai.capacity_utilization",
        )
        legacy = {
            metric: (self._max_value(current, metric), self._max_value(comparison, metric))
            for metric in metrics
        }
        candidate = {
            metric: (
                self._metric_stat(current, metric)["value"],
                self._metric_stat(comparison, metric)["value"],
            )
            for metric in metrics
        }
        return compare_metric_summaries(legacy, candidate)

    def _analyze_legacy(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        if len(datasets) != 1:
            raise ValueError("Cube health expects one normalized dataset")
        dataset = datasets[0]
        current = [row for row in dataset.rows if row.get("period", "current") == "current"]
        comparison = [row for row in dataset.rows if row.get("period") == "comparison"]
        metric_items: list[dict[str, Any]] = []
        critical: list[str] = []
        attention: list[str] = []
        for metric in (
            "ai.error_rate",
            "ai.http_4xx_rate",
            "ai.http_5xx_rate",
            "ai.interface_delay",
            "ai.ttft",
            "ai.rpm",
            "ai.tpm",
            "ai.capacity_utilization",
        ):
            current_value = self._max_value(current, metric)
            baseline_value = self._max_value(comparison, metric)
            if current_value is not None and metric in self.thresholds:
                threshold = self.thresholds[metric]
                if current_value >= threshold["critical"]:
                    critical.append(metric)
                elif current_value >= threshold["attention"]:
                    attention.append(metric)
            metric_items.append(
                {
                    "label": self._metric_label(metric),
                    "metric": metric,
                    "value": self._format_value(metric, current_value),
                    "raw_value": current_value,
                    "change": self._format_change(current_value, baseline_value),
                }
            )

        missing_metrics = [
            metric
            for metric in (
                "ai.error_rate",
                "ai.http_4xx_rate",
                "ai.http_5xx_rate",
                "ai.interface_delay",
                "ai.ttft",
                "ai.rpm",
                "ai.tpm",
                "ai.capacity_utilization",
            )
            if self._max_value(current, metric) is None
        ]
        if dataset.quality != "complete" or missing_metrics:
            status = "数据不足"
        elif critical:
            status = "异常"
        elif attention:
            status = "关注"
        else:
            status = "正常"
        status_reason = ""
        if critical:
            status_reason = "异常指标：" + "、".join(self._metric_label(item) for item in critical)
        elif attention:
            status_reason = "关注指标：" + "、".join(self._metric_label(item) for item in attention)
        elif dataset.quality != "complete":
            status_reason = "核心健康数据未完整返回"
        elif missing_metrics:
            status_reason = "缺少指标：" + "、".join(
                self._metric_label(item) for item in missing_metrics
            )
        metric_items.insert(0, {"label": "总体状态", "value": status, "change": status_reason})

        blocks: list[ReportBlock] = [ReportBlock("metrics", {"items": metric_items})]
        endpoint_rows = self._rank_rows(current, "endpoint", abnormal_only=True)
        model_rows = self._rank_rows(current, "model")
        if endpoint_rows:
            blocks.append(self._table_block("异常 Endpoint TopN", endpoint_rows, "endpoint"))
        if model_rows:
            blocks.append(self._table_block("模型性能 TopN", model_rows, "model"))
        warning_text = "；".join(dataset.warnings[:5]) if dataset.warnings else "无"
        period = str(current[0].get("period") if current else "当前窗口")
        blocks.extend(
            (
                ReportBlock(
                    "note",
                    {"content": f"状态：{status}；窗口：{period}；数据质量：{dataset.quality}"},
                ),
                ReportBlock(
                    "note",
                    {
                        "content": f"失败或缺失接口：{warning_text}",
                        "severity": "warning",
                    },
                )
                if dataset.warnings
                else ReportBlock(
                    "note",
                    {
                        "content": (
                            "缺少核心指标："
                            + "、".join(self._metric_label(item) for item in missing_metrics)
                        ),
                        "severity": "warning",
                    },
                )
                if missing_metrics
                else ReportBlock("note", {"content": "核心查询接口均返回数据。"}),
                ReportBlock(
                    "actions",
                    {
                        "actions": [
                            {
                                "action_id": "subscription_setup:health",
                                "label": "订阅健康日报",
                                "style": "default",
                            }
                        ]
                    },
                ),
            )
        )
        summary = "；".join(
            f"{item['label']}：{item['value']}（{item['change']}）"
            for item in metric_items[:9]
        )
        return ReportDocument(
            title="Cube 健康报告",
            subtitle="平台级聚合 · 固定阈值",
            document_id=self.manifest.template_id,
            fallback_text=f"Cube 健康报告\n总体状态：{status}\n{summary}",
            blocks=tuple(blocks),
            quality=dataset.quality,
            warnings=dataset.warnings,
        )

    def _analyze_v2(self, datasets: tuple[ReportDataset, ...]) -> ReportDocument:
        if len(datasets) != 1:
            raise ValueError("Cube health expects one normalized dataset")
        dataset = datasets[0]
        current = [row for row in dataset.rows if row.get("period", "current") == "current"]
        comparison = [row for row in dataset.rows if row.get("period") == "comparison"]
        metrics = (
            "ai.error_rate",
            "ai.http_4xx_rate",
            "ai.http_5xx_rate",
            "ai.interface_delay",
            "ai.ttft",
            "ai.rpm",
            "ai.tpm",
            "ai.capacity_utilization",
        )
        metric_items: list[dict[str, Any]] = []
        critical: list[str] = []
        attention: list[str] = []
        missing: list[str] = []
        sample_insufficient: list[str] = []
        for metric in metrics:
            current_stat = self._metric_stat(current, metric)
            baseline_stat = self._metric_stat(comparison, metric)
            value = current_stat["value"]
            baseline_value = baseline_stat["value"]
            valid_samples = int(current_stat.get("valid_sample_count") or 0)
            if value is None:
                missing.append(metric)
            metric_sample_insufficient = (
                metric == "ai.ttft"
                and current_stat.get("detail_available")
                and valid_samples < 20
            )
            if metric_sample_insufficient:
                sample_insufficient.append(metric)
            status_value = current_stat.get("status_value")
            if (
                status_value is not None
                and metric in self.thresholds
                and not metric_sample_insufficient
            ):
                threshold = self.thresholds[metric]
                if status_value >= threshold["critical"]:
                    critical.append(metric)
                elif status_value >= threshold["attention"]:
                    attention.append(metric)
            label = self._metric_label(metric)
            if metric == "ai.ttft" and current_stat.get("detail_available"):
                label = "TTFT P95"
            metric_items.append(
                {
                    "label": label,
                    "metric": metric,
                    "value": self._format_value(metric, value),
                    "raw_value": value,
                    "baseline_value": baseline_value,
                    "baseline": self._format_value(metric, baseline_value),
                    "change": self._format_change(value, baseline_value),
                    "unit": current_stat["unit"],
                    "aggregation": current_stat["aggregation"],
                    "sample_count": current_stat["sample_count"],
                    "valid_sample_count": valid_samples,
                    "source": current_stat["source"],
                    "trend_value": current_stat.get("trend_value"),
                    "detail_available": current_stat.get("detail_available", False),
                }
            )

        optional_warnings = tuple(dataset.metadata.get("optional_warnings") or ())
        if optional_warnings and not self._metric_stat(current, "ai.ttft").get(
            "detail_available"
        ):
            missing.append("ai.ttft")
        core_missing = [metric for metric in missing if metric in _HEALTH_STATUS_METRICS]
        ttft_missing = "ai.ttft" in missing
        detail_gap = bool(optional_warnings)
        if core_missing or ttft_missing or sample_insufficient or detail_gap:
            status = "数据不足"
        elif critical:
            status = "异常"
        elif attention:
            status = "关注"
        else:
            status = "正常"
        if critical:
            status_reason = "异常指标：" + "、".join(self._metric_label(item) for item in critical)
        elif attention:
            status_reason = "关注指标：" + "、".join(self._metric_label(item) for item in attention)
        elif sample_insufficient:
            status_reason = "样本不足：" + "、".join(self._metric_label(item) for item in sample_insufficient)
        elif core_missing:
            status_reason = "缺少核心指标：" + "、".join(
                self._metric_label(item) for item in dict.fromkeys(core_missing)
            )
        elif ttft_missing:
            status_reason = "请求级 TTFT 详情不可用"
        elif detail_gap:
            status_reason = "请求级 TTFT 部分详情不可用"
        elif missing:
            status_reason = "附加指标暂不可用：" + "、".join(
                self._metric_label(item) for item in dict.fromkeys(missing)
            )
        elif dataset.quality != "complete":
            status_reason = "核心指标可计算，但数据质量为 partial"
        else:
            status_reason = "所有核心指标均满足当前统计条件"
        metric_items.insert(0, {"label": "总体状态", "value": status, "change": status_reason})

        blocks: list[ReportBlock] = [ReportBlock("metrics", {"items": metric_items})]
        endpoint_rows = self._rank_rows_v2(current, "endpoint", abnormal_only=True)
        model_rows = self._rank_rows_v2(current, "model")
        if endpoint_rows:
            blocks.append(
                self._table_block_v2(
                    "异常 Endpoint TopN（按 5xx、错误率、TTFT P95 排序）",
                    endpoint_rows,
                    "endpoint",
                )
            )
        if model_rows:
            blocks.append(
                self._table_block_v2(
                    "模型性能 TopN（按 5xx、错误率、TTFT P95 排序）",
                    model_rows,
                    "model",
                )
            )
        ttft_detail_table = self._ttft_detail_table_v2(current)
        if ttft_detail_table is not None:
            blocks.append(ttft_detail_table)

        query_windows = dataset.metadata.get("query_windows") or []
        current_window = next(
            (item for item in query_windows if item.get("period") == "current"), None
        )
        baseline_window = next(
            (item for item in query_windows if item.get("period") == "comparison"), None
        )
        context = self._report_context(
            current_window=current_window,
            baseline_window=baseline_window,
            ttft_detail=bool(dataset.metadata.get("ttft_detail_enabled")),
        )
        warning_text = "；".join(dataset.warnings[:5]) if dataset.warnings else "无"
        period = str(current_window.get("period") if current_window else "当前窗口")
        blocks.append(
            ReportBlock(
                "note",
                {
                    "content": (
                        f"状态：{status}；统计窗口：{period}；数据质量：{dataset.quality}"
                    )
                },
            )
        )
        if dataset.warnings:
            blocks.append(
                ReportBlock(
                    "note",
                    {"content": f"失败或缺失接口：{warning_text}", "severity": "warning"},
                )
            )
        if optional_warnings:
            blocks.append(
                ReportBlock(
                    "note",
                    {
                        "content": "；".join(optional_warnings[:5]),
                        "severity": "warning",
                    },
                )
            )
        if sample_insufficient:
            blocks.append(
                ReportBlock(
                    "note",
                    {
                        "content": "样本不足时不使用 P95/P99 触发异常，需扩大统计窗口后再判断。",
                        "severity": "warning",
                    },
                )
            )
        blocks.append(
            ReportBlock(
                "note",
                {
                    "content": self._ttft_explanation(current, comparison),
                },
            )
        )
        blocks.append(
            ReportBlock(
                "actions",
                {
                    "actions": [
                        {
                            "action_id": "subscription_setup:health",
                            "label": "订阅健康日报",
                            "style": "default",
                        }
                    ]
                },
            )
        )
        summary = "；".join(
            f"{item['label']}：{item['value']}（{item['change']}）"
            for item in metric_items[:9]
        )
        fallback_lines = [
            "Cube 健康报告",
            f"总体状态：{status}（{status_reason}）",
            summary,
            self._context_text(context),
            self._ttft_explanation(current, comparison),
        ]
        if dataset.warnings:
            fallback_lines.append("数据提示：" + "；".join(dataset.warnings[:5]))
        if optional_warnings:
            fallback_lines.append("可选数据提示：" + "；".join(optional_warnings[:5]))
        return ReportDocument(
            title="Cube 健康报告",
            subtitle="平台级聚合 · 前一等长窗口对比",
            document_id=self.manifest.template_id,
            fallback_text="\n".join(fallback_lines),
            blocks=tuple(blocks),
            quality=dataset.quality,
            warnings=dataset.warnings,
            context=context if self.presentation_v2 else None,
        )

    def _metric_stat(self, rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
        metric_rows = [row for row in rows if row.get("metric") == metric]
        if metric == "ai.ttft":
            detail_rows = [
                row
                for row in metric_rows
                if row.get("aggregation") == "request_p95" and not row.get("endpoint")
            ]
            if detail_rows:
                row = max(detail_rows, key=lambda item: float(item.get("value") or 0))
                return {
                    "value": float(row["value"]),
                    "status_value": float(row["value"]),
                    "trend_value": self._trend_peak(metric_rows),
                    "unit": "ms",
                    "aggregation": "请求级 P95",
                    "sample_count": int(row.get("sample_count") or 0),
                    "valid_sample_count": int(row.get("valid_sample_count") or 0),
                    "source": "Cube Admin / gateway/usages.ttft",
                    "detail_available": True,
                }
            return {
                "value": None,
                "status_value": None,
                "trend_value": self._trend_peak(metric_rows),
                "unit": "ms",
                "aggregation": "请求级 P95（不可用）",
                "sample_count": len(metric_rows),
                "valid_sample_count": 0,
                "source": "Cube Admin / analysis/model-performance/query FIRST_TOKEN_DELAY",
                "detail_available": False,
            }

        values: list[float] = []
        numerator = 0.0
        denominator = 0.0
        has_weighted_counts = False
        for row in metric_rows:
            try:
                values.append(float(row["value"]))
            except (KeyError, TypeError, ValueError):
                continue
            try:
                row_numerator = float(row["numerator"])
                row_denominator = float(row["denominator"])
            except (KeyError, TypeError, ValueError):
                continue
            if row_denominator > 0:
                numerator += row_numerator
                denominator += row_denominator
                has_weighted_counts = True
        if not values:
            value = None
            aggregation = self._metric_aggregation(metric, weighted=False)
        elif has_weighted_counts and metric.endswith("rate"):
            value = numerator / denominator
            aggregation = self._metric_aggregation(metric, weighted=True)
        else:
            value = max(values)
            aggregation = self._metric_aggregation(metric, weighted=False)
        return {
            "value": value,
            "status_value": value,
            "trend_value": value,
            "unit": self._metric_unit(metric),
            "aggregation": aggregation,
            "sample_count": len(metric_rows),
            "valid_sample_count": len(values),
            "source": str(
                next(
                    (row.get("source") for row in metric_rows if row.get("source")),
                    "Cube Admin / analysis/model-performance/query",
                )
            ),
            "detail_available": False,
        }

    @staticmethod
    def _trend_peak(rows: list[dict[str, Any]]) -> float | None:
        values: list[float] = []
        for row in rows:
            if str(row.get("aggregation") or "").startswith("request_"):
                continue
            try:
                values.append(float(row["value"]))
            except (KeyError, TypeError, ValueError):
                continue
        return max(values) if values else None

    @staticmethod
    def _metric_aggregation(metric: str, *, weighted: bool) -> str:
        if weighted and metric.endswith("rate"):
            return "加权比例"
        if metric in {"ai.rpm", "ai.tpm"}:
            return "时间桶峰值"
        if metric == "ai.capacity_utilization":
            return "窗口峰值"
        return "时间桶峰值"

    def _rank_rows_v2(
        self, rows: list[dict[str, Any]], dimension: str, *, abnormal_only: bool = False
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get(dimension) or "").strip()
            if not key:
                continue
            entry = grouped.setdefault(
                key,
                {dimension: key, "model": str(row.get("model") or "-")},
            )
            metric = str(row.get("metric") or "")
            try:
                value = float(row["value"])
            except (KeyError, TypeError, ValueError):
                continue
            aggregation = str(row.get("aggregation") or "")
            if aggregation == "request_p95":
                entry["ai.ttft_p95"] = max(float(entry.get("ai.ttft_p95", 0)), value)
                entry["request_count"] = int(entry.get("request_count", 0)) + int(
                    row.get("sample_count") or 0
                )
            elif aggregation == "request_max":
                entry["ai.ttft_peak"] = max(float(entry.get("ai.ttft_peak", 0)), value)
            elif metric in {
                "ai.error_rate",
                "ai.http_4xx_rate",
                "ai.http_5xx_rate",
                "ai.interface_delay",
                "ai.rpm",
                "ai.tpm",
            }:
                entry[metric] = max(float(entry.get(metric, 0)), value)
        candidates = list(grouped.values())
        if abnormal_only:
            candidates = [
                item
                for item in candidates
                if any(
                    (
                        metric == "ai.ttft"
                        and item.get("ai.ttft_p95") is not None
                        and float(item["ai.ttft_p95"]) >= levels["attention"]
                    )
                    or (
                        metric != "ai.ttft"
                        and metric in item
                        and float(item[metric]) >= levels["attention"]
                    )
                    for metric, levels in self.thresholds.items()
                )
            ]
        return sorted(
            candidates,
            key=lambda item: (
                -float(item.get("ai.http_5xx_rate", 0)),
                -float(item.get("ai.error_rate", 0)),
                -float(item.get("ai.ttft_p95", 0)),
                -float(item.get("ai.interface_delay", 0)),
                str(item.get(dimension, "")).casefold(),
            ),
        )[: self.max_items]

    def _table_block_v2(
        self, title: str, rows: list[dict[str, Any]], dimension: str
    ) -> ReportBlock:
        columns = [
            {
                "tag": "column",
                "name": dimension,
                "display_name": "Endpoint" if dimension == "endpoint" else "模型",
                "data_type": "text",
            }
        ]
        headers = ["Endpoint" if dimension == "endpoint" else "模型"]
        if dimension != "model":
            columns.append(
                {"tag": "column", "name": "model", "display_name": "模型", "data_type": "text"}
            )
            headers.append("模型")
        columns.extend(
            [
                {"tag": "column", "name": "error_rate", "display_name": "错误率", "data_type": "text"},
                {"tag": "column", "name": "http_5xx_rate", "display_name": "5xx", "data_type": "text"},
                {"tag": "column", "name": "interface_delay", "display_name": "延迟峰值", "data_type": "text"},
                {"tag": "column", "name": "ttft_p95", "display_name": "TTFT P95", "data_type": "text"},
                {"tag": "column", "name": "tpm", "display_name": "TPM 峰值", "data_type": "text"},
                {"tag": "column", "name": "request_count", "display_name": "请求数", "data_type": "text"},
            ]
        )
        headers.extend(["错误率", "5xx", "延迟峰值", "TTFT P95", "TPM 峰值", "请求数"])
        table_rows: list[dict[str, Any]] = []
        for row in rows:
            table_row = {
                dimension: row.get(dimension, "-"),
                "error_rate": self._format_value("ai.error_rate", row.get("ai.error_rate")),
                "http_5xx_rate": self._format_value("ai.http_5xx_rate", row.get("ai.http_5xx_rate")),
                "interface_delay": self._format_value("ai.interface_delay", row.get("ai.interface_delay")),
                "ttft_p95": self._format_value("ai.ttft", row.get("ai.ttft_p95")),
                "tpm": self._format_value("ai.tpm", row.get("ai.tpm")),
                "request_count": str(int(row.get("request_count") or 0)),
            }
            if dimension != "model":
                table_row["model"] = row.get("model", "-")
            table_rows.append(table_row)
        return ReportBlock(
            "table",
            {
                "title": title,
                "columns": columns,
                "headers": headers,
                "rows": table_rows,
                "page_size": self.max_items,
            },
        )

    def _ttft_detail_table_v2(self, rows: list[dict[str, Any]]) -> ReportBlock | None:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            endpoint = str(row.get("endpoint") or "").strip()
            aggregation = str(row.get("aggregation") or "")
            if not endpoint or not aggregation.startswith("request_"):
                continue
            entry = grouped.setdefault(
                endpoint,
                {
                    "endpoint": endpoint,
                    "model": str(row.get("model") or "-"),
                    "request_count": int(row.get("sample_count") or 0),
                    "valid_sample_count": int(row.get("valid_sample_count") or 0),
                },
            )
            if aggregation == "request_p50":
                entry["p50"] = float(row.get("value") or 0)
            elif aggregation == "request_p95":
                entry["p95"] = float(row.get("value") or 0)
            elif aggregation == "request_p99":
                entry["p99"] = float(row.get("value") or 0)
            elif aggregation == "request_max":
                entry["peak"] = float(row.get("value") or 0)
        if not grouped:
            return None
        ordered = sorted(
            grouped.values(),
            key=lambda item: (-float(item.get("p95", 0)), str(item["endpoint"]).casefold()),
        )[: self.max_items]
        columns = [
            {"tag": "column", "name": "endpoint", "display_name": "Endpoint", "data_type": "text"},
            {"tag": "column", "name": "model", "display_name": "模型", "data_type": "text"},
            {"tag": "column", "name": "request_count", "display_name": "请求数", "data_type": "text"},
            {"tag": "column", "name": "valid_sample_count", "display_name": "有效样本", "data_type": "text"},
            {"tag": "column", "name": "p50", "display_name": "P50", "data_type": "text"},
            {"tag": "column", "name": "p95", "display_name": "P95", "data_type": "text"},
            {"tag": "column", "name": "p99", "display_name": "P99", "data_type": "text"},
            {"tag": "column", "name": "peak", "display_name": "峰值", "data_type": "text"},
        ]
        table_rows = [
            {
                "endpoint": item["endpoint"],
                "model": item["model"],
                "request_count": str(item["request_count"]),
                "valid_sample_count": str(item["valid_sample_count"]),
                "p50": self._format_value("ai.ttft", item.get("p50")),
                "p95": self._format_value("ai.ttft", item.get("p95")),
                "p99": self._format_value("ai.ttft", item.get("p99")),
                "peak": self._format_value("ai.ttft", item.get("peak")),
            }
            for item in ordered
        ]
        return ReportBlock(
            "table",
            {
                "title": "TTFT 请求级详情 TopN（按 P95 降序）",
                "columns": columns,
                "headers": ["Endpoint", "模型", "请求数", "有效样本", "P50", "P95", "P99", "峰值"],
                "rows": table_rows,
                "page_size": self.max_items,
            },
        )

    def _report_context(
        self,
        *,
        current_window: dict[str, Any] | None,
        baseline_window: dict[str, Any] | None,
        ttft_detail: bool,
    ) -> ReportContext:
        def window(value: dict[str, Any] | None) -> ReportWindow | None:
            if not value:
                return None
            return ReportWindow(
                start=str(value.get("start") or ""),
                end=str(value.get("end") or ""),
                label=str(value.get("period") or ""),
            )

        sources = (
            ReportSource(
                "Cube Admin",
                "analysis/model-performance/query",
                ("metric", "startTime", "endTime", "intervalMinutes", "value"),
            ),
            ReportSource(
                "Cube Admin",
                "analysis/token-utilization/query or daily/query",
                ("utilizationRate", "actualTokens", "tpmLimit"),
            ),
            ReportSource(
                "Cube Admin",
                "analysis/endpoint-tpm-trend/query",
                ("maxTpm", "date"),
            ),
        )
        if ttft_detail:
            sources += (
                ReportSource(
                    "Cube Admin",
                    "gateway/usages",
                    ("model", "endpoint", "respCode", "ttft", "createdAt"),
                ),
            )
        return ReportContext(
            timezone=self.timezone,
            current_window=window(current_window),
            baseline_window=window(baseline_window),
            baseline_policy="previous_equal_window",
            sources=sources,
            metric_definitions=tuple(
                MetricDefinition(
                    metric=metric,
                    label=self._metric_label(metric),
                    unit=self._metric_unit(metric),
                    aggregation=(
                        "请求级 P95（另提供时间桶峰值趋势）"
                        if metric == "ai.ttft" and ttft_detail
                        else self._metric_aggregation(metric, weighted=False)
                    ),
                    source=(
                        "Cube Admin / gateway/usages.ttft"
                        if metric == "ai.ttft" and ttft_detail
                        else "Cube Admin / analysis/model-performance/query"
                    ),
                    direction=(
                        "lower_is_better"
                        if metric not in {"ai.rpm", "ai.tpm"}
                        else "informational"
                    ),
                )
                for metric in (
                    "ai.error_rate",
                    "ai.http_4xx_rate",
                    "ai.http_5xx_rate",
                    "ai.interface_delay",
                    "ai.ttft",
                    "ai.rpm",
                    "ai.tpm",
                    "ai.capacity_utilization",
                )
            ),
            calculation_version="2",
        )

    def _ttft_explanation(
        self, current: list[dict[str, Any]], comparison: list[dict[str, Any]]
    ) -> str:
        current_detail = self._metric_stat(current, "ai.ttft")
        trend = current_detail.get("trend_value")
        if current_detail.get("detail_available"):
            detail_rows = [
                row
                for row in current
                if row.get("metric") == "ai.ttft"
                and str(row.get("aggregation") or "").startswith("request_")
                and not row.get("endpoint")
            ]
            detail_values = {
                str(row.get("aggregation")): self._format_value("ai.ttft", row.get("value"))
                for row in detail_rows
            }
            request_count = max(
                (int(row.get("sample_count") or 0) for row in detail_rows),
                default=0,
            )
            detail = (
                f"请求级 TTFT：请求数 {request_count}，有效样本 {current_detail['valid_sample_count']}，"
                f"P50 {detail_values.get('request_p50', '无数据')}，"
                f"P95 {detail_values.get('request_p95', '无数据')}，"
                f"P99 {detail_values.get('request_p99', '无数据')}，"
                f"峰值 {detail_values.get('request_max', '无数据')}"
            )
        else:
            detail = "请求级 TTFT 明细不可用，当前不使用趋势峰值判定健康状态"
        trend_text = self._format_value("ai.ttft", trend)
        return f"TTFT 说明：{detail}；趋势口径为 FIRST_TOKEN_DELAY 时间桶峰值：{trend_text}。"

    @staticmethod
    def _context_text(context: ReportContext) -> str:
        current = context.current_window
        baseline = context.baseline_window
        current_text = f"{current.start} - {current.end}" if current else "未提供"
        baseline_text = f"{baseline.start} - {baseline.end}" if baseline else "暂无可比基准"
        return (
            f"基准：前一等长窗口；当前：{current_text}；基准：{baseline_text}；"
            f"时区：{context.timezone}"
        )

    @staticmethod
    def _metric_unit(metric: str) -> str:
        if metric in {"ai.error_rate", "ai.http_4xx_rate", "ai.http_5xx_rate"}:
            return "ratio"
        if metric in {"ai.interface_delay", "ai.ttft"}:
            return "ms"
        if metric == "ai.rpm":
            return "requests/minute"
        return "tokens/minute"

    @staticmethod
    def _max_value(rows: list[dict[str, Any]], metric: str) -> float | None:
        values: list[float] = []
        for row in rows:
            if row.get("metric") != metric:
                continue
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError):
                continue
            values.append(value)
        return max(values) if values else None

    @staticmethod
    def _metric_label(metric: str) -> str:
        return {
            "ai.error_rate": "错误率",
            "ai.http_4xx_rate": "HTTP 4xx",
            "ai.http_5xx_rate": "HTTP 5xx",
            "ai.interface_delay": "接口延迟",
            "ai.ttft": "TTFT",
            "ai.rpm": "RPM",
            "ai.tpm": "TPM",
            "ai.capacity_utilization": "容量利用率",
        }.get(metric, metric)

    @classmethod
    def _format_value(cls, metric: str, value: float | None) -> str:
        if value is None:
            if metric == "ai.capacity_utilization":
                return "暂不可用"
            return "无数据"
        if metric in {"ai.error_rate", "ai.http_4xx_rate", "ai.http_5xx_rate", "ai.capacity_utilization"}:
            return f"{value:.1%}"
        if metric in {"ai.interface_delay", "ai.ttft"}:
            return f"{value:.0f} ms"
        return f"{value:.0f}"

    @classmethod
    def _format_change(cls, value: float | None, baseline: float | None) -> str:
        if value is None:
            return "当前无数据"
        if baseline is None:
            return "无对比数据"
        if baseline == 0:
            return "新增" if value else "持平"
        return f"{(value - baseline) / abs(baseline):+.1%}"

    def _rank_rows(
        self, rows: list[dict[str, Any]], dimension: str, *, abnormal_only: bool = False
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get(dimension) or "").strip()
            if not key:
                continue
            entry = grouped.setdefault(key, {dimension: key, "model": str(row.get("model") or "-")})
            metric = str(row.get("metric") or "")
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError):
                continue
            if metric in {"ai.error_rate", "ai.http_5xx_rate"}:
                entry[metric] = max(float(entry.get(metric, 0)), value)
            elif metric in {"ai.interface_delay", "ai.ttft", "ai.rpm", "ai.tpm"}:
                entry[metric] = max(float(entry.get(metric, 0)), value)
        candidates = grouped.values()
        if abnormal_only:
            candidates = [
                item
                for item in candidates
                if any(
                    metric in self.thresholds
                    and float(item.get(metric, 0)) >= levels["attention"]
                    for metric, levels in self.thresholds.items()
                )
            ]
        ordered = sorted(
            candidates,
            key=lambda item: (
                -float(item.get("ai.http_5xx_rate", 0)),
                -float(item.get("ai.error_rate", 0)),
                -float(item.get("ai.interface_delay", 0)),
                str(item.get(dimension, "")).casefold(),
            ),
        )
        return ordered[: self.max_items]

    def _table_block(
        self, title: str, rows: list[dict[str, Any]], dimension: str
    ) -> ReportBlock:
        identity_columns = [
            {
                "tag": "column",
                "name": dimension,
                "display_name": "Endpoint" if dimension == "endpoint" else "模型",
                "data_type": "text",
            }
        ]
        identity_headers = ["Endpoint" if dimension == "endpoint" else "模型"]
        if dimension != "model":
            identity_columns.append(
                {"tag": "column", "name": "model", "display_name": "模型", "data_type": "text"}
            )
            identity_headers.append("模型")
        return ReportBlock(
            "table",
            {
                "title": title,
                "columns": identity_columns + [
                    {"tag": "column", "name": "ai.error_rate", "display_name": "错误率", "data_type": "text"},
                    {"tag": "column", "name": "ai.http_5xx_rate", "display_name": "5xx", "data_type": "text"},
                    {"tag": "column", "name": "ai.interface_delay", "display_name": "延迟", "data_type": "text"},
                    {"tag": "column", "name": "ai.ttft", "display_name": "TTFT", "data_type": "text"},
                    {"tag": "column", "name": "ai.tpm", "display_name": "TPM", "data_type": "text"},
                ],
                "headers": identity_headers + ["错误率", "5xx", "延迟", "TTFT", "TPM"],
                "rows": [
                    {
                        dimension: row.get(dimension, "-"),
                        "ai.error_rate": self._format_value("ai.error_rate", row.get("ai.error_rate")),
                        "ai.http_5xx_rate": self._format_value("ai.http_5xx_rate", row.get("ai.http_5xx_rate")),
                        "ai.interface_delay": self._format_value("ai.interface_delay", row.get("ai.interface_delay")),
                        "ai.ttft": self._format_value("ai.ttft", row.get("ai.ttft")),
                        "ai.tpm": self._format_value("ai.tpm", row.get("ai.tpm")),
                        **({"model": row.get("model", "-")} if dimension != "model" else {}),
                    }
                    for row in rows
                ],
                "page_size": self.max_items,
            },
        )
