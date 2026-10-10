"""Semantic router safety/dispatch contracts, using deterministic provider fixtures.

These tests verify validation and compilation, not a real model's accuracy.
The canonical regex corpus remains an independent zero-regression gate.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.reporting.intent_router import (
    ReportIntentDraft,
    classify_report_intent,
    effective_router_mode,
    is_report_candidate,
)
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.magik_cube_admin import MagikCubeAdminApiTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.agent.test_magik_report_intent_route import _make_loop
from tests.tools.test_routing_eval import _RoutingChain


def _runtime(payload=None, *, response=None):
    """One forced-tool response, never a canned report or API result."""
    if response is None:
        response = LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="intent-fixture",
                    name="emit_report_intent",
                    arguments=payload,
                )
            ],
        )
    return SimpleNamespace(
        model="fixture-model", provider=SimpleNamespace(chat=AsyncMock(return_value=response))
    )


@pytest.mark.parametrize(
    "phrase",
    [
        "当前佛跳墙用户k3模型的TPM是多少",
        "现在佛跳墙的K3每分钟能跑多少token",
        "佛跳墙 Kimi-K3 实时TPM",
        "你好",
        "查询 https://example.com/api/",
    ],
)
async def test_realtime_and_unsafe_inputs_never_call_classifier(phrase):
    runtime = _runtime({"action": "home", "confidence": 1.0})
    assert not is_report_candidate(phrase)
    assert await classify_report_intent(phrase, runtime) is None
    runtime.provider.chat.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "subscribe", "confidence": 1.0},
        {"action": "cube_report", "confidence": 1.0, "cron": "* * * * *"},
        {"action": "cube_report", "confidence": "1.0"},
        {"action": "cube_report", "confidence": 1.1},
    ],
)
async def test_untrusted_mutations_and_schema_extras_are_rejected(payload):
    runtime = _runtime(payload)
    assert await classify_report_intent("昨天用量报告", runtime) is None
    runtime.provider.chat.assert_awaited_once()


@pytest.mark.parametrize(
    "response",
    [
        LLMResponse(content='{"action":"home","confidence":1}'),
        LLMResponse(
            content=None,
            finish_reason="refusal",
            tool_calls=[
                ToolCallRequest(
                    id="x",
                    name="emit_report_intent",
                    arguments={"action": "home", "confidence": 1.0},
                )
            ],
        ),
        LLMResponse(
            content=None, tool_calls=[ToolCallRequest(id="x", name="other_tool", arguments={})]
        ),
    ],
)
async def test_prose_refusal_and_wrong_tool_are_not_executable(response):
    assert await classify_report_intent("有哪些报表可以看", _runtime(response=response)) is None


async def test_timeout_is_bounded_and_does_not_retry():
    async def unavailable(**kwargs):
        import asyncio

        await asyncio.Event().wait()

    runtime = _runtime({})
    runtime.provider.chat.side_effect = unavailable
    assert await classify_report_intent("昨天用量报告", runtime, timeout_seconds=0.01) is None
    runtime.provider.chat.assert_awaited_once()


async def test_functional_evaluation_can_expand_generation_budget():
    """Reasoning-model acceptance may use a larger budget without retrying."""
    runtime = _runtime({"action": "cube_report", "confidence": 1.0})
    assert (
        await classify_report_intent("昨天用量报表", runtime, timeout_seconds=120, max_tokens=4096)
        is not None
    )
    runtime.provider.chat.assert_awaited_once()
    assert runtime.provider.chat.await_args.kwargs["max_tokens"] == 4096


@pytest.mark.parametrize(
    "text,offers_dates",
    [
        ("昨天K3用量报告", False),
        ("K3 2026-08-29日的日报", True),
    ],
)
async def test_relative_requests_do_not_offer_date_generation_slots(text, offers_dates):
    """Explicit dates remain supported, relative-date hallucinations are not solicited."""
    runtime = _runtime({"action": "cube_report", "confidence": 1.0})
    await classify_report_intent(text, runtime)
    properties = runtime.provider.chat.await_args.kwargs["tools"][0]["function"]["parameters"][
        "properties"
    ]
    assert ("start_date" in properties) is offers_dates
    assert ("end_date" in properties) is offers_dates


@pytest.mark.parametrize(
    "action,period",
    [
        ("customer_model_hourly_tpm", "recent1h"),
        ("health_report", "recent15m"),
        ("provider_quality_report", "recent15m"),
        ("cost_report", "month"),
    ],
)
def test_omitted_period_uses_action_default_not_daily(action, period):
    """Live-model failure: omitted hourly slot previously became an invalid day."""
    draft = ReportIntentDraft.model_validate({"action": action, "confidence": 1.0})
    assert draft.compile("模型报告", today=date(2026, 10, 10))["period"] == period


def test_explicit_all_customers_survives_omitted_model_boolean():
    """The source phrase, not an omitted LLM flag, owns an explicit all scope."""
    draft = ReportIntentDraft.model_validate({"action": "multi_scope_brief", "confidence": 1.0})
    params = draft.compile("昨天各个客户的用量", today=date(2026, 10, 10))
    assert params["all_tenants"] is True
    assert params["interactive"] is False


def test_omitted_week_period_uses_valid_relative_slot():
    draft = ReportIntentDraft.model_validate(
        {"action": "multi_scope_brief", "confidence": 1.0, "time_expression": "last_week"}
    )
    assert draft.compile("上周各客户用量", today=date(2026, 10, 10))["period"] == "week"


def test_hourly_relative_expression_is_not_forced_into_recent7():
    """The schema must represent a literal one-hour expression from live output."""
    draft = ReportIntentDraft.model_validate(
        {
            "action": "customer_model_hourly_tpm",
            "confidence": 1.0,
            "time_expression": "recent1h",
            "models": ["K3"],
        }
    )
    params = draft.compile("上一个钟头K3的TPM什么情况", today=date(2026, 10, 10))
    assert params["period"] == "recent1h"
    assert params["models"] == ["K3"]
    assert params["interactive"] is True


def test_model_cannot_hide_a_customer_or_invented_date_in_date_slots():
    """Misplaced live-model slots must clarify, never become an unscoped report."""
    draft = ReportIntentDraft.model_validate(
        {
            "action": "customer_model_hourly_tpm",
            "confidence": 1.0,
            "time_expression": "recent1h",
            "end_date": "豆汁",
        }
    )
    assert draft.compile("豆汁上个小时各模型TPM", today=date(2026, 10, 10)) == {
        "action": "report_parse_failed"
    }


@pytest.mark.parametrize(
    "text,payload,expected",
    [
        (
            "出一份昨天的用量报表",
            {"action": "cube_report", "time_expression": "yesterday"},
            {"action": "cube_report", "period": "day", "start_date": "2026-10-09"},
        ),
        (
            "给我看看前天K3的消耗情况",
            {"action": "cube_report", "models": ["K3"], "time_expression": "day_before_yesterday"},
            {"model": "K3", "start_date": "2026-10-08"},
        ),
        (
            "过去一小时佛跳墙全部模型的吞吐量怎么样",
            {
                "action": "customer_model_hourly_tpm",
                "period": "recent1h",
                "tenants": ["佛跳墙"],
                "all_models": True,
            },
            {"tenants": ["佛跳墙"], "model_scope": "all", "interactive": False},
        ),
        (
            "上周每个客户的模型用量对比一下",
            {"action": "multi_scope_brief", "period": "week", "all_tenants": True},
            {"action": "multi_scope_brief", "period": "week", "all_tenants": True},
        ),
        (
            "阳春面、豆汁、佛跳墙全部模型昨天用量",
            {
                "action": "multi_scope_brief",
                "tenants": ["阳春面", "豆汁", "佛跳墙"],
                "all_models": True,
            },
            {"tenants": ["阳春面", "豆汁", "佛跳墙"], "models": []},
        ),
        (
            "平台现在健康吗",
            {"action": "health_report", "period": "recent15m"},
            {"action": "health_report"},
        ),
        (
            "GLM-5.2哪家供应商跑得好",
            {"action": "provider_quality_report", "period": "recent15m", "models": ["GLM-5.2"]},
            {"model": "GLM-5.2"},
        ),
        ("我订阅了什么", {"action": "subscriptions"}, {"action": "subscriptions"}),
        ("之前生成的报表在哪看", {"action": "recent"}, {"action": "recent"}),
        (
            "Kimi-K3 2026-08-29日的日报",
            {
                "action": "cube_report",
                "models": ["Kimi-K3"],
                "time_expression": "explicit",
                "start_date": "2026-08-29",
            },
            {"start_date": "2026-08-29", "end_date": "2026-08-29"},
        ),
    ],
)
async def test_grounded_paraphrase_slots_compile_to_existing_actions(text, payload, expected):
    runtime = _runtime({"confidence": 1.0, **payload})
    draft = await classify_report_intent(text, runtime)
    assert draft is not None
    params = draft.compile(text, today=date(2026, 10, 10))
    assert params is not None
    assert {key: params[key] for key in expected} == expected
    assert runtime.provider.chat.await_args.kwargs["model"] == "fixture-model"
    assert runtime.provider.chat.await_args.kwargs["reasoning_effort"] == "none"


def test_compiled_slots_satisfy_the_actual_tool_schema(monkeypatch, tmp_path):
    """Prevent compiled inputs that real dispatch rejects despite mocked execution."""
    chain = _RoutingChain(monkeypatch, tmp_path)
    cases = [
        ("佛跳墙 K3 日报", {"action": "cube_report", "tenants": ["佛跳墙"], "models": ["K3"]}),
        (
            "佛跳墙 豆汁 全部模型昨天用量",
            {"action": "multi_scope_brief", "tenants": ["佛跳墙", "豆汁"], "all_models": True},
        ),
        (
            "佛跳墙上一小时TPM",
            {"action": "customer_model_hourly_tpm", "period": "recent1h", "tenants": ["佛跳墙"]},
        ),
        ("平台健康报告", {"action": "health_report", "period": "recent15m"}),
    ]
    for text, slots in cases:
        params = ReportIntentDraft.model_validate({"confidence": 1.0, **slots}).compile(
            text, today=date(2026, 10, 10)
        )
        assert params is not None
        assert chain.report_center.validate_params(params) == []


@pytest.mark.parametrize(
    "text,payload",
    [
        ("佛跳墙日报", {"tenants": ["豆汁"]}),
        ("日报", {"all_tenants": True}),
        ("K3日报", {"models": ["Kimi-K3"]}),
        ("K3 2026-08-29日的日报", {"models": ["K3"]}),
        ("K3 2026-08-29日的日报", {"time_expression": "explicit", "start_date": "2026-08-30"}),
        ("这个月账单多少了", {"action": "cost_report", "period": "month"}),
        ("这个礼拜的用量按模型拆开", {"period": "week"}),
        ("日报", {"confidence": 0.5}),
        ("佛跳墙全部模型日报", {"all_tenants": True}),
        (
            "佛跳墙供应商报告",
            {"action": "provider_quality_report", "period": "day", "tenants": ["佛跳墙"]},
        ),
        (
            "佛跳墙上一小时ep-k3 TPM",
            {
                "action": "customer_model_hourly_tpm",
                "period": "recent1h",
                "tenants": ["佛跳墙"],
                "endpoint": "ep-k3",
            },
        ),
        ("beast02日报", {"cluster": "beast02"}),
    ],
)
def test_invented_entities_dates_broadened_scope_and_unsupported_windows_clarify(text, payload):
    draft = ReportIntentDraft.model_validate(
        {"action": "cube_report", "confidence": 1.0, **payload}
    )
    assert draft.compile(text, today=date(2026, 10, 10)) == {"action": "report_parse_failed"}


@pytest.mark.parametrize("mode", ["fallback", "primary"])
async def test_registry_semantic_route_preserves_three_customers_and_one_call(
    monkeypatch, tmp_path, mode
):
    chain = _RoutingChain(monkeypatch, tmp_path)
    chain.report_center._store.set_setting("report_intent_router", mode)
    registry = ToolRegistry()
    # Legacy first reproduces actual tool-registration ordering risk.
    registry.register(chain.legacy)
    registry.register(chain.report_center)
    phrase = "阳春面、豆汁、佛跳墙全部模型昨天用量"
    runtime = _runtime(
        {
            "action": "multi_scope_brief",
            "confidence": 1.0,
            "tenants": ["阳春面", "豆汁", "佛跳墙"],
            "all_models": True,
        }
    )
    route = await registry.resolve_direct_request(phrase, runtime=runtime)
    assert route[0] == "report_center"
    assert route[1]["tenants"] == ["阳春面", "豆汁", "佛跳墙"]
    runtime.provider.chat.assert_awaited_once()


async def test_off_canonical_and_realtime_modes_do_not_add_model_calls(monkeypatch, tmp_path):
    chain = _RoutingChain(monkeypatch, tmp_path)
    registry = ToolRegistry()
    registry.register(chain.report_center)
    runtime = _runtime({})
    assert effective_router_mode(chain.report_center._store) == "off"
    assert await registry.resolve_direct_request("有哪些报表可以看", runtime=runtime) is None
    chain.report_center._store.set_setting("report_intent_router", "primary")
    assert (await registry.resolve_direct_request("帮助", runtime=runtime))[1] == {"action": "home"}
    assert await registry.resolve_direct_request("当前佛跳墙TPM是多少", runtime=runtime) is None
    runtime.provider.chat.assert_not_awaited()


async def test_failed_classification_is_clarification_not_legacy_retry(monkeypatch, tmp_path):
    chain = _RoutingChain(monkeypatch, tmp_path)
    chain.report_center._store.set_setting("report_intent_router", "fallback")
    registry = ToolRegistry()
    registry.register(chain.legacy)
    registry.register(chain.report_center)
    runtime = _runtime(response=LLMResponse(content="unstructured answer"))
    result = await registry.resolve_direct_request("出一份昨天的用量报表", runtime=runtime)
    assert result == ("report_center", {"action": "report_parse_failed"})
    runtime.provider.chat.assert_awaited_once()


async def test_semantic_report_in_agent_loop_is_not_a_subscription(monkeypatch, tmp_path):
    """Generic classifier eligibility must not activate subscription interception."""
    chain = _RoutingChain(monkeypatch, tmp_path)
    chain.report_center._store.set_setting("report_intent_router", "fallback")
    loop, provider = _make_loop(tmp_path)
    provider.chat = _runtime({"action": "cube_report", "confidence": 1.0}).provider.chat
    chain.report_center.execute = AsyncMock(return_value=ToolResult("resolved report"))
    loop.tools.register(chain.report_center)
    try:
        response = await loop._process_message(
            InboundMessage(
                channel="feishu",
                sender_id="user",
                chat_id="chat",
                content="出一份昨天的用量报表",
            )
        )
        assert response.content == "resolved report"
        assert chain.report_center.execute.await_args.kwargs["action"] == "cube_report"
        provider.chat.assert_awaited_once()
        provider.chat_with_retry.assert_not_awaited()
    finally:
        await loop.close_mcp()


async def test_primary_keeps_admin_exact_routes_authoritative(monkeypatch, tmp_path):
    """Semantic usage routing must not steal a read-only management query."""
    chain = _RoutingChain(monkeypatch, tmp_path)
    chain.report_center._store.set_setting("report_intent_router", "primary")
    registry = ToolRegistry()
    registry.register(chain.legacy)
    registry.register(chain.report_center)
    registry.register(MagikCubeAdminApiTool())
    runtime = _runtime({})
    route = await registry.resolve_direct_request("zhangyan用户有哪些endpoint", runtime=runtime)
    assert route[0] == "magik_cube_admin_api"
    runtime.provider.chat.assert_not_awaited()
