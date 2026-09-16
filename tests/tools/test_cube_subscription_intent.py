from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.reporting.cube_subscription_intent import (
    CubeSubscriptionIntent,
    classify_subscription_intent,
    is_subscription_intent_candidate,
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


def test_deterministic_hourly_tpm_broadcast_parses_without_clock() -> None:
    """The hourly TPM broadcast is clock-driven, so no clock is required."""

    intent = parse_deterministic_subscription_intent(
        "每小时播报上一小时阳春面、豆汁、佛跳墙全部模型的TPM"
    )

    assert intent == CubeSubscriptionIntent(
        report_type="usage_customer_model_hourly_tpm",
        tenant_scope="selected",
        tenant_aliases=(),
        model_scope="all",
        models=(),
        recurrence="hourly",
        # send_time is a structural placeholder; the compiled cron ignores it.
        send_time="00:00",
    )


def test_deterministic_hourly_named_models_defer_to_classifier() -> None:
    """Named-model hourly requests must be extracted by the bounded classifier."""

    assert (
        parse_deterministic_subscription_intent("每小时播报 Kimi-K3 上一小时TPM")
        is None
    )


def test_deterministic_hourly_quoted_card_inherits_hourly_recurrence() -> None:
    """Quoting an hourly report card keeps the inherited scope on hourly cadence."""

    intent = parse_deterministic_subscription_intent(
        "每小时播报给我",
        referenced_report=True,
    )

    assert intent is not None
    assert intent.report_type == "inherit"
    assert intent.recurrence == "hourly"
    assert intent.send_time == "00:00"


def test_subscription_candidate_matches_meige_xiaoshi_wording() -> None:
    """“每个小时” has no 每小时 substring but must gate the same way.

    Regression for the live failure on 2026-09-15: a quoted hourly report
    with “每个小时发送给我一次这个报表” fell through to the unstructured
    LLM turn, which fabricated a saved subscription and a wrong channel.
    """

    assert is_subscription_intent_candidate("每个小时发送给我一次这个报表") is True
    assert is_subscription_intent_candidate("每小时发送给我一次这个报表") is True


def test_deterministic_hourly_quoted_meige_xiaoshi_inherits_scope() -> None:
    """The exact live quoted-card wording must inherit the hourly scope."""

    intent = parse_deterministic_subscription_intent(
        "每个小时发送给我一次这个报表",
        referenced_report=True,
    )

    assert intent is not None
    assert intent.report_type == "inherit"
    assert intent.tenant_scope == "inherit"
    assert intent.model_scope == "inherit"
    assert intent.recurrence == "hourly"
    assert intent.send_time == "00:00"


def test_deterministic_hourly_direct_meige_xiaoshi_broadcast() -> None:
    """The 每个小时 variant also compiles the all-model hourly broadcast."""

    intent = parse_deterministic_subscription_intent(
        "每个小时播报上一小时阳春面、豆汁、佛跳墙全部模型的TPM"
    )

    assert intent == CubeSubscriptionIntent(
        report_type="usage_customer_model_hourly_tpm",
        tenant_scope="selected",
        tenant_aliases=(),
        model_scope="all",
        models=(),
        recurrence="hourly",
        send_time="00:00",
    )


def test_deterministic_explicit_hours_route_to_hourly_broadcast() -> None:
    """“每天 9 点、10 点播报上一小时 TPM” compiles an hour list (2026-09-16)."""

    intent = parse_deterministic_subscription_intent(
        "每天9点、10点播报阳春面、豆汁、佛跳墙全部模型上一小时TPM"
    )

    assert intent == CubeSubscriptionIntent(
        report_type="usage_customer_model_hourly_tpm",
        tenant_scope="selected",
        tenant_aliases=(),
        model_scope="all",
        models=(),
        recurrence="hourly",
        # send_time keeps the first mentioned clock as a placeholder; the
        # compiled cron derives from the hour list instead.
        send_time="09:00",
        hours=(9, 10),
    )


def test_deterministic_single_explicit_hour_routes_to_hourly_broadcast() -> None:
    intent = parse_deterministic_subscription_intent(
        "每天9点播报阳春面全部模型上一小时TPM"
    )
    assert intent is not None
    assert intent.recurrence == "hourly"
    assert intent.hours == (9,)


def test_deterministic_meridiem_hours_adjust_to_24h() -> None:
    intent = parse_deterministic_subscription_intent(
        "每天上午9点、下午3点播报阳春面全部模型上一小时TPM"
    )
    assert intent is not None
    assert intent.hours == (9, 15)


def test_deterministic_hourly_wording_with_clocks_narrows_hours() -> None:
    """Explicit clock hours win over the every-hour 每小时 wording."""

    intent = parse_deterministic_subscription_intent(
        "每小时9点、10点播报阳春面全部模型上一小时TPM"
    )
    assert intent is not None
    assert intent.recurrence == "hourly"
    assert intent.hours == (9, 10)


def test_deterministic_daily_brief_keeps_clock_out_of_hours() -> None:
    """Without the hourly TPM wording a clock stays a daily send time."""

    intent = parse_deterministic_subscription_intent(
        "每天上午十点发送阳春面、豆汁、佛跳墙全部模型的多客户日报简报"
    )
    assert intent is not None
    assert intent.recurrence == "every_day"
    assert intent.send_time == "10:00"
    assert intent.hours is None


def test_deterministic_nonzero_minutes_never_become_hours() -> None:
    """10:30 is a daily send time, not an hourly cadence marker."""

    intent = parse_deterministic_subscription_intent(
        "每天10点30分发送阳春面全部模型的多客户日报简报"
    )
    assert intent is not None
    assert intent.recurrence == "every_day"
    assert intent.send_time == "10:30"
    assert intent.hours is None


def test_deterministic_workdays_hours_stay_unsupported() -> None:
    """Workday cadence with an hour list must not silently fire weekends."""

    assert (
        parse_deterministic_subscription_intent(
            "工作日9点、10点播报阳春面全部模型上一小时TPM"
        )
        is None
    )


def test_intent_payload_hours_validation() -> None:
    base = {
        "report_type": "usage_customer_model_hourly_tpm",
        "tenant_scope": "selected",
        "tenant_aliases": [],
        "model_scope": "all",
        "models": [],
        "recurrence": "hourly",
        "send_time": "00:00",
        "weekday": 1,
        "month_day": 1,
        "inherit_report_scope": False,
    }
    valid = CubeSubscriptionIntent.from_payload({**base, "hours": [10, 9, 10]})
    assert valid is not None
    assert valid.hours == (9, 10)
    # An empty list is the every-hour default.
    empty = CubeSubscriptionIntent.from_payload({**base, "hours": []})
    assert empty is not None
    assert empty.hours is None
    # Hours attached to a non-hourly cadence reject the whole payload.
    assert (
        CubeSubscriptionIntent.from_payload(
            {**base, "recurrence": "every_day", "hours": [9]}
        )
        is None
    )
    # Out-of-range or non-list shapes reject the payload; strings stay for
    # the deterministic parser, not the model boundary.
    assert CubeSubscriptionIntent.from_payload({**base, "hours": [24]}) is None
    assert CubeSubscriptionIntent.from_payload({**base, "hours": "9,10"}) is None


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
