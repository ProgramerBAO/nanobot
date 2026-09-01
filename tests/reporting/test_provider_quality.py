from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from nanobot.agent.tools.magik_cube import MagikCubeToolConfig
from nanobot.agent.tools.report_center import ReportCenterTool, ReportCenterToolConfig
from nanobot.reporting import (
    CubeProviderQualityConnector,
    ProviderQualityTemplate,
    ReportDataset,
    ReportIntent,
)
from nanobot.reporting.capabilities import home_document
from nanobot.reporting.store import ReportStateStore


def _config() -> MagikCubeToolConfig:
    return MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example.internal",
        access_token="fixture-token",
        max_retries=0,
        max_pages=2,
    )


def _transport() -> httpx.MockTransport:
    providers = [
        {
            "id": "provider-ppio-k3",
            "name": "PPIO K3",
            "provider": "ppio",
            "modelName": "Kimi-K3",
            "modelEndpoint": "endpoint-k3",
            "modelInstance": "instance-a",
            "cluster": "cluster-a",
            "enabled": True,
            "lastProbeAt": "2026-08-31T10:00:00+08:00",
            "lastProbeStatus": "success",
            "lastProbeMsg": "probe ok",
            "tpmQuota": "100000",
            "inputPrice": "0.000001",
            "outputPrice": "0.000002",
            "baseUrl": "https://must-not-appear.example",
            "apiKey": "must-not-appear",
        },
        {
            "id": "provider-other-k3",
            "name": "Other K3",
            "provider": "other",
            "modelName": "Kimi-K3",
            "modelEndpoint": "endpoint-k3-b",
            "enabled": True,
            "lastProbeStatus": "failed",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/providers/list"):
            return httpx.Response(200, json={"code": 0, "data": {"list": providers, "total": 2}})
        if path.endswith("/providers/detail"):
            body = json.loads(request.content)
            if body["id"] == "provider-ppio-k3":
                detail = {
                    "realtime": {
                        "totalRequests": "42",
                        "totalTokens": "42000",
                        "actualTpm": "12000",
                        "avgLatencyMs": "800",
                        "avgTtftMs": "700",
                    },
                    "tests": [{"type": "offline", "dataset": "safe-set-v1", "score": "0.98"}],
                }
            else:
                detail = {"realtime": {"totalRequests": "4"}, "tests": []}
            return httpx.Response(200, json={"code": 0, "data": detail})
        if path.endswith("/provider-performance/query"):
            body = json.loads(request.content)
            value = {
                "PROVIDER_METRIC_ERROR_RATE": "0.01",
                "PROVIDER_METRIC_LATENCY": "800",
                "PROVIDER_METRIC_THROUGHPUT": "1200",
                "PROVIDER_METRIC_TPM": "12000",
            }[body["metric"]]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "series": [
                            {"provider": "ppio", "points": [{"timestamp": "2026-08-31T10:00:00+08:00", "value": value}]},
                            {"provider": "other", "points": [{"timestamp": "2026-08-31T10:00:00+08:00", "value": value}]},
                        ]
                    },
                },
            )
        if path.endswith("/provider-daily-traffic/query"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "provider": "ppio",
                                "model": "Kimi-K3",
                                "points": [{"timestamp": "2026-08-31T10:00:00+08:00", "totalTokens": "42000", "tpm": "12000", "trafficRatio": "0.8"}],
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected fixture route: {path}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_provider_quality_normalizes_catalog_and_keeps_sensitive_fields_out() -> None:
    connector = CubeProviderQualityConnector(_config(), transport=_transport(), include_details=True)
    template = ProviderQualityTemplate()
    intent = ReportIntent(
        connector_id="cube_provider_quality",
        template_id="provider_quality",
        period="day",
        start_date=date(2026, 8, 31),
        end_date=date(2026, 8, 31),
        provider="ppio",
        models=("Kimi-K3",),
        filters={"provider_quality": True, "provider": "ppio", "model": "Kimi-K3"},
    )

    dataset = await connector.query(template.plan(intent)[0])
    document = template.analyze((dataset,))
    serialized = json.dumps({"dataset": dataset.metadata, "document": document.to_agent_ui()}, ensure_ascii=False)

    assert dataset.quality == "complete"
    assert {item["provider"] for item in dataset.metadata["provider_catalog"]} == {"ppio"}
    assert "must-not-appear" not in serialized
    assert "https://must-not-appear.example" not in serialized
    assert document.quality == "complete"
    assert "供应商质量排行" in document.blocks[1].data["title"]
    assert any(row["model"] == "Kimi-K3" for row in document.blocks[1].data["rows"])
    assert "Cube Admin" in document.fallback_text


def test_provider_quality_requires_explicit_provider_quality_filters() -> None:
    template = ProviderQualityTemplate()
    intent = ReportIntent(
        connector_id="cube_provider_quality",
        template_id="provider_quality",
        period="day",
        start_date=date(2026, 8, 31),
        end_date=date(2026, 8, 31),
        tenant="should-not-be-accepted",
    )
    with pytest.raises(ValueError, match="tenant"):
        template.plan(intent)


def test_provider_quality_fixed_phrases_and_help_entry(tmp_path: Path) -> None:
    tool = ReportCenterTool(
        ReportCenterToolConfig(
            cube_provider_quality_connector=True,
            cube_provider_quality_template=True,
            cube_provider_quality_report=True,
        ),
        cron_service=None,
        magik_tool=None,
        cube_config=_config(),
    )

    assert tool.match_direct_request("供应商质量报告") == {
        "action": "provider_quality_report",
        "period": "recent15m",
        "provider": "",
    }
    assert tool.match_direct_request("查看供应商 ppio 的质量") == {
        "action": "provider_quality_report",
        "period": "recent15m",
        "provider": "ppio",
    }
    assert tool.match_direct_request("昨天各供应商性能") == {
        "action": "provider_quality_report",
        "period": "day",
        "provider": "",
    }
    assert tool.match_direct_request("Kimi-K3 各供应商性能对比") == {
        "action": "provider_quality_report",
        "period": "recent15m",
        "model": "Kimi-K3",
    }

    document = home_document(
        tool._registry,
        ReportStateStore(tmp_path / "state.db"),
        channel="feishu",
        user_id="ou_fixture",
        provider_quality_enabled=True,
    )
    assert "Cube 供应商质量" in document.fallback_text


def test_provider_quality_all_scope_collapses_successful_no_usage_only() -> None:
    template = ProviderQualityTemplate()
    dataset = ReportDataset(
        rows=(
            {
                "provider": "active-provider",
                "metric": "ai.provider.requests",
                "value": 42,
                "period": "current",
            },
            {
                "provider": "active-provider",
                "metric": "ai.provider.latency",
                "value": 800,
                "percentile": "p99",
                "period": "current",
            },
        ),
        quality="complete",
        metadata={
            "provider_catalog": (
                {"provider": "active-provider", "model": "Kimi-K3", "endpoint": "ep-a"},
                {"provider": "empty-provider", "model": "Kimi-K3", "endpoint": "ep-b"},
            ),
            "filters": {"period": "day"},
            "query_success_count": 2,
            "query_failure_count": 0,
            "query_windows": (),
        },
    )

    document = template.analyze((dataset,))

    assert document.blocks[1].data["rows"][0]["provider"] == "active-provider"
    collapsed = next(block for block in document.blocks if block.data.get("collapsed"))
    assert [row["provider"] for row in collapsed.data["rows"]] == ["empty-provider"]
    assert "provider_quality_show_empty" in str(document.to_agent_ui())


def test_provider_quality_explicit_no_usage_provider_stays_visible() -> None:
    template = ProviderQualityTemplate()
    dataset = ReportDataset(
        rows=(),
        quality="complete",
        metadata={
            "provider_catalog": ({"provider": "empty-provider", "model": "Kimi-K3"},),
            "filters": {"provider": "empty-provider", "period": "day"},
            "query_success_count": 2,
            "query_failure_count": 0,
            "query_windows": (),
        },
    )

    document = template.analyze((dataset,))

    assert document.blocks[1].data["rows"][0]["provider"] == "empty-provider"
    assert document.blocks[1].data["rows"][0]["status"] == "暂无用量"
