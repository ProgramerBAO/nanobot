"""Cron-run compilation and execution of stored report subscriptions."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from nanobot.agent.reporting.cube_subscription_intent import (
    EXPLICIT_ALL_MODELS_RE,
    EXPLICIT_ALL_TENANTS_RE,
    CubeSubscriptionIntent,
)
from nanobot.agent.reporting.magik_cube_intent import ReportIntent as MagikReportIntent
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import current_request_context
from nanobot.cron.session_turns import CRON_TRIGGER_META
from nanobot.reporting import (
    CubeConnector,
    CubeProviderQualityConnector,
    ReportIntent,
    ReportRunContext,
    ReportRunner,
)
from nanobot.reporting.schedules import (
    BRIEF_PERIOD_TEMPLATES,
    PERIOD_TEMPLATES,
)
from nanobot.reporting.store import ReportSubscription
from nanobot.utils.report_failures import is_transient_report_failure


# Module-level helper kept out of the class so static
# methods need no class-name self-reference.
@staticmethod
def _period_from_template(template_id: str) -> str:
    if "hourly" in template_id:
        return "recent1h"
    if "daily" in template_id:
        return "day"
    if "monthly" in template_id:
        return "month"
    return "week"


class _SubscriptionCompileMixin:

    @staticmethod
    def _subscription_preview_params(
        intent: CubeSubscriptionIntent, *, reference_message_id: str = ""
    ) -> dict[str, Any]:
        """Compile validated NLU output to the ReportCenter's public schema."""

        params: dict[str, Any] = {
            "action": "subscription_preview",
            "report_type": intent.report_type,
            "tenant_scope": intent.tenant_scope,
            "tenant_aliases": list(intent.tenant_aliases),
            "model_scope": intent.model_scope,
            "models": list(intent.models),
            "recurrence": intent.recurrence,
            "send_time": intent.send_time,
            "weekday": intent.weekday,
            "month_day": intent.month_day,
            "inherit_report_scope": intent.inherit_report_scope,
            "reference_message_id": reference_message_id,
        }
        if intent.hours is not None:
            # Explicit hourly-broadcast hours (user-confirmed 2026-09-16);
            # the every-hour default omits the key entirely because the tool
            # schema types hours as a non-nullable array.
            params["hours"] = list(intent.hours)
        return params


    async def _compile_subscription_intent(
        self,
        text: str,
        intent: CubeSubscriptionIntent,
        *,
        reference_message_id: str,
    ) -> dict[str, Any]:
        """Resolve a validated intent against live Cube catalog data.

        This boundary owns identity reconciliation and grouped-template
        promotion.  The classifier, deterministic parser, and references may
        provide only human-facing scope hints; no caller can inject a tenant
        ID or broaden a subscription here.
        """

        tenant_resolution = await self._resolve_catalog_tenant_mentions(
            text, intent.tenant_aliases
        )
        explicit_all_tenants = bool(EXPLICIT_ALL_TENANTS_RE.search(text))
        if tenant_resolution.error == "catalog_unavailable":
            return {
                "action": "subscription_scope_failed",
                "subscription_error": "catalog_unavailable",
                "catalog_unavailable": True,
            }
        # An explicit all-customer request does not need individual name
        # matching.  Ignore incidental duplicate tags or classifier noise, but
        # never ignore an unavailable catalog because the resulting scope would
        # no longer be verifiable at execution time.
        if tenant_resolution.error and not explicit_all_tenants:
            marker: dict[str, Any] = {
                "action": "subscription_scope_failed",
                "subscription_error": tenant_resolution.error,
                tenant_resolution.error: True,
            }
            if tenant_resolution.unresolved:
                marker["unresolved_tenants"] = list(tenant_resolution.unresolved)
            return marker
        merged_aliases = tenant_resolution.values
        # The original sentence is authoritative for an explicitly named
        # scope.  A classifier may return ``all`` or only its last entity when
        # a long Chinese list is present; live catalog matches repair that
        # loss.  Conversely, an explicit “全部客户” phrase must not be turned
        # into a selected subset merely because a name-like token was emitted.
        if explicit_all_tenants:
            intent = replace(intent, tenant_scope="all", tenant_aliases=())
        elif intent.tenant_scope == "all" and not merged_aliases:
            # ``tenant_scope=all`` from the classifier is only a hint.  Without
            # an explicit all-customer phrase or a live named match, accepting
            # it would broaden a natural-language request to every tenant.
            return {
                "action": "subscription_scope_failed",
                "subscription_error": "scope_unresolved",
                "scope_unresolved": True,
            }
        elif merged_aliases:
            intent = replace(
                intent,
                tenant_scope="selected",
                tenant_aliases=merged_aliases,
            )
        elif intent.tenant_scope == "selected":
            # The classifier supplied a name, but no live catalog record
            # matched it.  Do not let the later resolver guess from stale or
            # synthetic aliases; return the bounded recovery path instead.
            return {
                "action": "subscription_scope_failed",
                "subscription_error": "scope_unresolved",
                "scope_unresolved": True,
            }
        # A long scope sentence is often truncated by an LLM before it reaches
        # the schema boundary.  The explicit phrase in the original message is
        # authoritative for model scope, so a single accidentally extracted
        # model must never narrow an ``all models`` subscription.
        if EXPLICIT_ALL_MODELS_RE.search(text):
            intent = replace(intent, model_scope="all", models=())
        if (
            intent.report_type in {"usage_daily_brief", "usage_customer_model_daily_brief"}
            and (
                intent.report_type == "usage_customer_model_daily_brief"
                or len(merged_aliases) > 1
                or (EXPLICIT_ALL_TENANTS_RE.search(text) and EXPLICIT_ALL_MODELS_RE.search(text))
            )
        ):
            # Keep the grouped template for a multi-customer daily subscription;
            # the legacy daily brief has a single ``tenant_query`` slot and
            # would silently retain only one customer.
            intent = replace(
                intent,
                report_type="usage_customer_model_daily_brief",
                model_scope="all" if EXPLICIT_ALL_MODELS_RE.search(text) else intent.model_scope,
                models=() if EXPLICIT_ALL_MODELS_RE.search(text) else intent.models,
            )
        return self._subscription_preview_params(
            intent, reference_message_id=reference_message_id
        )


    @staticmethod
    def _subscription_period(subscription: ReportSubscription) -> str:
        saved_period = str(subscription.report_params.get("subscription_period") or "")
        if saved_period in {"day", "week", "month", "recent1h"}:
            return saved_period
        return _period_from_template(subscription.template_id)


    @staticmethod
    def _previous_complete_hour(now: datetime) -> datetime:
        """Floor to the current hour and step back one hour.

        The manual hourly report, its scope discovery, and the hourly
        subscription run must all target the same just-completed hour; this
        helper is the single definition of that window arithmetic.
        """
        return now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


    def _normalized_subscription_scope(
        self, params: dict[str, Any]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Shared scope normalization for the cron-run intent branches.

        Returns the deduplicated tenant tuple (falling back to selection
        tenant ids when the top-level list is empty) and the canonicalized
        model tuple. The hourly and multi-customer daily/weekly intent
        branches previously repeated these extraction idioms verbatim;
        branch-specific scope rules and empty checks stay with each branch.
        """

        tenants = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in params.get("tenants") or []
                if str(item).strip()
            )
        )
        if not tenants:
            selections = [
                item
                for item in params.get("report_selections") or []
                if isinstance(item, dict)
            ]
            tenants = tuple(
                dict.fromkeys(
                    str(item.get("tenant_query") or "").strip()
                    for item in selections
                    if str(item.get("tenant_query") or "").strip()
                )
            )
        models = self._canonical_cube_models(
            tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in params.get("models") or []
                    if str(item).strip()
                )
            )
        )
        return tenants, models


    @staticmethod
    def _subscription_service_template_id(
        *,
        data_period: str,
        report_template: str,
        report_variant: str,
    ) -> str:
        if report_variant == "customer_model_daily_brief":
            return "usage_customer_model_daily_brief"
        if report_variant == "customer_model_weekly_brief":
            return "usage_customer_model_weekly_brief"
        if report_variant == "customer_model_hourly_tpm":
            return "usage_customer_model_hourly_tpm"
        if report_template == "brief":
            return BRIEF_PERIOD_TEMPLATES[data_period]
        return PERIOD_TEMPLATES[data_period]


    def _dynamic_magik_params(self, subscription: ReportSubscription) -> dict[str, Any]:
        period = self._subscription_period(subscription)
        if period == "recent1h":
            # Hourly runs keep no persisted report window: the template plans
            # the just-completed hour from the clock, and the legacy intent
            # registry has no hourly report kind, so no date params are added.
            params: dict[str, Any] = {}
        else:
            intent = MagikReportIntent(report_kind=period)  # type: ignore[arg-type]
            params = intent.to_tool_params(
                today=datetime.now(ZoneInfo(subscription.timezone)).date()
            )
        saved = dict(subscription.report_params)
        params.update(saved)
        params.setdefault(
            "report_template",
            "brief" if self._usage_brief_default_enabled else "matrix_card",
        )
        params["save_snapshot"] = False
        params.pop("report_family", None)
        params.pop("subscription_period", None)
        params.pop("calculation_version", None)
        params.pop("threshold_version", None)
        return params


    def _subscription_cube_intent(
        self,
        subscription: ReportSubscription,
        *,
        tenant_models: dict[str, list[str]] | None = None,
    ) -> ReportIntent | None:
        """Compile the compatible one-scope subscription into a safe Intent."""

        params = self._dynamic_magik_params(subscription)
        family = str(subscription.report_params.get("report_family") or "usage")
        period = self._subscription_period(subscription)
        if subscription.template_id == "machine_tpm_peak":
            # Run-time availability is enforced by the template policy in the
            # runner; the retired machine-TPM runtime flag no longer gates
            # the compile path.
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            model = str(params.get("model") or "").strip()
            if not model:
                return None
            return ReportIntent(
                connector_id="magik_cube",
                template_id="machine_tpm_peak",
                period=period,  # type: ignore[arg-type]
                models=self._canonical_cube_models((model,)),
                start_date=start_date,
                end_date=end_date,
                filters={
                    "models": [model],
                    "model_scope": "selected",
                    "cluster": str(params.get("cluster") or "").strip(),
                },
            )
        if subscription.template_id == "usage_customer_model_hourly_tpm":
            # Hourly TPM runs are clock-driven: every execution reports the
            # just-completed hour, so the window is planned by the template
            # from the current clock instead of persisted dates.
            if period != "recent1h":
                return None
            tenants, models = self._normalized_subscription_scope(params)
            if not tenants:
                return None
            model_scope = str(params.get("model_scope") or "all")
            if model_scope == "all":
                if tenant_models is None:
                    return None
                # An all-model hourly subscription resolves its live per-tenant
                # active models immediately before each run; the static model
                # tuple stays empty.
                models = ()
            elif model_scope != "selected" or not models:
                return None
            else:
                # Rebuild the per-tenant relationships from the validated
                # selections; the flat model list alone cannot drive the
                # per-pair hourly queries.
                tenant_models = {
                    str(item.get("tenant_query") or "").strip(): [
                        str(model).strip()
                        for model in item.get("models") or []
                        if str(model).strip()
                    ]
                    for item in params.get("report_selections") or []
                    if isinstance(item, dict)
                    and str(item.get("model_scope") or "") == "selected"
                    and str(item.get("tenant_query") or "").strip()
                }
                if not tenant_models and len(tenants) == 1:
                    tenant_models = {tenants[0]: list(models)}
                if not tenant_models:
                    return None
            tenant_labels = [
                str(item).strip()
                for item in params.get("tenant_labels") or []
                if str(item).strip()
            ]
            tenant_names = {
                tenant_id: tenant_labels[index]
                for index, tenant_id in enumerate(tenants)
                if index < len(tenant_labels)
            }
            return ReportIntent(
                connector_id="magik_cube",
                template_id="usage_customer_model_hourly_tpm",
                period="recent1h",
                tenant_scope=(
                    "all" if params.get("all_tenants") is True else "selected"
                ),
                tenants=tenants,
                models=models,
                filters={
                    "tenants": list(tenants),
                    "tenant_scope": (
                        "all" if params.get("all_tenants") is True else "selected"
                    ),
                    "models": list(models),
                    "model_scope": model_scope,
                    "tenant_models": tenant_models or {},
                    "tenant_names": tenant_names,
                    "multi_scope": True,
                },
            )
        if subscription.template_id in {
            "usage_customer_model_daily_brief",
            "usage_customer_model_weekly_brief",
        }:
            expected_period = (
                "day"
                if subscription.template_id == "usage_customer_model_daily_brief"
                else "week"
            )
            # Availability is enforced by the always-on template policy at
            # run time; the retired family runtime flags no longer gate the
            # compile path.
            if period != expected_period:
                return None
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            tenants, models = self._normalized_subscription_scope(params)
            model_scope = str(params.get("model_scope") or "selected")
            if model_scope == "all":
                if not tenants or tenant_models is None:
                    return None
                # An all-model subscription resolves its live per-tenant catalog
                # immediately before each run; the static model tuple stays empty.
                models = ()
            elif model_scope != "selected" or not tenants or not models:
                return None
            return ReportIntent(
                connector_id="magik_cube",
                template_id="usage_customer_model_daily_brief",
                period=expected_period,  # type: ignore[arg-type]
                tenant_scope=(
                    "all" if params.get("all_tenants") is True else "selected"
                ),
                tenants=tenants,
                models=models,
                start_date=start_date,
                end_date=end_date,
                filters={
                    "tenants": list(tenants),
                    "tenant_scope": (
                        "all" if params.get("all_tenants") is True else "selected"
                    ),
                    "models": list(models),
                    "model_scope": model_scope,
                    "tenant_models": tenant_models or {},
                    "multi_scope": True,
                },
            )
        if family == "provider_quality":
            if not self.provider_quality_subscriptions_enabled or period not in {"day", "week"}:
                return None
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            model = str(params.get("model") or "").strip()
            providers = tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in (params.get("providers") or [])
                    if str(item).strip()
                )
            )
            return ReportIntent(
                connector_id=subscription.connector_id,
                template_id="provider_quality",
                period=period,  # type: ignore[arg-type]
                models=self._canonical_cube_models((model,)) if model else (),
                start_date=start_date,
                end_date=end_date,
                provider=str(params.get("provider") or "").strip(),
                endpoint=str(params.get("endpoint") or "").strip(),
                filters={
                    "provider_quality": True,
                    "provider": str(params.get("provider") or "").strip(),
                    "providers": list(providers),
                    "provider_id": str(params.get("provider_id") or "").strip(),
                    "model": model,
                    "endpoint": str(params.get("endpoint") or "").strip(),
                },
            )
        if family == "health":
            if not self.health_subscriptions_enabled:
                return None
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            return ReportIntent(
                connector_id=subscription.connector_id,
                template_id="health_sre",
                period=period,  # type: ignore[arg-type]
                start_date=start_date,
                end_date=end_date,
                filters={},
            )
        if family == "cost":
            if not self.cost_subscriptions_enabled or period != "month":
                return None
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            tenant = str(params.get("tenant_query") or "").strip()
            if not tenant:
                return None
            model = str(params.get("model") or "").strip()
            return ReportIntent(
                connector_id=subscription.connector_id,
                template_id="cost_account",
                period="month",
                tenant=tenant,
                project=str(params.get("project") or "").strip(),
                endpoint=str(params.get("endpoint") or "").strip(),
                model_scope="selected" if model else "summary",
                models=(model,) if model else (),
                start_date=start_date,
                end_date=end_date,
                filters={
                    "tenant": tenant,
                    "project": str(params.get("project") or "").strip(),
                    "endpoint": str(params.get("endpoint") or "").strip(),
                    "models": [model] if model else [],
                    "model_scope": "selected" if model else "summary",
                },
            )
        selections = params.get("report_selections")
        if isinstance(selections, list) and len(selections) > 1:
            # The legacy matrix supports several tenant/model scopes. Keep it on
            # the compatibility path until Cube has a bulk-scope contract.
            return None
        selection = selections[0] if isinstance(selections, list) and selections else {}
        if not isinstance(selection, dict):
            selection = {}
        try:
            start_date = date.fromisoformat(str(params["start_date"]))
            end_date = date.fromisoformat(str(params["end_date"]))
        except (KeyError, TypeError, ValueError):
            return None
        tenant = str(selection.get("tenant_query") or params.get("tenant_query") or "").strip()
        model = str(params.get("model") or "").strip()
        models = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in (selection.get("models") or params.get("models") or [])
                if str(item).strip()
            )
        )
        if model and not models:
            models = (model,)
        models = self._canonical_cube_models(models)
        model_scope = str(
            selection.get("model_scope") or params.get("model_scope") or "summary"
        )
        if model_scope not in {"summary", "all", "selected"}:
            return None
        all_tenants = params.get("all_tenants", False)
        if not isinstance(all_tenants, bool):
            return None
        if all_tenants and (tenant or len(models) != 1):
            return None
        return ReportIntent(
            connector_id=subscription.connector_id,
            template_id=subscription.template_id,
            period=period,  # type: ignore[arg-type]
            tenant=tenant,
            models=models,
            model_scope=model_scope,  # type: ignore[arg-type]
            start_date=start_date,
            end_date=end_date,
            filters={
                "tenant": tenant,
                "models": list(models),
                "all_tenants": all_tenants,
                "model_scope": model_scope,
            },
        )


    async def _run_cube_subscription(
        self,
        subscription: ReportSubscription,
        *,
        run_id: str,
        idempotency_key: str,
    ) -> ToolResult:
        params = self._dynamic_magik_params(subscription)
        tenant_models: dict[str, list[str]] | None = None
        if (
            subscription.template_id
            in {
                "usage_customer_model_daily_brief",
                "usage_customer_model_weekly_brief",
                "usage_customer_model_hourly_tpm",
            }
            and str(params.get("model_scope") or "") == "all"
        ):
            tenants = [
                str(item).strip()
                for item in params.get("tenants") or []
                if str(item).strip()
            ]
            if params.get("all_tenants") is True:
                catalog_loader = getattr(self._magik_tool, "list_tenant_catalog", None)
                if not callable(catalog_loader):
                    raise LookupError("Cube 客户目录当前不可用")
                tenant_catalog = await catalog_loader(limit=20)
                tenants = [str(item["tenant_id"]) for item in tenant_catalog]
                params["tenants"] = tenants
                params["report_selections"] = [
                    {
                        "tenant_query": tenant_id,
                        "model_scope": "all",
                        "models": [],
                    }
                    for tenant_id in tenants
                ]
                subscription = replace(subscription, report_params=params)
            if subscription.template_id == "usage_customer_model_hourly_tpm":
                # Hourly runs discover each tenant's active models for the
                # date of the just-completed hour; the exact hour window is
                # planned by the template from the current clock.
                now = datetime.now(ZoneInfo(self._config.timezone))
                previous_hour = self._previous_complete_hour(now)
                start_date = previous_hour.date()
                end_date = previous_hour.date()
            else:
                try:
                    start_date = date.fromisoformat(str(params["start_date"]))
                    end_date = date.fromisoformat(str(params["end_date"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "subscription has an invalid dynamic report window"
                    ) from exc
            tenant_models = await self._load_tenant_model_catalog(
                tenants,
                start_date=start_date,
                end_date=end_date,
            )
        intent = self._subscription_cube_intent(
            subscription,
            tenant_models=tenant_models,
        )
        if intent is None:
            raise ValueError("subscription requires the legacy Magik compatibility path")
        template = self._registry.template(intent.template_id)
        if template is None:
            raise LookupError(f"Cube report template unavailable: {intent.template_id}")
        context = ReportRunContext(
            channel=subscription.channel,
            chat_id=subscription.chat_id,
            user_id=subscription.user_id,
            timezone=subscription.timezone,
            trace_id=run_id,
            template_version=subscription.template_version or template.manifest.version,
            metadata={"subscription_id": subscription.subscription_id},
        )
        runner = ReportRunner(
            self._registry,
            self._store,
            semantic_shadow_enabled=self._config.cube_semantics_shadow,
        )
        outcome = await runner.run(intent, context)
        report_attempts = 1
        if (
            self._config.cube_transient_run_retry
            and outcome.quality == "missing"
            and any(is_transient_report_failure(item) for item in outcome.document.warnings)
        ):
            logger.warning(
                "Cube subscription transient failure; retrying once: subscription_id={} delay_seconds={}",
                subscription.subscription_id,
                self._config.cube_transient_retry_delay_seconds,
            )
            await asyncio.sleep(self._config.cube_transient_retry_delay_seconds)
            outcome = await runner.run(intent, context)
            report_attempts = 2
        return self._with_delivery_metadata(
            self._result(
                outcome.document,
                report_reference=self._report_reference_payload(
                    intent,
                    document=outcome.document,
                    run_id=run_id,
                ),
            ),
            idempotency_key=idempotency_key,
            run_id=run_id,
            report_attempts=report_attempts,
        )


    async def _run_subscription(self, subscription_id: str, metadata: dict[str, Any]) -> Any:
        ctx = current_request_context()
        trigger = metadata.get(CRON_TRIGGER_META)
        if ctx is None or ctx.sender_id != "cron" or not isinstance(trigger, dict):
            return ToolResult.error("Error: run_subscription is restricted to Cron")
        subscription = self._store.subscription(subscription_id)
        if subscription is None or not subscription.enabled:
            return ToolResult.error("Error: report subscription is missing or disabled")
        policy = self._store.template_policy(subscription.template_id)
        if policy is not None and not policy["enabled"]:
            return ToolResult.error("Error: report template is disabled")
        family = str(subscription.report_params.get("report_family") or "usage")
        if self._magik_tool is None and family != "provider_quality":
            return ToolResult.error("Error: Magik Cube connector is unavailable")
        scheduled_at = trigger.get("scheduled_at_ms")
        if not isinstance(scheduled_at, int):
            return ToolResult.error("Error: Cron trigger is missing scheduled_at_ms")
        idempotency_key = (
            f"{subscription.subscription_id}:{scheduled_at}:{subscription.template_version}"
        )
        if not self._store.claim_delivery(idempotency_key):
            return ToolResult("该计划周期的报表已经处理，已跳过重复发送。")
        run_id = str(trigger.get("run_id") or uuid.uuid4().hex)
        connector = self._registry.connector(subscription.connector_id)
        # Single Cube-vs-legacy decision point (phase 3e). The runner path
        # serves every family whose mechanism flag is on (usage and cost keep
        # theirs for pipeline rollback) and whose subscription compiles to a
        # Cube intent. The legacy magik fallback below only serves rolled-back
        # flags, multi-selection matrix windows the compiler rejects, and
        # pre-consolidation rows. Deletion condition: after one retention
        # cycle (default 30 days) with no flag rollbacks, verify that no
        # stored subscription hits the fallback path (it lacks the runner's
        # policy/RBAC authorization), then remove it together with
        # _dynamic_magik_params.
        cube_family_enabled = (
            (family == "usage" and self._flag("cube_subscription"))
            or family == "health"
            or (family == "cost" and self._flag("cube_cost_subscription"))
            or family == "provider_quality"
        )
        if (
            cube_family_enabled
            and isinstance(connector, (CubeConnector, CubeProviderQualityConnector))
            and (
            subscription.template_id in {
                # These templates always take the Cube path: their compile
                # either needs no external load (daily/weekly brief) or the
                # load happens inside _run_cube_subscription (hourly all-model
                # discovery). The hourly entry is load-bearing: the bare
                # compileability probe below passes tenant_models=None, and an
                # all-model hourly subscription would otherwise be misrouted
                # to the legacy magik fallback — which has no hourly concept
                # and delivered a daily brief instead (observed live
                # 2026-09-16).
                "usage_customer_model_daily_brief",
                "usage_customer_model_weekly_brief",
                "usage_customer_model_hourly_tpm",
            }
                or self._subscription_cube_intent(subscription) is not None
            )
        ):
            try:
                return await self._run_cube_subscription(
                    subscription,
                    run_id=run_id,
                    idempotency_key=idempotency_key,
                )
            except Exception:
                self._store.complete_delivery(idempotency_key, status="error")
                raise
        started = time.perf_counter()
        try:
            result = await self._magik_tool.execute(**self._dynamic_magik_params(subscription))
        except Exception:
            self._store.complete_delivery(idempotency_key, status="error")
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        self._store.record_run(
            run_id=run_id,
            channel=subscription.channel,
            chat_id=subscription.chat_id,
            user_id=subscription.user_id,
            connector_id=subscription.connector_id,
            template_id=subscription.template_id,
            template_version=subscription.template_version,
            request={
                "subscription_id": subscription_id,
                "calculation_version": subscription.report_params.get(
                    "calculation_version", subscription.template_version
                ),
            },
            status="error" if getattr(result, "is_error", False) else "ok",
            duration_ms=duration_ms,
            quality="partial" if getattr(result, "is_error", False) else "complete",
            error_type="tool_error" if getattr(result, "is_error", False) else "",
        )
        return self._with_delivery_metadata(
            result,
            idempotency_key=idempotency_key,
            run_id=run_id,
            report_attempts=1,
        )
