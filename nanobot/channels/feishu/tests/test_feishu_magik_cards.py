"""Structured Magik report cards and callback safety tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu.runtime import FeishuChannel, FeishuConfig


def _channel() -> FeishuChannel:
    channel = FeishuChannel(
        FeishuConfig(
            enabled=True,
            app_id="cli_test",
            app_secret="secret",
            allow_from=["ou_alice"],
        ),
        MessageBus(),
    )
    channel._running = True
    return channel


def _action(
    *,
    owner: str = "ou_alice",
    value=None,
    name: str = "",
    options=None,
    form_value=None,
):
    return SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=owner),
            action=SimpleNamespace(
                value=value,
                name=name,
                options=options or [],
                option=None,
                form_value=form_value or {},
            ),
        )
    )


def _find_tag(value, tag: str):
    if isinstance(value, dict):
        if value.get("tag") == tag:
            yield value
        for child in value.values():
            yield from _find_tag(child, tag)
    elif isinstance(value, list):
        for child in value:
            yield from _find_tag(child, tag)


def _scope_ui() -> dict:
    return {
        "kind": "magik_report_form",
        "phase": "scope",
        "title": "选择报表范围",
        "period": "2026-08-17 ~ 2026-08-23",
        "base_params": {
            "start_date": "2026-08-17",
            "end_date": "2026-08-23",
            "report_template": "matrix_card",
        },
        "tenant_options": [
            {"value": "tenant-a", "label": "A客户", "selected": False},
            {"value": "tenant-b", "label": "B客户", "selected": False},
        ],
        "tenant_required": True,
        "max_tenants": 5,
        "scope_options": [
            {"value": "summary", "label": "汇总"},
            {"value": "all", "label": "所有模型"},
            {"value": "selected", "label": "指定模型"},
        ],
    }


def test_scope_card_uses_opaque_options_and_owner_bound_callback() -> None:
    channel = _channel()
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice", OUTBOUND_META_AGENT_UI: _scope_ui()},
    )
    card = channel._build_agent_ui_cards(_scope_ui(), msg)[0]
    selector = next(_find_tag(card, "multi_select_static"))
    form = next(_find_tag(card, "form"))
    option = selector["options"][0]["value"]
    assert "tenant-a" not in option
    assert selector["required"] is True
    assert len(list(_find_tag(form, "button"))) == 3
    assert list(_find_tag(form, "action")) == []

    denied = channel._on_card_action_sync(
        _action(owner="ou_bob", name="tenants", options=[option])
    )
    assert denied.toast.type == "error"

    accepted = channel._on_card_action_sync(
        _action(name="tenants", options=[option])
    )
    assert accepted.toast.type == "success"
    state = next(iter(channel._card_interactions.values()))
    assert state.selected_tenants == ["tenant-a"]


def test_scope_submit_is_idempotent_and_maps_server_side_values() -> None:
    channel = _channel()
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )
    card = channel._build_scope_card(_scope_ui(), msg)
    state_id, state = next(iter(channel._card_interactions.items()))
    channel._schedule_card_resume = MagicMock(return_value=True)
    all_button = next(
        button
        for button in _find_tag(card, "button")
        if button.get("value", {}).get("scope") == "all"
    )
    tenant_option = next(_find_tag(card, "multi_select_static"))["options"][0]["value"]
    assert all_button["action_type"] == "form_submit"

    response = channel._on_card_action_sync(
        _action(
            value=all_button["value"],
            name=all_button["name"],
            form_value={"tenants": [tenant_option]},
        )
    )
    assert response.toast.type == "success"
    params = channel._schedule_card_resume.call_args.args[1]
    assert params["report_selections"] == [
        {"tenant_query": "tenant-a", "model_scope": "all", "models": []}
    ]

    duplicate = channel._on_card_action_sync(_action(value=all_button["value"]))
    assert duplicate.toast.type == "info"
    channel._schedule_card_resume.assert_called_once()
    assert state_id in channel._card_interactions


def test_model_form_submit_does_not_require_prior_select_callback() -> None:
    channel = _channel()
    ui = {
        "kind": "magik_report_form",
        "phase": "models",
        "title": "选择模型",
        "period": "2026-08-17 ~ 2026-08-23",
        "base_params": {"report_template": "matrix_card"},
        "tenant_models": [
            {
                "tenant_query": "tenant-a",
                "tenant_label": "A客户",
                "models": ["model-a", "model-b"],
            }
        ],
        "max_tenants": 5,
    }
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )
    card = channel._build_model_card(ui, msg)
    channel._schedule_card_resume = MagicMock(return_value=True)
    selector = next(_find_tag(card, "multi_select_static"))
    submit = next(_find_tag(card, "button"))

    response = channel._on_card_action_sync(
        _action(
            value=submit["value"],
            name=submit["name"],
            form_value={"models": [selector["options"][0]["value"]]},
        )
    )

    assert response.toast.type == "success"
    assert submit["action_type"] == "form_submit"
    params = channel._schedule_card_resume.call_args.args[1]
    assert params["report_selections"] == [
        {
            "tenant_query": "tenant-a",
            "model_scope": "selected",
            "models": ["model-a"],
        }
    ]


def test_expired_scope_card_is_rejected() -> None:
    channel = _channel()
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )
    channel._build_scope_card(_scope_ui(), msg)
    state_id, state = next(iter(channel._card_interactions.items()))
    state.expires_at = 0

    response = channel._on_card_action_sync(
        _action(value={"interaction_id": state_id, "action": "scope", "scope": "all"})
    )
    assert response.toast.type == "error"
    assert state_id not in channel._card_interactions


def test_report_card_contains_one_paginated_table() -> None:
    channel = _channel()
    ui = {
        "kind": "magik_report_cards",
        "cards": [
            {
                "title": "A客户 周报",
                "subtitle": "2026-08-17 ~ 2026-08-23",
                "overview": ["Token 100（+10 / ↑11.1%）"],
                "segments": ["周一 10｜+1 / ↑11.1%"],
                "table": {
                    "page_size": 8,
                    "columns": [
                        {"name": "model", "display_name": "模型", "data_type": "text"}
                    ],
                    "rows": [{"model": f"model-{index}"} for index in range(27)],
                },
                "insights": ["无异常"],
                "quality": "完整",
            }
        ],
    }
    msg = OutboundMessage(channel="feishu", chat_id="ou_alice", content="fallback")

    card = channel._build_agent_ui_cards(ui, msg)[0]
    tables = [element for element in card["elements"] if element.get("tag") == "table"]
    assert len(tables) == 1
    assert tables[0]["page_size"] == 8
    assert len(tables[0]["rows"]) == 27


def test_report_document_actions_are_opaque_and_owner_bound() -> None:
    channel = _channel()
    ui = {
        "kind": "report_document",
        "title": "报表中心",
        "blocks": [
            {"kind": "markdown", "data": {"content": "选择功能"}},
            {
                "kind": "actions",
                "data": {
                    "actions": [
                        {"action_id": "subscriptions", "label": "我的订阅"}
                    ]
                },
            },
        ],
    }
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )
    card = channel._build_agent_ui_cards(ui, msg)[0]
    button = next(_find_tag(card, "button"))
    assert "report_center" not in str(button["value"])
    channel._schedule_tool_resume = MagicMock(return_value=True)

    denied = channel._on_card_action_sync(
        _action(owner="ou_bob", value=button["value"], name=button["name"])
    )
    assert denied.toast.type == "error"

    accepted = channel._on_card_action_sync(
        _action(value=button["value"], name=button["name"])
    )
    assert accepted.toast.type == "success"
    _, tool_name, params, _ = channel._schedule_tool_resume.call_args.args
    assert tool_name == "report_center"
    assert params == {"action": "subscriptions"}


def test_subscription_form_maps_schedule_server_side_and_is_idempotent() -> None:
    channel = _channel()
    ui = {
        "kind": "report_subscription_form",
        "title": "设置报表订阅",
        "default_period": "day",
        "default_time": "10:00",
        "timezone": "Asia/Shanghai",
        "report_params": {"tenant_query": "tenant-a", "breakdown": "model"},
    }
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )
    card = channel._build_agent_ui_cards(ui, msg)[0]
    selectors = list(_find_tag(card, "select_static"))
    submit = next(_find_tag(card, "button"))
    schedule_selector = next(item for item in selectors if item["name"] == "schedule")
    time_selector = next(item for item in selectors if item["name"] == "send_time")
    schedule_option = schedule_selector["initial_option"]
    time_option = time_selector["initial_option"]
    assert "workdays" not in str(card)
    assert "10:00" not in time_option
    channel._schedule_tool_resume = MagicMock(return_value=True)

    response = channel._on_card_action_sync(
        _action(
            value=submit["value"],
            name=submit["name"],
            form_value={"schedule": schedule_option, "send_time": time_option},
        )
    )

    assert response.toast.type == "success"
    _, tool_name, params, _ = channel._schedule_tool_resume.call_args.args
    assert tool_name == "report_center"
    assert params == {
        "action": "subscribe",
        "period": "day",
        "daily_mode": "workdays",
        "send_time": "10:00",
        "report_params": {"tenant_query": "tenant-a", "breakdown": "model"},
    }
    duplicate = channel._on_card_action_sync(_action(value=submit["value"]))
    assert duplicate.toast.type == "info"
    channel._schedule_tool_resume.assert_called_once()


def test_subscription_form_rejects_forged_values() -> None:
    channel = _channel()
    ui = {
        "kind": "report_subscription_form",
        "default_period": "week",
        "report_params": {"tenant_query": "tenant-a"},
    }
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )
    card = channel._build_agent_ui_cards(ui, msg)[0]
    submit = next(_find_tag(card, "button"))
    channel._schedule_tool_resume = MagicMock(return_value=True)

    response = channel._on_card_action_sync(
        _action(
            value=submit["value"],
            name=submit["name"],
            form_value={"schedule": "week:1", "send_time": "10:00"},
        )
    )

    assert response.toast.type == "error"
    channel._schedule_tool_resume.assert_not_called()


def test_subscription_form_uses_server_defaults_when_client_omits_initial_values() -> None:
    channel = _channel()
    ui = {
        "kind": "report_subscription_form",
        "default_period": "week",
        "default_time": "10:00",
        "report_params": {"tenant_query": "tenant-a"},
    }
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )
    card = channel._build_agent_ui_cards(ui, msg)[0]
    submit = next(_find_tag(card, "button"))
    channel._schedule_tool_resume = MagicMock(return_value=True)

    response = channel._on_card_action_sync(
        _action(value=submit["value"], name=submit["name"], form_value={})
    )

    assert response.toast.type == "success"
    params = channel._schedule_tool_resume.call_args.args[2]
    assert params["period"] == "week"
    assert params["weekday"] == 1
    assert params["send_time"] == "10:00"


@pytest.mark.asyncio
async def test_authorized_onboarding_is_sent_once(monkeypatch, tmp_path) -> None:
    import nanobot.config.loader as config_loader
    import nanobot.reporting as reporting

    store = reporting.ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(reporting, "configured_report_state_store", lambda: store)
    monkeypatch.setattr(
        config_loader,
        "load_config",
        lambda: SimpleNamespace(
            tools=SimpleNamespace(
                reporting=SimpleNamespace(onboarding_version=1),
                magik_cube=SimpleNamespace(enable=True),
            )
        ),
    )
    channel = _channel()
    channel.send = AsyncMock()
    data = SimpleNamespace(
        event=SimpleNamespace(
            operator_id=SimpleNamespace(open_id="ou_alice"),
            chat_id="oc_private",
        )
    )

    await channel._send_report_home_onboarding(data)
    await channel._send_report_home_onboarding(data)

    channel.send.assert_awaited_once()
    sent = channel.send.await_args.args[0]
    assert sent.chat_id == "oc_private"
    assert sent.metadata[OUTBOUND_META_AGENT_UI]["kind"] == "report_document"
    assert store.onboarding_seen("feishu", "ou_alice", 1)


@pytest.mark.asyncio
async def test_disabled_reporting_does_not_send_onboarding(monkeypatch, tmp_path) -> None:
    import nanobot.config.loader as config_loader
    import nanobot.reporting as reporting

    store = reporting.ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(reporting, "configured_report_state_store", lambda: store)
    monkeypatch.setattr(
        config_loader,
        "load_config",
        lambda: SimpleNamespace(
            tools=SimpleNamespace(
                reporting=SimpleNamespace(enable=False, onboarding_version=1),
                magik_cube=SimpleNamespace(enable=True),
            )
        ),
    )
    channel = _channel()
    channel.send = AsyncMock()
    data = SimpleNamespace(
        event=SimpleNamespace(
            operator_id=SimpleNamespace(open_id="ou_alice"),
            chat_id="oc_private",
        )
    )

    await channel._send_report_home_onboarding(data)

    channel.send.assert_not_awaited()
    assert not store.onboarding_seen("feishu", "ou_alice", 1)
