from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from nanobot.agent.tools.magik_cube import (
    MagikCubeTenantResolutionError,
    MagikCubeTokenApiConfig,
    MagikCubeToolConfig,
    _as_int,
)
from nanobot.agent.tools.report_center import ReportCenterTool, ReportCenterToolConfig
from nanobot.reporting import (
    CubeConnector,
    CubeCostAccountTemplate,
    CubeHealthTemplate,
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportQuery,
    WeComReportRenderer,
    build_default_registry,
)
from nanobot.reporting.contracts import (
    ReportBlock,
    ReportQueryComparison,
    validate_report_intent,
    validate_report_query,
)


def _query(*metrics: str) -> ReportQuery:
    return ReportQuery(
        connector_id="magik_cube",
        metrics=metrics,
        dimensions=("tenant", "model", "date"),
        start_date=date(2026, 8, 28),
        end_date=date(2026, 8, 28),
        comparison_start=date(2026, 8, 27),
        comparison_end=date(2026, 8, 27),
    )


def _config(**overrides: object) -> MagikCubeToolConfig:
    return MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example.internal",
        access_token="fixture-token",
        max_retries=0,
        max_pages=2,
        **overrides,
    )


def test_sanitized_cube_contract_fixture_covers_usage_health_and_ttft() -> None:
    fixture_path = Path(__file__).parents[1] / "fixtures" / "reporting" / "cube_contract.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert set(fixture) == {"usage", "tpm", "health", "gateway_usages"}
    assert fixture["usage"]["items"][0]["points"][0]["totalTokens"] == "9007199254740993"
    assert fixture["tpm"]["items"][0]["points"][0]["maxTpm"] == "2400"
    assert fixture["tpm"]["items"][0]["points"][0]["avgTpm"] == "1800"
    assert fixture["gateway_usages"]["list"][0]["ttft"] == 120
    assert not any(
        sensitive in json.dumps(fixture).casefold()
        for sensitive in ("authorization", "bearer", "password", "api_key")
    )


@pytest.mark.asyncio
async def test_cube_connector_paginates_and_normalizes_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/tenants":
            page = int(request.url.params.get("page_num", "1"))
            if page == 1:
                data = {
                    "list": [{"tenantId": "tenant-a", "tenantName": "Tenant A"}],
                    "total": "501",
                }
            else:
                data = {
                    "list": [{"tenantId": "tenant-b", "tenantName": "Tenant B"}],
                    "total": "501",
                }
            return httpx.Response(200, json={"code": "0", "data": data})

        body = json.loads(request.content)
        day = body.get("startDate") or body["startTime"][:10]
        if request.url.path.endswith("active-tenant-daily-usage/query"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "model": "model-a",
                                "points": [
                                    {
                                        "date": day,
                                        "totalTokens": "9007199254740993",
                                        "requestCount": "3",
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("endpoint-max-tpm/daily/query"):
            return httpx.Response(
                200,
                json={
                    "code": "200",
                    "data": {
                        "items": [
                            {
                                "model": "model-a",
                                "endpoint": "endpoint-a",
                                "points": [
                                    {
                                        "date": day,
                                        "maxTpm": "40",
                                        "avgTpm": "9007199254740995",
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    connector = CubeConnector(_config(), transport=httpx.MockTransport(handler))
    result = await connector.query(
        _query("ai.usage.tokens", "ai.requests", "ai.tpm.avg", "ai.tpm")
    )

    assert result.quality == "complete"
    assert result.source == "magik_cube"
    token_rows = [row for row in result.rows if row["metric"] == "ai.usage.tokens"]
    assert len(token_rows) == 4
    assert token_rows[0]["value"] == 9007199254740993
    assert {row["period"] for row in result.rows} == {"current", "comparison"}
    assert result.metadata["query_windows"][0]["period"] == "current"
    assert result.metadata["source_refs"][0]["route"] == (
        "analysis/active-tenant-daily-usage/query"
    )
    assert token_rows[0]["unit"] == "tokens"
    assert token_rows[0]["aggregation"] == "window_sum"
    average_tpm_rows = [row for row in result.rows if row["metric"] == "ai.tpm.avg"]
    assert len(average_tpm_rows) == 4
    assert average_tpm_rows[0]["value"] == 9007199254740995
    assert average_tpm_rows[0]["endpoint"] == "endpoint-a"
    usage_request = next(
        request
        for request in requests
        if request.url.path.endswith("active-tenant-daily-usage/query")
        and json.loads(request.content)["startTime"].startswith("2026-08-28")
    )
    usage_body = json.loads(usage_request.content)
    assert usage_body["startTime"].startswith("2026-08-28T00:00:00+08:00")
    assert usage_body["endTime"].startswith("2026-08-29T00:00:00+08:00")
    assert any(request.url.path == "/api/v1/tenants" for request in requests)
    assert any(request.url.params.get("page_num") == "2" for request in requests)


@pytest.mark.asyncio
async def test_cube_connector_fetches_named_weekly_daily_comparison() -> None:
    requested_days: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenants"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [{"tenantId": "tenant-a", "tenantName": "Tenant A"}],
                        "total": 1,
                    },
                },
            )
        body = json.loads(request.content)
        day = body["startTime"][:10]
        requested_days.append(day)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [
                        {
                            "model": "model-a",
                            "points": [
                                {"date": day, "totalTokens": "10", "requestCount": "1"}
                            ],
                        }
                    ]
                },
            },
        )

    query = ReportQuery(
        connector_id="magik_cube",
        metrics=("ai.usage.tokens", "ai.requests"),
        dimensions=("tenant", "model", "date"),
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 29),
        comparison_start=date(2026, 8, 28),
        comparison_end=date(2026, 8, 28),
        additional_comparisons=(
            ReportQueryComparison(
                key="previous_week_same_day",
                start_date=date(2026, 8, 22),
                end_date=date(2026, 8, 22),
            ),
        ),
        filters={"tenant": "tenant-a"},
    )
    result = await CubeConnector(
        _config(), transport=httpx.MockTransport(handler)
    ).query(query)

    assert requested_days == ["2026-08-29", "2026-08-28", "2026-08-22"]
    assert {row["period"] for row in result.rows} == {
        "current",
        "comparison",
        "previous_week_same_day",
    }
    assert [item["period"] for item in result.metadata["query_windows"]] == [
        "current",
        "comparison",
        "previous_week_same_day",
    ]


@pytest.mark.asyncio
async def test_cube_connector_marks_partial_and_missing_data() -> None:
    def partial_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenants"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"id": "tenant-a", "name": "A"}]}}
            )
        if request.url.path.endswith("active-tenant-daily-usage/query"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {"model": "model-a", "points": [{"date": "2026-08-28", "totalTokens": "1"}]}
                        ]
                    },
                },
            )
        return httpx.Response(500, json={"message": "temporary"})

    partial = CubeConnector(_config(), transport=httpx.MockTransport(partial_handler))
    partial_result = await partial.query(_query("ai.usage.tokens", "ai.tpm"))
    assert partial_result.quality == "partial"
    assert any("TPM query failed" in warning for warning in partial_result.warnings)

    def empty_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenants"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"list": [{"id": "tenant-a", "name": "A"}]}}
            )
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    empty = CubeConnector(_config(), transport=httpx.MockTransport(empty_handler))
    empty_result = await empty.query(_query("ai.usage.tokens", "ai.tpm"))
    assert empty_result.quality == "missing"
    assert empty_result.rows == ()
    assert empty_result.warnings == ("no_business_data",)


@pytest.mark.asyncio
async def test_cube_account_connector_uses_tokenapi_and_monthly_baseline() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers.get("Authorization") == "Bearer tokenapi-fixture"
        if request.url.path.endswith("/bills"):
            amount = "12.50" if request.url.params["period"] == "2026-08" else "10.00"
            return httpx.Response(200, json={"code": 0, "data": {"list": [{"payableAmount": amount}]}})
        if request.url.path.endswith("/usages/token"):
            total = "101" if int(request.url.params["time_range.start_time"]) > 1_780_000_000 else "80"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"totalTokens": total, "requestCount": "4"}},
            )
        if request.url.path.endswith("/wallets/balance"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"balance": "88.20", "unbilledAmount": "2.30"}},
            )
        raise AssertionError(f"unexpected TokenAPI request: {request.url}")

    config = _config(
        token_api=MagikCubeTokenApiConfig(
            enable=True,
            base_url="https://token-api.example.internal",
            access_token="tokenapi-fixture",
            max_retries=0,
        )
    )
    query = ReportQuery(
        connector_id="magik_cube",
        metrics=("ai.usage.tokens", "ai.requests", "ai.cost", "ai.balance", "ai.unbilled_amount"),
        dimensions=("tenant", "project", "model", "endpoint", "date"),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        comparison_start=date(2026, 7, 1),
        comparison_end=date(2026, 7, 31),
        filters={"tenant": "佛跳墙", "project": "project-a"},
    )

    result = await CubeConnector(config, transport=httpx.MockTransport(handler)).query(query)

    assert result.quality == "complete"
    assert {row["metric"] for row in result.rows} == {
        "ai.usage.tokens",
        "ai.requests",
        "ai.cost",
        "ai.balance",
        "ai.unbilled_amount",
    }
    assert next(row for row in result.rows if row["metric"] == "ai.cost" and row["period"] == "current")["value"] == 12.5
    assert next(row for row in result.rows if row["metric"] == "ai.balance")["value"] == 88.2
    assert all("fixture-token" not in request.headers.get("Authorization", "") for request in requests)
    assert all("Authorization" not in row for row in result.rows)
    assert result.metadata["source_refs"][0]["system"] == "Cube TokenAPI"


@pytest.mark.asyncio
async def test_cube_account_connector_requires_independent_tokenapi_configuration() -> None:
    query = ReportQuery(
        connector_id="magik_cube",
        metrics=("ai.cost", "ai.balance"),
        dimensions=("tenant", "date"),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        filters={"tenant": "佛跳墙"},
    )

    result = await CubeConnector(_config()).query(query)

    assert result.quality == "missing"
    assert result.warnings == ("token_api_not_configured",)


def test_cost_template_keeps_bill_month_comparison_and_wallet_snapshot_distinct() -> None:
    template = CubeCostAccountTemplate()
    document = template.analyze(
        (
            ReportDataset(
                rows=(
                    {"tenant": "tencent_token_hub", "period": "current", "metric": "ai.cost", "value": 12.5},
                    {"tenant": "tencent_token_hub", "period": "comparison", "metric": "ai.cost", "value": 10.0},
                    {"tenant": "tencent_token_hub", "period": "snapshot", "metric": "ai.balance", "value": 88.2},
                    {"tenant": "tencent_token_hub", "period": "snapshot", "metric": "ai.unbilled_amount", "value": 2.3},
                ),
                metadata={
                    "query_windows": [
                        {"period": "current", "start": "2026-08-01 00:00", "end": "2026-09-01 00:00"},
                        {"period": "comparison", "start": "2026-07-01 00:00", "end": "2026-08-01 00:00"},
                    ],
                    "source_refs": [{"system": "Cube TokenAPI", "route": "bills", "fields": ["payableAmount"]}],
                },
            ),
        )
    )

    metrics = next(block for block in document.blocks if block.kind == "metrics").data["items"]
    cost = next(item for item in metrics if item["metric"] == "ai.cost")
    balance = next(item for item in metrics if item["metric"] == "ai.balance")
    assert cost["change"] == "+25.0%"
    assert balance["change"] == "当前钱包快照"
    assert document.context is not None
    assert document.context.baseline_window is not None
    assert "当前快照" in document.fallback_text


@pytest.mark.asyncio
async def test_cube_health_realtime_normalizes_fixed_routes_and_previous_window() -> None:
    requests: list[httpx.Request] = []
    current_start = datetime.fromisoformat("2026-08-28T10:00:00+08:00")
    comparison_start = current_start - timedelta(minutes=15)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("performance-endpoints/query"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {"endpoint": "endpoint-b", "model": "model-b", "tpm": "200"},
                            {"endpoint": "endpoint-a", "model": "model-a", "tpm": "100"},
                        ]
                    },
                },
            )
        if request.url.path.endswith("token-utilization/query"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "endpoint": "endpoint-a",
                                "model": "model-a",
                                "actualTokens": "9007199254740993",
                                "tpmLimit": "1000",
                                "utilizationRate": "0.95",
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("model-performance/query"):
            assert body["intervalMinutes"] == 1
            assert body["endpoints"] == ["endpoint-b", "endpoint-a"]
            value = {
                "TIMESERIES_METRIC_ERROR_RATE": 0.06,
                "TIMESERIES_METRIC_HTTP_4XX_RATE": 0.02,
                "TIMESERIES_METRIC_HTTP_5XX_RATE": 0.04,
                "TIMESERIES_METRIC_INTERFACE_DELAY": 1200,
                "TIMESERIES_METRIC_FIRST_TOKEN_DELAY": 800,
                "TIMESERIES_METRIC_RPM": 12,
                "TIMESERIES_METRIC_TPM": 200,
            }[body["metric"]]
            timestamp = body["startTime"]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "series": [
                            {
                                "endpoint": "endpoint-a",
                                "points": [{"timestamp": timestamp, "value": value}],
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected health request: {request.url}")

    query = ReportQuery(
        connector_id="magik_cube",
        metrics=(
            "ai.capacity_utilization",
            "ai.error_rate",
            "ai.http_4xx_rate",
            "ai.http_5xx_rate",
            "ai.interface_delay",
            "ai.ttft",
            "ai.rpm",
            "ai.tpm",
        ),
        dimensions=("model", "endpoint", "date", "hour"),
        start_date=current_start.date(),
        end_date=current_start.date(),
        start_time=current_start,
        end_time=current_start + timedelta(minutes=15),
        comparison_start_time=comparison_start,
        comparison_end_time=current_start,
        step_seconds=60,
    )
    result = await CubeConnector(_config(), transport=httpx.MockTransport(handler)).query(query)

    assert result.quality == "complete"
    assert {row["period"] for row in result.rows} == {"current", "comparison"}
    capacity = next(row for row in result.rows if row["metric"] == "ai.capacity_utilization")
    assert capacity["value"] == 0.95
    assert capacity["actual_tokens"] == 9007199254740993
    assert capacity["tpm_limit"] == 1000
    assert all("items" not in row for row in result.rows)
    assert all("Authorization" not in row for row in result.rows)
    assert len([request for request in requests if request.url.path.endswith("model-performance/query")]) == 14


@pytest.mark.asyncio
async def test_cube_health_ttft_detail_emits_request_percentiles_without_raw_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("performance-endpoints/query"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [{"endpoint": "kimi", "model": "Kimi-K3"}]},
                },
            )
        if request.url.path.endswith("model-performance/query"):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "series": [
                            {
                                "endpoint": "kimi",
                                "points": [{"timestamp": body["startTime"], "value": 900}],
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("gateway/usages"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "total": "4",
                        "list": [
                            {"stream": True, "streamDone": True, "respCode": 200, "ttft": 100},
                            {"stream": True, "streamDone": True, "respCode": 200, "ttft": 200},
                            {"stream": True, "streamDone": True, "respCode": 500, "ttft": 9000},
                            {"stream": False, "streamDone": True, "respCode": 200, "ttft": 8000},
                        ],
                    },
                },
            )
        raise AssertionError(f"unexpected health request: {request.url}")

    start = datetime.fromisoformat("2026-08-28T10:00:00+08:00")
    query = ReportQuery(
        connector_id="magik_cube",
        metrics=("ai.ttft",),
        dimensions=("endpoint", "model", "date", "hour"),
        start_date=start.date(),
        end_date=start.date(),
        start_time=start,
        end_time=start + timedelta(minutes=15),
        comparison_start_time=start - timedelta(minutes=15),
        comparison_end_time=start,
    )
    result = await CubeConnector(
        _config(),
        transport=httpx.MockTransport(handler),
        ttft_detail_enabled=True,
    ).query(query)

    detail = [row for row in result.rows if row.get("aggregation") == "request_p95"]
    assert result.quality == "complete"
    assert len(detail) == 4
    assert {row["valid_sample_count"] for row in detail} == {2}
    assert {row["sample_count"] for row in detail} == {2}
    assert all("request_id" not in row and "err_body" not in row for row in result.rows)
    assert all("req_header" not in row and "resp_header" not in row for row in result.rows)


def test_cube_health_v2_exposes_baseline_sources_and_ttft_semantics() -> None:
    template = CubeHealthTemplate(
        semantics_v2=True,
        presentation_v2=True,
        ttft_detail_enabled=True,
    )
    rows: list[dict[str, object]] = []
    for period, ttft_p95 in (("current", 1200.0), ("comparison", 800.0)):
        rows.extend(
            {
                "period": period,
                "metric": metric,
                "value": value,
                "endpoint": "kimi-endpoint",
                "model": "Kimi-K3",
                "aggregation": "time_bucket_value",
            }
            for metric, value in {
                "ai.error_rate": 0.01,
                "ai.http_4xx_rate": 0.01,
                "ai.http_5xx_rate": 0.005,
                "ai.interface_delay": 800.0,
                "ai.ttft": 900.0,
                "ai.rpm": 12.0,
                "ai.tpm": 200.0,
                "ai.capacity_utilization": 0.5,
            }.items()
        )
        rows.append(
            {
                "period": period,
                "metric": "ai.ttft",
                "value": ttft_p95,
                "endpoint": "",
                "model": "平台聚合",
                "aggregation": "request_p95",
                "sample_count": 40,
                "valid_sample_count": 40,
                "source": "Cube Admin / gateway/usages.ttft",
            }
        )
    document = template.analyze(
        (
            ReportDataset(
                rows=tuple(rows),
                quality="complete",
                metadata={
                    "ttft_detail_enabled": True,
                    "query_windows": [
                        {
                            "period": "current",
                            "start": "2026-08-28T10:00:00+08:00",
                            "end": "2026-08-28T10:15:00+08:00",
                            "interval_minutes": 1,
                        },
                        {
                            "period": "comparison",
                            "start": "2026-08-28T09:45:00+08:00",
                            "end": "2026-08-28T10:00:00+08:00",
                            "interval_minutes": 1,
                        },
                    ],
                },
            ),
        )
    )

    metrics = next(block for block in document.blocks if block.kind == "metrics").data["items"]
    ttft = next(item for item in metrics if item.get("metric") == "ai.ttft")
    assert ttft["label"] == "TTFT P95"
    assert ttft["value"] == "1200 ms"
    assert ttft["baseline"] == "800 ms"
    assert ttft["aggregation"] == "请求级 P95"
    assert document.context is not None
    assert document.context.current_window is not None
    assert document.context.baseline_window is not None
    assert any(source.route == "gateway/usages" for source in document.context.sources)
    assert any(
        "TTFT 说明" in block.data["content"] and "P50" in block.data["content"]
        for block in document.blocks
        if block.kind == "note"
    )


def test_cube_health_v2_marks_missing_ttft_detail_as_data_insufficient() -> None:
    template = CubeHealthTemplate(semantics_v2=True, presentation_v2=True)
    rows = [
        {
            "period": "current",
            "metric": metric,
            "value": (
                0.001
                if metric.endswith("rate")
                else 0.5
                if metric == "ai.capacity_utilization"
                else 1.0
            ),
            "endpoint": "endpoint-a",
            "model": "model-a",
            "aggregation": "time_bucket_value",
        }
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
    ]
    document = template.analyze(
        (ReportDataset(rows=tuple(rows), quality="complete", metadata={"ttft_detail_enabled": False}),)
    )
    metrics = next(block for block in document.blocks if block.kind == "metrics").data["items"]
    status = next(item for item in metrics if item.get("label") == "总体状态")
    assert status["value"] == "数据不足"
    assert "TTFT" in status["change"]
    assert "请求级 TTFT 明细不可用" in document.fallback_text


def test_cube_health_v2_keeps_core_status_when_optional_capacity_is_unavailable() -> None:
    template = CubeHealthTemplate(
        semantics_v2=True,
        presentation_v2=True,
        ttft_detail_enabled=True,
    )
    rows = list(
        {
            "period": "current",
            "metric": metric,
            "value": 0.001 if metric.endswith("rate") else 100.0,
            "endpoint": "endpoint-a",
            "model": "model-a",
            "aggregation": "time_bucket_value",
        }
        for metric in (
            "ai.error_rate",
            "ai.http_4xx_rate",
            "ai.http_5xx_rate",
            "ai.interface_delay",
            "ai.ttft",
            "ai.rpm",
            "ai.tpm",
        )
    )
    rows.append(
        {
            "period": "current",
            "metric": "ai.ttft",
            "value": 100.0,
            "endpoint": "",
            "model": "平台聚合",
            "aggregation": "request_p95",
            "sample_count": 40,
            "valid_sample_count": 40,
        }
    )
    document = template.analyze(
        (
            ReportDataset(
                rows=tuple(rows),
                quality="partial",
                warnings=("current capacity_utilization no_data",),
                metadata={"ttft_detail_enabled": True},
            ),
        )
    )
    metrics = next(block for block in document.blocks if block.kind == "metrics").data["items"]
    status = next(item for item in metrics if item.get("label") == "总体状态")
    capacity = next(item for item in metrics if item.get("metric") == "ai.capacity_utilization")
    assert status["value"] == "正常"
    assert capacity["value"] == "暂不可用"
    assert document.quality == "partial"
    assert "capacity_utilization no_data" in document.fallback_text


def test_cube_health_template_applies_fixed_thresholds_and_stable_ranking() -> None:
    template = CubeHealthTemplate()
    intent = ReportIntent(
        connector_id="magik_cube",
        template_id="health_sre",
        period="recent15m",
        start_time=datetime.fromisoformat("2026-08-28T10:00:00+08:00"),
        end_time=datetime.fromisoformat("2026-08-28T10:15:00+08:00"),
        comparison_start_time=datetime.fromisoformat("2026-08-28T09:45:00+08:00"),
        comparison_end_time=datetime.fromisoformat("2026-08-28T10:00:00+08:00"),
    )
    query = template.plan(intent)[0]
    health_rows = [
        {"period": "current", "metric": "ai.error_rate", "value": 0.06, "endpoint": "b", "model": "m"},
        {"period": "current", "metric": "ai.error_rate", "value": 0.06, "endpoint": "a", "model": "m"},
        {"period": "comparison", "metric": "ai.error_rate", "value": 0.01, "endpoint": "a", "model": "m"},
    ]
    health_rows.extend(
        {
            "period": "current",
            "metric": metric,
            "value": value,
            "endpoint": "a",
            "model": "m",
        }
        for metric, value in {
            "ai.http_4xx_rate": 0.01,
            "ai.http_5xx_rate": 0.005,
            "ai.interface_delay": 100.0,
            "ai.ttft": 100.0,
            "ai.rpm": 10.0,
            "ai.tpm": 100.0,
            "ai.capacity_utilization": 0.5,
        }.items()
    )
    document = template.analyze(
        (
            ReportDataset(
                rows=tuple(health_rows),
                quality="complete",
                source="magik_cube",
            ),
        )
    )

    assert query.start_time is not None and query.start_time.tzinfo is not None
    assert "总体状态：异常" in document.fallback_text
    table = next(block for block in document.blocks if block.kind == "table")
    assert table.data["rows"][0]["endpoint"] == "a"
    model_table = next(
        block for block in document.blocks if block.kind == "table" and "模型性能" in block.data["title"]
    )
    model_column_names = [column["name"] for column in model_table.data["columns"]]
    assert len(model_column_names) == len(set(model_column_names))


def test_health_time_contract_requires_aware_ordered_and_bounded_windows() -> None:
    naive_start = datetime(2026, 8, 28, 10, 0)
    naive_intent = ReportIntent(
        connector_id="magik_cube",
        template_id="health_sre",
        period="recent15m",
        start_time=naive_start,
        end_time=naive_start + timedelta(minutes=15),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_report_intent(naive_intent)

    aware_start = datetime.fromisoformat("2026-08-28T10:00:00+08:00")
    oversized = ReportIntent(
        connector_id="magik_cube",
        template_id="health_sre",
        period="recent15m",
        start_time=aware_start,
        end_time=aware_start + timedelta(minutes=16),
    )
    with pytest.raises(ValueError, match="15 minutes"):
        validate_report_intent(oversized)

    query = ReportQuery(
        connector_id="magik_cube",
        metrics=("ai.error_rate",),
        dimensions=("endpoint",),
        start_date=aware_start.date(),
        end_date=aware_start.date(),
        start_time=aware_start,
        end_time=aware_start + timedelta(minutes=15),
    )
    validate_report_query(query)


def test_cube_contract_preserves_large_integer_strings_and_fixed_route() -> None:
    assert _as_int("9007199254740993") == 9007199254740993
    tool = ReportCenterTool.__new__(ReportCenterTool)
    tool._config = ReportCenterToolConfig()
    for text, period in (("我要日报", "day"), ("我要周报", "week"), ("我要月报", "month")):
        assert tool.match_direct_request(text) == {
            "action": "cube_report",
            "period": period,
            "interactive": True,
            "report_template": "brief",
        }
    assert tool.match_direct_request("Kimi-K3模型的日报") == {
        "action": "cube_report",
        "period": "day",
        "model": "Kimi-K3",
        "all_tenants": True,
        "report_template": "brief",
    }


def test_cube_tenant_aliases_are_not_seeded_without_deployment_configuration() -> None:
    config = MagikCubeToolConfig()

    assert config.tenant_mappings == {}


@pytest.mark.asyncio
async def test_cube_connector_resolves_alias_only_when_catalog_returns_its_tenant() -> None:
    seen_tenants: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenants"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "tenantId": "tencent_token_hub",
                                "tenantName": "tencent_token_hub",
                                "tenantTags": ["佛跳墙"],
                            }
                        ]
                    },
                },
            )
        body = json.loads(request.content)
        seen_tenants.append(body["tenantId"])
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [
                        {
                            "model": "Kimi-K3",
                            "points": [
                                {"date": body["startTime"][:10], "totalTokens": "10"}
                            ],
                        }
                    ]
                },
            },
        )

    query = ReportQuery(
        connector_id="magik_cube",
        metrics=("ai.usage.tokens",),
        dimensions=("tenant", "model", "date"),
        start_date=date(2026, 8, 28),
        end_date=date(2026, 8, 28),
        filters={"tenant": "佛跳墙", "models": ["Kimi-K3"]},
    )
    result = await CubeConnector(
        _config(tenant_mappings={"错误别名": "does-not-exist"}),
        transport=httpx.MockTransport(handler),
    ).query(query)

    assert seen_tenants == ["tencent_token_hub"]
    assert {row["tenant"] for row in result.rows} == {"tencent_token_hub"}


@pytest.mark.asyncio
async def test_cube_connector_resolves_exact_catalog_tenant_id() -> None:
    seen_tenants: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenants"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "tenantId": "tenant-baowjhsicyf65",
                                "tenantName": "客户A",
                                "tenantTags": ["佛跳墙"],
                            }
                        ]
                    },
                },
            )
        body = json.loads(request.content)
        seen_tenants.append(body["tenantId"])
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [
                        {
                            "model": "Kimi-K3",
                            "points": [{"date": body["startTime"][:10], "totalTokens": "10"}],
                        }
                    ]
                },
            },
        )

    query = ReportQuery(
        connector_id="magik_cube",
        metrics=("ai.usage.tokens",),
        dimensions=("tenant", "model", "date"),
        start_date=date(2026, 8, 28),
        end_date=date(2026, 8, 28),
        filters={"tenant": "tenant-baowjhsicyf65"},
    )
    result = await CubeConnector(
        _config(), transport=httpx.MockTransport(handler)
    ).query(query)

    assert seen_tenants == ["tenant-baowjhsicyf65"]
    assert result.quality == "complete"


@pytest.mark.asyncio
async def test_cube_connector_rejects_ambiguous_name_or_tag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/tenants")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "list": [
                        {"tenantId": "prod", "tenantName": "生产", "tenantTags": ["佛跳墙"]},
                        {"tenantId": "test", "tenantName": "测试", "tenantTags": ["佛跳墙"]},
                    ]
                },
            },
        )

    query = ReportQuery(
        connector_id="magik_cube",
        metrics=("ai.usage.tokens",),
        dimensions=("tenant", "model", "date"),
        start_date=date(2026, 8, 28),
        end_date=date(2026, 8, 28),
        filters={"tenant": "佛跳墙"},
    )

    with pytest.raises(MagikCubeTenantResolutionError) as exc_info:
        await CubeConnector(_config(), transport=httpx.MockTransport(handler)).query(query)

    assert getattr(exc_info.value, "failure_code", "") == "tenant_ambiguous"


@pytest.mark.asyncio
async def test_cube_connector_queries_selected_model_for_all_catalog_tenants() -> None:
    queried_tenants: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenants"):
            assert "isKeyAccount" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {"tenantId": "tenant-a", "tenantName": "A", "isKeyAccount": True},
                            {"tenantId": "tenant-b", "tenantName": "B", "isKeyAccount": False},
                        ],
                        "total": 2,
                    },
                },
            )
        body = json.loads(request.content)
        queried_tenants.append(body["tenantId"])
        assert body["model"] == "Kimi-K3"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [
                        {
                            "model": "Kimi-K3",
                            "points": [
                                {"date": body["startTime"][:10], "totalTokens": "10"}
                            ],
                        }
                    ]
                },
            },
        )

    query = ReportQuery(
        connector_id="magik_cube",
        metrics=("ai.usage.tokens",),
        dimensions=("tenant", "model", "date"),
        start_date=date(2026, 8, 28),
        end_date=date(2026, 8, 28),
        filters={"models": ["Kimi-K3"], "all_tenants": True},
    )
    result = await CubeConnector(_config(), transport=httpx.MockTransport(handler)).query(query)

    assert set(queried_tenants) == {"tenant-a", "tenant-b"}
    assert result.metadata["scope"] == {
        "tenant_catalog": "all_tenants",
        "all_tenants": True,
        "tenant": "",
        "tenant_count": 2,
        "model_scope": "selected",
        "models": ["Kimi-K3"],
    }


def test_extension_points_are_opt_in_at_registry_boundary() -> None:
    registry = build_default_registry(
        discover_external=False,
        magik_enabled=False,
        grafana_config={"base_url": "https://grafana.example.internal"},
        wecom_renderer_enabled=False,
        dingtalk_renderer_enabled=False,
    )

    assert registry.connector("grafana") is None
    assert registry.exact_renderer("wecom") is None
    assert registry.exact_renderer("dingtalk") is None


def test_markdown_renderer_uses_structured_column_names_for_cube_tables() -> None:
    document = ReportDocument(
        title="Cube 用量",
        blocks=(
            ReportBlock(
                "table",
                {
                    "columns": [
                        {"name": "tenant", "display_name": "客户"},
                        {"name": "tokens", "display_name": "Token"},
                    ],
                    "headers": ["客户", "Token"],
                    "rows": [{"tenant": "tenant-a", "tokens": 123}],
                },
            ),
        ),
    )

    rendered = WeComReportRenderer().render(document)

    assert "tenant-a" in rendered.content
    assert "123" in rendered.content
