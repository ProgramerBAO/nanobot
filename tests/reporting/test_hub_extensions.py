from __future__ import annotations

from datetime import date
from json import loads

import httpx
import pytest

from nanobot.reporting import (
    DeliveryRouter,
    DingTalkReportRenderer,
    FeishuReportRenderer,
    GrafanaConnector,
    GrafanaConnectorConfig,
    GrafanaQueryDefinition,
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportPluginRegistry,
    ReportQuery,
    ReportRunContext,
    ReportRunner,
    ReportStateStore,
    SecretRef,
    WeComReportRenderer,
    build_default_registry,
    split_message,
)
from nanobot.reporting.contracts import ReportBlock
from nanobot.reporting.registry import (
    ConnectorCapabilities,
    ConnectorManifest,
    ConnectorPlugin,
    TemplateManifest,
    TemplatePlugin,
)


def _grafana_config() -> GrafanaConnectorConfig:
    return GrafanaConnectorConfig(
        base_url="https://grafana.example.internal",
        datasource_uid="prometheus",
        query_definitions=(
            GrafanaQueryDefinition(
                query_id="error-rate",
                metric="ai.error_rate",
                expression='sum(rate(requests_total{tenant=~"{{tenant}}"}[5m]))',
                dimensions=("tenant", "date"),
                allowed_filters=frozenset({"tenant"}),
            ),
        ),
        service_account_token=SecretRef(provider="env", key="GRAFANA_TOKEN"),
        max_retries=1,
        retry_backoff_seconds=0,
    )


@pytest.mark.asyncio
async def test_grafana_connector_uses_approved_query_and_normalizes_frames(monkeypatch) -> None:
    monkeypatch.setenv("GRAFANA_TOKEN", "fixture-value")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": {
                    "Q0": {
                        "frames": [
                            {
                                "schema": {
                                    "fields": [
                                        {"name": "Time", "type": "time"},
                                        {"name": "Value", "type": "number", "labels": {"tenant": "tenant-a"}},
                                    ]
                                },
                                "data": {"values": [[1724544000000], [0.25]]},
                            }
                        ]
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        connector = GrafanaConnector(_grafana_config(), http_client=client)
        result = await connector.query(
            ReportQuery(
                connector_id="grafana",
                metrics=("ai.error_rate",),
                dimensions=("tenant", "date"),
                start_date=date(2026, 8, 25),
                end_date=date(2026, 8, 25),
                filters={"tenant": "tenant-a"},
            )
        )
    finally:
        await client.aclose()

    assert result.quality == "complete"
    assert result.rows == (
        {
            "metric": "ai.error_rate",
            "value": 0.25,
            "timestamp": 1724544000000,
            "tenant": "tenant-a",
        },
    )
    payload = loads(requests[0].content)
    expression = payload["queries"][0]["model"]["expr"]
    assert "fixture-value" not in str(payload)
    assert r"tenant\-a" in expression


@pytest.mark.asyncio
async def test_grafana_connector_retries_429_and_rejects_unapproved_queries(monkeypatch) -> None:
    monkeypatch.setenv("GRAFANA_TOKEN", "fixture-value")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"message": "busy"})
        return httpx.Response(200, json={"results": {"Q0": {}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        connector = GrafanaConnector(_grafana_config(), http_client=client)
        result = await connector.query(
            ReportQuery(
                connector_id="grafana",
                metrics=("ai.error_rate",),
                dimensions=("date",),
                start_date=date(2026, 8, 25),
                end_date=date(2026, 8, 25),
            )
        )
        with pytest.raises(ValueError, match="not approved"):
            await connector.query(
                ReportQuery(
                    connector_id="grafana",
                    metrics=("ai.error_rate",),
                    dimensions=("date",),
                    start_date=date(2026, 8, 25),
                    end_date=date(2026, 8, 25),
                    query_id="arbitrary-promql",
                )
            )
    finally:
        await client.aclose()

    assert calls == 2
    assert result.quality == "complete"


@pytest.mark.asyncio
async def test_grafana_connector_escapes_selected_models_as_regex_alternatives(monkeypatch) -> None:
    monkeypatch.setenv("GRAFANA_TOKEN", "fixture-value")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": {"Q0": {}}})

    config = GrafanaConnectorConfig(
        base_url="https://grafana.example.internal",
        datasource_uid="prometheus",
        query_definitions=(
            GrafanaQueryDefinition(
                query_id="model-requests",
                metric="ai.requests",
                expression='sum(rate(requests_total{model=~"{{model}}"}[5m]))',
                dimensions=("model", "date"),
                allowed_filters=frozenset({"model"}),
            ),
        ),
        service_account_token=SecretRef(provider="env", key="GRAFANA_TOKEN"),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        connector = GrafanaConnector(config, http_client=client)
        await connector.query(
            ReportQuery(
                connector_id="grafana",
                metrics=("ai.requests",),
                dimensions=("model", "date"),
                start_date=date(2026, 8, 25),
                end_date=date(2026, 8, 25),
                filters={"model_scope": "selected", "models": ["model-a", "model.b"]},
            )
        )
    finally:
        await client.aclose()

    expression = loads(requests[0].content)["queries"][0]["model"]["expr"]
    assert r'model=~"model\-a|model\.b"' in expression


def test_renderer_capabilities_and_message_split() -> None:
    document = ReportDocument(
        title="健康报告",
        subtitle="固定指标",
        blocks=(
            ReportBlock("metrics", {"items": [{"label": "错误率", "value": 0.01}]}),
        ),
    )
    wecom = WeComReportRenderer().render(document)
    dingtalk = DingTalkReportRenderer().render(document)
    assert "错误率" in wecom.content
    assert "错误率" in dingtalk.content
    feishu = FeishuReportRenderer().render(document)
    assert feishu.metadata["_agent_ui"]["kind"] == "report_document"
    assert len(split_message("a" * 500, 100)) == 5


class _FailOnceTransport:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, msg) -> None:
        self.messages.append(msg)
        if len(self.messages) == 1:
            raise RuntimeError("temporary")


@pytest.mark.asyncio
async def test_delivery_router_retries_and_suppresses_duplicate(tmp_path) -> None:
    transport = _FailOnceTransport()
    store = ReportStateStore(tmp_path / "state.db")
    router = DeliveryRouter(
        build_default_registry(discover_external=False, magik_enabled=False),
        store,
        {"dingtalk": transport},
        max_attempts=2,
        retry_backoff_seconds=0,
    )
    document = ReportDocument(title="Report", document_id="report-1", fallback_text="hello")

    first = await router.deliver(
        document,
        channel_id="dingtalk",
        chat_id="chat-1",
        idempotency_key="delivery-1",
    )
    second = await router.deliver(
        document,
        channel_id="dingtalk",
        chat_id="chat-1",
        idempotency_key="delivery-1",
    )

    assert first.status == "ok"
    assert first.attempts == 2
    assert second.status == "duplicate"
    assert len(transport.messages) == 2


class _ShardConnector(ConnectorPlugin):
    manifest = ConnectorManifest(
        connector_id="shard_connector",
        display_name="Shard",
        version="1.0",
        auth_methods=("none",),
        capabilities=ConnectorCapabilities(
            metrics=frozenset({"ai.requests"}),
            dimensions=frozenset({"date"}),
            max_window_days=2,
        ),
    )

    def __init__(self) -> None:
        self.queries = []

    async def health_check(self):
        return {"status": "ok"}

    async def discover_catalog(self):
        return {}

    async def query(self, query):
        self.queries.append(query)
        return ReportDataset(rows=({"metric": "ai.requests", "value": 1},))


class _ShardTemplate(TemplatePlugin):
    manifest = TemplateManifest(
        template_id="shard_report",
        display_name="Shard",
        version="1.0",
        category="test",
        periods=frozenset({"range"}),
        required_metrics=frozenset({"ai.requests"}),
        required_dimensions=frozenset({"date"}),
    )

    def plan(self, intent):
        return (
            ReportQuery(
                connector_id="shard_connector",
                metrics=("ai.requests",),
                dimensions=("date",),
                start_date=intent.start_date,
                end_date=intent.end_date,
            ),
        )

    def analyze(self, datasets):
        return ReportDocument(title="Shard", fallback_text="sharded")


@pytest.mark.asyncio
async def test_report_runner_shards_large_windows_and_preserves_quality(tmp_path) -> None:
    connector = _ShardConnector()
    registry = ReportPluginRegistry()
    registry.register_connector(connector)
    registry.register_template(_ShardTemplate())
    runner = ReportRunner(registry, ReportStateStore(tmp_path / "state.db"))
    outcome = await runner.run(
        ReportIntent(
            connector_id="shard_connector",
            template_id="shard_report",
            period="range",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 5),
        ),
        ReportRunContext(
            channel="test",
            chat_id="chat",
            user_id="user",
            timezone="Asia/Shanghai",
            trace_id="trace-shard",
            template_version="1.0",
        ),
    )

    assert outcome.query_count == 3
    assert [(item.start_date, item.end_date) for item in connector.queries] == [
        (date(2026, 8, 1), date(2026, 8, 2)),
        (date(2026, 8, 3), date(2026, 8, 4)),
        (date(2026, 8, 5), date(2026, 8, 5)),
    ]
    assert outcome.quality == "complete"


@pytest.mark.asyncio
async def test_report_runner_authorizes_scope_filters(tmp_path) -> None:
    registry = ReportPluginRegistry()
    registry.register_connector(_ShardConnector())
    registry.register_template(_ShardTemplate())
    store = ReportStateStore(tmp_path / "state.db")
    store.set_rbac_enabled(True)
    store.grant("test", "user", "connector", "shard_connector")
    store.grant("test", "user", "template", "shard_report")
    runner = ReportRunner(registry, store)

    with pytest.raises(PermissionError, match="denied"):
        await runner.run(
            ReportIntent(
                connector_id="shard_connector",
                template_id="shard_report",
                period="range",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 1),
                filters={"tenant": "unapproved-tenant"},
            ),
            ReportRunContext(
                channel="test",
                chat_id="chat",
                user_id="user",
                timezone="Asia/Shanghai",
                trace_id="trace-rbac",
                template_version="1.0",
            ),
        )
