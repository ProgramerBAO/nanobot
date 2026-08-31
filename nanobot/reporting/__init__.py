"""Channel-neutral deterministic reporting platform contracts."""

from nanobot.reporting.builtins import build_default_registry
from nanobot.reporting.contracts import (
    CANONICAL_DIMENSIONS,
    CANONICAL_METRICS,
    DataQuality,
    MetricDefinition,
    ReportAction,
    ReportContext,
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportQuery,
    ReportRunContext,
    ReportSource,
    ReportWindow,
    SecretRef,
    validate_report_intent,
    validate_report_query,
)
from nanobot.reporting.cube import CubeConnector, CubeCostAccountTemplate, CubeHealthTemplate
from nanobot.reporting.cube_contract_gate import (
    CubeContractGate,
    CubeContractGateResult,
    CubeContractProbeResult,
    profile_cube_contract_fixture,
)
from nanobot.reporting.delivery import (
    ChannelTransport,
    DeliveryResult,
    DeliveryRouter,
    split_message,
)
from nanobot.reporting.grafana import (
    GrafanaConnector,
    GrafanaConnectorConfig,
    GrafanaConnectorError,
    GrafanaQueryDefinition,
)
from nanobot.reporting.intents import IntentRouter, build_default_intent_router
from nanobot.reporting.registry import ReportPluginRegistry
from nanobot.reporting.renderer import (
    ChannelRenderer,
    DingTalkReportRenderer,
    FeishuReportRenderer,
    RenderedReport,
    RendererCapabilities,
    RendererManifest,
    TextChannelRenderer,
    WeComReportRenderer,
)
from nanobot.reporting.runner import ReportRunner, ReportRunOutcome
from nanobot.reporting.store import (
    PostgresReportStateStore,
    ReportStateStore,
    configured_report_state_store,
    create_report_state_store,
    get_report_state_store,
)

__all__ = [
    "DataQuality",
    "CANONICAL_DIMENSIONS",
    "CANONICAL_METRICS",
    "ChannelRenderer",
    "ChannelTransport",
    "MetricDefinition",
    "CubeConnector",
    "CubeCostAccountTemplate",
    "CubeHealthTemplate",
    "CubeContractGate",
    "CubeContractGateResult",
    "CubeContractProbeResult",
    "ReportAction",
    "ReportContext",
    "ReportDataset",
    "ReportDocument",
    "ReportIntent",
    "ReportPluginRegistry",
    "ReportQuery",
    "ReportRunContext",
    "ReportSource",
    "ReportRunOutcome",
    "ReportRunner",
    "ReportWindow",
    "ReportStateStore",
    "PostgresReportStateStore",
    "SecretRef",
    "RenderedReport",
    "DeliveryResult",
    "DeliveryRouter",
    "DingTalkReportRenderer",
    "FeishuReportRenderer",
    "GrafanaConnector",
    "GrafanaConnectorConfig",
    "GrafanaConnectorError",
    "GrafanaQueryDefinition",
    "IntentRouter",
    "RendererCapabilities",
    "RendererManifest",
    "WeComReportRenderer",
    "build_default_intent_router",
    "profile_cube_contract_fixture",
    "split_message",
    "validate_report_intent",
    "validate_report_query",
    "TextChannelRenderer",
    "build_default_registry",
    "configured_report_state_store",
    "create_report_state_store",
    "get_report_state_store",
]
