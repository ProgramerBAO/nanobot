from __future__ import annotations

from datetime import date

import pytest

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
from nanobot.reporting.authorization import authorize_magik_params
from nanobot.reporting.builtins import UsageMatrixTemplate
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
from nanobot.reporting.templates import (
    load_builtin_template_specs,
    parse_template_spec,
)


def test_builtin_template_pack_is_versioned_and_compatible() -> None:
    specs = load_builtin_template_specs()
    assert {item.template_id for item in specs} == {
        "usage_daily_matrix",
        "usage_weekly_matrix",
        "usage_monthly_matrix",
    }
    registry = build_default_registry(discover_external=False)
    assert {item.manifest.template_id for item in registry.compatible_templates("magik_cube")} == {
        "usage_daily_matrix",
        "usage_weekly_matrix",
        "usage_monthly_matrix",
    }


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
                    {"period": "current", "date": "2026-08-27", "tenant": "a", "model": "m", "metric": "ai.tpm", "value": 20},
                    {"period": "current", "date": "2026-08-28", "tenant": "a", "model": "m", "metric": "ai.usage.tokens", "value": 300},
                    {"period": "current", "date": "2026-08-28", "tenant": "a", "model": "m", "metric": "ai.requests", "value": 3},
                    {"period": "current", "date": "2026-08-28", "tenant": "a", "model": "m", "metric": "ai.tpm", "value": 80},
                    {"period": "comparison", "date": "2026-08-26", "tenant": "a", "model": "m", "metric": "ai.usage.tokens", "value": 200},
                    {"period": "comparison", "date": "2026-08-26", "tenant": "a", "model": "m", "metric": "ai.requests", "value": 2},
                    {"period": "comparison", "date": "2026-08-26", "tenant": "a", "model": "m", "metric": "ai.tpm", "value": 40},
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
    assert items["ai.tpm"]["value"] == "80 tokens/min"
    assert items["ai.tpm"]["detail"] == "平均日峰值 50 tokens/min"
    assert items["ai.tpm"]["aggregation"] == "日峰值的窗口峰值"
    assert items["ai.usage.tokens"]["change"] == "+100.0%"
    assert document.context is not None
    assert document.context.baseline_window == ReportWindow(
        "2026-08-25 00:00", "2026-08-27 00:00", "comparison"
    )
    assert document.context.sources[0] == ReportSource(
        "Cube Admin", "analysis/active-tenant-daily-usage/query", ("totalTokens",)
    )


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
