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
from nanobot.utils.report_failures import (
    classify_report_failure,
    report_failure_code_from_warning,
)


@dataclass(frozen=True, slots=True)
class ReportRunOutcome:
    document: ReportDocument
    quality: DataQuality
    duration_ms: int
    query_count: int
    semantic_shadow: dict[str, Any] | None = None


class ReportRunner:
    """The shared 0-LLM path used by navigation, rules, and subscriptions."""

    def __init__(
        self,
        registry: ReportPluginRegistry,
        store: ReportStateStore,
        *,
        semantic_shadow_enabled: bool = False,
        template_policy_enforced: bool = False,
    ) -> None:
        self._registry = registry
        self._store = store
        self._query_slots = asyncio.Semaphore(2)
        self._semantic_shadow_enabled = semantic_shadow_enabled
        self._template_policy_enforced = template_policy_enforced

    def _authorize(self, intent: ReportIntent, context: ReportRunContext) -> None:
        validate_report_intent(intent)
        if self._template_policy_enforced:
            policy = self._store.template_policy(intent.template_id)
            if policy is not None and not policy["enabled"]:
                raise PermissionError("report template is disabled")
        checks = [
            ("connector", intent.connector_id),
            ("template", intent.template_id),
            ("tenant", intent.tenant),
            ("project", intent.project),
            ("endpoint", intent.endpoint),
            ("provider", intent.provider),
            ("environment", intent.environment),
        ]
        if intent.filters.get("all_tenants") is True:
            # Cross-customer reports require an explicit tenant wildcard grant.
            # A grant for one tenant must never fan out to every Cube customer.
            checks.append(("tenant", "*"))
        checks.extend(("tenant", tenant) for tenant in intent.tenants)
        checks.extend(("model", model) for model in intent.models)
        filter_scope_keys = {
            "tenant": ("tenant", "tenants"),
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
        tenant_models = intent.filters.get("tenant_models")
        if isinstance(tenant_models, dict):
            for tenant, models in tenant_models.items():
                tenant_id = str(tenant).strip()
                if tenant_id:
                    checks.append(("tenant", tenant_id))
                if isinstance(models, Sequence) and not isinstance(models, (str, bytes, bytearray)):
                    checks.extend(
                        ("model", str(model).strip())
                        for model in models
                        if str(model).strip()
                    )
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
                failure_code = classify_report_failure(exc)
                logger.warning(
                    "Report connector query failed: connector={} error_type={} failure_code={}",
                    connector.manifest.connector_id,
                    type(exc).__name__,
                    failure_code,
                )
                return ReportDataset(
                    rows=(),
                    quality="missing",
                    warnings=(failure_code,),
                    source=connector.manifest.connector_id,
                    metadata={"quality_reasons": (failure_code,)},
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

    def _semantic_shadow(self, template: Any, datasets: tuple[ReportDataset, ...]) -> dict[str, Any] | None:
        if not self._semantic_shadow_enabled:
            return None
        summary = getattr(template, "shadow_summary", None)
        if not callable(summary):
            return {"status": "unsupported"}
        try:
            raw = summary(datasets)
        except Exception as exc:
            return {"status": "unavailable", "error_type": type(exc).__name__}
        if not isinstance(raw, dict):
            return {"status": "unavailable", "error_type": "invalid_shadow_summary"}
        allowed = {
            "calculation_version",
            "status",
            "compared_metrics",
            "differing_metrics",
            "legacy_only_metrics",
            "candidate_only_metrics",
        }
        safe: dict[str, Any] = {}
        for key in allowed:
            value = raw.get(key)
            if isinstance(value, str):
                safe[key] = value[:128]
            elif isinstance(value, (list, tuple)):
                safe[key] = [str(item)[:128] for item in value[:64]]
        return safe or {"status": "unavailable", "error_type": "empty_shadow_summary"}

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
        metadata: dict[str, Any] = {}
        for item in datasets:
            for key, value in item.metadata.items():
                if isinstance(value, (list, tuple)):
                    existing = metadata.setdefault(key, [])
                    if not isinstance(existing, list):
                        existing = metadata[key] = [existing]
                    existing.extend(value)
                elif key not in metadata:
                    metadata[key] = value
        return ReportDataset(
            rows=tuple(row for item in datasets for row in item.rows),
            quality=quality,
            warnings=warnings,
            source=source,
            metadata=metadata,
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
        semantic_shadow = self._semantic_shadow(template, datasets)
        if document.context is not None:
            document_context = replace(
                document.context,
                quality=quality,
                quality_reasons=tuple(
                    dict.fromkeys(
                        warnings
                        + tuple(
                            reason
                            for dataset in datasets
                            for reason in dataset.metadata.get("quality_reasons", ())
                        )
                    )
                ),
                template_version=context.template_version or template.manifest.version,
            )
            document = replace(document, context=document_context)
        fallback_text = document.fallback_text
        if quality != "complete":
            fallback_text = f"{fallback_text}\n数据质量：{quality}"
        document = replace(document, quality=quality, warnings=warnings, fallback_text=fallback_text)
        duration_ms = int((time.perf_counter() - started) * 1000)
        request = asdict(intent)
        for key in (
            "start_date",
            "end_date",
            "start_time",
            "end_time",
            "comparison_start_time",
            "comparison_end_time",
        ):
            if request[key] is not None:
                request[key] = request[key].isoformat()
        if document.context is not None:
            request["report_context"] = asdict(document.context)
        if warnings:
            request["quality_reasons"] = list(warnings)
        if semantic_shadow is not None:
            request["semantic_shadow"] = semantic_shadow
        run_id = context.idempotency_key or context.trace_id
        failure_codes = tuple(
            code
            for warning in warnings
            if (code := report_failure_code_from_warning(warning)) is not None
        )
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
            error_type=(failure_codes[0] if quality == "missing" and failure_codes else ""),
        )
        logger.info(
            "Report run complete: connector={} template={} quality={} duration_ms={} queries={} shadow={}",
            intent.connector_id,
            intent.template_id,
            quality,
            duration_ms,
            len(expanded_queries),
            (semantic_shadow or {}).get("status", "disabled"),
        )
        return ReportRunOutcome(
            document,
            quality,
            duration_ms,
            len(expanded_queries),
            semantic_shadow,
        )
