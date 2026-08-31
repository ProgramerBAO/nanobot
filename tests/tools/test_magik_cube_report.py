from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanobot.agent.tools.magik_cube import (
    MagikCubeApiError,
    MagikCubeClient,
    MagikCubeDailyReportTool,
    MagikCubeReporter,
    MagikCubeToolConfig,
    _DateWindow,
    _diff_proxy_snapshots,
    _plan_comparison_windows,
    _Tenant,
)
from nanobot.bus.events import OUTBOUND_META_AGENT_UI
from nanobot.utils.report_failures import classify_report_failure


class _FakeClient:
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if path == "tenants":
            return {
                "list": [
                    {"tenantId": "t1", "tenantName": "甲客户", "isKeyAccount": True},
                    {"tenantId": "t2", "tenantName": "普通客户", "isKeyAccount": False},
                ],
                "total": 2,
            }
        if path == "analysis/active-tenant-daily-usage/query":
            assert json_body and json_body["tenantId"] == "t1"
            return {
                "items": [
                    {
                        "tenantId": "t1",
                        "points": [
                            {"date": "2026-08-06", "totalTokens": "50"},
                            {"date": "2026-08-12", "totalTokens": "100"},
                            {"date": "2026-08-13", "totalTokens": "200"},
                        ],
                    }
                ]
            }
        if path == "analysis/endpoint-max-tpm/daily/query":
            return {
                "items": [
                    {
                        "endpoint": "ep-a",
                        "points": [
                            {"date": "2026-08-06", "maxTpm": "10"},
                            {"date": "2026-08-12", "maxTpm": "20"},
                            {"date": "2026-08-13", "maxTpm": "40"},
                        ],
                    }
                ]
            }
        if path == "inference/endpoints":
            return {"list": [{"endpointId": "ep-id"}], "total": 1}
        if path == "inference/model-configs":
            return {"list": [{"modelConfigId": "mc-id"}], "total": 1}
        if path == "quota-changes/list":
            return {
                "list": [
                    {
                        "entityId": "ep-id",
                        "entityName": "生产接入点",
                        "requesterName": "张三",
                        "tpmChange": {"oldValue": 100, "newValue": 200},
                    }
                ],
                "total": 1,
            }
        if path == "clusters":
            return {"list": [{"name": "prod"}], "total": 1}
        if path == "gateway/proxy-configs":
            return {
                "list": [
                    {
                        "name": "proxy-a",
                        "data": {
                            "proxy.yaml": (
                                "name: proxy-a\nmaxTPM: 500\n"
                                "maxRunningRequests: 20\nmaxNewSessions: 5\n"
                            )
                        },
                    }
                ],
                "total": 1,
            }
        if path == "analysis/model-machine-usage/query":
            return {
                "list": [
                    {
                        "clusterName": "prod",
                        "model": "GLM-5",
                        "machineCount": 2,
                        "gpuCount": 16,
                        "gpuProduct": "H100",
                    }
                ]
            }
        if path == "gateway/usages":
            return {
                "list": [
                    {"prefillPodName": "p-1", "podName": "d-1"},
                    {"prefillPodName": "p-1", "podName": "d-2"},
                ],
                "total": 2,
            }
        raise AssertionError(f"unexpected API request: {method} {path} {params} {json_body}")


class _FocusedUsageClient:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.paths: list[str] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.paths.append(path)
        if path == "tenants":
            return {
                "list": [
                    {
                        "tenantId": "prod",
                        "tenantName": "customer_prod",
                        "tenantTags": ["佛跳墙", "生产"],
                    },
                    {
                        "tenantId": "test",
                        "tenantName": "customer_test",
                        "tenantTags": ["佛跳墙", "测试"],
                    },
                ],
                "total": 2,
            }
        assert json_body is not None
        self.bodies.append(json_body)
        tenant_id = json_body["tenantId"]
        if path == "analysis/active-tenant-daily-usage/query":
            return {
                "items": [
                    {
                        "tenantId": tenant_id,
                        "points": [{"date": "2026-08-13", "totalTokens": 100}],
                    }
                ]
            }
        if path == "analysis/endpoint-max-tpm/daily/query":
            return {
                "items": [
                    {
                        "endpoint": f"ep-{tenant_id}",
                        "points": [{"date": "2026-08-13", "maxTpm": 20}],
                    }
                ]
            }
        raise AssertionError(f"unexpected API request: {method} {path} {params} {json_body}")


async def test_client_unwraps_envelope_and_sets_bearer_token() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"code": 0, "data": {"value": 42}})

    config = MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example",
        api_prefix="/api/admin-manager",
        access_token="secret",
    )
    async with MagikCubeClient(config, transport=httpx.MockTransport(handler)) as client:
        result = await client.request("GET", "tenants")

    assert result == {"value": 42}
    assert seen["url"] == "https://cube.example/api/admin-manager/tenants"
    assert seen["authorization"] == "Bearer secret"


async def test_client_applies_shared_api_concurrency_limit() -> None:
    active = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})

    config = MagikCubeToolConfig(
        base_url="https://cube.example", access_token="secret", max_concurrency=8
    )
    async with MagikCubeClient(config, transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(*(client.request("GET", "tenants") for _ in range(20)))

    assert peak == 8
    assert client.route_counts == {"tenants": 20}


async def test_client_logs_in_with_password_before_read_queries() -> None:
    seen: list[tuple[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/token-api/v1/accounts/login/with-password":
            assert json.loads(request.content) == {"account": "operator", "password": "pw"}
            return httpx.Response(
                200,
                json={"code": 0, "data": {"accessToken": "runtime-token"}},
            )
        return httpx.Response(200, json={"code": 0, "data": {"value": 42}})

    config = MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example",
        api_prefix="/api/admin-manager",
        account="operator",
        password="pw",
    )
    async with MagikCubeClient(config, transport=httpx.MockTransport(handler)) as client:
        result = await client.request("GET", "tenants")

    assert result == {"value": 42}
    assert seen == [
        ("/token-api/v1/accounts/login/with-password", None),
        ("/api/admin-manager/tenants", "Bearer runtime-token"),
    ]


async def test_client_rejects_business_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40301, "message": "forbidden"})

    config = MagikCubeToolConfig(base_url="https://cube.example")
    async with MagikCubeClient(config, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MagikCubeApiError, match="forbidden"):
            await client.request("GET", "tenants")


@pytest.mark.parametrize(
    ("status_code", "expected_code", "expected_calls"),
    [
        (401, "auth_failed", 1),
        (403, "auth_failed", 1),
        (429, "rate_limited", 3),
        (500, "upstream_failed", 3),
    ],
)
async def test_client_classifies_http_failures_and_retries_only_transient_statuses(
    status_code: int,
    expected_code: str,
    expected_calls: int,
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, json={"message": "fixture upstream detail"})

    config = MagikCubeToolConfig(
        base_url="https://cube.example",
        max_retries=2,
        retry_backoff_seconds=0,
    )
    async with MagikCubeClient(config, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MagikCubeApiError) as exc_info:
            await client.request("GET", "tenants")

    assert exc_info.value.failure_code == expected_code
    assert calls == expected_calls


async def test_client_retries_transport_errors_and_classifies_connection_failure() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("fixture secret detail", request=request)

    config = MagikCubeToolConfig(
        base_url="https://cube.example",
        max_retries=2,
        retry_backoff_seconds=0,
    )
    async with MagikCubeClient(config, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ConnectError) as exc_info:
            await client.request("GET", "tenants")

    assert classify_report_failure(exc_info.value) == "connection_failed"
    assert calls == 3


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "tenants"),
        ("PATCH", "tenants/tenant-1"),
        ("PUT", "accounts/user-1"),
        ("DELETE", "inference/endpoints/ep-1"),
        ("POST", "analysis/tenant-token-daily-stats/backfill"),
    ],
)
async def test_client_blocks_every_non_allowlisted_route(method: str, path: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("blocked request reached the network transport")

    config = MagikCubeToolConfig(base_url="https://cube.example")
    async with MagikCubeClient(config, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MagikCubeApiError, match="blocked non-allowlisted"):
            await client.request(method, path)


async def test_client_blocks_configurable_login_path() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("blocked login request reached the network transport")

    config = MagikCubeToolConfig(
        base_url="https://cube.example",
        account="operator",
        password="pw",
        login_path="/api/v1/accounts",
    )
    with pytest.raises(MagikCubeApiError, match="login path is not on the strict allowlist"):
        async with MagikCubeClient(config, transport=httpx.MockTransport(handler)):
            pass


async def test_report_renders_comparisons_changes_machines_and_pd(tmp_path: Path) -> None:
    config = MagikCubeToolConfig(enable=True, base_url="https://cube.example")
    snapshot_path = tmp_path / "proxy.json"
    reporter = MagikCubeReporter(_FakeClient(), config, snapshot_path, "Asia/Shanghai")

    report = await reporter.generate(date(2026, 8, 13), save_snapshot=True)

    assert "大客户运营日报 · 2026-08-13" in report
    assert "甲客户" in report
    assert "Token 200｜较前日 ↑100.0%｜较7日前 ↑300.0%" in report
    assert "峰值TPM 40（ep-a）｜较前日 ↑100.0%｜较7日前 ↑300.0%" in report
    assert "TPM 100 → 200" in report
    assert "GLM-5 / prod：2 台（8卡等效），16 × H100 GPU" in report
    assert "P=1、D=2，观测比例 1:2" in report
    assert "暂未接入告警事件数据源" in report
    assert "Proxy 配置基线已建立" in report
    assert json.loads(snapshot_path.read_text(encoding="utf-8"))["proxies"] == {
        "prod/proxy-a": {
            "maxNewSessions": 5,
            "maxRunningRequests": 20,
            "maxTPM": 500,
        }
    }


async def test_focused_usage_uses_tenant_mapping_and_passes_model(tmp_path: Path) -> None:
    config = MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example",
        tenant_mappings={"佛跳墙": "prod"},
    )
    client = _FocusedUsageClient()
    reporter = MagikCubeReporter(client, config, tmp_path / "proxy.json", "Asia/Shanghai")

    report = await reporter.generate_usage_query(
        date(2026, 8, 13), tenant_query="佛跳墙用户", model="GLM-5.2"
    )

    assert "客户=佛跳墙用户，模型=GLM-5.2" in report
    assert "匹配 1 个租户｜Token 合计 100｜最高峰值 TPM 20" in report
    assert "佛跳墙" in report
    assert client.paths[0] == "tenants"
    assert all(body["tenantId"] == "prod" for body in client.bodies)
    assert all(body["model"] == "GLM-5.2" for body in client.bodies)


def test_proxy_snapshot_diff_reports_relevant_fields() -> None:
    changes = _diff_proxy_snapshots(
        {"prod/proxy-a": {"maxTPM": 100, "maxNewSessions": 5}},
        {
            "prod/proxy-a": {"maxTPM": 200, "maxNewSessions": 5},
            "prod/proxy-b": {"maxRunningRequests": 10},
        },
    )

    assert changes == [
        "prod/proxy-a：maxTPM 100 → 200",
        "prod/proxy-b：新增，maxRunningRequests=10",
    ]


def test_direct_route_extracts_tenant_model_and_date(tmp_path: Path) -> None:
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")

    assert tool.match_direct_request("看看昨天佛跳墙用户 GLM-5.2 有多少用量") == {
        "save_snapshot": False,
        "tenant_query": "佛跳墙",
        "model": "GLM-5.2",
    }
    assert tool.match_direct_request("查询 2026-08-10 佛跳墙客户 GLM-5.2 token") == {
        "save_snapshot": False,
        "report_date": "2026-08-10",
        "tenant_query": "佛跳墙",
        "model": "GLM-5.2",
    }


def test_direct_route_prefers_configured_tenant_alias(tmp_path: Path) -> None:
    tool = MagikCubeDailyReportTool(
        config=MagikCubeToolConfig(tenant_mappings={"豆汁": "tenant-baka99jxwy88n"}),
        snapshot_path=tmp_path / "proxy.json",
    )

    assert tool.match_direct_request("豆汁昨天用了多少量呢？") == {
        "save_snapshot": False,
        "tenant_query": "豆汁",
    }
    assert tool.max_calls_per_turn == 1


def test_direct_route_treats_generic_customer_quantifiers_as_all_customers(
    tmp_path: Path,
) -> None:
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")

    assert tool.match_direct_request("看看昨天各大客户都用了多少 tpm，峰值多少") == {
        "save_snapshot": False,
    }
    assert tool.match_direct_request("统计昨天所有客户的 token 用量") == {
        "save_snapshot": False,
    }
    assert tool.match_direct_request("查询每个租户昨天的 TPM") == {
        "save_snapshot": False,
    }


def test_direct_route_does_not_capture_unrelated_questions(tmp_path: Path) -> None:
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")

    assert tool.match_direct_request("GLM-5.2 支持多长上下文？") is None
    assert tool.match_direct_request("昨天机器是否正常？") is None


def test_comparison_planner_merges_adjacent_periods_and_chunks_long_ranges() -> None:
    single = _plan_comparison_windows(
        date(2026, 1, 1), date(2026, 1, 1), comparison="previous_period"
    )
    assert single.comparison == _DateWindow(date(2025, 12, 31), date(2025, 12, 31))
    assert single.fetch_windows == (_DateWindow(date(2025, 12, 31), date(2026, 1, 1)),)

    weekly = _plan_comparison_windows(
        date(2026, 8, 18),
        date(2026, 8, 24),
        comparison="previous_period",
    )
    assert weekly.comparison == _DateWindow(date(2026, 8, 11), date(2026, 8, 17))
    assert weekly.fetch_windows == (_DateWindow(date(2026, 8, 11), date(2026, 8, 24)),)

    monthly = _plan_comparison_windows(
        date(2024, 3, 1),
        date(2024, 3, 31),
        comparison="previous_month",
    )
    assert monthly.comparison == _DateWindow(date(2024, 2, 1), date(2024, 2, 29))
    assert monthly.fetch_windows == (_DateWindow(date(2024, 2, 1), date(2024, 3, 31)),)

    long_range = _plan_comparison_windows(
        date(2026, 1, 1), date(2026, 7, 1), max_request_days=90
    )
    assert [window.days for window in long_range.fetch_windows] == [90, 90, 2]
    assert long_range.fetch_windows[0].end + timedelta(days=1) == long_range.fetch_windows[1].start


def test_comparison_planner_keeps_far_windows_exact_and_enforces_total_limit() -> None:
    plan = _plan_comparison_windows(
        date(2026, 8, 1),
        date(2026, 8, 7),
        compare_start=date(2025, 8, 1),
        compare_end=date(2025, 8, 7),
    )
    assert plan.fetch_windows == (
        _DateWindow(date(2025, 8, 1), date(2025, 8, 7)),
        _DateWindow(date(2026, 8, 1), date(2026, 8, 7)),
    )
    with pytest.raises(ValueError, match="exceeds 366 days"):
        _plan_comparison_windows(date(2025, 1, 1), date(2026, 1, 2))


class _FilteringRangeClient:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def request(
        self,
        _method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if path == "tenants":
            return {"list": [{"tenantId": "prod", "tenantName": "生产客户"}], "total": 1}
        assert params is None
        assert json_body is not None
        self.bodies.append(json_body)
        if path == "analysis/active-tenant-daily-usage/query":
            return {
                "items": [
                    {
                        "tenantId": "tenant-1",
                        "points": [
                            {"date": "2026-08-01", "totalTokens": 10, "requestCount": 1},
                            {"date": "2026-08-01", "totalTokens": 10, "requestCount": 1},
                            {"date": "2026-08-02", "totalTokens": 20, "requestCount": 2},
                            {"date": "2026-08-03", "totalTokens": 999, "requestCount": 99},
                        ],
                    }
                ]
            }
        if path == "analysis/endpoint-max-tpm/daily/query":
            return {
                "items": [
                    {
                        "endpoint": "ep-a",
                        "points": [
                            {"date": "2026-08-01", "maxTpm": 5},
                            {"date": "2026-08-02", "maxTpm": 8},
                            {"date": "2026-08-03", "maxTpm": 999},
                        ],
                    }
                ]
            }
        raise AssertionError(path)


async def test_range_metrics_filters_extra_day_and_deduplicates_points(tmp_path: Path) -> None:
    reporter = MagikCubeReporter(
        _FilteringRangeClient(),
        MagikCubeToolConfig(base_url="https://cube.example"),
        tmp_path / "proxy.json",
        "Asia/Shanghai",
    )
    metrics = await reporter._tenant_metrics_for_windows(
        _Tenant("tenant-1", "甲客户"),
        (_DateWindow(date(2026, 8, 1), date(2026, 8, 2)),),
    )

    assert metrics.tokens == {"2026-08-01": 10, "2026-08-02": 20}
    assert metrics.requests == {"2026-08-01": 1, "2026-08-02": 2}
    assert metrics.max_tpm == {"2026-08-01": 5, "2026-08-02": 8}
    assert metrics.token_complete and metrics.tpm_complete


class _PartialFailureClient:
    async def request(
        self,
        _method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if path == "tenants":
            assert json_body is None
            return {"list": [{"tenantId": "prod", "tenantName": "生产客户"}], "total": 1}
        assert params is None
        assert json_body is not None
        if path == "analysis/active-tenant-daily-usage/query":
            raise httpx.ReadTimeout("token timeout")
        if path == "analysis/endpoint-max-tpm/daily/query":
            return {"items": []}
        raise AssertionError(path)


async def test_partial_interface_failure_is_marked_instead_of_rendered_as_zero(
    tmp_path: Path,
) -> None:
    config = MagikCubeToolConfig(
        base_url="https://cube.example", tenant_mappings={"测试租户": "prod"}
    )
    reporter = MagikCubeReporter(
        _PartialFailureClient(), config, tmp_path / "proxy.json", "Asia/Shanghai"
    )
    plan = _plan_comparison_windows(date(2026, 8, 1), date(2026, 8, 7))

    report = await reporter.generate_range_report(plan, tenant_query="测试租户")

    assert "Token 0（数据不完整）" in report
    assert "Token/请求数 2026-08-01 ~ 2026-08-07 获取失败" in report
    assert "完整：无接口失败" not in report


class _DeterministicReportClient:
    def __init__(self) -> None:
        self.model_list_calls = 0
        self.usage_calls = 0
        self.requested_models: list[str] = []

    async def request(
        self,
        _method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if path == "tenants":
            return {"list": [{"tenantId": "prod", "tenantName": "生产客户"}], "total": 1}
        if path == "inference/model-configs":
            self.model_list_calls += 1
            return {
                "list": [{"model": "MODEL-A"}, {"model": "MODEL-B"}, {"model": "LOW"}],
                "total": 3,
            }
        assert json_body is not None
        self.usage_calls += 1
        model = str(json_body.get("model") or "SUMMARY")
        self.requested_models.append(model)
        if path == "analysis/active-tenant-daily-usage/query":
            start = date.fromisoformat(str(json_body["startTime"])[:10])
            end_exclusive = date.fromisoformat(str(json_body["endTime"])[:10])
            points = []
            cursor = start
            while cursor <= end_exclusive:
                in_current = cursor >= date(2026, 8, 8)
                if model == "MODEL-A":
                    tokens = 700 if cursor == date(2026, 8, 14) else (100 if in_current else 0)
                elif model == "MODEL-B":
                    tokens = 0 if in_current else 50
                elif model == "LOW":
                    tokens = 1 if in_current else 0
                else:
                    tokens = (10_000 if in_current else 8_000) + (
                        700 if cursor == date(2026, 8, 14) else 0
                    )
                points.append(
                    {
                        "date": cursor.isoformat(),
                        "totalTokens": tokens,
                        "requestCount": 1 if tokens else 0,
                    }
                )
                cursor += timedelta(days=1)
            return {"items": [{"tenantId": "prod", "points": points}]}
        if path == "analysis/endpoint-max-tpm/daily/query":
            return {
                "items": [
                    {
                        "endpoint": "ep-a",
                        "points": [
                            {"date": f"2026-08-{day:02d}", "maxTpm": day * 10}
                            for day in range(1, 16)
                        ],
                    }
                ]
            }
        raise AssertionError(path)


async def test_model_report_is_deterministic_and_metadata_is_cached(tmp_path: Path) -> None:
    config = MagikCubeToolConfig(
        base_url="https://cube.example",
        tenant_mappings={"测试租户": "prod"},
    )
    client = _DeterministicReportClient()
    reporter = MagikCubeReporter(client, config, tmp_path / "proxy.json", "Asia/Shanghai")
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    first = await reporter.generate_range_report(
        plan, tenant_query="测试租户", breakdown="model", include_tpm=True
    )
    second = await reporter.generate_range_report(
        plan, tenant_query="测试租户", breakdown="model", include_tpm=True
    )

    assert first == second
    assert client.model_list_calls == 1
    assert "新增：LOW、MODEL-A" in first
    assert "停用：MODEL-B" in first
    assert "MODEL-A：2026-08-14" in first
    assert "LOW：" not in first.split("峰值异常：", 1)[1]
    assert first.index("• MODEL-A｜") < first.index("• LOW｜") < first.index("• MODEL-B｜")
    assert "完整：无接口失败、分页截断或分片缺失" in first


async def test_selected_model_alias_resolves_to_configured_model(tmp_path: Path) -> None:
    config = MagikCubeToolConfig(
        base_url="https://cube.example",
        tenant_mappings={"测试租户": "prod"},
        model_aliases={"k3": "MODEL-A"},
    )
    client = _DeterministicReportClient()
    reporter = MagikCubeReporter(client, config, tmp_path / "proxy.json", "Asia/Shanghai")
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [{"tenant_query": "测试租户", "model_scope": "selected", "models": ["k3"]}],
        granularity="day",
        include_tpm=True,
    )

    card = result.metadata[OUTBOUND_META_AGENT_UI]["cards"][0]
    assert card["title"] == "生产客户 周报"
    assert [row["model"] for row in card["table"]["rows"]] == ["MODEL-A"]
    assert "Token **1,300" in card["overview"][0]
    assert "Token **10,700" not in card["overview"][0]
    assert card["segments"][0].startswith("周六 100")
    assert card["segments"][-1].startswith("周五 700")
    assert client.requested_models == ["MODEL-A", "MODEL-A"]


async def test_selected_model_daily_card_uses_model_for_overview_and_segments(
    tmp_path: Path,
) -> None:
    client = _DeterministicReportClient()
    reporter = MagikCubeReporter(
        client,
        MagikCubeToolConfig(
            base_url="https://cube.example",
            tenant_mappings={"测试租户": "prod"},
            model_aliases={"k3": "MODEL-A"},
        ),
        tmp_path / "proxy.json",
        "Asia/Shanghai",
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 14), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [{"tenant_query": "测试租户", "model_scope": "selected", "models": ["k3"]}],
        granularity="day",
        include_tpm=True,
    )

    card = result.metadata[OUTBOUND_META_AGENT_UI]["cards"][0]
    assert "Token **700**" in card["overview"][0]
    assert card["segments"] == ["08-14 700｜+600 / ↑600.0%"]
    assert client.requested_models == ["MODEL-A", "MODEL-A"]


async def test_matrix_report_returns_single_table_payload_with_absolute_daily_deltas(
    tmp_path: Path,
) -> None:
    config = MagikCubeToolConfig(
        base_url="https://cube.example",
        tenant_mappings={"测试租户": "prod"},
        matrix_page_size=8,
    )
    reporter = MagikCubeReporter(
        _DeterministicReportClient(), config, tmp_path / "proxy.json", "Asia/Shanghai"
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [{"tenant_query": "测试租户", "model_scope": "all", "models": []}],
        granularity="day",
        include_tpm=True,
    )

    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    assert ui["kind"] == "magik_report_cards"
    assert len(ui["cards"]) == 1
    subscription_action = ui["cards"][0]["actions"][0]
    assert subscription_action["params"]["action"] == "subscription_setup"
    assert subscription_action["params"]["period"] == "week"
    table = ui["cards"][0]["table"]
    assert table["page_size"] == 8
    assert [column["name"] for column in table["columns"]] == [
        "model",
        "total",
        "change",
        "segments",
    ]
    assert [row["model"] for row in table["rows"]] == ["MODEL-A", "LOW", "MODEL-B"]
    assert "新增" in table["rows"][0]["change"]
    assert "周五" in table["rows"][0]["segments"]
    assert "+" in table["rows"][0]["segments"]


async def test_matrix_report_hides_subscription_action_when_reporting_is_disabled(
    tmp_path: Path,
) -> None:
    config = MagikCubeToolConfig(
        base_url="https://cube.example",
        tenant_mappings={"测试租户": "prod"},
    )
    reporter = MagikCubeReporter(
        _DeterministicReportClient(),
        config,
        tmp_path / "proxy.json",
        "Asia/Shanghai",
        reporting_actions_enabled=False,
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [{"tenant_query": "测试租户", "model_scope": "all", "models": []}],
        granularity="day",
        include_tpm=True,
    )

    card = result.metadata[OUTBOUND_META_AGENT_UI]["cards"][0]
    assert "actions" not in card


async def test_matrix_report_isolates_one_tenant_failure(tmp_path: Path) -> None:
    config = MagikCubeToolConfig(
        base_url="https://cube.example",
        tenant_mappings={"测试租户": "prod"},
    )
    reporter = MagikCubeReporter(
        _DeterministicReportClient(), config, tmp_path / "proxy.json", "Asia/Shanghai"
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [
            {"tenant_query": "测试租户", "model_scope": "summary", "models": []},
            {"tenant_query": "不存在客户", "model_scope": "summary", "models": []},
        ],
        granularity="day",
        include_tpm=True,
    )

    cards = result.metadata[OUTBOUND_META_AGENT_UI]["cards"]
    assert len(cards) == 2
    assert cards[0]["title"] == "生产客户 周报"
    assert cards[1]["title"] == "不存在客户 报表失败"
    assert cards[1]["overview"] == ["客户标识已失效，请重新选择客户"]
    assert "本客户未生成任何业务数值" not in str(result)
    assert "未将缺失数据按零处理" in cards[1]["quality"]
    assert "不存在客户 报表失败" in result
    assert "未将缺失数据按零处理" in result


async def test_matrix_report_resolves_selector_tenant_id_before_names_and_tags(
    tmp_path: Path,
) -> None:
    reporter = MagikCubeReporter(
        _FocusedUsageClient(),
        MagikCubeToolConfig(
            base_url="https://cube.example",
            tenant_mappings={"佛跳墙生产": "prod"},
        ),
        tmp_path / "proxy.json",
        "Asia/Shanghai",
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [{"tenant_query": "prod", "model_scope": "summary", "models": []}],
        granularity="day",
        include_tpm=True,
    )

    card = result.metadata[OUTBOUND_META_AGENT_UI]["cards"][0]
    assert card["title"] == "customer_prod 周报"
    assert "报表失败" not in card["title"]


async def test_matrix_report_rejects_ambiguous_tag_without_guessing_customer(
    tmp_path: Path,
) -> None:
    reporter = MagikCubeReporter(
        _FocusedUsageClient(),
        MagikCubeToolConfig(base_url="https://cube.example"),
        tmp_path / "proxy.json",
        "Asia/Shanghai",
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [{"tenant_query": "佛跳墙", "model_scope": "summary", "models": []}],
        granularity="day",
        include_tpm=False,
    )

    card = result.metadata[OUTBOUND_META_AGENT_UI]["cards"][0]
    assert card["overview"] == ["匹配到多个客户，请从列表中精确选择"]
    assert "本客户未生成任何业务数值" not in str(result)


async def test_matrix_report_labels_successful_empty_response_as_no_business_data(
    tmp_path: Path,
) -> None:
    class _EmptyClient:
        async def request(self, _method, path, *, params=None, json_body=None):
            if path == "tenants":
                return {
                    "list": [{"tenantId": "tenant-a", "tenantName": "客户A"}],
                    "total": 1,
                }
            if path == "analysis/active-tenant-daily-usage/query":
                return {"items": []}
            raise AssertionError((path, params, json_body))

    reporter = MagikCubeReporter(
        _EmptyClient(),
        MagikCubeToolConfig(base_url="https://cube.example"),
        tmp_path / "proxy.json",
        "Asia/Shanghai",
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [{"tenant_query": "tenant-a", "model_scope": "summary", "models": []}],
        granularity="day",
        include_tpm=False,
    )

    card = result.metadata[OUTBOUND_META_AGENT_UI]["cards"][0]
    assert card["overview"] == ["查询成功，当前周期暂无业务数据"]
    assert "查询已成功" in card["quality"]


async def test_interactive_forms_collect_scope_then_models(tmp_path: Path) -> None:
    config = MagikCubeToolConfig(
        base_url="https://cube.example", tenant_mappings={"测试租户": "prod"}
    )
    reporter = MagikCubeReporter(
        _DeterministicReportClient(), config, tmp_path / "proxy.json", "Asia/Shanghai"
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    scope = await reporter.prepare_scope_interaction(
        plan, tenant_query="测试租户", granularity="day", include_tpm=True
    )
    scope_ui = scope.metadata[OUTBOUND_META_AGENT_UI]
    assert scope_ui["phase"] == "scope"
    assert scope_ui["tenant_options"][0]["value"] == "prod"
    assert scope_ui["tenant_options"][0]["label"] == "测试租户（生产客户）"
    assert scope_ui["tenant_options"][0]["selected"] is True

    models = await reporter.prepare_model_interaction(
        plan,
        [{"tenant_query": "测试租户", "model_scope": "selected", "models": []}],
        granularity="day",
        include_tpm=True,
    )
    model_ui = models.metadata[OUTBOUND_META_AGENT_UI]
    assert model_ui["phase"] == "models"
    assert model_ui["tenant_models"][0]["models"] == ["LOW", "MODEL-A", "MODEL-B"]


def test_direct_route_supports_weekly_model_slug_and_bypasses_deep_analysis(
    tmp_path: Path,
) -> None:
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")
    params = tool.match_direct_request(
        "给我 tencent_token_hub 各个模型，一周的使用情况分析"
    )

    assert params is not None
    assert params["tenant_query"] == "tencent_token_hub"
    assert params["breakdown"] == "model"
    assert params["report_template"] == "matrix_card"
    assert params["report_selections"][0]["model_scope"] == "all"
    assert params["comparison"] == "previous_period"
    assert date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"]) == timedelta(days=6)
    assert tool.match_direct_request("深度分析上周和上上周各模型用量") is None


async def test_scope_selector_uses_only_cube_catalog_entries(tmp_path: Path) -> None:
    reporter = MagikCubeReporter(
        _FakeClient(),
        MagikCubeToolConfig(
            base_url="https://cube.example",
            tenant_mappings={"甲方别名": "t1", "佛跳墙": "tencent_token_hub"},
        ),
        tmp_path / "proxy.json",
        "Asia/Shanghai",
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    scope = await reporter.prepare_scope_interaction(
        plan, tenant_query="", granularity="day", include_tpm=True
    )
    options = scope.metadata[OUTBOUND_META_AGENT_UI]["tenant_options"]

    assert options == [{"value": "t1", "label": "甲方别名（甲客户）", "selected": False}]


def test_direct_route_uses_cards_only_to_fill_missing_slots(tmp_path: Path) -> None:
    tool = MagikCubeDailyReportTool(
        config=MagikCubeToolConfig(tenant_mappings={"A客户": "tenant-a"}),
        snapshot_path=tmp_path / "proxy.json",
    )

    bare = tool.match_direct_request("我要周报")
    assert bare is not None
    assert bare["report_template"] == "matrix_card"
    assert bare["interactive"] is True

    partial = tool.match_direct_request("A客户上周周报")
    assert partial is not None
    assert partial["tenant_query"] == "A客户"
    assert partial["interactive"] is True

    complete = tool.match_direct_request("A客户所有模型上周周报")
    assert complete is not None
    assert "interactive" not in complete
    assert complete["report_selections"] == [
        {"tenant_query": "A客户", "model_scope": "all", "models": []}
    ]

    full = tool.match_direct_request("A客户完整周报")
    assert full is not None
    assert full["report_template"] == "full"
    assert "interactive" not in full


def test_direct_route_normalizes_k3_alias_and_keeps_model_before_chinese_suffix(
    tmp_path: Path,
) -> None:
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")

    for text in (
        "tencent_token_hub k3 模型的日报",
        "tencent_token_hub Kimi-K3模型的日报",
    ):
        params = tool.match_direct_request(text)
        assert params is not None
        assert params["report_template"] == "matrix_card"
        assert "interactive" not in params
        assert params["model"] == "Kimi-K3"
        assert params["report_selections"] == [
            {
                "tenant_query": "tencent_token_hub",
                "model_scope": "selected",
                "models": ["Kimi-K3"],
            }
        ]


def test_direct_route_prefers_explicit_daily_date_and_understands_vllm(
    tmp_path: Path,
) -> None:
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")

    cases = [
        ("tencent_token_hub Kimi-K3 2026-08-29日的日报", "Kimi-K3"),
        ("tencent_token_hub vLLM 2026-08-29日的日报", "vLLM"),
    ]
    for text, expected_model in cases:
        params = tool.match_direct_request(text)

        assert params is not None
        assert params["model"] == expected_model
        assert params["start_date"] == "2026-08-29"
        assert params["end_date"] == "2026-08-29"
        assert params["comparison"] == "previous_period"
        assert params["report_template"] == "matrix_card"
        assert params["report_selections"] == [
            {
                "tenant_query": "tencent_token_hub",
                "model_scope": "selected",
                "models": [expected_model],
            }
        ]


def test_direct_route_sends_period_reports_and_single_day_models_to_range_engine(
    tmp_path: Path,
) -> None:
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")

    weekly = tool.match_direct_request("生成上周各模型用量周报")
    assert weekly is not None
    assert weekly["breakdown"] == "model"
    assert weekly["comparison"] == "previous_period"
    assert date.fromisoformat(weekly["end_date"]) - date.fromisoformat(weekly["start_date"]) == timedelta(days=6)

    single_day = tool.match_direct_request("2026-08-10 tencent_token_hub 各模型用量")
    assert single_day is not None
    assert single_day["start_date"] == "2026-08-10"
    assert single_day["end_date"] == "2026-08-10"
    assert "report_date" not in single_day


async def test_execute_rejects_comparison_dates_without_primary_range(tmp_path: Path) -> None:
    tool = MagikCubeDailyReportTool(
        config=MagikCubeToolConfig(
            base_url="https://cube.example", access_token="secret"
        ),
        snapshot_path=tmp_path / "proxy.json",
    )

    result = await tool.execute(
        compare_start_date="2026-08-01", compare_end_date="2026-08-07"
    )

    assert result == "Error: comparison dates require start_date and end_date"
