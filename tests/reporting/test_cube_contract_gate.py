from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from nanobot.agent.tools.magik_cube import MagikCubeToolConfig
from nanobot.reporting.cube_contract_gate import (
    CubeContractGate,
    compare_metric_summaries,
    profile_cube_contract_fixture,
)


def _fixture() -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures" / "reporting" / "cube_contract.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _config(**overrides: object) -> MagikCubeToolConfig:
    values: dict[str, object] = {
        "enable": True,
        "deployment_environment": "staging",
        "contract_validation_enabled": True,
        "base_url": "https://cube.staging.example.internal",
        "access_token": "fixture-token",
        "max_retries": 0,
    }
    values.update(overrides)
    return MagikCubeToolConfig(**values)


def test_sanitized_fixture_matches_every_cube_staging_probe() -> None:
    probes = profile_cube_contract_fixture(_fixture())

    assert len(probes) == 8
    assert all(probe.status == "verified" for probe in probes)


@pytest.mark.asyncio
async def test_cube_contract_gate_uses_only_fixed_routes_and_returns_safe_shape_summary() -> None:
    fixture = _fixture()
    health = fixture["health"]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        payloads = {
            "analysis/active-tenant-daily-usage/query": fixture["usage"],
            "analysis/endpoint-max-tpm/daily/query": fixture["tpm"],
            "analysis/performance-endpoints/query": health["performance_endpoints"],
            "analysis/token-utilization/query": health["token_utilization"],
            "analysis/token-utilization/daily/query": health["daily_token_utilization"],
            "analysis/model-performance/query": health["model_performance"],
            "analysis/endpoint-tpm-trend/query": health["endpoint_tpm_trend"],
            "gateway/usages": fixture["gateway_usages"],
        }
        route = next((item for item in payloads if path.endswith(item)), None)
        assert route is not None, path
        return httpx.Response(200, json={"code": 0, "data": payloads[route]})

    gate = CubeContractGate(
        _config(),
        transport=httpx.MockTransport(handler),
        now=datetime.fromisoformat("2026-08-28T10:15:30+08:00"),
    )
    result = await gate.run(tenant_id="fixture-tenant")
    safe = result.to_safe_dict()
    encoded = json.dumps(safe, ensure_ascii=False)

    assert result.quality == "complete"
    assert result.request_count == 8
    assert all(probe.status == "verified" for probe in result.probes)
    assert "fixture-token" not in encoded
    assert "fixture-endpoint" not in encoded
    assert "fixture-tenant" not in encoded
    assert "Authorization" not in encoded
    assert all(request.method in {"GET", "POST"} for request in requests)
    assert all(request.url.path.startswith("/api/v1/") for request in requests)


@pytest.mark.asyncio
async def test_cube_contract_gate_refuses_unlabeled_or_disabled_staging_configs() -> None:
    gate = CubeContractGate(_config(deployment_environment="production"))

    with pytest.raises(ValueError, match="deployment_environment=staging"):
        await gate.run(tenant_id="fixture-tenant")

    disabled = CubeContractGate(_config(contract_validation_enabled=False))
    with pytest.raises(ValueError, match="disabled"):
        await disabled.run(tenant_id="fixture-tenant")


@pytest.mark.asyncio
async def test_cube_contract_gate_keeps_remote_error_messages_out_of_safe_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "sensitive remote response body"})

    result = await CubeContractGate(
        _config(),
        transport=httpx.MockTransport(handler),
    ).run(tenant_id="fixture-tenant")
    encoded = json.dumps(result.to_safe_dict(), ensure_ascii=False)

    assert result.quality == "missing"
    assert any(probe.status == "error" and probe.error_type == "MagikCubeApiError" for probe in result.probes)
    assert "sensitive remote response body" not in encoded
    assert "fixture-tenant" not in encoded


def test_semantic_shadow_summary_reports_only_metric_identity_not_values() -> None:
    summary = compare_metric_summaries(
        {"ai.ttft": (120.0, 100.0), "ai.rpm": (80.0, 70.0)},
        {"ai.ttft": (240.0, 100.0), "ai.rpm": (80.0, 70.0)},
    )
    encoded = json.dumps(summary, ensure_ascii=False)

    assert summary["status"] == "drift"
    assert summary["differing_metrics"] == ["ai.ttft"]
    assert "120" not in encoded
    assert "240" not in encoded
