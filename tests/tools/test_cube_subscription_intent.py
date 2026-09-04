from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.reporting.cube_subscription_intent import (
    CubeSubscriptionIntent,
    classify_subscription_intent,
    parse_deterministic_subscription_intent,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest


def test_deterministic_direct_subscription_parses_customer_model_brief() -> None:
    """A complete Chinese schedule must not depend on provider tool calling."""

    intent = parse_deterministic_subscription_intent(
        "每天上午十点发送阳春面、豆汁、佛跳墙全部模型的多客户日报简报"
    )

    assert intent == CubeSubscriptionIntent(
        report_type="usage_customer_model_daily_brief",
        tenant_scope="selected",
        tenant_aliases=(),
        model_scope="all",
        models=(),
        recurrence="every_day",
        send_time="10:00",
    )


def test_deterministic_referenced_subscription_inherits_scope() -> None:
    """A simple quoted-card schedule must preserve the server-side report scope."""

    intent = parse_deterministic_subscription_intent(
        "我要订阅这个报表，工作日上午十点发送给我",
        referenced_report=True,
    )

    assert intent is not None
    assert intent.report_type == "inherit"
    assert intent.tenant_scope == "inherit"
    assert intent.model_scope == "inherit"
    assert intent.recurrence == "workdays"
    assert intent.send_time == "10:00"


@pytest.mark.asyncio
async def test_direct_multi_customer_all_model_subscription_is_strictly_parsed() -> None:
    """Protect the user phrase that previously collapsed to one customer."""

    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="emit_cube_subscription_intent",
                        arguments={
                            "report_type": "usage_customer_model_daily_brief",
                            "tenant_scope": "selected",
                            "tenant_aliases": ["阳春面", "豆汁", "佛跳墙"],
                            "model_scope": "all",
                            "models": [],
                            "recurrence": "every_day",
                            "send_time": "10:00",
                            "weekday": 1,
                            "month_day": 1,
                            "inherit_report_scope": False,
                        },
                    )
                ],
            )
        )
    )
    runtime = SimpleNamespace(provider=provider, model="fixture-model")

    intent = await classify_subscription_intent(
        "每天上午十点发给我阳春面、豆汁、佛跳墙全部模型的多客户多模型日报简报",
        runtime,
        timeout_seconds=1,
    )

    assert intent == CubeSubscriptionIntent(
        report_type="usage_customer_model_daily_brief",
        tenant_scope="selected",
        tenant_aliases=("阳春面", "豆汁", "佛跳墙"),
        model_scope="all",
        models=(),
        recurrence="every_day",
        send_time="10:00",
    )
    request = provider.chat.await_args.kwargs
    assert request["tool_choice"]["function"]["name"] == "emit_cube_subscription_intent"
    assert request["temperature"] == 0


def test_referenced_subscription_allows_explicit_scope_override() -> None:
    intent = CubeSubscriptionIntent.from_payload(
        {
            "report_type": "inherit",
            "tenant_scope": "selected",
            "tenant_aliases": ["豆汁"],
            "model_scope": "all",
            "models": [],
            "recurrence": "workdays",
            "send_time": "10:00",
            "weekday": 1,
            "month_day": 1,
            "inherit_report_scope": True,
        }
    )

    assert intent is not None
    assert intent.tenant_aliases == ("豆汁",)


def test_subscription_intent_rejects_cron_or_unbounded_fields() -> None:
    payload = {
        "report_type": "usage_daily_brief",
        "tenant_scope": "selected",
        "tenant_aliases": ["佛跳墙"],
        "model_scope": "all",
        "models": [],
        "recurrence": "every_day",
        "send_time": "10:00",
        "weekday": 1,
        "month_day": 1,
        "inherit_report_scope": False,
        "cron": "0 10 * * *",
    }

    intent = CubeSubscriptionIntent.from_payload(payload)

    assert intent is None


def test_subscription_intent_splits_one_serialized_customer_list() -> None:
    """Protect providers that serialize a Chinese list into one array item."""

    intent = CubeSubscriptionIntent.from_payload(
        {
            "report_type": "usage_customer_model_daily_brief",
            "tenant_scope": "selected",
            "tenant_aliases": ["阳春面、豆汁、佛跳墙"],
            "model_scope": "all",
            "models": [],
            "recurrence": "every_day",
            "send_time": "10:00",
            "weekday": 1,
            "month_day": 1,
            "inherit_report_scope": False,
        }
    )

    assert intent is not None
    assert intent.tenant_aliases == ("阳春面", "豆汁", "佛跳墙")
