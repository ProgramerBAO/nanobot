"""Channel-neutral deterministic reporting platform contracts."""

from nanobot.reporting.builtins import build_default_registry
from nanobot.reporting.contracts import (
    DataQuality,
    ReportAction,
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportQuery,
    ReportRunContext,
    SecretRef,
)
from nanobot.reporting.registry import ReportPluginRegistry
from nanobot.reporting.renderer import ChannelRenderer, RenderedReport, TextChannelRenderer
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
    "ChannelRenderer",
    "ReportAction",
    "ReportDataset",
    "ReportDocument",
    "ReportIntent",
    "ReportPluginRegistry",
    "ReportQuery",
    "ReportRunContext",
    "ReportRunOutcome",
    "ReportRunner",
    "ReportStateStore",
    "PostgresReportStateStore",
    "SecretRef",
    "RenderedReport",
    "TextChannelRenderer",
    "build_default_registry",
    "configured_report_state_store",
    "create_report_state_store",
    "get_report_state_store",
]
