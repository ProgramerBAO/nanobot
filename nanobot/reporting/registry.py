"""Discovery and lifecycle management for report connectors and templates."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

from loguru import logger

from nanobot.reporting.contracts import ReportDataset, ReportIntent, ReportQuery
from nanobot.reporting.renderer import ChannelRenderer


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    metrics: frozenset[str]
    dimensions: frozenset[str]
    max_window_days: int = 90
    supports_bulk_dimensions: bool = True
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class ConnectorManifest:
    connector_id: str
    display_name: str
    version: str
    auth_methods: tuple[str, ...]
    capabilities: ConnectorCapabilities
    config_schema: dict[str, Any] = field(default_factory=dict)
    secret_fields: frozenset[str] = frozenset()
    allowed_hosts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateManifest:
    template_id: str
    display_name: str
    version: str
    category: str
    periods: frozenset[str]
    required_metrics: frozenset[str]
    required_dimensions: frozenset[str]
    connector_ids: frozenset[str] = frozenset()
    description: str = ""


class ConnectorPlugin(ABC):
    manifest: ConnectorManifest

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        ...

    @abstractmethod
    async def discover_catalog(self) -> dict[str, list[str]]:
        ...

    @abstractmethod
    async def query(self, query: ReportQuery) -> ReportDataset:
        ...


class TemplatePlugin(ABC):
    manifest: TemplateManifest

    @abstractmethod
    def plan(self, intent: ReportIntent) -> tuple[ReportQuery, ...]:
        ...

    @abstractmethod
    def analyze(self, datasets: tuple[ReportDataset, ...]) -> Any:
        ...


class ReportPluginRegistry:
    """Fail-isolated registry; one bad plugin never prevents Gateway startup."""

    def __init__(self) -> None:
        self._connectors: dict[str, ConnectorPlugin] = {}
        self._templates: dict[str, TemplatePlugin] = {}
        self._renderers: dict[str, ChannelRenderer] = {}
        self.load_errors: dict[str, str] = {}

    def register_connector(self, plugin: ConnectorPlugin) -> None:
        manifest = plugin.manifest
        if not manifest.capabilities.read_only:
            raise ValueError("report connectors must declare read_only=True")
        if manifest.connector_id in self._connectors:
            raise ValueError(f"duplicate report connector: {manifest.connector_id}")
        self._connectors[manifest.connector_id] = plugin

    def register_template(self, plugin: TemplatePlugin) -> None:
        template_id = plugin.manifest.template_id
        if template_id in self._templates:
            raise ValueError(f"duplicate report template: {template_id}")
        self._templates[template_id] = plugin

    def register_renderer(self, renderer: ChannelRenderer) -> None:
        if renderer.channel_id in self._renderers:
            raise ValueError(f"duplicate report renderer: {renderer.channel_id}")
        self._renderers[renderer.channel_id] = renderer

    def renderer(self, channel_id: str) -> ChannelRenderer | None:
        return self._renderers.get(channel_id) or self._renderers.get("text")

    def connector(self, connector_id: str) -> ConnectorPlugin | None:
        return self._connectors.get(connector_id)

    def template(self, template_id: str) -> TemplatePlugin | None:
        return self._templates.get(template_id)

    def connectors(self) -> tuple[ConnectorPlugin, ...]:
        return tuple(self._connectors[key] for key in sorted(self._connectors))

    def templates(self) -> tuple[TemplatePlugin, ...]:
        return tuple(self._templates[key] for key in sorted(self._templates))

    def compatible_templates(self, connector_id: str) -> tuple[TemplatePlugin, ...]:
        connector = self.connector(connector_id)
        if connector is None:
            return ()
        caps = connector.manifest.capabilities
        return tuple(
            template
            for template in self.templates()
            if (
                not template.manifest.connector_ids
                or connector_id in template.manifest.connector_ids
            )
            and template.manifest.required_metrics <= caps.metrics
            and template.manifest.required_dimensions <= caps.dimensions
        )

    def discover_entry_points(self) -> None:
        self._discover_group("nanobot.report_connectors", self.register_connector)
        self._discover_group("nanobot.report_templates", self.register_template)
        self._discover_group("nanobot.report_renderers", self.register_renderer)

    def _discover_group(self, group: str, register: Any) -> None:
        try:
            discovered = entry_points(group=group)
        except Exception as exc:
            self.load_errors[group] = type(exc).__name__
            logger.warning("Report plugin discovery failed: group={} error_type={}", group, type(exc).__name__)
            return
        for point in discovered:
            try:
                loaded = point.load()
                plugin = loaded() if isinstance(loaded, type) else loaded
                register(plugin)
            except Exception as exc:
                key = f"{group}:{point.name}"
                self.load_errors[key] = type(exc).__name__
                logger.warning("Report plugin skipped: plugin={} error_type={}", key, type(exc).__name__)

    def public_catalog(self) -> dict[str, Any]:
        return {
            "connectors": [
                {
                    "id": item.manifest.connector_id,
                    "name": item.manifest.display_name,
                    "version": item.manifest.version,
                    "metrics": sorted(item.manifest.capabilities.metrics),
                    "dimensions": sorted(item.manifest.capabilities.dimensions),
                    "read_only": item.manifest.capabilities.read_only,
                }
                for item in self.connectors()
            ],
            "templates": [
                {
                    "id": item.manifest.template_id,
                    "name": item.manifest.display_name,
                    "version": item.manifest.version,
                    "category": item.manifest.category,
                    "periods": sorted(item.manifest.periods),
                    "description": item.manifest.description,
                }
                for item in self.templates()
            ],
            "renderers": [
                {"id": key, "version": self._renderers[key].version}
                for key in sorted(self._renderers)
            ],
            "load_errors": dict(self.load_errors),
        }
