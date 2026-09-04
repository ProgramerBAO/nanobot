from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.magik_cube import MagikCubeToolConfig
from nanobot.cron.service import CronService
from nanobot.reporting import (
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportRunContext,
    ReportRunner,
    ReportStateStore,
    build_default_registry,
    create_report_state_store,
)
from nanobot.reporting.authorization import authorize_magik_params, template_id_for_magik_params
from nanobot.reporting.builtins import MultiCustomerModelDailyBriefTemplate, UsageMatrixTemplate
from nanobot.reporting.capabilities import capability_catalog
from nanobot.reporting.contracts import ReportContext, ReportSource, ReportWindow
from nanobot.reporting.registry import (
    ConnectorCapabilities,
    ConnectorManifest,
    ConnectorPlugin,
    ReportPluginRegistry,
    TemplateManifest,
    TemplatePlugin,
)
from nanobot.reporting.renderer import document_to_markdown
from nanobot.reporting.store import ReportMessageReference
from nanobot.reporting.subscriptions import ReportSubscriptionService, SubscriptionServiceError
from nanobot.reporting.templates import (
    load_builtin_template_specs,
    parse_template_spec,
)


def test_builtin_template_pack_is_versioned_and_compatible() -> None:
    specs = load_builtin_template_specs()
    assert {item.template_id for item in specs} == {
        "usage_custom_brief",
        "usage_custom_matrix",
        "usage_daily_brief",
        "usage_daily_matrix",
        "usage_monthly_brief",
        "usage_weekly_matrix",
        "usage_weekly_brief",
        "usage_monthly_matrix",
    }
    registry = build_default_registry(discover_external=False)
    assert {item.manifest.template_id for item in registry.compatible_templates("magik_cube")} == {
        "usage_custom_brief",
        "usage_custom_matrix",
        "usage_daily_brief",
        "usage_daily_matrix",
        "usage_monthly_brief",
        "usage_weekly_matrix",
        "usage_weekly_brief",
        "usage_monthly_matrix",
    }


def test_daily_brief_keeps_named_baselines_but_omits_detail_sections() -> None:
    spec = next(
        item for item in load_builtin_template_specs() if item.template_id == "usage_daily_brief"
    )
    document = UsageMatrixTemplate(spec, semantics_v2=True, presentation="brief").analyze(
        (
            ReportDataset(
                rows=(
                    {"period": "current", "date": "2026-08-31", "tenant": "a", "model": "Kimi-K3", "metric": "ai.usage.tokens", "value": 162},
                    {"period": "current", "date": "2026-08-31", "tenant": "a", "model": "Kimi-K3", "metric": "ai.requests", "value": 110},
                    {"period": "comparison", "date": "2026-08-30", "tenant": "a", "model": "Kimi-K3", "metric": "ai.usage.tokens", "value": 100},
                    {"period": "comparison", "date": "2026-08-30", "tenant": "a", "model": "Kimi-K3", "metric": "ai.requests", "value": 100},
                    {"period": "previous_week_same_day", "date": "2026-08-24", "tenant": "a", "model": "Kimi-K3", "metric": "ai.usage.tokens", "value": 137},
                    {"period": "previous_week_same_day", "date": "2026-08-24", "tenant": "a", "model": "Kimi-K3", "metric": "ai.requests", "value": 114},
                ),
                metadata={
                    "query_windows": [
                        {"period": "current", "start": "2026-08-31 00:00", "end": "2026-09-01 00:00"},
                        {"period": "comparison", "start": "2026-08-30 00:00", "end": "2026-08-31 00:00"},
                        {"period": "previous_week_same_day", "start": "2026-08-24 00:00", "end": "2026-08-25 00:00"},
                    ],
                    "scope": {
                        "all_tenants": True,
                        "tenant": "",
                        "model_scope": "selected",
                        "models": ["Kimi-K3"],
                    },
                },
            ),
        )
    )

    token = document.blocks[0].data["items"][0]
    assert token["comparisons"] == [
        {
            "key": "previous_week_same_day",
            "label": "同比",
            "baseline_value": 137.0,
            "change": "↑18.2%",
        },
        {
            "key": "previous_period",
            "label": "环比",
            "baseline_value": 100.0,
            "change": "↑62.0%",
        },
    ]
    assert document.title == "全部客户 Kimi-K3模型日报简报"
    assert document.context is not None
    assert [item.key for item in document.context.comparison_windows] == [
        "previous_period",
        "previous_week_same_day",
    ]
    assert {block.kind for block in document.blocks} == {"metrics", "note", "actions"}
    assert "对比（" not in document.fallback_text
    assert "同比基准：上周同期 2026-08-24" in document.fallback_text
    assert "环比基准：前一日 2026-08-30" in document.fallback_text
    action = next(block for block in document.blocks if block.kind == "actions").data["actions"][0]
    assert action["command"] == (
        "进一步分析（日报）：客户 全部客户，模型 Kimi-K3，"
        "日期 2026-08-31 至 2026-08-31"
    )
    actions = next(block for block in document.blocks if block.kind == "actions").data["actions"]
    assert len(actions) == 1
    assert all(not action["action_id"].startswith("usage_subscription_setup:") for action in actions)


def test_daily_brief_uses_display_alias_without_result_subscription_action() -> None:
    spec = next(
        item for item in load_builtin_template_specs() if item.template_id == "usage_daily_brief"
    )
    document = UsageMatrixTemplate(spec, semantics_v2=True, presentation="brief").analyze(
        (
            ReportDataset(
                rows=(
                    {
                        "period": "current",
                        "date": "2026-08-31",
                        "tenant": "tenant-baowjhsicyf65",
                        "model": "Kimi-K3",
                        "metric": "ai.usage.tokens",
                        "value": 100,
                    },
                ),
                metadata={
                    "scope": {
                        "tenant": "佛跳墙",
                        "tenant_id": "tenant-baowjhsicyf65",
                        "tenant_name": "佛跳墙",
                        "model_scope": "selected",
                        "models": ["Kimi-K3"],
                    }
                },
            ),
        )
    )

    assert document.title == "佛跳墙 Kimi-K3模型日报简报"
    actions = next(block for block in document.blocks if block.kind == "actions").data["actions"]
    assert len(actions) == 1
    assert actions[0]["action_id"] == "usage_further_analysis"


def test_multi_customer_model_brief_groups_aliases_and_named_changes() -> None:
    document = MultiCustomerModelDailyBriefTemplate().analyze(
        (
            ReportDataset(
                rows=(
                    {"period": "current", "tenant": "佛跳墙", "model": "Kimi-K3", "metric": "ai.usage.tokens", "value": 80},
                    {"period": "comparison", "tenant": "佛跳墙", "model": "Kimi-K3", "metric": "ai.usage.tokens", "value": 100},
                    {"period": "previous_week_same_day", "tenant": "佛跳墙", "model": "Kimi-K3", "metric": "ai.usage.tokens", "value": 64},
                    {"period": "current", "tenant": "豆汁", "model": "GLM-5.2", "metric": "ai.usage.tokens", "value": 167},
                    {"period": "comparison", "tenant": "豆汁", "model": "GLM-5.2", "metric": "ai.usage.tokens", "value": 100},
                    {"period": "previous_week_same_day", "tenant": "豆汁", "model": "GLM-5.2", "metric": "ai.usage.tokens", "value": 100},
                ),
                metadata={
                    "query_windows": [
                        {"period": "current", "start": "2026-09-01 00:00", "end": "2026-09-02 00:00"},
                        {"period": "comparison", "start": "2026-08-31 00:00", "end": "2026-09-01 00:00"},
                        {"period": "previous_week_same_day", "start": "2026-08-25 00:00", "end": "2026-08-26 00:00"},
                    ],
                    "scope": {
                        "tenant_names": ["佛跳墙", "豆汁"],
                        "models": ["Kimi-K3", "GLM-5.2", "DeepSeek-R1"],
                        "model_scope": "all",
                        "tenant_models": {
                            "佛跳墙": ["Kimi-K3", "DeepSeek-R1"],
                            "豆汁": ["GLM-5.2"],
                        },
                        "tenant_statuses": {"佛跳墙": "active", "豆汁": "active"},
                    },
                },
                quality="partial",
                warnings=("佛跳墙 current usage model=DeepSeek-R1 no_data",),
            ),
        )
    )

    grouped = next(block for block in document.blocks if block.kind == "grouped_metrics")
    assert [group["label"] for group in grouped.data["groups"]] == ["佛跳墙", "豆汁"]
    assert grouped.data["groups"][0]["items"][0]["comparisons"] == [
        {"key": "previous_week_same_day", "label": "同比", "change": "↑25.0%"},
        {"key": "previous_period", "label": "环比", "change": "↓20.0%"},
    ]
    markdown = document_to_markdown(document)
    assert "## 佛跳墙" in markdown
    assert "同比：↑25.0%" in markdown
    assert "DeepSeek-R1" not in markdown
    assert "2 个模型" in document.subtitle
    assert "已自动隐藏 1 个" in markdown
    assert "<summary>报表说明与数据质量</summary>" in markdown
    assert "时间桶" not in markdown
    assert "订阅" not in markdown
    note = next(block for block in document.blocks if block.kind == "note")
    assert note.data["collapsed"] is True
    assert note.data["include_context"] is True
    assert document.quality == "complete"
    assert document.warnings == ()


def test_multi_customer_model_brief_keeps_explicit_or_historical_models() -> None:
    """Explicit selections and historical activity must not disappear as idle models."""

    template = MultiCustomerModelDailyBriefTemplate()
    base_metadata = {
        "query_windows": [
            {"period": "current", "start": "2026-09-01 00:00", "end": "2026-09-02 00:00"},
        ],
        "scope": {
            "tenant_names": ["佛跳墙"],
            "models": ["DeepSeek-R1"],
            "tenant_models": {"佛跳墙": ["DeepSeek-R1"]},
            "tenant_statuses": {"佛跳墙": "active"},
        },
    }
    selected = template.analyze(
        (ReportDataset(rows=(), metadata=base_metadata, warnings=("no_business_data",)),)
    )
    selected_group = next(
        block for block in selected.blocks if block.kind == "grouped_metrics"
    )
    assert selected_group.data["groups"][0]["items"][0]["label"] == "DeepSeek-R1"

    all_scope_metadata = {
        **base_metadata,
        "scope": {**base_metadata["scope"], "model_scope": "all"},
    }
    historical = template.analyze(
        (
            ReportDataset(
                rows=(
                    {
                        "period": "comparison",
                        "tenant": "佛跳墙",
                        "model": "DeepSeek-R1",
                        "metric": "ai.usage.tokens",
                        "value": 100,
                    },
                ),
                metadata=all_scope_metadata,
            ),
        )
    )
    historical_group = next(
        block for block in historical.blocks if block.kind == "grouped_metrics"
    )
    assert historical_group.data["groups"][0]["items"][0]["label"] == "DeepSeek-R1"

    failed_metadata = {
        **all_scope_metadata,
        "scope": {
            **all_scope_metadata["scope"],
            "tenant_statuses": {"佛跳墙": "unavailable"},
        },
    }
    failed = template.analyze(
        (
            ReportDataset(
                rows=(),
                quality="missing",
                warnings=("connection_failed:佛跳墙 usage query failed",),
                metadata=failed_metadata,
            ),
        )
    )
    failed_group = next(block for block in failed.blocks if block.kind == "grouped_metrics")
    assert failed_group.data["groups"][0]["items"][0]["status"] == "unavailable"


def test_template_policy_uses_revision_and_subscription_grant(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    created = store.set_template_policy(
        "machine_tpm_peak",
        enabled=True,
        subscription_mode="allowlist",
        updated_by="test-admin",
        expected_revision=0,
    )

    assert created["revision"] == 1
    assert store.template_policy("machine_tpm_peak")["subscription_mode"] == "allowlist"
    with pytest.raises(ValueError, match="another operator"):
        store.set_template_policy(
            "machine_tpm_peak",
            enabled=False,
            subscription_mode="disabled",
            updated_by="stale-admin",
            expected_revision=0,
        )
    store.set_rbac_enabled(True)
    store.grant("feishu", "ou-a", "subscription_template", "machine_tpm_peak")
    assert store.allowed("feishu", "ou-a", "subscription_template", "machine_tpm_peak")


def test_declarative_template_rejects_executable_fields() -> None:
    with pytest.raises(ValueError, match="unsupported template fields"):
        parse_template_spec(
            {
                "schema_version": 1,
                "template_id": "usage_custom",
                "display_name": "Custom",
                "version": "1.0",
                "category": "usage",
                "period": "day",
                "metrics": ["ai.usage.tokens"],
                "dimensions": ["date"],
                "python_hook": "os.system('whoami')",
            }
        )


def test_brief_subscription_authorization_uses_period_template() -> None:
    assert template_id_for_magik_params(
        {
            "report_template": "brief",
            "subscription_period": "week",
            "tenant_query": "tenant-a",
        }
    ) == "usage_weekly_brief"


def test_report_store_onboarding_rbac_runs_and_subscriptions(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    assert not store.onboarding_seen("feishu", "ou_a", 1)
    store.mark_onboarding_seen("feishu", "ou_a", 1)
    assert store.onboarding_seen("feishu", "ou_a", 1)
    assert not store.onboarding_seen("feishu", "ou_a", 2)

    store.set_rbac_enabled(True)
    params = {
        "start_date": date(2026, 8, 25).isoformat(),
        "end_date": date(2026, 8, 25).isoformat(),
        "report_template": "matrix_card",
        "tenant_query": "tenant-a",
    }
    assert authorize_magik_params(
        store, channel="feishu", user_id="ou_a", params=params
    ) is not None
    for resource_type, resource_id in (
        ("connector", "magik_cube"),
        ("template", "usage_daily_matrix"),
        ("tenant", "tenant-a"),
    ):
        store.grant("feishu", "ou_a", resource_type, resource_id)
    assert authorize_magik_params(
        store, channel="feishu", user_id="ou_a", params=params
    ) is None

    store.record_run(
        run_id="run-1",
        channel="feishu",
        chat_id="chat-a",
        user_id="ou_a",
        connector_id="magik_cube",
        template_id="usage_daily_matrix",
        template_version="1.0",
        request={"tenant_query": "tenant-a"},
        status="ok",
        duration_ms=123,
    )
    row = store.recent_runs("feishu", "ou_a")[0]
    assert row["request"] == {"tenant_query": "tenant-a"}
    assert row["duration_ms"] == 123
    assert store.claim_delivery("sub-a:scheduled-a:1.0")
    assert not store.claim_delivery("sub-a:scheduled-a:1.0")
    store.complete_delivery("sub-a:scheduled-a:1.0", status="error")
    assert store.claim_delivery("sub-a:scheduled-a:1.0")


def test_unauthorized_capability_home_does_not_disclose_catalog(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    store.set_rbac_enabled(True)
    catalog = capability_catalog(
        build_default_registry(discover_external=False),
        store,
        channel="feishu",
        user_id="ou_denied",
    )
    assert [item.capability_id for item in catalog] == ["request_access"]
    assert "magik" not in catalog[0].description.casefold()


def test_postgres_backend_requires_secret_ref_environment(monkeypatch) -> None:
    monkeypatch.delenv("TEST_REPORTING_DSN", raising=False)
    with pytest.raises(RuntimeError, match="environment variable is not set"):
        create_report_state_store(
            backend="postgresql", postgres_dsn_env="TEST_REPORTING_DSN"
        )


def test_usage_semantics_v2_exposes_baseline_source_and_correct_tpm_meaning() -> None:
    spec = load_builtin_template_specs()[0]
    template = UsageMatrixTemplate(spec, semantics_v2=True)
    document = template.analyze(
        (
            ReportDataset(
                rows=(
                    {"period": "current", "date": "2026-08-27", "tenant": "a", "model": "m", "metric": "ai.usage.tokens", "value": 100},
                    {"period": "current", "date": "2026-08-27", "tenant": "a", "model": "m", "metric": "ai.requests", "value": 2},
                    {"period": "current", "date": "2026-08-27", "tenant": "a", "model": "m", "endpoint": "ep-a", "metric": "ai.tpm.avg", "value": 10},
                    {"period": "current", "date": "2026-08-27", "tenant": "a", "model": "m", "endpoint": "ep-a", "metric": "ai.tpm", "value": 20},
                    {"period": "current", "date": "2026-08-28", "tenant": "a", "model": "m", "metric": "ai.usage.tokens", "value": 300},
                    {"period": "current", "date": "2026-08-28", "tenant": "a", "model": "m", "metric": "ai.requests", "value": 3},
                    {"period": "current", "date": "2026-08-28", "tenant": "a", "model": "m", "endpoint": "ep-a", "metric": "ai.tpm.avg", "value": 30},
                    {"period": "current", "date": "2026-08-28", "tenant": "a", "model": "m", "endpoint": "ep-a", "metric": "ai.tpm", "value": 80},
                    {"period": "comparison", "date": "2026-08-26", "tenant": "a", "model": "m", "metric": "ai.usage.tokens", "value": 200},
                    {"period": "comparison", "date": "2026-08-26", "tenant": "a", "model": "m", "metric": "ai.requests", "value": 2},
                    {"period": "comparison", "date": "2026-08-26", "tenant": "a", "model": "m", "endpoint": "ep-a", "metric": "ai.tpm.avg", "value": 10},
                    {"period": "comparison", "date": "2026-08-26", "tenant": "a", "model": "m", "endpoint": "ep-a", "metric": "ai.tpm", "value": 40},
                ),
                metadata={
                    "query_windows": [
                        {"period": "current", "start": "2026-08-27 00:00", "end": "2026-08-29 00:00"},
                        {"period": "comparison", "start": "2026-08-25 00:00", "end": "2026-08-27 00:00"},
                    ],
                    "source_refs": [
                        {"system": "Cube Admin", "route": "analysis/active-tenant-daily-usage/query", "fields": ["totalTokens"]}
                    ],
                    "last_sample_at": "2026-08-28",
                },
            ),
        )
    )

    items = {item["metric"]: item for item in document.blocks[0].data["items"]}
    assert items["ai.usage.tokens"]["value"] == "400"
    assert items["ai.requests"]["value"] == "5"
    assert items["ai.tpm.avg"]["value"] == "20 tokens/min"
    assert items["ai.tpm.avg"]["detail"] == "有效样本日 2"
    assert items["ai.tpm"]["value"] == "80 tokens/min"
    assert items["ai.tpm"]["detail"] == ""
    assert items["ai.tpm"]["aggregation"] == "单 Endpoint 日峰值的窗口最大值"
    assert items["ai.usage.tokens"]["change"] == "↑100.0%"
    assert document.context is not None
    assert document.context.baseline_window == ReportWindow(
        "2026-08-25 00:00", "2026-08-27 00:00", "comparison"
    )
    assert document.context.sources[0] == ReportSource(
        "Cube Admin", "analysis/active-tenant-daily-usage/query", ("totalTokens",)
    )


def test_daily_usage_names_previous_day_and_weekly_comparisons() -> None:
    spec = next(
        item
        for item in load_builtin_template_specs()
        if item.template_id == "usage_daily_matrix"
    )
    template = UsageMatrixTemplate(spec, semantics_v2=True)
    query = template.plan(
        ReportIntent(
            connector_id="magik_cube",
            template_id="usage_daily_matrix",
            period="day",
            tenant="tenant-a",
            start_date=date(2026, 8, 29),
            end_date=date(2026, 8, 29),
        )
    )[0]

    assert query.comparison_start == date(2026, 8, 28)
    assert query.comparison_end == date(2026, 8, 28)
    assert len(query.additional_comparisons) == 1
    assert query.additional_comparisons[0].key == "previous_week_same_day"
    assert query.additional_comparisons[0].start_date == date(2026, 8, 22)

    document = template.analyze(
        (
            ReportDataset(
                rows=(
                    {"period": "current", "date": "2026-08-29", "tenant": "a", "model": "m", "metric": "ai.usage.tokens", "value": 300},
                    {"period": "comparison", "date": "2026-08-28", "tenant": "a", "model": "m", "metric": "ai.usage.tokens", "value": 200},
                    {"period": "previous_week_same_day", "date": "2026-08-22", "tenant": "a", "model": "m", "metric": "ai.usage.tokens", "value": 100},
                ),
                metadata={
                    "query_windows": [
                        {"period": "current", "start": "2026-08-29 00:00", "end": "2026-08-30 00:00"},
                        {"period": "comparison", "start": "2026-08-28 00:00", "end": "2026-08-29 00:00"},
                        {"period": "previous_week_same_day", "start": "2026-08-22 00:00", "end": "2026-08-23 00:00"},
                    ]
                },
            ),
        )
    )

    token = document.blocks[0].data["items"][0]
    assert token["comparisons"] == [
        {
            "key": "previous_period",
            "label": "较前一日（2026-08-28）",
            "baseline_value": 200.0,
            "baseline": "200",
            "change": "↑50.0%",
        },
        {
            "key": "previous_week_same_day",
            "label": "较上周同期（2026-08-22）",
            "baseline_value": 100.0,
            "baseline": "100",
            "change": "↑200.0%",
        },
    ]
    assert document.context is not None
    assert [item.label for item in document.context.comparison_windows] == [
        "前一日",
        "上周同期",
    ]
    markdown = document_to_markdown(document)
    assert "较前一日（2026-08-28）：↑50.0%" in markdown
    assert "较上周同期（2026-08-22）：↑200.0%" in markdown
    assert "基准 200" not in markdown


def test_average_tpm_is_not_aggregated_across_endpoints() -> None:
    spec = next(
        item
        for item in load_builtin_template_specs()
        if item.template_id == "usage_daily_matrix"
    )
    document = UsageMatrixTemplate(spec, semantics_v2=True).analyze(
        (
            ReportDataset(
                rows=(
                    {"period": "current", "date": "2026-08-29", "tenant": "a", "model": "m", "endpoint": "ep-a", "metric": "ai.tpm.avg", "value": 10},
                    {"period": "current", "date": "2026-08-29", "tenant": "a", "model": "m", "endpoint": "ep-a", "metric": "ai.tpm", "value": 20},
                    {"period": "current", "date": "2026-08-29", "tenant": "a", "model": "m", "endpoint": "ep-b", "metric": "ai.tpm.avg", "value": 30},
                    {"period": "current", "date": "2026-08-29", "tenant": "a", "model": "m", "endpoint": "ep-b", "metric": "ai.tpm", "value": 40},
                )
            ),
        )
    )

    metrics = {item["metric"]: item for item in document.blocks[0].data["items"]}
    assert metrics["ai.tpm.avg"]["value"] == "多 Endpoint/客户，不汇总"
    assert metrics["ai.tpm.avg"]["comparisons"] == []
    endpoint_table = next(
        block for block in document.blocks if block.data.get("title", "").startswith("Endpoint TPM")
    )
    assert [row["avg_tpm"] for row in endpoint_table.data["rows"]] == [
        "30 tokens/min",
        "10 tokens/min",
    ]


class _Connector(ConnectorPlugin):
    manifest = ConnectorManifest(
        connector_id="test_connector",
        display_name="Test",
        version="1.0",
        auth_methods=("none",),
        capabilities=ConnectorCapabilities(
            metrics=frozenset({"ai.usage.tokens"}),
            dimensions=frozenset({"date"}),
        ),
    )

    async def health_check(self):
        return {"status": "ok"}

    async def discover_catalog(self):
        return {}

    async def query(self, query):
        return ReportDataset(rows=({"date": "2026-08-25", "tokens": 1},))


class _Template(TemplatePlugin):
    manifest = TemplateManifest(
        template_id="test_daily",
        display_name="Test Daily",
        version="1.0",
        category="test",
        periods=frozenset({"day"}),
        required_metrics=frozenset({"ai.usage.tokens"}),
        required_dimensions=frozenset({"date"}),
    )

    def plan(self, intent):
        from nanobot.reporting import ReportQuery

        return (
            ReportQuery(
                connector_id=intent.connector_id,
                metrics=("ai.usage.tokens",),
                dimensions=("date",),
                start_date=intent.start_date,
                end_date=intent.end_date,
            ),
        )

    def analyze(self, datasets):
        return ReportDocument(title="Test", fallback_text="deterministic")


@pytest.mark.asyncio
async def test_report_runner_executes_connector_template_without_llm(tmp_path) -> None:
    registry = ReportPluginRegistry()
    registry.register_connector(_Connector())
    registry.register_template(_Template())
    store = ReportStateStore(tmp_path / "state.db")
    runner = ReportRunner(registry, store)
    outcome = await runner.run(
        ReportIntent(
            connector_id="test_connector",
            template_id="test_daily",
            period="day",
            start_date=date(2026, 8, 25),
            end_date=date(2026, 8, 25),
        ),
        ReportRunContext(
            channel="test",
            chat_id="chat",
            user_id="user",
            timezone="Asia/Shanghai",
            trace_id="trace-1",
            template_version="1.0",
        ),
    )
    assert outcome.document.fallback_text == "deterministic"
    assert outcome.quality == "complete"
    assert outcome.query_count == 1


@pytest.mark.asyncio
async def test_report_runner_persists_value_free_semantic_shadow_only(tmp_path) -> None:
    class _ShadowTemplate(_Template):
        def shadow_summary(self, datasets):
            return {
                "calculation_version": "cube-shadow-v1",
                "status": "drift",
                "compared_metrics": ["ai.ttft"],
                "differing_metrics": ["ai.ttft"],
                "untrusted_raw_value": "must-not-persist",
            }

    registry = ReportPluginRegistry()
    registry.register_connector(_Connector())
    registry.register_template(_ShadowTemplate())
    store = ReportStateStore(tmp_path / "state.db")
    outcome = await ReportRunner(registry, store, semantic_shadow_enabled=True).run(
        ReportIntent(
            connector_id="test_connector",
            template_id="test_daily",
            period="day",
            start_date=date(2026, 8, 25),
            end_date=date(2026, 8, 25),
        ),
        ReportRunContext(
            channel="test",
            chat_id="chat",
            user_id="user",
            timezone="Asia/Shanghai",
            trace_id="trace-shadow",
            template_version="1.0",
        ),
    )

    run = store.recent_runs("test", "user")[0]
    assert outcome.semantic_shadow == {
        "calculation_version": "cube-shadow-v1",
        "status": "drift",
        "compared_metrics": ["ai.ttft"],
        "differing_metrics": ["ai.ttft"],
    }
    assert run["request"]["semantic_shadow"] == outcome.semantic_shadow
    assert "must-not-persist" not in str(run)


@pytest.mark.asyncio
async def test_report_runner_persists_safe_context_only(tmp_path) -> None:
    class _ContextConnector(_Connector):
        async def query(self, query):
            return ReportDataset(
                rows=(),
                metadata={"raw_response": {"token": "must-not-persist"}},
            )

    class _ContextTemplate(_Template):
        def analyze(self, datasets):
            return ReportDocument(
                title="Context",
                context=ReportContext(
                    timezone="Asia/Shanghai",
                    current_window=ReportWindow("2026-08-27", "2026-08-28"),
                    sources=(ReportSource("Cube Admin", "safe/route"),),
                    calculation_version="2",
                ),
            )

    registry = ReportPluginRegistry()
    registry.register_connector(_ContextConnector())
    registry.register_template(_ContextTemplate())
    store = ReportStateStore(tmp_path / "state.db")
    runner = ReportRunner(registry, store)
    await runner.run(
        ReportIntent(
            connector_id="test_connector",
            template_id="test_daily",
            period="day",
            start_date=date(2026, 8, 27),
            end_date=date(2026, 8, 27),
        ),
        ReportRunContext(
            channel="test",
            chat_id="chat",
            user_id="user",
            timezone="Asia/Shanghai",
            trace_id="trace-context",
            template_version="2.0",
        ),
    )

    run = store.recent_runs("test", "user")[0]
    assert run["request"]["report_context"]["quality"] == "complete"
    assert run["request"]["report_context"]["sources"] == [
        {"system": "Cube Admin", "route": "safe/route", "fields": []}
    ]
    assert "raw_response" not in run["request"]
    assert "must-not-persist" not in str(run)


def test_report_message_reference_is_chat_bound_and_expires(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    now = datetime.now(UTC)
    reference = ReportMessageReference(
        channel="feishu",
        chat_id="chat-a",
        message_id="om-report-1",
        run_id="run-1",
        document_id="usage_customer_model_daily_brief",
        connector_id="magik_cube",
        template_id="usage_customer_model_daily_brief",
        period="day",
        scope={
            "tenants": ["tenant-a"],
            "model_scope": "all",
            "models": [],
        },
        created_at=now.isoformat(),
        expires_at=(now + timedelta(days=1)).isoformat(),
    )

    store.save_message_reference(reference)

    assert store.message_reference(
        channel="feishu", chat_id="chat-a", message_id="om-report-1"
    ) == reference
    assert (
        store.message_reference(
            channel="feishu", chat_id="chat-b", message_id="om-report-1"
        )
        is None
    )

    store.save_message_reference(
        replace(
            reference,
            message_id="om-expired",
            expires_at=(now - timedelta(seconds=1)).isoformat(),
        )
    )
    assert (
        store.message_reference(
            channel="feishu", chat_id="chat-a", message_id="om-expired"
        )
        is None
    )


def test_guided_subscription_resolves_display_alias_to_live_cube_id(tmp_path) -> None:
    """The guided control plane stores the live Cube ID, never a synthetic alias."""

    store = ReportStateStore(tmp_path / "state.db")
    cube_config = MagikCubeToolConfig(
        enable=True,
        base_url="https://cube.example.internal",
        access_token="fixture-token",
    )
    registry = build_default_registry(
        discover_external=False,
        magik_enabled=True,
        cube_config=cube_config,
        cube_multi_scope_brief_enabled=True,
    )
    config = SimpleNamespace(
        workspace_path=tmp_path,
        tools=SimpleNamespace(
            reporting=SimpleNamespace(
                report_management_v1=False,
                timezone="Asia/Shanghai",
            )
        ),
        agents=SimpleNamespace(defaults=SimpleNamespace(unified_session=False)),
    )

    def resolve_tenants(values: list[str]):
        assert values == ["佛跳墙"]
        return (
            [{"query": "佛跳墙", "tenant_id": "tenant-fo", "display_name": "佛跳墙"}],
            [],
        )

    service = ReportSubscriptionService(
        config=config,
        store=store,
        registry=registry,
        cron=CronService(tmp_path / "cron" / "jobs.json"),
        tenant_resolver=resolve_tenants,
    )
    compiled = service.compile_form(
        {
            "template_id": "usage_customer_model_daily_brief",
            "channel": "feishu",
            "chat_id": "chat-a",
            "user_id": "ou-a",
            "tenant_scope": "selected",
            "tenants": ["佛跳墙"],
            "model_scope": "all",
            "models": [],
            "period": "day",
            "recurrence": "every_day",
            "send_time": "10:00",
            "weekday": 1,
            "month_day": 1,
            "timezone": "Asia/Shanghai",
        }
    )

    assert compiled.report_params["tenants"] == ["tenant-fo"]
    assert compiled.report_params["tenant_query"] == "tenant-fo"
    assert compiled.tenant_names == ("佛跳墙",)


def test_guided_subscription_rejects_incomplete_model_catalog_response(tmp_path) -> None:
    """A missing tenant key cannot silently broaden or narrow a model scope."""

    store = ReportStateStore(tmp_path / "state.db")
    registry = build_default_registry(discover_external=False, magik_enabled=True)
    config = SimpleNamespace(
        workspace_path=tmp_path,
        tools=SimpleNamespace(
            reporting=SimpleNamespace(
                report_management_v1=False,
                timezone="Asia/Shanghai",
            )
        ),
        agents=SimpleNamespace(defaults=SimpleNamespace(unified_session=False)),
    )

    def resolve_tenants(values: list[str]):
        return (
            [
                {"query": value, "tenant_id": value, "display_name": value}
                for value in values
            ],
            [],
        )

    def resolve_models(tenant_ids: list[str], models: list[str]):
        assert tenant_ids == ["tenant-a", "tenant-b"]
        assert models == ["Kimi-K3"]
        return {"tenant-a": ["Kimi-K3"]}, []

    service = ReportSubscriptionService(
        config=config,
        store=store,
        registry=registry,
        tenant_resolver=resolve_tenants,
        model_resolver=resolve_models,
    )

    with pytest.raises(SubscriptionServiceError, match="完整验证"):
        service.compile_form(
            {
                "template_id": "usage_daily_brief",
                "channel": "feishu",
                "chat_id": "chat-a",
                "user_id": "ou-a",
                "tenant_scope": "selected",
                "tenants": ["tenant-a", "tenant-b"],
                "model_scope": "selected",
                "models": ["Kimi-K3"],
                "period": "day",
                "recurrence": "workdays",
                "send_time": "10:00",
                "timezone": "Asia/Shanghai",
            }
        )


def test_sqlite_reporting_migration_adds_cas_columns_without_rewriting_rows(tmp_path) -> None:
    """Existing subscription/policy rows survive the additive schema migration."""

    path = tmp_path / "legacy-reporting.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE report_subscriptions (
                subscription_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                template_id TEXT NOT NULL,
                template_version TEXT NOT NULL,
                schedule TEXT NOT NULL,
                timezone TEXT NOT NULL,
                report_params_json TEXT NOT NULL,
                cron_job_id TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(channel, user_id, fingerprint)
            );
            CREATE TABLE report_template_policies (
                template_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL,
                subscription_mode TEXT NOT NULL,
                revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            );
            INSERT INTO report_subscriptions VALUES (
                'legacy-sub', 'feishu', 'chat-a', 'ou-a', 'magik_cube',
                'usage_daily_brief', '1.0', '0 9 * * *', 'Asia/Shanghai',
                '{"report_template":"brief"}', 'job-a', 1, 'fp-a',
                '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00'
            );
            INSERT INTO report_template_policies VALUES (
                'usage_daily_brief', 1, 'all_authorized', 3,
                '2026-09-01T00:00:00+00:00', 'legacy-admin'
            );
            """
        )

    store = ReportStateStore(path)
    with sqlite3.connect(path) as db:
        subscription_columns = {
            row[1] for row in db.execute("PRAGMA table_info(report_subscriptions)")
        }
        policy_columns = {
            row[1] for row in db.execute("PRAGMA table_info(report_template_policies)")
        }
    assert "revision" in subscription_columns
    assert "show_subscription_button" in policy_columns
    assert store.subscription("legacy-sub").revision == 0
    assert store.template_policy("usage_daily_brief")["revision"] == 3
    assert store.template_policy("usage_daily_brief")["show_subscription_button"] is True
