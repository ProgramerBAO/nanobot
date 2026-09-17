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
    OUTBOUND_META_REPORT_REFERENCE,
    InboundMessage,
)
from nanobot.bus.queue import MessageBus
from nanobot.cron.service import CronService
from nanobot.cron.session_turns import CRON_TRIGGER_META
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.reporting import CubeConnector, ReportDataset, ReportStateStore
from nanobot.reporting.store import ReportMessageReference, ReportSubscription
from nanobot.reporting.subscriptions import (
    ReportSubscriptionService,
    SubscriptionServiceError,
)


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
    tool, store, _cron = _tool(monkeypatch, tmp_path)

    assert tool.match_direct_request("日报") == {
        "action": "cube_report",
        "period": "day",
        "interactive": True,
        "report_template": "brief",
    }
    assert tool.match_direct_request("详细日报")["report_template"] == "matrix_card"
    assert tool.match_direct_request("完整日报")["report_template"] == "full"

    # Brief-template availability lives in the template policy since the
    # 2026-09-16 consolidation (the retired cube_usage_brief_template flag
    # is ignored): a disabled row restores matrix routing.
    store.set_template_policy(
        "usage_daily_brief",
        enabled=False,
        subscription_mode="all_authorized",
        updated_by="test",
        expected_revision=0,
    )
    disabled = ReportCenterTool(
        ReportCenterToolConfig(), _FakeCron(), MagicMock()
    )
    assert disabled.match_direct_request("日报")["report_template"] == "matrix_card"


def test_new_capacity_and_multi_scope_phrases_use_report_center(monkeypatch, tmp_path) -> None:
    tool, _store, _cron = _tool(monkeypatch, tmp_path)

    assert tool.match_direct_request("多客户多模型日报简报") == {
        "action": "multi_scope_brief",
        "interactive": True,
        "period": "day",
    }
    assert tool.match_direct_request("多客户多模型周报简报") == {
        "action": "multi_scope_brief",
        "interactive": True,
        "period": "week",
    }
    assert tool.match_direct_request("查看各客户模型周报") == {
        "action": "multi_scope_brief",
        "interactive": True,
        "period": "week",
    }
    named_weekly = tool.match_direct_request(
        "阳春面、豆汁、佛跳墙全部模型的多客户周报简报"
    )
    assert named_weekly["action"] == "multi_scope_brief"
    assert named_weekly["period"] == "week"
    assert named_weekly["tenants"] == ["阳春面", "豆汁", "佛跳墙"]
    assert len(named_weekly["report_selections"]) == 3


def test_hourly_tpm_phrases_preserve_all_customers_and_selected_models(
    monkeypatch,
    tmp_path,
) -> None:
    """Regression: hourly wording must never fall through to a daily usage report."""

    tool, _store, _cron = _tool(monkeypatch, tmp_path)

    assert tool.match_direct_request("佛跳墙 Kimi-K3 上一小时 TPM 峰值和均值") == {
        "action": "customer_model_hourly_tpm",
        "period": "recent1h",
        "tenants": ["佛跳墙"],
        "models": ["Kimi-K3"],
        "model_scope": "selected",
        "interactive": False,
    }
    assert tool.match_direct_request("查看阳春面、豆汁、佛跳墙全部模型上一小时 TPM") == {
        "action": "customer_model_hourly_tpm",
        "period": "recent1h",
        "tenants": ["阳春面", "豆汁", "佛跳墙"],
        "model_scope": "all",
        "interactive": False,
    }
    assert tool.match_direct_request("上一小时 TPM") == {
        "action": "customer_model_hourly_tpm",
        "period": "recent1h",
        "interactive": True,
    }


def _hourly_subscription(**param_overrides) -> ReportSubscription:
    params = {
        "tenants": ["tenant-fo", "tenant-noodle"],
        "tenant_labels": ["佛跳墙", "阳春面"],
        "models": ["Kimi-K3"],
        "model_scope": "selected",
        "subscription_period": "recent1h",
        # Confirmed subscriptions always carry the per-tenant selections the
        # preview built from the live catalog.
        "report_selections": [
            {
                "tenant_query": "tenant-fo",
                "model_scope": "selected",
                "models": ["Kimi-K3"],
            },
            {
                "tenant_query": "tenant-noodle",
                "model_scope": "selected",
                "models": ["Kimi-K3"],
            },
        ],
    }
    params.update(param_overrides)
    return ReportSubscription(
        subscription_id="sub-hourly",
        channel="feishu",
        chat_id="chat-a",
        user_id="ou-a",
        connector_id="magik_cube",
        template_id="usage_customer_model_hourly_tpm",
        template_version="2.0",
        schedule="5 * * * *",
        timezone="Asia/Shanghai",
        report_params=params,
        cron_job_id="job-a",
        enabled=True,
        created_at="2026-09-15T10:00:00+08:00",
        updated_at="2026-09-15T10:00:00+08:00",
    )


def test_hourly_subscription_compiles_clock_driven_intent() -> None:
    """The cron-run compiler must resolve hourly subscriptions to the hourly intent."""

    tool = ReportCenterTool(
        ReportCenterToolConfig(
            cube_customer_model_hourly_tpm=True,
            cube_customer_model_hourly_tpm_subscription=True,
        ),
        _FakeCron(),
        MagicMock(),
    )

    intent = tool._subscription_cube_intent(_hourly_subscription())

    assert intent is not None
    assert intent.template_id == "usage_customer_model_hourly_tpm"
    assert intent.period == "recent1h"
    assert intent.tenants == ("tenant-fo", "tenant-noodle")
    assert "Kimi-K3" in intent.models
    # The selected branch rebuilds the per-tenant pairs from the validated
    # selections so cron runs query real customer/model relationships.
    assert intent.filters["tenant_models"] == {
        "tenant-fo": ["Kimi-K3"],
        "tenant-noodle": ["Kimi-K3"],
    }
    assert intent.filters["tenant_names"] == {
        "tenant-fo": "佛跳墙",
        "tenant-noodle": "阳春面",
    }

    # A multi-tenant selected subscription without per-tenant selections
    # cannot map the flat model list to real relationships and must fail
    # closed instead of querying with an empty tenant.
    flat_only = _hourly_subscription(report_selections=[])
    assert tool._subscription_cube_intent(flat_only) is None

    # All-model hourly subscriptions only compile with a fresh live discovery
    # result, so each run refreshes the active catalog before delivery.
    all_models = _hourly_subscription(model_scope="all", models=[])
    assert (
        tool._subscription_cube_intent(
            all_models, tenant_models={"tenant-fo": ["Kimi-K3"]}
        )
        is not None
    )
    assert tool._subscription_cube_intent(all_models) is None

    # Since the 2026-09-16 consolidation, compilation is no longer flag-gated
    # (the retired family flags are ignored): the intent always compiles and
    # the always-enforced template policy denies disabled templates at run
    # time instead (covered by the policy gate tests).
    unflagged = ReportCenterTool(
        ReportCenterToolConfig(),
        _FakeCron(),
        MagicMock(),
    )
    assert unflagged._subscription_cube_intent(_hourly_subscription()) is not None


@pytest.mark.asyncio
async def test_deterministic_subscription_compiles_all_live_tenants_without_llm(
    monkeypatch, tmp_path
) -> None:
    """The complete schedule phrase must retain all catalog-matched tenants."""

    tool, _store, _cron = _tool(monkeypatch, tmp_path)
    tool._magik_tool.find_tenant_mentions.return_value = [
        {"tenant_id": "tenant-noodle", "display_name": "阳春面"},
        {"tenant_id": "tenant-douzhi", "display_name": "豆汁"},
        {"tenant_id": "tenant-fo", "display_name": "佛跳墙"},
    ]

    result = await tool.classify_direct_request(
        "每天上午十点发送阳春面、豆汁、佛跳墙全部模型的多客户日报简报",
        MagicMock(),
    )

    assert result is not None
    assert result["action"] == "subscription_preview"
    assert result["report_type"] == "usage_customer_model_daily_brief"
    assert result["tenant_aliases"] == ["阳春面", "豆汁", "佛跳墙"]
    assert result["model_scope"] == "all"


@pytest.mark.asyncio
async def test_reference_scope_is_found_even_when_subscription_nlu_fails(
    monkeypatch, tmp_path
) -> None:
    """A valid reference must produce a parser error, never a missing-card error."""

    tool, store, _cron = _tool(monkeypatch, tmp_path)
    store.save_message_reference(
        ReportMessageReference(
            channel="feishu",
            chat_id="chat-a",
            message_id="om-report",
            run_id="run-a",
            document_id="doc-a",
            connector_id="magik_cube",
            template_id="usage_customer_model_daily_brief",
            period="day",
            scope={"tenants": ["tenant-fo"], "model_scope": "all"},
            created_at="2026-09-02T00:00:00+00:00",
            expires_at="2099-09-02T00:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        report_center_module,
        "classify_subscription_intent",
        AsyncMock(return_value=None),
    )

    result = await tool.classify_referenced_subscription(
        "请订阅这个报表，按一个无法识别的时间发送给我",
        MagicMock(),
        channel="feishu",
        chat_id="chat-a",
        reference_message_id="om-report",
    )

    assert result == {
        "action": "subscription_parse_failed",
        "subscription_error": "nlu_unavailable",
        "reference_message_id": "om-report",
    }


@pytest.mark.asyncio
async def test_quoted_hourly_report_meige_xiaoshi_routes_to_subscription_preview(
    monkeypatch, tmp_path
) -> None:
    """The live failure wording must inherit the hourly scope via the reference.

    Regression for 2026-09-15: “每个小时发送给我一次这个报表” failed the
    subscription-candidate gate (每个小时 contains no 每小时 substring), fell
    through to the unstructured LLM turn, and produced a fabricated
    "saved subscription" reply with a wrong target channel.
    """

    tool, store, _cron = _tool(monkeypatch, tmp_path)
    store.save_message_reference(
        ReportMessageReference(
            channel="feishu",
            chat_id="chat-a",
            message_id="om-hourly",
            run_id="run-hourly",
            document_id="usage_customer_model_hourly_tpm",
            connector_id="magik_cube",
            template_id="usage_customer_model_hourly_tpm",
            period="recent1h",
            scope={
                "report_variant": "customer_model_hourly_tpm",
                "report_template_id": "usage_customer_model_hourly_tpm",
                "tenants": ["tenant-fo"],
                "tenant_labels": ["佛跳墙"],
                "model_scope": "all",
                "models": [],
            },
            created_at="2026-09-15T00:00:00+00:00",
            expires_at="2099-09-15T00:00:00+00:00",
        )
    )

    result = await tool.classify_referenced_subscription(
        "每个小时发送给我一次这个报表",
        MagicMock(),
        channel="feishu",
        chat_id="chat-a",
        reference_message_id="om-hourly",
    )

    assert result is not None
    assert result["action"] == "subscription_preview"
    assert result["report_type"] == "inherit"
    assert result["recurrence"] == "hourly"
    assert result["send_time"] == "00:00"
    assert result["inherit_report_scope"] is True
    assert result["reference_message_id"] == "om-hourly"


def test_tool_schema_accepts_hourly_subscription_preview_params() -> None:
    """Preview params for hourly/weekly subscriptions must pass the tool schema.

    Regression for 2026-09-15: the preview re-entered report_center with
    recurrence="hourly" and was rejected by the parameter enum before any
    handler ran. The NLU vocabulary, the tool schema, and the preview
    compiler are separate registries; this test keeps every supported
    recurrence/report-type combination inside the public schema.
    """
    from nanobot.agent.reporting.cube_subscription_intent import CubeSubscriptionIntent
    from nanobot.agent.tools.base import Schema

    for report_type, recurrence in [
        ("usage_customer_model_hourly_tpm", "hourly"),
        ("usage_customer_model_weekly_brief", "weekly"),
        ("usage_customer_model_daily_brief", "every_day"),
        ("inherit", "hourly"),
    ]:
        intent = CubeSubscriptionIntent(
            report_type=report_type,  # type: ignore[arg-type]
            tenant_scope="inherit" if report_type == "inherit" else "selected",
            tenant_aliases=(),
            model_scope="inherit" if report_type == "inherit" else "all",
            models=(),
            recurrence=recurrence,  # type: ignore[arg-type]
            send_time="00:00" if recurrence == "hourly" else "10:00",
        )
        params = report_center_module.ReportCenterTool._subscription_preview_params(
            intent, reference_message_id="om-hourly"
        )
        errors = Schema.validate_json_schema_value(
            params, report_center_module._REPORT_CENTER_PARAMETERS, "report_center"
        )
        assert errors == [], f"{report_type}/{recurrence}: {errors}"

    # Explicit broadcast hours (2026-09-16) must pass the same schema and
    # arrive as a plain list.
    hours_intent = CubeSubscriptionIntent(
        report_type="usage_customer_model_hourly_tpm",
        tenant_scope="selected",
        tenant_aliases=(),
        model_scope="all",
        models=(),
        recurrence="hourly",
        send_time="00:00",
        hours=(10, 9),
    )
    hours_params = report_center_module.ReportCenterTool._subscription_preview_params(
        hours_intent
    )
    assert hours_params["hours"] == [10, 9]
    errors = Schema.validate_json_schema_value(
        hours_params, report_center_module._REPORT_CENTER_PARAMETERS, "report_center"
    )
    assert errors == []
    # The every-hour default omits the key entirely: the schema types hours
    # as a non-nullable array.
    plain_params = report_center_module.ReportCenterTool._subscription_preview_params(
        CubeSubscriptionIntent(
            report_type="usage_customer_model_hourly_tpm",
            tenant_scope="selected",
            tenant_aliases=(),
            model_scope="all",
            models=(),
            recurrence="hourly",
            send_time="00:00",
        )
    )
    assert "hours" not in plain_params


@pytest.mark.asyncio
async def test_quoted_hourly_card_per_tenant_models_skip_cross_validation(
    monkeypatch, tmp_path
) -> None:
    """Quoted multi-customer cards validate per-tenant pairs, not the cross product.

    Regression for the live failure on 2026-09-15: inheriting a card whose
    reference stored a flat cross-tenant model list made the preview validate
    every model against every tenant and reject the whole subscription with a
    long cross-pair error list.
    """

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    magik.resolve_tenant_queries.return_value = (
        [
            {"query": "tenant-fo", "tenant_id": "tenant-fo", "display_name": "佛跳墙"},
            {"query": "tenant-douzhi", "tenant_id": "tenant-douzhi", "display_name": "豆汁"},
            {"query": "tenant-noodle", "tenant_id": "tenant-noodle", "display_name": "阳春面"},
        ],
        [],
    )
    # Ordered per-tenant responses mirroring the real catalog: each tenant
    # only confirms its own models, so a flat cross-product validation would
    # exhaust this list or receive the wrong tenant's answer.
    magik.resolve_models_for_tenants.side_effect = [
        ({"tenant-fo": ["GLM-5.2", "Kimi-K3"]}, []),
        ({"tenant-douzhi": ["GLM-5.2"]}, []),
        ({"tenant-noodle": ["MiniMax-M2.7", "MiniMax-M3"]}, []),
    ]
    tool = ReportCenterTool(
        ReportCenterToolConfig(
            cube_customer_model_hourly_tpm=True,
            cube_customer_model_hourly_tpm_subscription=True,
        ),
        _FakeCron(),
        magik,
        # The connector only needs to exist for template registration; these
        # tests never issue a Cube request.
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
        ),
    )
    store.save_message_reference(
        ReportMessageReference(
            channel="feishu",
            chat_id="chat-a",
            message_id="om-hourly",
            run_id="run-hourly",
            document_id="usage_customer_model_hourly_tpm",
            connector_id="magik_cube",
            template_id="usage_customer_model_hourly_tpm",
            period="recent1h",
            scope={
                "report_variant": "customer_model_hourly_tpm",
                "report_template_id": "usage_customer_model_hourly_tpm",
                "tenants": ["tenant-fo", "tenant-douzhi", "tenant-noodle"],
                "tenant_labels": ["佛跳墙", "豆汁", "阳春面"],
                "model_scope": "selected",
                "models": ["GLM-5.2", "Kimi-K3", "MiniMax-M2.7", "MiniMax-M3"],
                "report_selections": [
                    {
                        "tenant_query": "tenant-fo",
                        "model_scope": "selected",
                        "models": ["GLM-5.2", "Kimi-K3"],
                    },
                    {
                        "tenant_query": "tenant-douzhi",
                        "model_scope": "selected",
                        "models": ["GLM-5.2"],
                    },
                    {
                        "tenant_query": "tenant-noodle",
                        "model_scope": "selected",
                        "models": ["MiniMax-M2.7", "MiniMax-M3"],
                    },
                ],
            },
            created_at="2026-09-15T00:00:00+00:00",
            expires_at="2099-09-15T00:00:00+00:00",
        )
    )

    preview_params = await tool.classify_referenced_subscription(
        "每个小时发送给我一次这个报表",
        MagicMock(),
        channel="feishu",
        chat_id="chat-a",
        reference_message_id="om-hourly",
    )
    assert preview_params["action"] == "subscription_preview"
    assert preview_params["recurrence"] == "hourly"
    assert preview_params["inherit_report_scope"] is True

    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou-a",
        session_key="feishu:chat-a",
        # The preview binds the quoted card to the reply's parent message.
        metadata={"parent_id": "om-hourly"},
    )
    with request_context(context):
        preview = await tool.execute(
            action="subscription_preview",
            report_type="inherit",
            tenant_scope="inherit",
            tenant_aliases=[],
            model_scope="inherit",
            models=[],
            recurrence="hourly",
            send_time="00:00",
            weekday=1,
            month_day=1,
            inherit_report_scope=True,
            reference_message_id="om-hourly",
        )

    assert "指定模型无法通过实时目录校验" not in str(preview)
    ui = preview.metadata[OUTBOUND_META_AGENT_UI]
    actions = [
        item
        for block in ui["blocks"]
        if block["kind"] == "actions"
        for item in block["data"]["actions"]
    ]
    confirm = next(
        (item for item in actions if item["action_id"] == "subscription_confirm"),
        None,
    )
    assert confirm is not None
    # Every resolver call carried exactly one tenant and only that tenant's
    # own models; the flat cross product was never queried.
    resolver_calls = [
        (call.args[0], tuple(call.args[1]))
        for call in magik.resolve_models_for_tenants.await_args_list
    ]
    assert resolver_calls == [
        (["tenant-fo"], ("GLM-5.2", "Kimi-K3")),
        (["tenant-douzhi"], ("GLM-5.2",)),
        (["tenant-noodle"], ("MiniMax-M2.7", "MiniMax-M3")),
    ]
    report_params = confirm["params"]["report_params"]
    assert sorted(report_params["models"]) == [
        "GLM-5.2",
        "Kimi-K3",
        "MiniMax-M2.7",
        "MiniMax-M3",
    ]
    selection_models = {
        item["tenant_query"]: item["models"]
        for item in report_params["report_selections"]
    }
    assert selection_models == {
        "tenant-fo": ["GLM-5.2", "Kimi-K3"],
        "tenant-douzhi": ["GLM-5.2"],
        "tenant-noodle": ["MiniMax-M2.7", "MiniMax-M3"],
    }


@pytest.mark.asyncio
async def test_quoted_hourly_all_model_card_skips_selected_validation(
    monkeypatch, tmp_path
) -> None:
    """All-model hourly cards inherit per-run discovery; no model re-validation."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    magik.resolve_tenant_queries.return_value = (
        [
            {"query": "tenant-fo", "tenant_id": "tenant-fo", "display_name": "佛跳墙"},
            {"query": "tenant-douzhi", "tenant_id": "tenant-douzhi", "display_name": "豆汁"},
        ],
        [],
    )
    tool = ReportCenterTool(
        ReportCenterToolConfig(
            cube_customer_model_hourly_tpm=True,
            cube_customer_model_hourly_tpm_subscription=True,
        ),
        _FakeCron(),
        magik,
        # The connector only needs to exist for template registration; these
        # tests never issue a Cube request.
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
        ),
    )
    store.save_message_reference(
        ReportMessageReference(
            channel="feishu",
            chat_id="chat-a",
            message_id="om-hourly-all",
            run_id="run-hourly-all",
            document_id="usage_customer_model_hourly_tpm",
            connector_id="magik_cube",
            template_id="usage_customer_model_hourly_tpm",
            period="recent1h",
            scope={
                "report_variant": "customer_model_hourly_tpm",
                "report_template_id": "usage_customer_model_hourly_tpm",
                "tenants": ["tenant-fo", "tenant-douzhi"],
                "tenant_labels": ["佛跳墙", "豆汁"],
                "model_scope": "all",
                "models": [],
                "report_selections": [
                    {"tenant_query": "tenant-fo", "model_scope": "all", "models": []},
                    {"tenant_query": "tenant-douzhi", "model_scope": "all", "models": []},
                ],
            },
            created_at="2026-09-15T00:00:00+00:00",
            expires_at="2099-09-15T00:00:00+00:00",
        )
    )

    preview_params = await tool.classify_referenced_subscription(
        "每个小时发送给我一次这个报表",
        MagicMock(),
        channel="feishu",
        chat_id="chat-a",
        reference_message_id="om-hourly-all",
    )
    assert preview_params["action"] == "subscription_preview"
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou-a",
        session_key="feishu:chat-a",
        # The preview binds the quoted card to the reply's parent message.
        metadata={"parent_id": "om-hourly-all"},
    )
    with request_context(context):
        preview = await tool.execute(
            action="subscription_preview",
            report_type="inherit",
            tenant_scope="inherit",
            tenant_aliases=[],
            model_scope="inherit",
            models=[],
            recurrence="hourly",
            send_time="00:00",
            weekday=1,
            month_day=1,
            inherit_report_scope=True,
            reference_message_id="om-hourly-all",
        )

    ui = preview.metadata[OUTBOUND_META_AGENT_UI]
    actions = [
        item
        for block in ui["blocks"]
        if block["kind"] == "actions"
        for item in block["data"]["actions"]
    ]
    assert any(item["action_id"] == "subscription_confirm" for item in actions)
    # All-model inheritance resolves models per run; the flat model resolver
    # must not be consulted at preview time.
    magik.resolve_models_for_tenants.assert_not_awaited()


@pytest.mark.asyncio
async def test_quoted_hourly_card_with_explicit_hours_confirms_hour_list(
    monkeypatch, tmp_path
):
    """“每小时9点、10点…” previews the hour list and carries it into confirm."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    magik.resolve_tenant_queries.return_value = (
        [
            {"query": "tenant-fo", "tenant_id": "tenant-fo", "display_name": "佛跳墙"},
            {"query": "tenant-douzhi", "tenant_id": "tenant-douzhi", "display_name": "豆汁"},
        ],
        [],
    )
    tool = ReportCenterTool(
        ReportCenterToolConfig(
            cube_customer_model_hourly_tpm=True,
            cube_customer_model_hourly_tpm_subscription=True,
        ),
        _FakeCron(),
        magik,
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
        ),
    )
    store.save_message_reference(
        ReportMessageReference(
            channel="feishu",
            chat_id="chat-a",
            message_id="om-hourly-hours",
            run_id="run-hourly-hours",
            document_id="usage_customer_model_hourly_tpm",
            connector_id="magik_cube",
            template_id="usage_customer_model_hourly_tpm",
            period="recent1h",
            scope={
                "report_variant": "customer_model_hourly_tpm",
                "report_template_id": "usage_customer_model_hourly_tpm",
                "tenants": ["tenant-fo", "tenant-douzhi"],
                "tenant_labels": ["佛跳墙", "豆汁"],
                "model_scope": "all",
                "models": [],
                "report_selections": [
                    {"tenant_query": "tenant-fo", "model_scope": "all", "models": []},
                    {"tenant_query": "tenant-douzhi", "model_scope": "all", "models": []},
                ],
            },
            created_at="2026-09-15T00:00:00+00:00",
            expires_at="2099-09-15T00:00:00+00:00",
        )
    )

    preview_params = await tool.classify_referenced_subscription(
        "每小时9点、10点发送给我一次这个报表",
        MagicMock(),
        channel="feishu",
        chat_id="chat-a",
        reference_message_id="om-hourly-hours",
    )
    assert preview_params["action"] == "subscription_preview"
    assert preview_params["hours"] == [9, 10]

    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou-a",
        session_key="feishu:chat-a",
        metadata={"parent_id": "om-hourly-hours"},
    )
    with request_context(context):
        preview = await tool.execute(
            action="subscription_preview",
            report_type="inherit",
            tenant_scope="inherit",
            tenant_aliases=[],
            model_scope="inherit",
            models=[],
            recurrence="hourly",
            send_time="00:00",
            weekday=1,
            month_day=1,
            hours=[9, 10],
            inherit_report_scope=True,
            reference_message_id="om-hourly-hours",
        )

    ui = preview.metadata[OUTBOUND_META_AGENT_UI]
    # The confirmation card shows the hour-list cadence wording that
    # describe_subscription_schedule also renders in "我的订阅".
    markdown = next(
        block["data"]["content"]
        for block in ui["blocks"]
        if block["kind"] == "markdown"
    )
    assert "每天 9、10 点（整点后 5 分钟）" in markdown
    actions = [
        item
        for block in ui["blocks"]
        if block["kind"] == "actions"
        for item in block["data"]["actions"]
    ]
    confirm = next(
        (item for item in actions if item["action_id"] == "subscription_confirm"),
        None,
    )
    assert confirm is not None
    assert confirm["params"]["hours"] == [9, 10]
    assert confirm["params"]["period"] == "recent1h"


@pytest.mark.asyncio
async def test_feature_flag_override_gates_hourly_run_without_restart(
    monkeypatch, tmp_path
) -> None:
    """Store overrides flip execution gates on the next request, no restart."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        # The config leaves the hourly flag at its default-on value; only the
        # store override changes behavior.
        ReportCenterToolConfig(),
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(enable=True),
    )
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou-a",
        session_key="feishu:chat-a",
    )

    async def run_hourly():
        with request_context(context):
            return await tool.execute(
                action="customer_model_hourly_tpm",
                period="recent1h",
                tenants=["佛跳墙"],
                model_scope="all",
                interactive=False,
            )

    # Default-on: the gate passes and execution proceeds to catalog loading.
    result = await run_hourly()
    assert "hourly customer/model TPM report is not enabled" not in str(result)

    # Since the 2026-09-16 consolidation the per-template switch lives in the
    # always-enforced template policy (the runtime flag was retired): a
    # disabled row rejects the very next request.
    store.set_template_policy(
        "usage_customer_model_hourly_tpm",
        enabled=False,
        subscription_mode="all_authorized",
        updated_by="webui_admin",
        expected_revision=0,
    )
    result = await run_hourly()
    assert "hourly customer/model TPM report is not enabled" in str(result)

    # Re-enabling the policy restores execution instantly.
    store.set_template_policy(
        "usage_customer_model_hourly_tpm",
        enabled=True,
        subscription_mode="all_authorized",
        updated_by="webui_admin",
        expected_revision=1,
    )
    result = await run_hourly()
    assert "hourly customer/model TPM report is not enabled" not in str(result)


@pytest.mark.asyncio
async def test_subscription_parse_error_does_not_claim_reference_is_missing(
    monkeypatch, tmp_path
) -> None:
    """The user-facing recovery card must preserve the parser failure cause."""

    tool, _store, _cron = _tool(monkeypatch, tmp_path)
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou-a",
        session_key="feishu:chat-a",
    )

    with request_context(context):
        result = await tool.execute(
            action="subscription_parse_failed",
            reference_message_id="om-report",
            subscription_error="nlu_unavailable",
        )

    assert "发送计划未能识别" in str(result)
    assert "卡片恢复" not in str(result)
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
    assert "每天上午十点发送阳春面、豆汁、佛跳墙" in str(result)
    assert "引用日报卡片并回复" in str(result)


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
async def test_agent_loop_routes_quoted_report_to_subscription_confirmation(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    store.save_message_reference(
        ReportMessageReference(
            channel="feishu",
            chat_id="chat-a",
            message_id="om-report",
            run_id="run-a",
            document_id="usage_customer_model_daily_brief",
            connector_id="magik_cube",
            template_id="usage_customer_model_daily_brief",
            period="day",
            scope={
                "report_variant": "customer_model_daily_brief",
                "tenant_scope": "selected",
                "tenants": ["tenant-fo"],
                "model_scope": "all",
                "models": [],
                "report_selections": [
                    {"tenant_query": "tenant-fo", "model_scope": "all", "models": []}
                ],
                "report_template": "brief",
            },
            created_at="2026-09-02T00:00:00+00:00",
            expires_at="2099-09-02T00:00:00+00:00",
        )
    )
    magik = AsyncMock()
    magik.resolve_tenant_queries.return_value = (
        [
            {
                "query": "tenant-fo",
                "tenant_id": "tenant-fo",
                "display_name": "佛跳墙",
            }
        ],
        [],
    )
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
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    provider.chat = AsyncMock(
        return_value=LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCallRequest(
                    id="call-1",
                    name="emit_cube_subscription_intent",
                    arguments={
                        "report_type": "inherit",
                        "tenant_scope": "inherit",
                        "tenant_aliases": [],
                        "model_scope": "inherit",
                        "models": [],
                        "recurrence": "workdays",
                        "send_time": "10:00",
                        "weekday": 1,
                        "month_day": 1,
                        "inherit_report_scope": True,
                    },
                )
            ],
        )
    )
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
            sender_id="ou-a",
            chat_id="chat-a",
            content="我要订阅该报表，工作日上午十点发送给我",
            metadata={
                "parent_id": "om-report",
                "direct_request_text": "我要订阅该报表，工作日上午十点发送给我",
            },
        )
    )

    assert response is not None
    assert response.metadata[OUTBOUND_META_AGENT_UI]["title"] == "确认 Cube 报表订阅"
    assert "佛跳墙" in response.content
    # The unambiguous schedule is parsed deterministically; the LLM is only a
    # fallback for incomplete or ambiguous subscription language.
    provider.chat.assert_not_awaited()
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
async def test_structured_subscription_control_uses_revision_cas(
    monkeypatch, tmp_path
) -> None:
    """A stale card action cannot overwrite a newer subscription state."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    cron = CronService(tmp_path / "cron" / "jobs.json")
    tool = ReportCenterTool(ReportCenterToolConfig(), cron, AsyncMock())
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou_a",
        session_key="feishu:chat-a",
    )
    with request_context(context):
        await tool.execute(
            action="subscribe",
            period="day",
            report_params={"tenant_query": "tenant-a", "breakdown": "model"},
        )
    subscription = store.subscriptions("feishu", "ou_a")[0]

    with request_context(context):
        disabled = await tool.execute(
            action="subscription_disable",
            subscription_id=subscription.subscription_id,
            revision=subscription.revision,
        )
        stale = await tool.execute(
            action="subscription_enable",
            subscription_id=subscription.subscription_id,
            revision=subscription.revision,
        )

    assert not disabled.is_error
    assert store.subscription(subscription.subscription_id).enabled is False
    assert store.subscription(subscription.subscription_id).revision == 1
    assert stale.is_error
    assert "another operator" in str(stale)


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
async def test_hourly_subscribe_uses_service_and_shares_fingerprint(
    monkeypatch, tmp_path
) -> None:
    """Phase 3a: hourly chat confirmations create through the guided service.

    The subscription must carry the hourly template/variant markers and the
    clock-driven schedule, and an equivalent direct service create must hit
    the shared-fingerprint duplicate (one identity across entries).
    """

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    cron = _FakeCron()
    magik = AsyncMock()
    magik.resolve_tenant_queries.return_value = (
        [{"query": "tenant-a", "tenant_id": "tenant-a", "display_name": "客户A"}],
        [],
    )
    magik.resolve_models_for_tenants.return_value = ({"tenant-a": ["Kimi-K3"]}, [])
    tool = ReportCenterTool(
        ReportCenterToolConfig(),
        cron,
        magik,
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="",
        ),
    )
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou_a",
        session_key="feishu:chat-a",
    )
    hourly_params = {
        "report_variant": "customer_model_hourly_tpm",
        "report_template_id": "usage_customer_model_hourly_tpm",
        "report_template": "brief",
        "report_family": "usage",
        "subscription_period": "recent1h",
        "tenant_scope": "selected",
        "tenants": ["tenant-a"],
        "model_scope": "selected",
        "models": ["Kimi-K3"],
        "report_selections": [
            {"tenant_query": "tenant-a", "model_scope": "selected", "models": ["Kimi-K3"]}
        ],
    }
    with request_context(context):
        created = await tool.execute(
            action="subscribe",
            period="recent1h",
            send_time="10:00",
            report_params=hourly_params,
        )

    assert not created.is_error
    subscriptions = store.subscriptions("feishu", "ou_a")
    assert len(subscriptions) == 1
    subscription = subscriptions[0]
    assert subscription.template_id == "usage_customer_model_hourly_tpm"
    assert subscription.schedule == "5 * * * *"
    assert subscription.report_params["report_variant"] == "customer_model_hourly_tpm"
    assert (
        subscription.report_params["report_template_id"]
        == "usage_customer_model_hourly_tpm"
    )
    assert len(cron.jobs) == 1

    def resolve_tenants(values: list[str]):
        # Must return the same display name the chat-path catalog mock used:
        # tenant_labels are part of the fingerprint identity.
        return (
            [
                {"query": value, "tenant_id": value, "display_name": "客户A"}
                for value in values
            ],
            [],
        )

    def resolve_models(tenant_ids: list[str], models: list[str]):
        return {tenant_id: list(models) for tenant_id in tenant_ids}, []

    service = ReportSubscriptionService(
        config=SimpleNamespace(
            workspace_path=tmp_path,
            tools=SimpleNamespace(
                reporting=SimpleNamespace(
                    report_management_v1=True, timezone="Asia/Shanghai"
                )
            ),
            agents=SimpleNamespace(defaults=SimpleNamespace(unified_session=False)),
        ),
        store=store,
        registry=tool._registry,
        cron=cron,
        tenant_resolver=resolve_tenants,
        model_resolver=resolve_models,
    )
    with pytest.raises(SubscriptionServiceError, match="identical"):
        service.create(
            {
                "template_id": "usage_customer_model_hourly_tpm",
                "channel": "feishu",
                "chat_id": "chat-a",
                "user_id": "ou_a",
                "tenant_scope": "selected",
                "tenants": ["tenant-a"],
                "model_scope": "selected",
                "models": ["Kimi-K3"],
                "period": "recent1h",
                "recurrence": "hourly",
                "send_time": "10:00",
                "weekday": 1,
                "month_day": 1,
                "timezone": "Asia/Shanghai",
            },
            updated_by="ou_a",
        )
    # The duplicate attempt compensated its Cron side effect (_FakeCron
    # records removals without mutating the jobs list).
    assert cron.removed == [cron.jobs[1].id]
    assert len(store.subscriptions("feishu", "ou_a")) == 1


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


def test_with_delivery_metadata_wraps_plain_string_result() -> None:
    """Regression for the live 2026-09-15 server crash.

    The legacy Magik tool legally returns plain strings (ToolResult is a str
    subclass), so the delivery wrapper must normalize instead of assuming a
    ToolResult and raising AttributeError on ``metadata``.
    """

    result = ReportCenterTool._with_delivery_metadata(
        "legacy plain text",
        idempotency_key="sub-a:1:1",
        run_id="run-a",
        report_attempts=1,
    )

    assert str(result) == "legacy plain text"
    delivery = result.metadata[OUTBOUND_META_REPORT_DELIVERY]
    assert delivery["run_id"] == "run-a"
    assert delivery["report_attempts"] == 1
    assert delivery["idempotency_key"] == "sub-a:1:1"


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
async def test_multi_scope_all_models_empty_catalog_renders_no_usage_notice(
    monkeypatch, tmp_path
) -> None:
    """An all-idle window is successful empty data: reminder card, no error (2026-09-17).

    Previously the empty active-model discovery raised "请检查客户模型配置"
    as an error; a tenant with configured models but no usage in the window
    is now an informational no-usage notice instead.
    """

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

    assert result.is_error is False
    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    assert ui["title"] == "多客户多模型日报简报（本期无用量）"
    assert ui["quality"] == "complete"
    content = ui["blocks"][0]["data"]["content"]
    assert "tenant-a" in content
    assert "没有可用量的模型" in content
    assert "自动恢复完整报表" in content
    # The successful empty-data state never queries Cube and never records a
    # failed delivery.
    query.assert_not_awaited()


class _CatalogMagik:
    """Test double exposing the direct active-model discovery entrypoint.

    The real method lives on the concrete Cube tool's class, so the catalog
    loader's ``type(...)`` native check only passes for a real method — an
    AsyncMock attribute would fall through to the legacy selector path.
    """

    def __init__(self, active: dict[str, list[str]]) -> None:
        self._active = active

    async def list_active_models_for_tenants(
        self, tenant_ids, *, start_date, end_date
    ):
        return {tenant_id: list(self._active.get(tenant_id, [])) for tenant_id in tenant_ids}


def _hourly_all_model_subscription(subscription_id: str = "sub-hourly-idle") -> ReportSubscription:
    now = "2026-09-17T00:00:00+00:00"
    return ReportSubscription(
        subscription_id=subscription_id,
        channel="feishu",
        chat_id="chat-a",
        user_id="ou-a",
        connector_id="magik_cube",
        template_id="usage_customer_model_hourly_tpm",
        template_version="2.1",
        schedule="5 * * * *",
        timezone="Asia/Shanghai",
        report_params={
            "report_family": "usage",
            "report_template": "brief",
            "report_variant": "customer_model_hourly_tpm",
            "report_template_id": "usage_customer_model_hourly_tpm",
            "subscription_period": "recent1h",
            "tenant_scope": "selected",
            "tenants": ["tenant-a", "tenant-b"],
            "model_scope": "all",
            "models": [],
            "report_selections": [
                {"tenant_query": "tenant-a", "model_scope": "all", "models": []},
                {"tenant_query": "tenant-b", "model_scope": "all", "models": []},
            ],
        },
        cron_job_id="job-hourly-idle",
        enabled=True,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_all_idle_hourly_subscription_delivers_no_usage_notice(
    monkeypatch, tmp_path
) -> None:
    """A fully idle scheduled window delivers a reminder card, not an error."""
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(),
        _FakeCron(),
        _CatalogMagik(active={}),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            # Fixture-only config; no real credential is embedded.
            access_token="",
        ),
    )
    query = AsyncMock()
    monkeypatch.setattr(CubeConnector, "query", query)

    result = await tool._run_cube_subscription(
        _hourly_all_model_subscription(),
        run_id="run-idle",
        idempotency_key="delivery-idle",
    )

    assert result.is_error is False
    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    assert ui["title"] == "多客户多模型小时 TPM 报告（本期无用量）"
    assert ui["quality"] == "complete"
    content = ui["blocks"][0]["data"]["content"]
    assert "tenant-a" in content
    assert "tenant-b" in content
    assert "自动恢复完整报表" in content
    # The notice rides the normal delivery chain (idempotency metadata) and
    # never queries Cube or records a failed run.
    assert result.metadata[OUTBOUND_META_REPORT_DELIVERY]["idempotency_key"] == (
        "delivery-idle"
    )
    query.assert_not_awaited()
    runs = store.recent_runs("feishu", "ou-a")
    assert runs and runs[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_partially_idle_hourly_subscription_annotates_no_usage_tenants(
    monkeypatch, tmp_path
) -> None:
    """A partially idle window keeps every requested customer visible."""
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(),
        _FakeCron(),
        _CatalogMagik(active={"tenant-a": ["Kimi-K3"]}),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            # Fixture-only config; no real credential is embedded.
            access_token="",
        ),
    )

    async def fake_query(_connector, _query):
        return ReportDataset(
            rows=(
                {
                    "metric": "ai.tpm.peak",
                    "value": 900.0,
                    "model": "Kimi-K3",
                    "endpoint": "ep-k3",
                    "tenant_id": "tenant-a",
                },
                {
                    "metric": "ai.tpm.avg",
                    "value": 600.0,
                    "model": "Kimi-K3",
                    "endpoint": "ep-k3",
                    "tenant_id": "tenant-a",
                },
                {
                    "metric": "ai.machine.count",
                    "value": 3.0,
                    "model": "Kimi-K3",
                    "endpoint": "",
                    "tenant_id": "",
                    "metric_scope": "platform_model",
                },
            ),
            quality="complete",
            source="magik_cube",
            metadata={
                "tenant_models": {"tenant-a": ["Kimi-K3"]},
                "tenant_scope": "selected",
                "tenant_names": {},
                "window_start": "2026-09-17T10:00:00+08:00",
                "window_end": "2026-09-17T11:00:00+08:00",
            },
        )

    monkeypatch.setattr(CubeConnector, "query", fake_query)

    result = await tool._run_cube_subscription(
        _hourly_all_model_subscription(),
        run_id="run-partial-idle",
        idempotency_key="delivery-partial-idle",
    )

    assert result.is_error is False
    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    # The report renders for the active tenant...
    assert any(block["kind"] == "table" for block in ui["blocks"])
    # ...and the idle tenant stays explicitly listed instead of silently
    # disappearing (the one-customer-loss failure class).
    notes = [
        block for block in ui["blocks"]
        if block["kind"] == "note" and "本期无用量客户" in str(block["data"].get("content"))
    ]
    assert notes and "tenant-b" in notes[-1]["data"]["content"]
    assert "本期无用量客户：tenant-b" in str(result)


@pytest.mark.asyncio
async def test_interactive_hourly_single_idle_tenant_renders_notice(
    monkeypatch, tmp_path
) -> None:
    """“查看糖番茄上一小时TPM” with an idle tenant: reminder card, not error."""
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(),
        _FakeCron(),
        _CatalogMagik(active={}),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            # Fixture-only config; no real credential is embedded.
            access_token="",
        ),
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
            action="customer_model_hourly_tpm",
            period="recent1h",
            tenants=["tenant-idle"],
            model_scope="all",
            interactive=False,
        )

    assert result.is_error is False
    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    assert ui["title"] == "多客户多模型小时 TPM 报告（本期无用量）"
    content = ui["blocks"][0]["data"]["content"]
    assert "tenant-idle" in content
    assert "没有可用量的模型" in content
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


def test_cost_flags_are_runtime_switchable(monkeypatch, tmp_path) -> None:
    """Phase 2c: cost report/subscription gates read the runtime flags.

    The connector wiring (TokenAPI credential, template registration) is
    covered by the dedicated cost runner test above; this test pins only the
    store-override-over-configured-default resolution, which used to be a
    bare config read that the WebUI flags page could not toggle.
    """

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    monkeypatch.setattr(
        report_center_module.ReportCenterTool,
        "cost_connector_enabled",
        property(lambda self: True),
    )
    tool = ReportCenterTool(
        ReportCenterToolConfig(cube_cost_report=True),
        _FakeCron(),
        AsyncMock(),
    )

    assert tool.cost_reports_enabled is True
    assert tool.cost_subscriptions_enabled is False

    store.set_feature_flag("cube_cost_report", False, updated_by="webui_admin")
    assert tool.cost_reports_enabled is False
    store.set_feature_flag("cube_cost_subscription", True, updated_by="webui_admin")
    assert tool.cost_subscriptions_enabled is True

    store.clear_feature_flag("cube_cost_report")
    store.clear_feature_flag("cube_cost_subscription")
    assert tool.cost_reports_enabled is True
    assert tool.cost_subscriptions_enabled is False


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
        cube_health_report=True,
        cube_health_subscription=True,
        # Pin the v1 presentation so the status wording assertion below stays
        # deterministic; the v2 semantics now default on for new deployments.
        cube_health_semantics_v2=False,
        cube_health_card_v2=False,
        cube_ttft_detail=False,
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


@pytest.mark.asyncio
async def test_nlu_preview_preserves_three_customers_and_all_models(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(cube_multi_scope_brief=True),
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    tool._magik_tool.resolve_tenant_queries = AsyncMock(
        return_value=(
            [
                {"query": "阳春面", "tenant_id": "tenant-noodle", "display_name": "阳春面"},
                {"query": "豆汁", "tenant_id": "tenant-douzhi", "display_name": "豆汁"},
                {"query": "佛跳墙", "tenant_id": "tenant-fo", "display_name": "佛跳墙"},
            ],
            [],
        )
    )
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou-a",
        session_key="feishu:chat-a",
    )

    with request_context(context):
        preview = await tool.execute(
            action="subscription_preview",
            report_type="usage_customer_model_daily_brief",
            tenant_scope="selected",
            tenant_aliases=["阳春面", "豆汁", "佛跳墙"],
            model_scope="all",
            recurrence="every_day",
            send_time="10:00",
        )
        action = next(
            item
            for block in preview.metadata[OUTBOUND_META_AGENT_UI]["blocks"]
            if block["kind"] == "actions"
            for item in block["data"]["actions"]
            if item["action_id"] == "subscription_confirm"
        )
        created = await tool.execute(**action["params"])

    assert not created.is_error
    subscription = store.subscriptions("feishu", "ou-a")[0]
    assert subscription.template_id == "usage_customer_model_daily_brief"
    assert subscription.report_params["subscription_period"] == "day"
    assert subscription.report_params["model_scope"] == "all"
    assert subscription.report_params["tenants"] == [
        "tenant-noodle",
        "tenant-douzhi",
        "tenant-fo",
    ]
    assert all(
        item["model_scope"] == "all" and item["models"] == []
        for item in subscription.report_params["report_selections"]
    )


@pytest.mark.asyncio
async def test_agent_loop_subscription_phrase_cannot_fall_back_to_one_tenant_daily(
    monkeypatch, tmp_path
) -> None:
    """Protect the production route ordering for a long multi-tenant phrase."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    catalog = [
        {"tenant_id": "tenant-noodle", "display_name": "阳春面"},
        {"tenant_id": "tenant-douzhi", "display_name": "豆汁"},
        {"tenant_id": "tenant-fo", "display_name": "佛跳墙"},
    ]
    magik.find_tenant_mentions.return_value = catalog
    magik.resolve_tenant_queries.return_value = (
        [
            {"query": item["display_name"], **item}
            for item in catalog
        ],
        [],
    )
    monkeypatch.setattr(
        report_center_module,
        "classify_subscription_intent",
        AsyncMock(
            return_value=report_center_module.CubeSubscriptionIntent(
                report_type="usage_daily_brief",
                tenant_scope="selected",
                # Simulate the model returning only the last customer.
                tenant_aliases=("佛跳墙",),
                model_scope="selected",
                models=("Kimi-K3",),
                recurrence="every_day",
                send_time="10:00",
            )
        ),
    )
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

    phrase = "每天上午十点发送阳春面、豆汁、佛跳墙全部模型的多客户日报简报"
    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="ou-a",
            chat_id="chat-a",
            content=phrase,
        )
    )

    assert response is not None
    action = next(
        item
        for block in response.metadata[OUTBOUND_META_AGENT_UI]["blocks"]
        if block["kind"] == "actions"
        for item in block["data"]["actions"]
        if item["action_id"] == "subscription_confirm"
    )
    params = action["params"]["report_params"]
    assert action["params"]["period"] == "day"
    assert params["report_variant"] == "customer_model_daily_brief"
    assert params["tenants"] == ["tenant-noodle", "tenant-douzhi", "tenant-fo"]
    assert params["model_scope"] == "all"
    assert params["models"] == []
    assert provider.chat.call_count == 0
    await loop.close_mcp()


@pytest.mark.asyncio
async def test_quoted_report_preview_revalidates_scope_without_history_date(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(cube_multi_scope_brief=True),
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    store.save_message_reference(
        ReportMessageReference(
            channel="feishu",
            chat_id="chat-a",
            message_id="om-report",
            run_id="run-a",
            document_id="usage_customer_model_daily_brief",
            connector_id="magik_cube",
            template_id="usage_customer_model_daily_brief",
            period="day",
            scope={
                "report_variant": "customer_model_daily_brief",
                "tenant_scope": "selected",
                "tenants": ["tenant-fo"],
                "model_scope": "all",
                "models": [],
                "report_selections": [
                    {
                        "tenant_query": "tenant-fo",
                        "model_scope": "all",
                        "models": [],
                    }
                ],
                "report_template": "brief",
            },
            created_at="2026-09-02T00:00:00+00:00",
            expires_at="2099-09-02T00:00:00+00:00",
        )
    )
    tool._magik_tool.resolve_tenant_queries = AsyncMock(
        return_value=(
            [
                {
                    "query": "tenant-fo",
                    "tenant_id": "tenant-fo",
                    "display_name": "佛跳墙",
                }
            ],
            [],
        )
    )
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou-a",
        session_key="feishu:chat-a",
        metadata={"parent_id": "om-report"},
    )

    with request_context(context):
        preview = await tool.execute(
            action="subscription_preview",
            report_type="inherit",
            tenant_scope="inherit",
            model_scope="inherit",
            recurrence="workdays",
            send_time="10:00",
            inherit_report_scope=True,
            reference_message_id="om-report",
        )

    action = next(
        item
        for block in preview.metadata[OUTBOUND_META_AGENT_UI]["blocks"]
        if block["kind"] == "actions"
        for item in block["data"]["actions"]
        if item["action_id"] == "subscription_confirm"
    )
    report_params = action["params"]["report_params"]
    assert action["params"]["period"] == "day"
    assert action["params"]["daily_mode"] == "workdays"
    assert report_params["tenants"] == ["tenant-fo"]
    assert report_params["model_scope"] == "all"
    assert "start_date" not in report_params
    assert "end_date" not in report_params
    assert "历史日期不会固化" in str(preview)


@pytest.mark.asyncio
async def test_quoted_schedule_forces_reference_scope_when_classifier_defaults_to_daily(
    monkeypatch, tmp_path
) -> None:
    """A schedule-only quote must inherit the stored multi-scope report.

    This regression protects the trust boundary between the quoted card and
    the one-shot classifier: the classifier may return a generic daily intent,
    but it must not narrow a verified customer/model scope when the user did
    not explicitly mention a replacement entity.
    """

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(cube_multi_scope_brief=True),
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="fixture-token",
        ),
    )
    store.save_message_reference(
        ReportMessageReference(
            channel="feishu",
            chat_id="chat-a",
            message_id="om-grouped-report",
            run_id="run-grouped",
            document_id="usage_customer_model_daily_brief",
            connector_id="magik_cube",
            template_id="usage_customer_model_daily_brief",
            period="day",
            scope={
                "report_variant": "customer_model_daily_brief",
                "tenant_scope": "selected",
                "tenants": ["tenant-noodle", "tenant-douzhi", "tenant-fo"],
                "tenant_labels": ["阳春面", "豆汁", "佛跳墙"],
                "model_scope": "all",
                "models": [],
                "report_template": "brief",
            },
            created_at="2026-09-02T00:00:00+00:00",
            expires_at="2099-09-02T00:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        report_center_module,
        "classify_subscription_intent",
        AsyncMock(
            return_value=report_center_module.CubeSubscriptionIntent(
                report_type="usage_daily_brief",
                tenant_scope="selected",
                tenant_aliases=("佛跳墙",),
                model_scope="selected",
                models=("Kimi-K3",),
                recurrence="workdays",
                send_time="10:00",
            )
        ),
    )

    result = await tool.classify_referenced_subscription(
        "工作日上午十点发送给我",
        MagicMock(),
        channel="feishu",
        chat_id="chat-a",
        reference_message_id="om-grouped-report",
    )

    assert result is not None
    assert result["report_type"] == "inherit"
    assert result["tenant_scope"] == "inherit"
    assert result["model_scope"] == "inherit"
    assert result["tenant_aliases"] == []
    assert result["models"] == []
    assert result["inherit_report_scope"] is True


@pytest.mark.asyncio
async def test_subscription_preview_validates_selected_models_against_live_catalog(
    monkeypatch, tmp_path
) -> None:
    """Protect explicit model overrides from bypassing the live Cube catalog."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    magik.resolve_tenant_queries.return_value = (
        [
            {
                "query": "佛跳墙",
                "tenant_id": "tenant-fo",
                "display_name": "佛跳墙",
            }
        ],
        [],
    )
    magik.resolve_models_for_tenants.return_value = (
        {"tenant-fo": ["Kimi-K3"]},
        [],
    )
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
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou-a",
        session_key="feishu:chat-a",
    )

    with request_context(context):
        preview = await tool.execute(
            action="subscription_preview",
            report_type="usage_customer_model_daily_brief",
            tenant_scope="selected",
            tenant_aliases=["佛跳墙"],
            model_scope="selected",
            models=["K3"],
            recurrence="workdays",
            send_time="10:00",
        )

    action = next(
        item
        for block in preview.metadata[OUTBOUND_META_AGENT_UI]["blocks"]
        if block["kind"] == "actions"
        for item in block["data"]["actions"]
        if item["action_id"] == "subscription_confirm"
    )
    report_params = action["params"]["report_params"]
    magik.resolve_models_for_tenants.assert_awaited_once_with(
        ["tenant-fo"],
        ["K3"],
    )
    assert report_params["models"] == ["Kimi-K3"]
    assert report_params["report_selections"] == [
        {
            "tenant_query": "tenant-fo",
            "model_scope": "selected",
            "models": ["Kimi-K3"],
        }
    ]


@pytest.mark.asyncio
async def test_subscription_preview_rejects_models_missing_from_live_catalog(
    monkeypatch, tmp_path
) -> None:
    """A catalog miss must fail closed instead of creating a guessed subscription."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    magik.resolve_tenant_queries.return_value = (
        [
            {
                "query": "佛跳墙",
                "tenant_id": "tenant-fo",
                "display_name": "佛跳墙",
            }
        ],
        [],
    )
    magik.resolve_models_for_tenants.return_value = (
        {"tenant-fo": []},
        [
            {
                "tenant_id": "tenant-fo",
                "model": "不存在模型",
                "reason": "该客户实时模型目录中不存在此模型",
            }
        ],
    )
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
    context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="ou-a",
        session_key="feishu:chat-a",
    )

    with request_context(context):
        preview = await tool.execute(
            action="subscription_preview",
            report_type="usage_customer_model_daily_brief",
            tenant_scope="selected",
            tenant_aliases=["佛跳墙"],
            model_scope="selected",
            models=["不存在模型"],
            recurrence="workdays",
            send_time="10:00",
        )

    assert "指定模型无法通过实时目录校验" in str(preview)
    assert all(
        item["action_id"] != "subscription_confirm"
        for block in preview.metadata[OUTBOUND_META_AGENT_UI]["blocks"]
        if block["kind"] == "actions"
        for item in block["data"]["actions"]
    )
    assert store.subscriptions("feishu", "ou-a") == []


@pytest.mark.asyncio
async def test_all_model_multi_scope_subscription_refreshes_catalog_before_run(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    magik.execute.return_value = ToolResult(
        "models",
        metadata={
            OUTBOUND_META_AGENT_UI: {
                "kind": "magik_report_form",
                "phase": "models",
                "tenant_models": [
                    {"tenant_query": "tenant-fo", "models": ["Kimi-K3", "GLM-5.2"]}
                ],
            }
        },
    )
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
    queries = []

    async def fake_query(_connector, query):
        queries.append(query)
        return ReportDataset(rows=(), quality="complete", source="magik_cube")

    monkeypatch.setattr(CubeConnector, "query", fake_query)
    subscription = ReportSubscription(
        subscription_id="sub-all-models",
        channel="feishu",
        chat_id="chat-a",
        user_id="ou-a",
        connector_id="magik_cube",
        template_id="usage_customer_model_daily_brief",
        template_version="1.0",
        schedule="0 10 * * 1-5",
        timezone="Asia/Shanghai",
        report_params={
            "report_variant": "customer_model_daily_brief",
            "subscription_period": "day",
            "tenant_scope": "selected",
            "tenants": ["tenant-fo"],
            "model_scope": "all",
            "models": [],
            "report_selections": [
                {"tenant_query": "tenant-fo", "model_scope": "all", "models": []}
            ],
            "report_template": "brief",
        },
        cron_job_id="job-a",
        enabled=True,
        created_at="2026-09-02T00:00:00+00:00",
        updated_at="2026-09-02T00:00:00+00:00",
    )

    result = await tool._run_cube_subscription(
        subscription,
        run_id="run-all-models",
        idempotency_key="delivery-all-models",
    )

    magik.execute.assert_awaited_once()
    assert queries
    assert queries[0].filters["model_scope"] == "all"
    assert queries[0].filters["tenant_models"] == {
        "tenant-fo": ["Kimi-K3", "GLM-5.2"]
    }
    assert OUTBOUND_META_REPORT_REFERENCE in result.metadata


@pytest.mark.asyncio
async def test_subscription_classifier_truncation_is_repaired_from_live_catalog(
    monkeypatch, tmp_path
) -> None:
    """The original sentence must win when the LLM returns only one customer."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(cube_multi_scope_brief=True),
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            tenant_mappings={"佛跳墙": "tenant-fo"},
        ),
    )
    monkeypatch.setattr(
        report_center_module,
        "classify_subscription_intent",
        AsyncMock(
            return_value=report_center_module.CubeSubscriptionIntent(
                report_type="usage_daily_brief",
                tenant_scope="selected",
                tenant_aliases=("佛跳墙",),
                model_scope="all",
                models=(),
                recurrence="every_day",
                send_time="10:00",
            )
        ),
    )
    monkeypatch.setattr(
        tool,
        "_load_catalog_tenant_mentions",
        AsyncMock(
            return_value=(
                [
                    {"tenant_id": "tenant-noodle", "display_name": "阳春面", "matched_label": "阳春面"},
                    {"tenant_id": "tenant-douzhi", "display_name": "豆汁", "matched_label": "豆汁"},
                    {"tenant_id": "tenant-fo", "display_name": "佛跳墙", "matched_label": "佛跳墙"},
                ],
                True,
            )
        ),
    )

    result = await tool.classify_direct_request(
        "每天上午十点发送阳春面、豆汁、佛跳墙全部模型的多客户日报简报",
        MagicMock(),
    )

    assert result is not None
    assert result["report_type"] == "usage_customer_model_daily_brief"
    assert result["tenant_aliases"] == ["阳春面", "豆汁", "佛跳墙"]
    assert result["model_scope"] == "all"


@pytest.mark.asyncio
async def test_subscription_tenant_ambiguity_fails_closed(monkeypatch, tmp_path) -> None:
    """A shared display label cannot select an arbitrary live tenant."""

    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    tool = ReportCenterTool(
        ReportCenterToolConfig(),
        _FakeCron(),
        AsyncMock(),
        MagikCubeToolConfig(enable=True, base_url="https://cube.example.internal"),
    )
    monkeypatch.setattr(
        report_center_module,
        "classify_subscription_intent",
        AsyncMock(
            return_value=report_center_module.CubeSubscriptionIntent(
                report_type="usage_daily_brief",
                tenant_scope="selected",
                tenant_aliases=("同名客户",),
                model_scope="all",
                models=(),
                recurrence="every_day",
                send_time="10:00",
            )
        ),
    )
    monkeypatch.setattr(
        tool,
        "_load_catalog_tenant_mentions",
        AsyncMock(
            return_value=(
                [
                    {"tenant_id": "tenant-a", "display_name": "同名客户", "matched_label": "同名客户"},
                    {"tenant_id": "tenant-b", "display_name": "同名客户", "matched_label": "同名客户"},
                ],
                True,
            )
        ),
    )

    result = await tool.classify_direct_request("每天发送同名客户日报", MagicMock())

    assert result == {
        "action": "subscription_scope_failed",
        "subscription_error": "tenant_ambiguous",
        "tenant_ambiguous": True,
        "unresolved_tenants": ["同名客户"],
    }
