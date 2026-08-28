from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import nanobot.agent.tools.report_center as report_center_module
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, current_request_context, request_context
from nanobot.agent.tools.report_center import ReportCenterTool, ReportCenterToolConfig
from nanobot.bus.events import INBOUND_META_DIRECT_TOOL, OUTBOUND_META_AGENT_UI, InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.cron.session_turns import CRON_TRIGGER_META
from nanobot.reporting import ReportStateStore


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
    assert cron.jobs == []


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
