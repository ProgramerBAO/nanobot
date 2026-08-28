"""Connector-neutral deterministic report execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from typing import Any

from loguru import logger

from nanobot.reporting.contracts import (
    DataQuality,
    ReportBlock,
    ReportDataset,
    ReportDocument,
    ReportIntent,
    ReportRunContext,
    validate_report_intent,
    validate_report_query,
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
        validate_report_intent(intent)
        checks = [
            ("connector", intent.connector_id),
            ("template", intent.template_id),
            ("tenant", intent.tenant),
            ("project", intent.project),
            ("endpoint", intent.endpoint),
            ("provider", intent.provider),
            ("environment", intent.environment),
        ]
        checks.extend(("model", model) for model in intent.models)
        filter_scope_keys = {
            "tenant": ("tenant",),
            "project": ("project",),
            "endpoint": ("endpoint",),
            "provider": ("provider",),
            "environment": ("environment",),
            "model": ("model", "models"),
        }
        for resource_type, keys in filter_scope_keys.items():
            for key in keys:
                value = intent.filters.get(key)
                if isinstance(value, str):
                    values = (value,)
                elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
                    values = tuple(str(item) for item in value)
                else:
                    values = ()
                checks.extend((resource_type, item.strip()) for item in values if item.strip())
        for resource_type, resource_id in checks:
            if not resource_id:
                continue
            if not self._store.allowed(
                context.channel, context.user_id, resource_type, resource_id
            ):
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

    @staticmethod
    def _shard_query(query: Any, max_days: int) -> tuple[Any, ...]:
        if max_days < 1:
            raise ValueError("connector max_window_days must be positive")
        total_days = (query.end_date - query.start_date).days + 1
        if total_days <= max_days:
            return (query,)
        comparison_offset = None
        if query.comparison_start is not None:
            comparison_offset = query.start_date - query.comparison_start
        shards = []
        cursor = query.start_date
        while cursor <= query.end_date:
            shard_end = min(query.end_date, cursor + timedelta(days=max_days - 1))
            comparison_start = (
                cursor - comparison_offset if comparison_offset is not None else None
            )
            comparison_end = (
                shard_end - comparison_offset if comparison_offset is not None else None
            )
            shards.append(
                replace(
                    query,
                    start_date=cursor,
                    end_date=shard_end,
                    comparison_start=comparison_start,
                    comparison_end=comparison_end,
                    # Keep the approved connector query id unchanged across shards.
                    query_id=query.query_id,
                )
            )
            cursor = shard_end + timedelta(days=1)
        return tuple(shards)

    @staticmethod
    def _merge_datasets(datasets: tuple[ReportDataset, ...]) -> ReportDataset:
        quality = ReportRunner._quality(datasets)
        warnings = tuple(dict.fromkeys(warning for item in datasets for warning in item.warnings))
        source = ",".join(dict.fromkeys(item.source for item in datasets if item.source))
        return ReportDataset(
            rows=tuple(row for item in datasets for row in item.rows),
            quality=quality,
            warnings=warnings,
            source=source,
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
            validate_report_query(query)
        expanded_queries = tuple(
            shard for query in queries for shard in self._shard_query(query, max_days)
        )
        fetched = tuple(
            await asyncio.gather(*(self._query(connector, query) for query in expanded_queries))
        )
        grouped: list[ReportDataset] = []
        offset = 0
        for query in queries:
            shard_count = len(self._shard_query(query, max_days))
            grouped.append(self._merge_datasets(fetched[offset:offset + shard_count]))
            offset += shard_count
        datasets = tuple(grouped)
        quality = self._quality(datasets)
        warnings = tuple(dict.fromkeys(warning for item in datasets for warning in item.warnings))
        document = self._document(template.analyze(datasets), quality)
        fallback_text = document.fallback_text
        if quality != "complete":
            fallback_text = f"{fallback_text}\n数据质量：{quality}"
        document = replace(document, quality=quality, warnings=warnings, fallback_text=fallback_text)
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
            template_version=context.template_version or template.manifest.version,
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
            len(expanded_queries),
        )
        return ReportRunOutcome(document, quality, duration_ms, len(expanded_queries))
