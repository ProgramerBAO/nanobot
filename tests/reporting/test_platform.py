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
from nanobot.reporting.capabilities import capability_catalog
from nanobot.reporting.registry import (
    ConnectorCapabilities,
    ConnectorManifest,
    ConnectorPlugin,
    ReportPluginRegistry,
    TemplateManifest,
    TemplatePlugin,
)
from nanobot.reporting.templates import load_builtin_template_specs, parse_template_spec


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
