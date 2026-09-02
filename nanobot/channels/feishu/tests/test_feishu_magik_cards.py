"""Structured Magik report cards and callback safety tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu.runtime import FeishuChannel, FeishuConfig
from nanobot.reporting.capabilities import subscriptions_document
from nanobot.reporting.interactions import report_interactions
from nanobot.reporting.provider_quality import provider_quality_selector_document
from nanobot.reporting.store import ReportSubscription


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


def test_report_center_scope_selector_resumes_the_owning_tool() -> None:
    """Protect the callback ownership regression that sent ReportCenter params to legacy Tool."""

    channel = _channel()
    ui = _scope_ui()
    ui["base_params"] = {
        "action": "multi_scope_brief",
        "period": "day",
        "interactive": False,
        "_report_center_selector": True,
    }
    card = channel._build_scope_card(
        ui,
        OutboundMessage(
            channel="feishu",
            chat_id="ou_alice",
            content="fallback",
            metadata={"sender_open_id": "ou_alice"},
        ),
    )
    channel._schedule_tool_resume = MagicMock(return_value=True)
    tenant_option = next(_find_tag(card, "multi_select_static"))["options"][0]["value"]
    all_button = next(
        button
        for button in _find_tag(card, "button")
        if button.get("value", {}).get("scope") == "all"
    )

    response = channel._on_card_action_sync(
        _action(
            value=all_button["value"],
            name=all_button["name"],
            form_value={"tenants": [tenant_option]},
        )
    )

    assert response.toast.type == "success"
    _, tool_name, params, _ = channel._schedule_tool_resume.call_args.args
    assert tool_name == "report_center"
    assert params["action"] == "multi_scope_brief"
    assert params["period"] == "day"
    assert "_report_center_selector" not in params


def test_scope_card_preserves_selected_report_template() -> None:
    channel = _channel()
    ui = _scope_ui()
    ui["base_params"]["report_template"] = "brief"
    ui["report_template_options"] = [
        {"value": "brief", "label": "简报（默认）"},
        {"value": "matrix_card", "label": "详细分析"},
        {"value": "full", "label": "完整报表"},
    ]
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )
    card = channel._build_scope_card(ui, msg)
    channel._schedule_card_resume = MagicMock(return_value=True)
    selectors = list(_find_tag(card, "select_static"))
    template_selector = next(item for item in selectors if item["name"] == "report_template")
    assert template_selector["initial_option"] == "brief"
    detail_option = next(
        item["value"] for item in template_selector["options"] if item["value"] == "matrix_card"
    )
    tenant_option = next(_find_tag(card, "multi_select_static"))["options"][0]["value"]
    submit = next(
        button for button in _find_tag(card, "button") if button.get("value", {}).get("scope") == "summary"
    )

    response = channel._on_card_action_sync(
        _action(
            value=submit["value"],
            name=submit["name"],
            form_value={
                "tenants": [tenant_option],
                "report_template": detail_option,
            },
        )
    )

    assert response.toast.type == "success"
    params = channel._schedule_card_resume.call_args.args[1]
    assert params["report_template"] == "matrix_card"


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
                "comparison_windows": [
                    {"label": "前一日", "window": "2026-08-22"},
                    {"label": "上周同期", "window": "2026-08-16"},
                ],
                "overview": ["Token 100｜较上上周：↑11.1%"],
                "segments": ["周一 10｜较上上周 ↑11.1%"],
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
    comparison = next(
        element
        for element in card["elements"]
        if element.get("tag") == "markdown"
        and "**对比周期**" in element.get("content", "")
    )
    assert "前一日：2026-08-22" in comparison["content"]
    assert "上周同期：2026-08-16" in comparison["content"]


def test_daily_report_omits_empty_segments_and_splits_tpm_detail_table() -> None:
    channel = _channel()
    ui = {
        "kind": "magik_report_cards",
        "cards": [
            {
                "title": "A客户 日报",
                "subtitle": "2026-08-29",
                "overview": ["Token 100｜较前一日（2026-08-28）：↑10.0%"],
                "segments": [],
                "table": {
                    "title": "模型矩阵：按 Token 总量降序",
                    "columns": [
                        {"name": "model", "display_name": "模型", "data_type": "text"}
                    ],
                    "rows": [{"model": "Kimi-K3"}],
                },
                "tpm_table": {
                    "title": "Endpoint TPM 明细：按平均 TPM 降序",
                    "columns": [
                        {
                            "name": "endpoint",
                            "display_name": "Endpoint",
                            "data_type": "text",
                        }
                    ],
                    "rows": [{"endpoint": "ep-a"}],
                },
                "insights": [],
                "quality": "完整",
            }
        ],
    }

    cards = channel._build_agent_ui_cards(
        ui, OutboundMessage(channel="feishu", chat_id="ou_alice", content="fallback")
    )

    assert len(cards) == 2
    assert all(
        len([item for item in card["elements"] if item.get("tag") == "table"]) == 1
        for card in cards
    )
    assert not any(
        "分段总量" in str(element.get("content") or "")
        for element in cards[0]["elements"]
    )


def test_report_document_health_cards_use_status_theme_kpi_grid_and_table_split() -> None:
    channel = _channel()
    ui = {
        "kind": "report_document",
        "title": "Cube 健康报告",
        "subtitle": "平台级聚合 · 固定阈值",
        "quality": "complete",
        "context": {
            "timezone": "Asia/Shanghai",
            "current_window": {
                "start": "2026-08-28T10:00:00+08:00",
                "end": "2026-08-28T10:15:00+08:00",
            },
            "baseline_window": {
                "start": "2026-08-28T09:45:00+08:00",
                "end": "2026-08-28T10:00:00+08:00",
            },
            "sources": [
                {"system": "Cube Admin", "route": "gateway/usages"},
            ],
            "metric_definitions": [
                {"aggregation": "请求级 P95"},
            ],
        },
        "blocks": [
            {
                "kind": "metrics",
                "data": {
                    "items": [
                        {"label": "总体状态", "value": "异常", "change": "异常指标：错误率"},
                        {"label": "错误率", "value": "6.0%", "change": "+500.0%"},
                        {"label": "接口延迟", "value": "120 ms", "change": "无对比数据"},
                    ]
                },
            },
            {
                "kind": "table",
                "data": {
                    "title": "异常 Endpoint TopN",
                    "columns": [{"tag": "column", "name": "endpoint", "display_name": "Endpoint"}],
                    "rows": [{"endpoint": "endpoint-a"}],
                },
            },
            {
                "kind": "table",
                "data": {
                    "title": "模型性能 TopN",
                    "columns": [{"tag": "column", "name": "model", "display_name": "模型"}],
                    "rows": [{"model": "model-a"}],
                },
            },
            {"kind": "note", "data": {"content": "核心查询接口均返回数据。"}},
            {
                "kind": "actions",
                "data": {"actions": [{"action_id": "health_report", "label": "刷新健康快照"}]},
            },
        ],
    }
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )

    cards = channel._build_agent_ui_cards(ui, msg)

    assert len(cards) == 2
    assert all(card["header"]["template"] == "red" for card in cards)
    assert all(
        sum(element.get("tag") == "table" for element in card["elements"]) <= 1
        for card in cards
    )
    assert any(element.get("tag") == "column_set" for element in cards[0]["elements"])
    assert any(element.get("tag") == "action" for element in cards[-1]["elements"])
    assert all(
        any(
            element.get("tag") == "note"
            and "前一等长窗口" in element["elements"][0]["content"]
            and "Cube Admin / gateway/usages" in element["elements"][0]["content"]
            for element in card["elements"]
        )
        for card in cards
    )
    assert "第 1/2 页" in cards[0]["header"]["title"]["content"]
    assert "第 2/2 页" in cards[1]["header"]["title"]["content"]


def test_report_document_daily_metrics_name_both_comparisons() -> None:
    channel = _channel()
    ui = {
        "kind": "report_document",
        "title": "Cube 日报",
        "quality": "complete",
        "context": {
            "timezone": "Asia/Shanghai",
            "current_window": {
                "start": "2026-08-29 00:00",
                "end": "2026-08-30 00:00",
            },
            "comparison_windows": [
                {
                    "key": "previous_period",
                    "label": "前一日",
                    "window": {
                        "start": "2026-08-28 00:00",
                        "end": "2026-08-29 00:00",
                    },
                },
                {
                    "key": "previous_week_same_day",
                    "label": "上周同期",
                    "window": {
                        "start": "2026-08-22 00:00",
                        "end": "2026-08-23 00:00",
                    },
                },
            ],
        },
        "blocks": [
            {
                "kind": "metrics",
                "data": {
                    "items": [
                        {
                            "label": "Token 消耗",
                            "value": "300",
                            "comparisons": [
                                {
                                    "label": "较前一日",
                                    "baseline": "200",
                                    "change": "↑50.0%",
                                },
                                {
                                    "label": "较上周同期",
                                    "baseline": "100",
                                    "change": "↑200.0%",
                                },
                            ],
                        }
                    ]
                },
            }
        ],
    }

    card = channel._build_agent_ui_cards(
        ui,
        OutboundMessage(channel="feishu", chat_id="ou_alice", content="fallback"),
    )[0]
    rendered = "\n".join(
        element.get("text", {}).get("content", "")
        for column_set in card["elements"]
        if column_set.get("tag") == "column_set"
        for column in column_set.get("columns", [])
        for element in column.get("elements", [])
    )
    note = next(element for element in card["elements"] if element.get("tag") == "note")
    note_text = note["elements"][0]["content"]

    assert "较前一日：↑50.0%" in rendered
    assert "较上周同期：↑200.0%" in rendered
    assert "基准 200" not in rendered
    assert "对比（前一日）：2026-08-28 00:00 - 2026-08-29 00:00" in note_text
    assert "对比（上周同期）：2026-08-22 00:00 - 2026-08-23 00:00" in note_text


def test_multi_customer_brief_collapses_explanations_and_context() -> None:
    """Verbose multi-scope semantics belong in one closed disclosure panel."""

    channel = _channel()
    ui = {
        "kind": "report_document",
        "document_id": "usage_customer_model_daily_brief",
        "title": "多客户多模型日报简报",
        "quality": "partial",
        "warnings": ["connection_failed"],
        "context": {
            "timezone": "Asia/Shanghai",
            "current_window": {
                "start": "2026-09-01 00:00",
                "end": "2026-09-02 00:00",
            },
            "sources": [
                {
                    "system": "Cube Admin",
                    "route": "analysis/active-tenant-daily-usage/query",
                }
            ],
            "quality": "partial",
            "quality_reasons": ["connection_failed"],
        },
        "blocks": [
            {
                "kind": "grouped_metrics",
                "data": {
                    "groups": [
                        {
                            "label": "佛跳墙",
                            "items": [
                                {
                                    "label": "Kimi-K3",
                                    "status": "active",
                                    "comparisons": [],
                                }
                            ],
                        }
                    ]
                },
            },
            {
                "kind": "note",
                "data": {
                    "content": "口径：每个客户、模型的 Token 按日求和。",
                    "collapsed": True,
                    "collapsed_label": "报表说明与数据质量",
                    "include_context": True,
                    "include_warnings": True,
                },
            },
        ],
    }

    card = channel._build_agent_ui_cards(
        ui,
        OutboundMessage(channel="feishu", chat_id="ou_alice", content="fallback"),
    )[0]
    panel = next(
        element for element in card["elements"] if element.get("tag") == "collapsible_panel"
    )
    panel_text = panel["elements"][0]["text"]["content"]

    assert panel["expanded"] is False
    assert panel["header"]["title"]["content"] == "报表说明与数据质量"
    assert "口径：每个客户、模型的 Token 按日求和。" in panel_text
    assert "当前：2026-09-01 00:00 - 2026-09-02 00:00" in panel_text
    assert "Cube Admin / analysis/active-tenant-daily-usage/query" in panel_text
    assert "connection_failed" in panel_text
    assert not any(element.get("tag") == "note" for element in card["elements"])


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


def test_provider_quality_selector_uses_opaque_options_and_resumes_report() -> None:
    channel = _channel()
    interaction = report_interactions().create(
        channel="feishu",
        chat_id="ou_alice",
        user_id="ou_alice",
        options={"opaque-ppio": "ppio", "opaque-other": "other"},
    )
    document = provider_quality_selector_document(
        interaction,
        [
            {"provider": "ppio", "name": "PPIO", "model": "Kimi-K3", "enabled": True},
            {"provider": "other", "name": "Other", "model": "Kimi-K3", "enabled": True},
        ],
        timezone="Asia/Shanghai",
    ).to_agent_ui()
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )
    card = channel._build_agent_ui_cards(document, msg)[0]
    selector = next(_find_tag(card, "multi_select_static"))
    submit = next(button for button in _find_tag(card, "button") if button["name"] == "submit_provider_quality")
    selected = next(option["value"] for option in selector["options"] if option["value"] == "opaque-ppio")
    channel._schedule_tool_resume = MagicMock(return_value=True)

    response = channel._on_card_action_sync(
        _action(
            value=submit["value"],
            name=submit["name"],
            form_value={"provider_options": [selected], "period": ["day"]},
        )
    )

    assert response.toast.type == "success"
    _, tool_name, params, _ = channel._schedule_tool_resume.call_args.args
    assert tool_name == "report_center"
    assert params["providers"] == ["ppio"]
    assert params["selection_confirmed"] is True


def test_health_document_actions_route_to_health_report_and_health_subscription() -> None:
    channel = _channel()
    health = channel._resolve_report_document_action({"action_id": "health_report"})
    subscription = channel._resolve_report_document_action(
        {"action_id": "subscription_setup:health"}
    )

    assert health == {
        "tool_name": "report_center",
        "params": {"action": "health_report", "period": "recent15m"},
        "content": "生成 Cube 健康报告",
    }
    assert subscription == {
        "tool_name": "report_center",
        "params": {
            "action": "subscription_setup",
            "period": "day",
            "report_family": "health",
        },
        "content": "设置 Cube 健康日报订阅",
    }

    provider_quality = channel._resolve_report_document_action(
        {"action_id": "provider_quality_report"}
    )
    assert provider_quality == {
        "tool_name": "report_center",
        "params": {"action": "provider_quality_report", "period": "recent15m"},
        "content": "生成 Cube 供应商质量报告",
    }


def test_subscription_card_keeps_each_button_with_its_opaque_subscription_target() -> None:
    channel = _channel()
    rows = [
        ReportSubscription(
            subscription_id="aaaaaaaaaaaaaaaa",
            channel="feishu",
            chat_id="ou_alice",
            user_id="ou_alice",
            connector_id="magik_cube",
            template_id="usage_daily_brief",
            template_version="2.0",
            schedule="0 10 * * 1-5",
            timezone="Asia/Shanghai",
            report_params={"tenant_query": "tenant-a", "model_scope": "summary"},
            cron_job_id="job-a",
            enabled=True,
            created_at="2026-09-02T10:00:00+08:00",
            updated_at="2026-09-02T10:00:00+08:00",
        ),
        ReportSubscription(
            subscription_id="bbbbbbbbbbbbbbbb",
            channel="feishu",
            chat_id="ou_alice",
            user_id="ou_alice",
            connector_id="magik_cube",
            template_id="usage_weekly_brief",
            template_version="2.0",
            schedule="0 9 * * 1",
            timezone="Asia/Shanghai",
            report_params={"all_tenants": True, "model_scope": "summary"},
            cron_job_id="job-b",
            enabled=False,
            created_at="2026-09-02T10:00:00+08:00",
            updated_at="2026-09-02T10:00:00+08:00",
        ),
    ]
    msg = OutboundMessage(
        channel="feishu",
        chat_id="ou_alice",
        content="fallback",
        metadata={"sender_open_id": "ou_alice"},
    )

    card = channel._build_agent_ui_cards(subscriptions_document(rows).to_agent_ui(), msg)[0]
    direct_elements = card["elements"]
    markdown_indices = [
        index
        for index, element in enumerate(direct_elements)
        if element.get("tag") == "markdown" and "**订阅 " in element.get("content", "")
    ]
    action_indices = [
        index for index, element in enumerate(direct_elements) if element.get("tag") == "action"
    ]
    assert markdown_indices[0] < action_indices[0] < markdown_indices[1] < action_indices[1]

    buttons = [element["actions"][0] for element in direct_elements if element.get("tag") == "action"]
    assert [button["text"]["content"] for button in buttons] == ["停用订阅 1", "启用订阅 2"]
    for button, expected_id, expected_action in zip(
        buttons,
        ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"),
        ("subscription_disable", "subscription_enable"),
        strict=True,
    ):
        value = button["value"]
        state = channel._card_interactions[value["interaction_id"]]
        target = state.option_values[value["action_token"]]
        assert target["params"] == {
            "action": expected_action,
            "subscription_id": expected_id,
        }


def test_period_report_action_routes_to_interactive_scope_selection() -> None:
    channel = _channel()

    for period in ("day", "week", "month", "recent7"):
        result = channel._resolve_report_document_action({"action_id": f"generate:{period}"})

        assert result == {
            "tool_name": "report_center",
            "params": {
                "action": "cube_report",
                "period": period,
                "interactive": True,
            },
            "content": f"生成固定 Cube {period} 报",
        }


def test_health_subscription_form_preserves_report_family_on_submit() -> None:
    channel = _channel()
    ui = {
        "kind": "report_subscription_form",
        "default_period": "day",
        "report_params": {"report_family": "health", "subscription_period": "day"},
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
    assert params["report_params"]["report_family"] == "health"


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
