"""Built-in report catalog used by Capability Home and compatibility adapters."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from nanobot.reporting.contracts import ReportDataset, ReportIntent, ReportQuery
from nanobot.reporting.registry import (
    ConnectorCapabilities,
    ConnectorManifest,
    ConnectorPlugin,
    ReportPluginRegistry,
    TemplateManifest,
    TemplatePlugin,
)
from nanobot.reporting.renderer import TextChannelRenderer
from nanobot.reporting.templates import DeclarativeTemplateSpec, load_builtin_template_specs

_USAGE_METRICS = frozenset({"ai.usage.tokens", "ai.requests", "ai.tpm"})
_USAGE_DIMENSIONS = frozenset({"tenant", "model", "date"})


class MagikCubeConnector(ConnectorPlugin):
    """Manifest adapter for the existing, separately executed read-only Tool."""

    manifest = ConnectorManifest(
        connector_id="magik_cube",
        display_name="Magik Cube",
        version="1.0",
        auth_methods=("bearer", "password"),
        capabilities=ConnectorCapabilities(
            metrics=_USAGE_METRICS,
            dimensions=_USAGE_DIMENSIONS,
            max_window_days=90,
        ),
        secret_fields=frozenset({"token", "password"}),
        allowed_hosts=("www.magikcloud.cn",),
    )

    async def health_check(self) -> dict[str, Any]:
        return {"status": "delegated", "tool": "magik_cube_daily_report"}

    async def discover_catalog(self) -> dict[str, list[str]]:
        return {"status": ["delegated_to_magik_cube_tool"]}

    async def query(self, query: ReportQuery) -> ReportDataset:
        raise RuntimeError("Magik Cube queries are executed through the compatibility Tool adapter")


class UsageMatrixTemplate(TemplatePlugin):
    def __init__(self, spec: DeclarativeTemplateSpec) -> None:
        self._spec = spec
        self.manifest = TemplateManifest(
            template_id=spec.template_id,
            display_name=spec.display_name,
            version=spec.version,
            category=spec.category,
            periods=frozenset({spec.period}),
            required_metrics=frozenset(spec.metrics),
            required_dimensions=frozenset(spec.dimensions),
            description=spec.description,
        )

    def plan(self, intent: ReportIntent) -> tuple[ReportQuery, ...]:
        if intent.start_date is None or intent.end_date is None:
            raise ValueError("template planning requires concrete dates")
        days = (intent.end_date - intent.start_date).days + 1
        return (
            ReportQuery(
                connector_id=intent.connector_id,
                metrics=self._spec.metrics,
                dimensions=self._spec.dimensions,
                start_date=intent.start_date,
                end_date=intent.end_date,
                comparison_start=intent.start_date - timedelta(days=days),
                comparison_end=intent.start_date - timedelta(days=1),
                filters={
                    "tenant": intent.tenant,
                    "model_scope": intent.model_scope,
                    "models": list(intent.models),
                },
            ),
        )

    def analyze(self, datasets: tuple[ReportDataset, ...]) -> ReportDataset:
        if len(datasets) != 1:
            raise ValueError("usage matrix expects one normalized dataset")
        return datasets[0]


def build_default_registry(
    *, discover_external: bool = True, magik_enabled: bool = True
) -> ReportPluginRegistry:
    registry = ReportPluginRegistry()
    registry.register_renderer(TextChannelRenderer())
    if magik_enabled:
        registry.register_connector(MagikCubeConnector())
    for spec in load_builtin_template_specs():
        registry.register_template(UsageMatrixTemplate(spec))
    if discover_external:
        registry.discover_entry_points()
    return registry
