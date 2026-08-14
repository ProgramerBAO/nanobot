from __future__ import annotations

import json
from datetime import date
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
    _diff_proxy_snapshots,
)


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
    assert "tenants" not in client.paths
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
    assert tool.max_calls_per_turn == 3


def test_direct_route_does_not_capture_unrelated_questions(tmp_path: Path) -> None:
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")

    assert tool.match_direct_request("GLM-5.2 支持多长上下文？") is None
    assert tool.match_direct_request("昨天机器是否正常？") is None
