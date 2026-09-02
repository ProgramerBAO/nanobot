from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import nanobot.agent.tools.report_center as report_center_module
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, current_request_context, request_context
from nanobot.agent.tools.magik_cube import MagikCubeTokenApiConfig, MagikCubeToolConfig
from nanobot.agent.tools.report_center import ReportCenterTool, ReportCenterToolConfig
from nanobot.bus.events import (
    INBOUND_META_DIRECT_TOOL,
    OUTBOUND_META_AGENT_UI,
    OUTBOUND_META_REPORT_DELIVERY,
    InboundMessage,
)
from nanobot.bus.queue import MessageBus
from nanobot.cron.session_turns import CRON_TRIGGER_META
from nanobot.reporting import CubeConnector, ReportDataset, ReportStateStore
from nanobot.reporting.store import ReportSubscription


class _FakeCron:
    def __init__(self) -> None:
        self.jobs = []
        self.removed = []

    def add_job(self, **kwargs):
        job = SimpleNamespace(id=f"job-{len(self.jobs) + 1}", **kwargs)
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str):
        self.removed.append(job_id)
        return "removed"


def _tool(monkeypatch, tmp_path):
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    cron = _FakeCron()
    magik = AsyncMock()
    magik.execute.return_value = ToolResult("ok")
    tool = ReportCenterTool(ReportCenterToolConfig(), cron, magik)
    return tool, store, cron


def test_usage_depth_words_route_to_brief_detail_and_full(monkeypatch, tmp_path) -> None:
    tool, _store, _cron = _tool(monkeypatch, tmp_path)

    assert tool.match_direct_request("日报") == {
        "action": "cube_report",
        "period": "day",
        "interactive": True,
        "report_template": "brief",
    }
    assert tool.match_direct_request("详细日报")["report_template"] == "matrix_card"
    assert tool.match_direct_request("完整日报")["report_template"] == "full"

    disabled = ReportCenterTool(
        ReportCenterToolConfig(cube_usage_brief_template=False), _FakeCron(), MagicMock()
    )
    assert disabled.match_direct_request("日报")["report_template"] == "matrix_card"


def test_new_capacity_and_multi_scope_phrases_use_report_center(monkeypatch, tmp_path) -> None:
    tool, _store, _cron = _tool(monkeypatch, tmp_path)

    assert tool.match_direct_request("多客户多模型日报简报") == {
        "action": "multi_scope_brief",
        "interactive": True,
        "period": "day",
    }
    assert tool.match_direct_request("Kimi-K3 单机 TPM 峰值") == {
        "action": "machine_tpm_report",
        "period": "day",
        "model": "Kimi-K3",
    }


def test_explicit_daily_scope_defaults_to_brief_and_preserves_date(monkeypatch, tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = MagicMock()
    magik.match_direct_request.return_value = {
        "tenant_query": "tencent_token_hub",
        "model": "Kimi-K3",
        "start_date": "2026-08-31",
        "end_date": "2026-08-31",
        "report_template": "matrix_card",
        "breakdown": "model",
    }
    tool = ReportCenterTool(ReportCenterToolConfig(), _FakeCron(), magik)

    params = tool.match_direct_request("tencent_token_hub Kimi-K3 2026-08-31日的日报")

    assert params is not None
    assert params["report_template"] == "brief"
    assert params["tenant_query"] == "tencent_token_hub"
    assert params["model"] == "Kimi-K3"
    assert params["start_date"] == "2026-08-31"
    assert params["end_date"] == "2026-08-31"


def test_further_analysis_command_preserves_scope_without_legacy_parser(
    monkeypatch, tmp_path
) -> None:
    tool, _store, _cron = _tool(monkeypatch, tmp_path)

    params = tool.match_direct_request(
        "进一步分析（日报）：客户 tencent_token_hub，模型 Kimi-K3，"
        "日期 2026-08-31 至 2026-08-31"
    )

    assert params == {
        "action": "cube_report",
        "period": "day",
        "report_template": "matrix_card",
        "tenant_query": "tencent_token_hub",
        "model": "Kimi-K3",
        "models": ["Kimi-K3"],
        "all_tenants": False,
        "breakdown": "model",
        "start_date": "2026-08-31",
        "end_date": "2026-08-31",
        "interactive": False,
        "report_selections": [
            {
                "tenant_query": "tencent_token_hub",
                "model_scope": "selected",
                "models": ["Kimi-K3"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_report_home_is_direct_and_channel_neutral(monkeypatch, tmp_path) -> None:
    tool, _store, _cron = _tool(monkeypatch, tmp_path)
    assert tool.match_direct_request("报表中心") == {"action": "home"}
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(action="home")
    assert result.metadata[OUTBOUND_META_AGENT_UI]["kind"] == "report_document"
    assert "报表中心" in str(result)


@pytest.mark.asyncio
async def test_examples_expose_selected_model_all_customer_report(monkeypatch, tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(cube_model_all_tenant_report=True),
        _FakeCron(),
        AsyncMock(),
    )
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(action="examples")

    assert "Kimi-K3模型的日报（全部客户）" in str(result)


@pytest.mark.asyncio
async def test_help_direct_route_binds_request_context(monkeypatch, tmp_path) -> None:
    tool, _store, _cron = _tool(monkeypatch, tmp_path)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
    )
    loop.tools.register(tool)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou_a",
            chat_id="chat-a",
            content="帮助",
        )
    )

    assert response is not None
    assert "报表中心" in response.content
    assert response.metadata[OUTBOUND_META_AGENT_UI]["kind"] == "report_document"
    assert current_request_context() is None
    provider.chat_with_retry.assert_not_called()
    await loop.close_mcp()


@pytest.mark.asyncio
async def test_subscription_is_idempotent_and_uses_direct_cron(monkeypatch, tmp_path) -> None:
    tool, store, cron = _tool(monkeypatch, tmp_path)
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou_a",
        session_key="feishu:chat-a",
        metadata={"chat_type": "p2p"},
    )
    params = {
        "tenant_query": "tenant-a",
        "breakdown": "model",
        "report_template": "matrix_card",
        "granularity": "day",
    }
    with request_context(context):
        first = await tool.execute(
            action="subscribe",
            period="week",
            weekday=1,
            send_time="10:00",
            report_params=params,
        )
        second = await tool.execute(
            action="subscribe",
            period="week",
            weekday=1,
            send_time="10:00",
            report_params=params,
        )
    assert "订阅已创建" in str(first)
    assert "每周一 10:00" in str(first)
    assert "0 10 * * 1" not in str(first)
    assert first.metadata[OUTBOUND_META_AGENT_UI]["kind"] == "report_document"
    assert "已经存在" in str(second)
    assert len(store.subscriptions("feishu", "ou_a")) == 1
    assert len(cron.jobs) == 2
    assert cron.removed == ["job-2"]
    direct = cron.jobs[0].origin_metadata[INBOUND_META_DIRECT_TOOL]
    assert direct["name"] == "report_center"
    assert direct["params"]["action"] == "run_subscription"
    assert cron.jobs[0].schedule.expr == "0 10 * * 1"


@pytest.mark.asyncio
async def test_subscription_setup_returns_a_form_without_creating_job(
    monkeypatch, tmp_path
) -> None:
    tool, _store, cron = _tool(monkeypatch, tmp_path)
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou_a",
        session_key="feishu:chat-a",
    )
    with request_context(context):
        result = await tool.execute(
            action="subscription_setup",
            period="day",
            report_params={"tenant_query": "tenant-a", "breakdown": "model"},
        )

    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    assert ui["kind"] == "report_subscription_form"
    assert ui["default_period"] == "day"
    assert ui["default_time"] == "10:00"
    assert ui["report_params"]["report_template"] == "brief"
    assert cron.jobs == []


@pytest.mark.asyncio
async def test_subscription_form_round_trip_accepts_server_normalized_params(
    monkeypatch, tmp_path
) -> None:
    """Protect the Feishu form round trip that previously rejected save_snapshot."""

    tool, store, cron = _tool(monkeypatch, tmp_path)
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou_a",
        session_key="feishu:chat-a",
    )
    with request_context(context):
        setup = await tool.execute(
            action="subscription_setup",
            period="day",
            report_params={
                "tenant_query": "tenant-a",
                "models": ["Kimi-K3"],
                "model_scope": "selected",
                "report_template": "brief",
            },
        )
        normalized = setup.metadata[OUTBOUND_META_AGENT_UI]["report_params"]
        assert normalized["save_snapshot"] is False
        created = await tool.execute(
            action="subscribe",
            period="day",
            send_time="10:00",
            report_params=normalized,
        )

    assert not created.is_error
    assert "订阅已创建" in str(created)
    assert len(store.subscriptions("feishu", "ou_a")) == 1
    assert len(cron.jobs) == 1


def test_subscription_params_still_reject_unknown_external_controls(
    monkeypatch, tmp_path
) -> None:
    tool, _store, _cron = _tool(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="unsupported report subscription parameters"):
        tool._safe_report_params({"save_snapshot": True, "url": "https://invalid.example"})


@pytest.mark.asyncio
async def test_subscription_delivery_is_idempotent_per_scheduled_run(
    monkeypatch, tmp_path
) -> None:
    tool, _store, cron = _tool(monkeypatch, tmp_path)
    owner_context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou_a",
        session_key="feishu:chat-a",
    )
    with request_context(owner_context):
        await tool.execute(
            action="subscribe",
            period="week",
            report_params={"tenant_query": "tenant-a", "breakdown": "model"},
        )
    subscription_id = cron.jobs[0].origin_metadata[INBOUND_META_DIRECT_TOOL]["params"][
        "subscription_id"
    ]
    cron_context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="cron",
        session_key="feishu:chat-a",
        metadata={
            CRON_TRIGGER_META: {
                "run_id": "run-a",
                "scheduled_at_ms": 1_787_680_800_000,
            }
        },
    )

    with request_context(cron_context):
        first = await tool.execute(action="run_subscription", subscription_id=subscription_id)
        second = await tool.execute(action="run_subscription", subscription_id=subscription_id)

    assert "跳过重复发送" not in str(first)
    assert "跳过重复发送" in str(second)
    assert tool._magik_tool.execute.await_count == 1


@pytest.mark.asyncio
async def test_multi_scope_subscription_falls_back_to_legacy_tool(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    cron = _FakeCron()
    magik = AsyncMock()
    magik.execute.return_value = ToolResult("legacy report")
    cube_config = MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example.internal",
        access_token="fixture-token",
    )
    tool = ReportCenterTool(ReportCenterToolConfig(), cron, magik, cube_config)
    owner_context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou_a",
        session_key="feishu:chat-a",
    )
    with request_context(owner_context):
        await tool.execute(
            action="subscribe",
            period="week",
            report_params={
                "tenant_query": "tenant-a",
                "report_selections": [
                    {"tenant_query": "tenant-a"},
                    {"tenant_query": "tenant-b"},
                ],
            },
        )
    subscription_id = cron.jobs[0].origin_metadata[INBOUND_META_DIRECT_TOOL]["params"][
        "subscription_id"
    ]
    cron_context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="cron",
        session_key="feishu:chat-a",
        metadata={
            CRON_TRIGGER_META: {
                "run_id": "run-a",
                "scheduled_at_ms": 1_787_680_800_000,
            }
        },
    )

    with request_context(cron_context):
        result = await tool.execute(action="run_subscription", subscription_id=subscription_id)

    assert str(result) == "legacy report"
    magik.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_cube_subscription_retries_one_transient_failure_with_same_run_id(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    config = ReportCenterToolConfig(
        cube_transient_run_retry=True,
        cube_transient_retry_delay_seconds=1,
    )
    tool = ReportCenterTool(
        config,
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    calls = 0

    async def fake_query(_connector, _query):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("fixture connection failure")
        return ReportDataset(
            rows=(
                {
                    "metric": "ai.usage.tokens",
                    "value": 10,
                    "date": "2026-08-24",
                    "period": "current",
                    "tenant": "客户A",
                    "model": "Kimi-K3",
                },
            ),
            quality="complete",
            source="magik_cube",
        )

    monkeypatch.setattr(CubeConnector, "query", fake_query)
    sleep = AsyncMock()
    monkeypatch.setattr(report_center_module.asyncio, "sleep", sleep)
    subscription = ReportSubscription(
        subscription_id="sub-a",
        channel="feishu",
        chat_id="chat-a",
        user_id="ou-a",
        connector_id="magik_cube",
        template_id="usage_weekly_matrix",
        template_version="1.0",
        schedule="0 10 * * 1",
        timezone="Asia/Shanghai",
        report_params={
            "tenant_query": "tenant-a",
            "report_selections": [
                {"tenant_query": "tenant-a", "model_scope": "summary", "models": []}
            ],
        },
        cron_job_id="job-a",
        enabled=True,
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )

    result = await tool._run_cube_subscription(
        subscription,
        run_id="run-a",
        idempotency_key="delivery-a",
    )

    assert calls == 2
    sleep.assert_awaited_once_with(1.0)
    assert result.metadata[OUTBOUND_META_REPORT_DELIVERY] == {
        "idempotency_key": "delivery-a",
        "run_id": "run-a",
        "report_attempts": 2,
    }
    runs = store.recent_runs("feishu", "ou-a")
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-a"
    assert runs[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_cube_subscription_does_not_retry_non_transient_missing_data(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(
            cube_transient_run_retry=True,
            cube_transient_retry_delay_seconds=1,
        ),
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )

    async def fake_query(_connector, _query):
        return ReportDataset(
            rows=(),
            quality="missing",
            warnings=("no_business_data",),
            source="magik_cube",
        )

    monkeypatch.setattr(CubeConnector, "query", fake_query)
    sleep = AsyncMock()
    monkeypatch.setattr(report_center_module.asyncio, "sleep", sleep)
    subscription = ReportSubscription(
        subscription_id="sub-a",
        channel="feishu",
        chat_id="chat-a",
        user_id="ou-a",
        connector_id="magik_cube",
        template_id="usage_weekly_matrix",
        template_version="1.0",
        schedule="0 10 * * 1",
        timezone="Asia/Shanghai",
        report_params={"tenant_query": "tenant-a"},
        cron_job_id="job-a",
        enabled=True,
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
    )

    result = await tool._run_cube_subscription(
        subscription,
        run_id="run-a",
        idempotency_key="delivery-a",
    )

    sleep.assert_not_awaited()
    assert result.metadata[OUTBOUND_META_REPORT_DELIVERY]["report_attempts"] == 1
    assert "查询成功，当前周期暂无业务数据" in str(result)


@pytest.mark.asyncio
async def test_fixed_cube_report_uses_runner_and_returns_report_document(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    cron = _FakeCron()
    magik = AsyncMock()

    async def fake_query(_connector, _query):
        return ReportDataset(
            rows=(
                {"period": "current", "date": "2026-08-27", "metric": "ai.usage.tokens", "value": 100},
                {"period": "current", "date": "2026-08-27", "metric": "ai.requests", "value": 4},
                {"period": "current", "date": "2026-08-27", "metric": "ai.tpm", "value": 20},
                {"period": "comparison", "date": "2026-08-26", "metric": "ai.usage.tokens", "value": 80},
                {"period": "comparison", "date": "2026-08-26", "metric": "ai.requests", "value": 5},
                {"period": "comparison", "date": "2026-08-26", "metric": "ai.tpm", "value": 16},
            ),
            source="magik_cube",
        )

    monkeypatch.setattr(CubeConnector, "query", fake_query)
    tool = ReportCenterTool(
        ReportCenterToolConfig(
            cube_usage_semantics_v2=True,
            cube_model_all_tenant_report=True,
        ),
        cron,
        magik,
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    assert tool.fixed_cube_reports_enabled is True

    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(action="cube_report", period="day")

    assert "Token 消耗" in str(result)
    assert result.metadata[OUTBOUND_META_AGENT_UI]["kind"] == "report_document"
    assert result.metadata[OUTBOUND_META_AGENT_UI]["quality"] == "complete"
    assert result.metadata[OUTBOUND_META_AGENT_UI]["context"]["calculation_version"] == "2"
    assert "0 LLM" not in str(result.metadata[OUTBOUND_META_AGENT_UI])
    actions = [
        action
        for block in result.metadata[OUTBOUND_META_AGENT_UI]["blocks"]
        if block["kind"] == "actions"
        for action in block["data"]["actions"]
    ]
    assert not any(action["action_id"] == "usage_subscription_setup:day" for action in actions)
    magik.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_daily_report_queries_all_cube_customers_and_labels_scope(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    captured: list[object] = []
    magik = AsyncMock()

    async def fake_query(_connector, query):
        captured.append(query)
        return ReportDataset(
            rows=(
                {"period": "current", "date": "2026-08-27", "tenant": "A", "model": "Kimi-K3", "metric": "ai.usage.tokens", "value": 100},
                {"period": "current", "date": "2026-08-27", "tenant": "B", "model": "Kimi-K3", "metric": "ai.usage.tokens", "value": 80},
                {"period": "comparison", "date": "2026-08-26", "tenant": "A", "model": "Kimi-K3", "metric": "ai.usage.tokens", "value": 50},
            ),
            source="magik_cube",
            metadata={"scope": {"all_tenants": True, "models": ["Kimi-K3"]}},
        )

    monkeypatch.setattr(CubeConnector, "query", fake_query)
    tool = ReportCenterTool(
        ReportCenterToolConfig(
            cube_usage_semantics_v2=True,
            cube_model_all_tenant_report=True,
        ),
        _FakeCron(),
        magik,
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
            model_aliases={"k3": "Kimi-K3"},
        ),
    )
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(
            action="cube_report", period="day", model="k3", all_tenants=True
        )

    assert len(captured) == 1
    assert captured[0].filters["all_tenants"] is True
    assert captured[0].filters["models"] == ["Kimi-K3"]
    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    assert ui["title"] == "全部客户 Kimi-K3模型日报简报"
    assert all(block["kind"] != "table" for block in ui["blocks"])
    assert magik.execute.await_count == 0


def test_brief_subscription_command_preserves_scope() -> None:
    tool = ReportCenterTool(
        ReportCenterToolConfig(),
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(enable=True, base_url="https://cube.example.internal"),
    )
    params = tool.match_direct_request(
        "订阅日报简报：客户 佛跳墙，模型 Kimi-K3"
    )
    assert params == {
        "action": "subscription_setup",
        "period": "day",
        "report_family": "usage",
        "report_params": {
            "report_template": "brief",
            "tenant_query": "佛跳墙",
            "models": ["Kimi-K3"],
            "all_tenants": False,
            "model_scope": "selected",
            "breakdown": "model",
            "report_selections": [
                {"tenant_query": "佛跳墙", "model_scope": "selected", "models": ["Kimi-K3"]}
            ],
        },
    }

    catalog_backed = tool.match_direct_request(
        "订阅日报简报：客户 佛跳墙（ID tenant-baowjhsicyf65），模型 Kimi-K3"
    )
    assert catalog_backed is not None
    assert catalog_backed["report_params"]["tenant_query"] == "tenant-baowjhsicyf65"
    assert catalog_backed["report_params"]["report_selections"][0]["tenant_query"] == (
        "tenant-baowjhsicyf65"
    )

    summary = tool.match_direct_request("订阅日报简报：客户 佛跳墙，模型 汇总")
    assert summary is not None
    assert summary["report_params"]["model_scope"] == "summary"
    assert summary["report_params"]["models"] == []

    multiple = tool.match_direct_request(
        "订阅周报简报：客户 佛跳墙，模型 Kimi-K3、vLLM"
    )
    assert multiple is not None
    assert multiple["report_params"]["model_scope"] == "selected"
    assert multiple["report_params"]["models"] == ["Kimi-K3", "vLLM"]

    assert tool.match_direct_request("停用订阅：aaaaaaaaaaaaaaaa") == {
        "action": "subscription_disable",
        "subscription_id": "aaaaaaaaaaaaaaaa",
    }


@pytest.mark.asyncio
async def test_model_all_customer_report_is_stopped_until_feature_flag_is_enabled(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    query = AsyncMock()
    monkeypatch.setattr(CubeConnector, "query", query)
    tool = ReportCenterTool(
        ReportCenterToolConfig(),
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(
            action="cube_report", period="day", model="Kimi-K3", all_tenants=True
        )

    assert result.is_error
    assert "尚未开启" in str(result)
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_customer_model_report_requires_tenant_wildcard_grant_when_rbac_enabled(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()

    async def fake_query(_connector, _query):
        return ReportDataset(rows=(), quality="missing", source="magik_cube")

    monkeypatch.setattr(CubeConnector, "query", fake_query)
    tool = ReportCenterTool(
        ReportCenterToolConfig(rbac_enforced=True, cube_model_all_tenant_report=True),
        _FakeCron(),
        magik,
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    for resource_type, resource_id in (
        ("connector", "magik_cube"),
        ("template", "usage_daily_brief"),
        ("model", "Kimi-K3"),
    ):
        store.grant("feishu", "ou_a", resource_type, resource_id)
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou_a",
        session_key="feishu:chat-a",
    )
    with request_context(context):
        denied = await tool.execute(
            action="cube_report", period="day", model="Kimi-K3", all_tenants=True
        )
        store.grant("feishu", "ou_a", "tenant", "*")
        allowed = await tool.execute(
            action="cube_report", period="day", model="Kimi-K3", all_tenants=True
        )

    assert denied.is_error
    assert "没有执行该 Cube 报表的权限" in str(denied)
    assert not allowed.is_error
    actions = [
        action
        for block in allowed.metadata[OUTBOUND_META_AGENT_UI]["blocks"]
        if block["kind"] == "actions"
        for action in block["data"]["actions"]
    ]
    assert all(
        not action["action_id"].startswith("usage_subscription_setup:")
        for action in actions
    )


@pytest.mark.asyncio
async def test_fixed_period_interactive_route_restores_tenant_and_model_selector(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    cron = _FakeCron()
    magik = AsyncMock()
    magik.execute.return_value = ToolResult(
        "请选择报表范围。",
        metadata={OUTBOUND_META_AGENT_UI: {"kind": "magik_report_form", "phase": "scope"}},
    )
    cube_config = MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example.internal",
        access_token="fixture-token",
    )
    tool = ReportCenterTool(ReportCenterToolConfig(), cron, magik, cube_config)

    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(action="cube_report", period="day", interactive=True)

    assert str(result) == "请选择报表范围。"
    magik.execute.assert_awaited_once()
    params = magik.execute.call_args.kwargs
    assert params["report_template"] == "matrix_card"
    assert params["interactive"] is True
    assert params["start_date"]
    assert params["end_date"]
    assert params["comparison"] == "previous_period"


@pytest.mark.asyncio
async def test_unified_selector_returns_to_report_center_when_flag_enabled(
    monkeypatch, tmp_path
) -> None:
    tool, _store, _cron = _tool(monkeypatch, tmp_path)
    tool._config.cube_scope_selector_v2 = True
    tool._magik_tool.execute.return_value = ToolResult(
        "请选择报表范围。",
        metadata={
            OUTBOUND_META_AGENT_UI: {
                "kind": "magik_report_form",
                "phase": "scope",
                "base_params": {},
                "scope_options": [],
            }
        },
    )
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(action="cube_report", period="week", interactive=True)

    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    assert ui["base_params"]["action"] == "cube_report"
    assert ui["base_params"]["_report_center_selector"] is True
    assert ui["max_tenants"] == 1


@pytest.mark.asyncio
async def test_multi_scope_model_selector_stays_on_report_center(
    monkeypatch, tmp_path
) -> None:
    """The second selector must not send ReportCenter action fields to the legacy Tool."""

    tool, _store, _cron = _tool(monkeypatch, tmp_path)
    tool._config.cube_multi_scope_brief = True
    tool._magik_tool.execute.return_value = ToolResult(
        "请选择模型。",
        metadata={
            OUTBOUND_META_AGENT_UI: {
                "kind": "magik_report_form",
                "phase": "models",
                "base_params": {},
                "tenant_models": [],
            }
        },
    )
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(
            action="multi_scope_brief",
            period="day",
            interactive=True,
            start_date="2026-09-01",
            end_date="2026-09-01",
            report_selections=[
                {
                    "tenant_query": "tenant-a",
                    "model_scope": "selected",
                    "models": [],
                }
            ],
        )

    call = tool._magik_tool.execute.call_args.kwargs
    assert call["_trusted_selection_limit"] == 20
    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    assert ui["base_params"] == {
        "action": "multi_scope_brief",
        "period": "day",
        "interactive": False,
        "start_date": "2026-09-01",
        "end_date": "2026-09-01",
        "_report_center_selector": True,
    }
    assert ui["max_tenants"] == 20


@pytest.mark.asyncio
async def test_multi_scope_all_models_expands_live_catalog_before_report_runner(
    monkeypatch, tmp_path
) -> None:
    """All-model scope must become explicit pairs before querying model-level usage."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    tool = ReportCenterTool(
        ReportCenterToolConfig(cube_multi_scope_brief=True),
        _FakeCron(),
        magik,
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    catalog_models = [f"MODEL-{index:02d}" for index in range(1, 29)]
    magik.execute.return_value = ToolResult(
        "请选择模型。已加载 1 个客户、28 个模型。",
        metadata={
            OUTBOUND_META_AGENT_UI: {
                "kind": "magik_report_form",
                "phase": "models",
                "tenant_models": [
                    {
                        "tenant_query": "tenant-a",
                        "tenant_label": "佛跳墙",
                        "models": catalog_models,
                    }
                ],
            }
        },
    )
    captured = []

    async def fake_query(_connector, query):
        captured.append(query)
        models = list(query.filters["tenant_models"]["tenant-a"])
        return ReportDataset(
            rows=tuple(
                {
                    "period": "current",
                    "date": "2026-09-01",
                    "tenant": "佛跳墙",
                    "model": model,
                    "metric": "ai.usage.tokens",
                    "value": 100,
                }
                for model in models
            ),
            quality="complete",
            source="magik_cube",
            metadata={
                "query_windows": (
                    {
                        "period": "current",
                        "start": "2026-09-01 00:00",
                        "end": "2026-09-02 00:00",
                    },
                ),
                "scope": {
                    "tenant_names": ["佛跳墙"],
                    "models": models,
                    "tenant_models": {"佛跳墙": models},
                },
            },
        )

    monkeypatch.setattr(CubeConnector, "query", fake_query)
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(
            action="multi_scope_brief",
            period="day",
            start_date="2026-09-01",
            end_date="2026-09-01",
            report_selections=[
                {"tenant_query": "tenant-a", "model_scope": "all", "models": []}
            ],
        )

    assert result.is_error is False
    assert "1 个客户 / 28 个模型" in str(result)
    assert captured[0].filters["tenant_models"] == {"tenant-a": catalog_models}
    assert captured[0].filters["models"] == []
    catalog_call = magik.execute.call_args.kwargs
    assert catalog_call["report_selections"] == [
        {"tenant_query": "tenant-a", "model_scope": "selected", "models": []}
    ]


@pytest.mark.asyncio
async def test_multi_scope_all_models_stops_when_live_catalog_is_empty(
    monkeypatch, tmp_path
) -> None:
    """An empty catalog is unavailable data, not a successful zero-model report."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    tool = ReportCenterTool(
        ReportCenterToolConfig(cube_multi_scope_brief=True),
        _FakeCron(),
        magik,
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    magik.execute.return_value = ToolResult(
        "请选择模型。已加载 1 个客户、0 个模型。",
        metadata={
            OUTBOUND_META_AGENT_UI: {
                "kind": "magik_report_form",
                "phase": "models",
                "tenant_models": [
                    {
                        "tenant_query": "tenant-a",
                        "tenant_label": "佛跳墙",
                        "models": [],
                    }
                ],
            }
        },
    )
    query = AsyncMock()
    monkeypatch.setattr(CubeConnector, "query", query)
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(
            action="multi_scope_brief",
            period="day",
            start_date="2026-09-01",
            end_date="2026-09-01",
            report_selections=[
                {"tenant_query": "tenant-a", "model_scope": "all", "models": []}
            ],
        )

    assert result.is_error is True
    assert "实时模型目录未返回可用模型" in str(result)
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_cost_report_is_feature_gated_and_uses_report_runner(monkeypatch, tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    cube_config = MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example.internal",
        access_token="admin-fixture-token",
        token_api=MagikCubeTokenApiConfig(
            enable=True,
            base_url="https://token-api.example.internal",
            access_token="tokenapi-fixture",
        ),
    )

    async def fake_query(_connector, _query):
        return ReportDataset(
            rows=(
                {"tenant": "tencent_token_hub", "period": "current", "metric": "ai.cost", "value": 12.5},
                {"tenant": "tencent_token_hub", "period": "comparison", "metric": "ai.cost", "value": 10.0},
                {"tenant": "tencent_token_hub", "period": "snapshot", "metric": "ai.balance", "value": 88.2},
                {"tenant": "tencent_token_hub", "period": "snapshot", "metric": "ai.unbilled_amount", "value": 2.3},
            ),
            source="magik_cube",
        )

    monkeypatch.setattr(CubeConnector, "query", fake_query)
    config = report_center_module.ReportCenterToolConfig(
        cube_cost_connector=True,
        cube_cost_template=True,
        cube_cost_report=True,
    )
    tool = ReportCenterTool(config, _FakeCron(), magik, cube_config)
    assert tool.match_direct_request("成本报告") == {
        "action": "cost_report",
        "period": "month",
        "interactive": True,
    }
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(
            action="cost_report", period="month", tenant_query="佛跳墙"
        )
        home = await tool.execute(action="home")

    assert "应付金额" in str(result)
    assert result.metadata[OUTBOUND_META_AGENT_UI]["kind"] == "report_document"
    assert "Cube 成本与账户" in str(home)
    magik.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_report_is_direct_and_visible_only_when_flags_are_enabled(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    cron = _FakeCron()
    magik = AsyncMock()
    cube_config = MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example.internal",
        access_token="fixture-token",
    )
    config = ReportCenterToolConfig(
        cube_health_connector=True,
        cube_health_template=True,
        cube_health_report=True,
        cube_health_subscription=True,
    )

    async def fake_query(_connector, _query):
        values = {
            "ai.error_rate": 0.06,
            "ai.http_4xx_rate": 0.01,
            "ai.http_5xx_rate": 0.005,
            "ai.interface_delay": 100.0,
            "ai.ttft": 100.0,
            "ai.rpm": 10.0,
            "ai.tpm": 100.0,
            "ai.capacity_utilization": 0.5,
        }
        return ReportDataset(
            rows=tuple(
                {
                    "period": "current",
                    "metric": metric,
                    "value": value,
                    "endpoint": "endpoint-a",
                    "model": "model-a",
                }
                for metric, value in values.items()
            ),
            quality="complete",
            source="magik_cube",
        )

    monkeypatch.setattr(ReportCenterTool, "_run_health_report", ReportCenterTool._run_health_report)
    monkeypatch.setattr(report_center_module.CubeConnector, "query", fake_query)
    tool = ReportCenterTool(config, cron, magik, cube_config)

    assert tool.match_direct_request("健康报告") == {
        "action": "health_report",
        "period": "recent15m",
    }
    with request_context(
        RequestContext(
            channel="feishu",
            chat_id="chat-a",
            sender_id="ou_a",
            session_key="feishu:chat-a",
        )
    ):
        result = await tool.execute(action="health_report", period="recent15m")
        home = await tool.execute(action="home")

    assert "总体状态：异常" in str(result)
    assert "Cube 健康报告" in str(home)
    assert magik.execute.await_count == 0


@pytest.mark.asyncio
async def test_health_subscription_supports_day_and_week_but_not_recent15m(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    config = ReportCenterToolConfig(
        cube_health_connector=True,
        cube_health_template=True,
        cube_health_report=True,
        cube_health_subscription=True,
    )
    tool = ReportCenterTool(
        config,
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou_a",
        session_key="feishu:chat-a",
    )
    with request_context(context):
        rejected = await tool.execute(
            action="subscribe", period="recent15m", report_family="health"
        )
        created = await tool.execute(
            action="subscribe", period="day", report_family="health", send_time="10:00"
        )

    assert "day or week only" in str(rejected)
    subscriptions = store.subscriptions("feishu", "ou_a")
    assert len(subscriptions) == 1
    assert subscriptions[0].template_id == "health_sre"
    assert subscriptions[0].report_params["report_family"] == "health"
    assert "订阅已创建" in str(created)
