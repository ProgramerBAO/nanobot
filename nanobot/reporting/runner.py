"""Connector-neutral deterministic report execution."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any

from loguru import logger

from nanobot.reporting.contracts import (
    DataQuality,
    ReportBlock,
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportRunContext,
)
from nanobot.reporting.registry import ReportPluginRegistry
from nanobot.reporting.store import ReportStateStore


@dataclass(frozen=True, slots=True)
class ReportRunOutcome:
    document: ReportDocument
    quality: DataQuality
    duration_ms: int
    query_count: int


class ReportRunner:
    """The shared 0-LLM path used by navigation, rules, and subscriptions."""

    def __init__(self, registry: ReportPluginRegistry, store: ReportStateStore) -> None:
        self._registry = registry
        self._store = store
        self._query_slots = asyncio.Semaphore(2)

    def _authorize(self, intent: ReportIntent, context: ReportRunContext) -> None:
        for resource_type, resource_id in (
            ("connector", intent.connector_id),
            ("template", intent.template_id),
        ):
            if not self._store.allowed(
                context.channel, context.user_id, resource_type, resource_id
            ):
                raise PermissionError("report access denied")
        if intent.tenant and not self._store.allowed(
            context.channel, context.user_id, "tenant", intent.tenant
        ):
            raise PermissionError("report access denied")
        for model in intent.models:
            if not self._store.allowed(context.channel, context.user_id, "model", model):
                raise PermissionError("report access denied")

    async def _query(self, connector: Any, query: Any) -> ReportDataset:
        async with self._query_slots:
            try:
                return await connector.query(query)
            except Exception as exc:
                logger.warning(
                    "Report connector query failed: connector={} error_type={}",
                    connector.manifest.connector_id,
                    type(exc).__name__,
                )
                return ReportDataset(
                    rows=(),
                    quality="missing",
                    warnings=(f"connector_error:{type(exc).__name__}",),
                    source=connector.manifest.connector_id,
                )

    @staticmethod
    def _quality(datasets: tuple[ReportDataset, ...]) -> DataQuality:
        qualities = {item.quality for item in datasets}
        if qualities == {"complete"}:
            return "complete"
        if "complete" in qualities or "partial" in qualities:
            return "partial"
        return "missing"

    @staticmethod
    def _document(value: Any, quality: DataQuality) -> ReportDocument:
        if isinstance(value, ReportDocument):
            return value
        row_count = len(value.rows) if isinstance(value, ReportDataset) else 0
        return ReportDocument(
            title="确定性报表",
            fallback_text=f"报表已生成，数据质量：{quality}，标准化记录：{row_count}。",
            blocks=(
                ReportBlock("note", {"content": f"数据质量：{quality}"}),
            ),
        )

    async def run(
        self, intent: ReportIntent, context: ReportRunContext
    ) -> ReportRunOutcome:
        started = time.perf_counter()
        self._authorize(intent, context)
        connector = self._registry.connector(intent.connector_id)
        template = self._registry.template(intent.template_id)
        if connector is None or template is None:
            raise LookupError("report connector or template is unavailable")
        if template not in self._registry.compatible_templates(intent.connector_id):
            raise ValueError("report template is incompatible with connector")
        queries = template.plan(intent)
        max_days = connector.manifest.capabilities.max_window_days
        for query in queries:
            if (query.end_date - query.start_date).days + 1 > max_days:
                raise ValueError("report query exceeds connector window; planner must shard it")
        datasets = tuple(await asyncio.gather(*(self._query(connector, query) for query in queries)))
        quality = self._quality(datasets)
        document = self._document(template.analyze(datasets), quality)
        duration_ms = int((time.perf_counter() - started) * 1000)
        request = asdict(intent)
        for key in ("start_date", "end_date"):
            if request[key] is not None:
                request[key] = request[key].isoformat()
        run_id = context.idempotency_key or context.trace_id
        self._store.record_run(
            run_id=run_id,
            channel=context.channel,
            chat_id=context.chat_id,
            user_id=context.user_id,
            connector_id=intent.connector_id,
            template_id=intent.template_id,
            template_version=context.template_version,
            request=request,
            status="ok" if quality != "missing" else "error",
            duration_ms=duration_ms,
            quality=quality,
            error_type="connector_error" if quality == "missing" else "",
        )
        logger.info(
            "Report run complete: connector={} template={} quality={} duration_ms={} queries={}",
            intent.connector_id,
            intent.template_id,
            quality,
            duration_ms,
            len(queries),
        )
        return ReportRunOutcome(document, quality, duration_ms, len(queries))
