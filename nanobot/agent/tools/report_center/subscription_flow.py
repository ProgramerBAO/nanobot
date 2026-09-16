"""Channel-facing subscription actions: preview, confirmation, setup, subscribe."""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.report_center.phrases import (
    _ALLOWED_REPORT_PARAM_KEYS,
    _SAFE_CUBE_ID_RE,
)
from nanobot.agent.tools.report_center.schema import _EffectiveFlagConfigView
from nanobot.bus.events import (
    INBOUND_META_DIRECT_TOOL,
    OUTBOUND_META_AGENT_UI,
)
from nanobot.cron.types import CronSchedule
from nanobot.reporting import (
    ReportDocument,
)
from nanobot.reporting.authorization import authorize_magik_params
from nanobot.reporting.capabilities import (
    subscription_created_document,
)
from nanobot.reporting.contracts import ReportBlock
from nanobot.reporting.schedules import (
    BRIEF_PERIOD_TEMPLATES,
    PERIOD_TEMPLATES,
    RECURRENCE_SCHEDULE_PERIODS,
    SUBSCRIPTION_REPORT_TYPE_TABLE,
    build_subscription_schedule,
)
from nanobot.reporting.store import ReportSubscription
from nanobot.reporting.subscriptions import (
    ReportSubscriptionService,
    SubscriptionServiceError,
    subscription_fingerprint,
)


class _SubscriptionFlowMixin:

    @staticmethod
    def _subscription_unavailable_document(message: str) -> ReportDocument:
        """Return a safe recovery path when NLU or a quoted reference is unavailable."""

        return ReportDocument(
            title="无法创建报表订阅",
            fallback_text=message,
            quality="missing",
            blocks=(
                ReportBlock("note", {"content": message}),
                ReportBlock(
                    "actions",
                    {
                        "actions": [
                            {
                                "action_id": "subscriptions",
                                "label": "打开订阅中心",
                                "style": "primary",
                            }
                        ]
                    },
                ),
            ),
        )


    async def _subscription_preview(
        self,
        *,
        report_type: str,
        tenant_scope: str,
        tenant_aliases: list[str],
        model_scope: str,
        models: list[str],
        recurrence: str,
        send_time: str,
        weekday: int,
        month_day: int,
        inherit_report_scope: bool,
        reference_message_id: str,
    ) -> ToolResult:
        """Resolve NLU output and produce an explicit, opaque confirmation action."""

        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        if not user_id or not self._store.allowed(
            channel, user_id, "capability", "subscriptions"
        ):
            return ToolResult.error("Error: no permission to manage report subscriptions")
        reference = None
        if reference_message_id:
            if (
                not self._flag("cube_report_reference_subscription")
                or str(metadata.get("parent_id") or "") != reference_message_id
            ):
                return self._result(
                    self._subscription_unavailable_document(
                        "无法从该卡片恢复可验证的报表范围。请重新生成报表，或在订阅中心选择客户和模型。"
                    )
                )
            reference = self._store.message_reference(
                channel=channel,
                chat_id=chat_id,
                message_id=reference_message_id,
            )
            if reference is None:
                return self._result(
                    self._subscription_unavailable_document(
                        "引用报表已过期或不是可订阅的结构化报表。请重新生成报表后再引用。"
                    )
                )

        # Shared single-source table: report type -> (data period, variant).
        # Every concrete subscription report type must appear here or the
        # preview rejects it as unsupported.
        safe_report_types = SUBSCRIPTION_REPORT_TYPE_TABLE
        unresolved: list[dict[str, str]] = []
        display_names: list[str] = []

        async def resolve_tenants(
            queries: list[str],
        ) -> tuple[list[dict[str, str]], list[dict[str, str]]] | None:
            resolver = getattr(self._magik_tool, "resolve_tenant_queries", None)
            if not callable(resolver):
                return None
            try:
                return await resolver(queries)
            except Exception as exc:
                logger.warning(
                    "Cube subscription tenant resolution failed: error_type={}",
                    type(exc).__name__,
                )
                return None

        def unique_resolved(
            items: list[dict[str, str]],
        ) -> list[dict[str, str]]:
            """Collapse aliases that resolve to one tenant before fan-out."""

            result: list[dict[str, str]] = []
            seen: set[str] = set()
            for item in items:
                tenant_id = str(item.get("tenant_id") or "").strip()
                if not tenant_id or tenant_id in seen:
                    continue
                seen.add(tenant_id)
                result.append(item)
            return result

        def apply_tenant_scope(
            target: dict[str, Any],
            resolved: list[dict[str, str]],
            *,
            selected_model_scope: str,
            selected_models: list[str],
        ) -> None:
            tenant_ids = [item["tenant_id"] for item in resolved]
            target["tenants"] = tenant_ids
            # Persist display labels next to the verified IDs.  Labels are only
            # presentation metadata; every later query and authorization check
            # continues to use the exact Cube tenant ID.
            target["tenant_labels"] = [
                str(item.get("display_name") or item.get("tenant_id") or "").strip()
                for item in resolved
            ]
            target["tenant_scope"] = "selected"
            target["all_tenants"] = False
            target["report_selections"] = [
                {
                    "tenant_query": tenant_id,
                    "model_scope": selected_model_scope,
                    "models": selected_models if selected_model_scope == "selected" else [],
                }
                for tenant_id in tenant_ids
            ]

        if inherit_report_scope:
            if reference is None or report_type != "inherit":
                return self._result(
                    self._subscription_unavailable_document(
                        "没有找到可继承的引用报表范围，请重新引用报表卡片。"
                    )
                )
            if reference.template_id not in SUBSCRIPTION_REPORT_TYPE_TABLE:
                return self._result(
                    self._subscription_unavailable_document(
                        "该报表类型当前不允许创建订阅。请在 Report platform → 报表类型中，"
                        "将订阅策略设为“全部授权用户”或“指定用户”；“显示订阅按钮”只控制按钮显示。"
                    )
                )
            data_period = reference.period
            params = {
                key: value
                for key, value in reference.scope.items()
                if key in _ALLOWED_REPORT_PARAM_KEYS
            }
            if tenant_scope == "all" or (
                tenant_scope == "inherit" and params.get("all_tenants") is True
            ):
                params.update(
                    {
                        "tenant_scope": "all",
                        "all_tenants": True,
                        "tenants": [],
                        "report_selections": [],
                    }
                )
                display_names = ["全部客户"]
            else:
                tenant_queries = (
                    list(tenant_aliases)
                    if tenant_scope == "selected"
                    else [str(item) for item in params.get("tenants") or []]
                )
                resolution = await resolve_tenants(tenant_queries)
                if resolution is None:
                    return self._result(
                        self._subscription_unavailable_document(
                            "Cube 客户目录当前不可用，请稍后重试或打开订阅中心。"
                        )
                    )
                resolved, unresolved = resolution
                resolved = unique_resolved(resolved)
                if not resolved:
                    details = "、".join(
                        f"{item['query']}（{item['reason']}）" for item in unresolved
                    )
                    return self._result(
                        self._subscription_unavailable_document(
                            f"没有匹配到可订阅客户：{details or '请重新选择客户'}"
                        )
                    )
                effective_model_scope = (
                    model_scope
                    if model_scope != "inherit"
                    else str(params.get("model_scope") or "summary")
                )
                effective_models = (
                    list(models)
                    if model_scope == "selected"
                    else [str(item) for item in params.get("models") or []]
                )
                # Preserve the reference's per-tenant model relationships:
                # apply_tenant_scope rebuilds selections from a flat list,
                # which would fabricate cross-tenant pairs the live catalog
                # never confirmed (observed live 2026-09-15 with a quoted
                # multi-customer hourly card).
                reference_selection_models = {
                    str(item.get("tenant_query") or "").strip(): [
                        str(model).strip()
                        for model in item.get("models") or []
                        if str(model).strip()
                    ]
                    for item in params.get("report_selections") or []
                    if isinstance(item, dict)
                    and str(item.get("tenant_query") or "").strip()
                    and item.get("models")
                }
                apply_tenant_scope(
                    params,
                    resolved,
                    selected_model_scope=effective_model_scope,
                    selected_models=effective_models,
                )
                if reference_selection_models and effective_model_scope == "selected":
                    for selection in params.get("report_selections") or []:
                        if not isinstance(selection, dict):
                            continue
                        tenant_id = str(selection.get("tenant_query") or "").strip()
                        if tenant_id in reference_selection_models:
                            selection["models"] = list(
                                reference_selection_models[tenant_id]
                            )
                display_names = [item["display_name"] for item in resolved]
            if model_scope != "inherit":
                params["model_scope"] = model_scope
                params["models"] = list(models) if model_scope == "selected" else []
                for selection in params.get("report_selections") or []:
                    if isinstance(selection, dict):
                        selection["model_scope"] = model_scope
                        selection["models"] = (
                            list(models) if model_scope == "selected" else []
                        )
        else:
            if report_type not in safe_report_types:
                return self._result(
                    self._subscription_unavailable_document("未识别出可订阅的 Cube 报表类型。")
                )
            data_period, report_variant = safe_report_types[report_type]
            # A long natural-language customer list is a product-level scope
            # signal.  The classifier may truncate that list, but once the
            # server has resolved more than one live tenant the subscription
            # must use the grouped template; otherwise the legacy one-tenant
            # brief silently drops all but the last selection.
            multi_scope_requested = (
                report_type in {
                    "usage_customer_model_daily_brief",
                    "usage_customer_model_weekly_brief",
                }
                or (tenant_scope == "selected" and len(tenant_aliases) > 1)
                or (tenant_scope == "all" and model_scope == "all")
            )
            if multi_scope_requested and report_type != "usage_customer_model_hourly_tpm":
                # The hourly TPM template is inherently a multi-customer
                # grouped report and must never be rewritten into a daily or
                # weekly brief by the scope-shape heuristic below.
                report_type = (
                    "usage_customer_model_weekly_brief"
                    if data_period == "week"
                    else "usage_customer_model_daily_brief"
                )
                data_period, report_variant = safe_report_types[report_type]
            params = {
                "report_variant": report_variant,
                "tenant_scope": tenant_scope,
                "model_scope": model_scope,
                "models": list(models),
                "report_template": "brief",
                "breakdown": "model" if model_scope in {"all", "selected"} else "summary",
            }
            if tenant_scope == "all":
                params["all_tenants"] = True
                params["tenants"] = []
                display_names = ["全部客户"]
            else:
                resolution = await resolve_tenants(tenant_aliases)
                if resolution is None:
                    return self._result(
                        self._subscription_unavailable_document(
                            "Cube 客户目录当前不可用，请稍后重试或打开订阅中心。"
                        )
                    )
                resolved, unresolved = resolution
                resolved = unique_resolved(resolved)
                tenant_ids = [item["tenant_id"] for item in resolved]
                display_names = [item["display_name"] for item in resolved]
                if not tenant_ids:
                    details = "、".join(
                        f"{item['query']}（{item['reason']}）" for item in unresolved
                    )
                    return self._result(
                        self._subscription_unavailable_document(
                            f"没有匹配到可订阅客户：{details or '请重新选择客户'}"
                        )
                    )
                apply_tenant_scope(
                    params,
                    resolved,
                    selected_model_scope=model_scope,
                    selected_models=list(models),
                )
                if len(tenant_ids) == 1 and report_variant == "usage_brief":
                    params["tenant_query"] = tenant_ids[0]

        if params.get("model_scope") == "selected":
            selected_tenants = [
                str(item).strip()
                for item in params.get("tenants") or []
                if str(item).strip()
            ]
            selected_models = [
                str(item).strip()
                for item in params.get("models") or []
                if str(item).strip()
            ]
            model_resolver = getattr(
                self._magik_tool, "resolve_models_for_tenants", None
            )
            # Per-tenant selections describe the confirmed customer/model
            # relationships. Validating the flat model list against every
            # tenant fabricates cross-tenant pairs the live catalog never
            # confirmed (observed live 2026-09-15 with a quoted multi-customer
            # hourly card), so prefer the per-tenant pairs whenever they
            # exist.
            per_tenant_pairs: list[tuple[str, list[str]]] = []
            for item in params.get("report_selections") or []:
                if not isinstance(item, dict):
                    continue
                tenant_id = str(item.get("tenant_query") or "").strip()
                values = [
                    str(model).strip()
                    for model in item.get("models") or []
                    if str(model).strip()
                ]
                if (
                    tenant_id
                    and values
                    and str(item.get("model_scope") or "") == "selected"
                ):
                    per_tenant_pairs.append((tenant_id, values))
            if per_tenant_pairs and callable(model_resolver):
                resolved_by_tenant: dict[str, list[str]] = {}
                unresolved_models: list[dict[str, Any]] = []
                resolver_failed = False
                for tenant_id, values in per_tenant_pairs:
                    try:
                        response = await model_resolver([tenant_id], values)
                    except Exception as exc:
                        logger.warning(
                            "Cube subscription model resolution failed: error_type={}",
                            type(exc).__name__,
                        )
                        resolver_failed = True
                        break
                    if not (isinstance(response, tuple) and len(response) == 2):
                        resolver_failed = True
                        break
                    tenant_models, unresolved = response
                    if isinstance(tenant_models, dict):
                        for key, model_values in tenant_models.items():
                            resolved_by_tenant[str(key)] = [
                                str(model).strip()
                                for model in model_values
                                if str(model).strip()
                            ]
                    unresolved_models.extend(
                        item for item in unresolved or [] if isinstance(item, dict)
                    )
                if resolver_failed:
                    return self._result(
                        self._subscription_unavailable_document(
                            "Cube 模型目录当前不可用，请稍后重试或打开订阅中心。"
                        )
                    )
                if unresolved_models:
                    details = "、".join(
                        f"{item.get('tenant_id')} / {item.get('model')}（{item.get('reason')}）"
                        for item in unresolved_models
                    )
                    return self._result(
                        self._subscription_unavailable_document(
                            f"指定模型无法通过实时目录校验：{details}"
                        )
                    )
                params["models"] = list(
                    dict.fromkeys(
                        model
                        for values in resolved_by_tenant.values()
                        for model in values
                    )
                )
                for selection in params.get("report_selections") or []:
                    if not isinstance(selection, dict):
                        continue
                    tenant_id = str(selection.get("tenant_query") or "")
                    selection["models"] = list(resolved_by_tenant.get(tenant_id, []))
            elif selected_tenants and selected_models:
                if not callable(model_resolver):
                    return self._result(
                        self._subscription_unavailable_document(
                            "Cube 模型目录当前不可用，请稍后重试或打开订阅中心。"
                        )
                    )
                try:
                    tenant_models, unresolved_models = await model_resolver(
                        selected_tenants,
                        selected_models,
                    )
                except Exception as exc:
                    logger.warning(
                        "Cube subscription model resolution failed: error_type={}",
                        type(exc).__name__,
                    )
                    return self._result(
                        self._subscription_unavailable_document(
                            "Cube 模型目录当前不可用，请稍后重试或打开订阅中心。"
                        )
                    )
                if unresolved_models:
                    details = "、".join(
                        f"{item['tenant_id']} / {item['model']}（{item['reason']}）"
                        for item in unresolved_models
                    )
                    return self._result(
                        self._subscription_unavailable_document(
                            f"指定模型无法通过实时目录校验：{details}"
                        )
                    )
                params["models"] = list(
                    dict.fromkeys(
                        model
                        for tenant_id in selected_tenants
                        for model in tenant_models.get(tenant_id, [])
                    )
                )
                for selection in params.get("report_selections") or []:
                    if not isinstance(selection, dict):
                        continue
                    tenant_id = str(selection.get("tenant_query") or "")
                    selection["models"] = list(tenant_models.get(tenant_id, []))

        params["subscription_period"] = data_period
        if data_period == "recent1h":
            # Hourly TPM only makes sense with an hourly cadence; any other
            # recurrence would compile a wrong cron and mislead the user.
            if recurrence != "hourly":
                return self._result(
                    self._subscription_unavailable_document(
                        "小时 TPM 报表当前仅支持每小时播报，请使用“每小时播报…”的表达。"
                    )
                )
            params["report_variant"] = "customer_model_hourly_tpm"
            params["report_template_id"] = "usage_customer_model_hourly_tpm"
        if str(params.get("report_variant") or "") in {
            "customer_model_daily_brief",
            "customer_model_weekly_brief",
            "customer_model_hourly_tpm",
        }:
            # Family availability is governed by the always-enforced template
            # policy (the denial check below rejects disabled templates and
            # disabled subscription modes); the retired runtime flags are
            # gone.
            params["report_template"] = "brief"
        try:
            params = self._safe_report_params(params)
        except ValueError as exc:
            return ToolResult.error(f"Error: {exc}")
        denial = authorize_magik_params(
            self._store, channel=channel, user_id=user_id, params=params
        )
        if denial:
            return ToolResult.error(denial)

        template_id = (
            "usage_customer_model_daily_brief"
            if str(params.get("report_variant") or "") == "customer_model_daily_brief"
            else "usage_customer_model_weekly_brief"
            if str(params.get("report_variant") or "") == "customer_model_weekly_brief"
            else "usage_customer_model_hourly_tpm"
            if str(params.get("report_variant") or "") == "customer_model_hourly_tpm"
            else BRIEF_PERIOD_TEMPLATES[data_period]
        )
        policy_denial = self._subscription_policy_denial(
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        if policy_denial:
            return ToolResult.error(policy_denial)

        schedule_period = RECURRENCE_SCHEDULE_PERIODS[recurrence]
        subscribe_params = {
            "action": "subscribe",
            "period": schedule_period,
            "report_family": "usage",
            "report_params": params,
            "send_time": send_time,
            "daily_mode": "workdays" if recurrence == "workdays" else "every_day",
            "weekday": weekday,
            "month_day": month_day,
        }
        unresolved_text = ""
        if unresolved:
            unresolved_text = "\n**未包含**：" + "、".join(
                f"{item['query']}（{item['reason']}）" for item in unresolved
            )
        model_text = (
            "每个客户的全部模型"
            if params.get("model_scope") == "all"
            else "、".join(str(item) for item in params.get("models") or [])
            if params.get("model_scope") == "selected"
            else "汇总"
        )
        recurrence_text = {
            "every_day": "每天",
            "workdays": "每个工作日",
            "weekly": f"每周{'一二三四五六日'[weekday - 1]}",
            "monthly": f"每月 {month_day} 日",
            "hourly": "每小时（整点后 5 分钟）",
        }[recurrence]
        # The hourly cadence is clock-driven, so the placeholder send_time is
        # never shown to the user.
        send_time_text = "" if recurrence == "hourly" else f" {send_time}"
        content = (
            f"**客户**：{'、'.join(display_names)}\n"
            f"**模型**：{model_text}\n"
            f"**发送计划**：{recurrence_text}{send_time_text}\n"
            f"**时区**：{self._config.timezone}{unresolved_text}"
        )
        if reference is not None:
            content += "\n**说明**：引用卡片的历史日期不会固化，发送时使用最近完整周期。"
        action_label = "确认仅订阅已匹配客户" if unresolved else "确认创建订阅"
        document = ReportDocument(
            title="确认 Cube 报表订阅",
            subtitle=f"{recurrence_text}{send_time_text}｜{self._config.timezone}",
            fallback_text=content,
            blocks=(
                ReportBlock("markdown", {"content": content}),
                ReportBlock(
                    "actions",
                    {
                        "actions": [
                            {
                                "action_id": "subscription_confirm",
                                "label": action_label,
                                "style": "primary",
                                "tool_name": "report_center",
                                "params": subscribe_params,
                                "content": "确认创建 Cube 报表订阅",
                            },
                            {
                                "action_id": "subscriptions",
                                "label": "取消并打开订阅中心",
                                "style": "default",
                            },
                        ]
                    },
                ),
            ),
        )
        return self._result(document)


    def _safe_report_params(self, value: Any) -> dict[str, Any]:
        """Normalize subscription scope while keeping internal controls server-owned.

        Subscription forms round-trip the already normalized parameters. Internal
        controls such as ``save_snapshot`` must therefore be removed before the
        external allowlist check and re-applied with the safe fixed value below.
        """

        if not isinstance(value, dict):
            raise ValueError("report_params must be an object")
        candidate = dict(value)
        candidate.pop("save_snapshot", None)
        unknown = set(candidate) - _ALLOWED_REPORT_PARAM_KEYS
        if unknown:
            raise ValueError("unsupported report subscription parameters")
        params = {
            key: candidate[key]
            for key in candidate
            if key in _ALLOWED_REPORT_PARAM_KEYS
        }
        params.setdefault(
            "report_template",
            "brief" if self._usage_brief_default_enabled else "matrix_card",
        )
        params["save_snapshot"] = False
        params.pop("start_date", None)
        params.pop("end_date", None)
        return params


    def _subscription_service_for_confirmed_scope(
        self,
        *,
        tenant_records: list[dict[str, str]] | None = None,
        model_records: dict[str, list[str]] | None = None,
        catalog_tenants: list[dict[str, Any]] | None = None,
    ) -> ReportSubscriptionService:
        """Build the shared service for a server-resolved channel confirmation.

        ``ReportCenterTool`` owns the async Cube adapters, while
        ``ReportSubscriptionService`` owns synchronous persistence and Cron
        mutation.  The small config adapter keeps that dependency direction
        explicit and prevents the service from depending on channel code.
        ``tenant_records`` and ``model_records`` are bounded snapshots used
        only to re-check a confirmation; scheduled executions still refresh
        ``all`` model scopes from Cube.
        """

        tenant_by_id = {
            str(item.get("tenant_id") or item.get("tenantId") or "").strip(): item
            for item in (tenant_records or [])
            if str(item.get("tenant_id") or item.get("tenantId") or "").strip()
        }

        def resolve_tenants(requested: list[str]):
            resolved: list[dict[str, str]] = []
            unresolved: list[dict[str, str]] = []
            for value in requested:
                record = tenant_by_id.get(value)
                if record is None:
                    unresolved.append({"query": value, "reason": "客户不在已验证目录中"})
                    continue
                resolved.append(
                    {
                        "query": value,
                        "tenant_id": value,
                        "display_name": str(
                            record.get("display_name")
                            or record.get("displayName")
                            or record.get("name")
                            or value
                        ).strip(),
                    }
                )
            return resolved, unresolved

        def resolve_models(tenant_ids: list[str], requested: list[str]):
            resolved: dict[str, list[str]] = {}
            unresolved: list[dict[str, str]] = []
            for tenant_id in tenant_ids:
                available = set(model_records.get(tenant_id, ())) if model_records else set()
                selected = [model for model in requested if model in available]
                resolved[tenant_id] = selected
                unresolved.extend(
                    {
                        "tenant_id": tenant_id,
                        "model": model,
                        "reason": "模型不在确认时的实时目录中",
                    }
                    for model in requested
                    if model not in available
                )
            return resolved, unresolved

        adapter = SimpleNamespace(
            workspace_path=None,
            tools=SimpleNamespace(
                reporting=_EffectiveFlagConfigView(self._config, self._flag)
            ),
            agents=SimpleNamespace(
                defaults=SimpleNamespace(unified_session=False),
            ),
        )
        return ReportSubscriptionService(
            config=adapter,
            store=self._store,
            registry=self._registry,
            cron=self._cron,
            tenant_resolver=resolve_tenants if tenant_by_id else None,
            model_resolver=resolve_models if model_records is not None else None,
            catalog_tenants=catalog_tenants or tenant_records or [],
        )


    async def _revalidate_confirmed_usage_scope(
        self, params: dict[str, Any]
    ) -> tuple[
        list[dict[str, str]],
        dict[str, list[str]] | None,
        list[dict[str, Any]],
        str | None,
    ]:
        """Re-check IDs in a confirmation without trusting rendered card text.

        The preview already resolves human names, but the confirmation action
        is still client-controlled.  A real Cube adapter result is therefore
        preferred; a bounded opaque-ID fallback is retained only for legacy
        adapters that do not expose a resolver.  It rejects aliases, URLs and
        control characters rather than assuming a particular Cube ID prefix.
        """

        all_tenants = params.get("all_tenants") is True or params.get("tenant_scope") == "all"
        requested = [
            str(item).strip()
            for item in params.get("tenants") or []
            if str(item).strip()
        ]
        if not requested and params.get("tenant_query"):
            requested = [str(params["tenant_query"]).strip()]
        records: list[dict[str, str]] = []
        catalog: list[dict[str, Any]] = []
        resolver = getattr(self._magik_tool, "resolve_tenant_queries", None)
        # Normalized confirmations carry ``tenants``/``tenant_scope``.  A
        # legacy row may contain only ``tenant_query`` and is intentionally
        # allowed to use the bounded opaque-ID fallback during the migration
        # window; probing an absent/old adapter in that path would turn a
        # compatibility subscription into a false catalog outage.
        normalized_scope = bool(
            "tenants" in params
            or "tenant_scope" in params
            or "report_variant" in params
        )
        if not all_tenants and requested and callable(resolver) and normalized_scope:
            try:
                response = await resolver(requested)
            except Exception as exc:
                logger.warning(
                    "Cube subscription confirmation tenant check failed: error_type={}",
                    type(exc).__name__,
                )
                return [], None, [], "客户目录在确认时不可用，请重新选择客户"
            if not (isinstance(response, tuple) and len(response) == 2):
                return [], None, [], "客户范围在确认时无法验证，请重新选择客户"
            if isinstance(response, tuple) and len(response) == 2:
                resolved, unresolved = response
                if unresolved:
                    return [], None, [], "客户范围在确认时已变化，请重新选择客户"
                if isinstance(resolved, list):
                    records = [item for item in resolved if isinstance(item, dict)]
                    requested_values = list(dict.fromkeys(requested))
                    resolved_ids = [
                        str(item.get("tenant_id") or item.get("tenantId") or "").strip()
                        for item in records
                    ]
                    query_values = [
                        str(item.get("query") or "").strip().casefold() for item in records
                    ]
                    query_match_valid = True
                    ids_are_requested_values = set(resolved_ids) == set(requested_values)
                    if any(query_values) and not ids_are_requested_values:
                        query_match_valid = (
                            all(query_values)
                            and len(set(query_values)) == len(query_values)
                            and set(query_values) == {item.casefold() for item in requested_values}
                        )
                    if (
                        len(records) != len(requested_values)
                        or any(not _SAFE_CUBE_ID_RE.fullmatch(item) for item in resolved_ids)
                        or len(set(resolved_ids)) != len(resolved_ids)
                        or not query_match_valid
                    ):
                        return [], None, [], "客户范围在确认时未能完整验证，请重新选择客户"
                    params["tenants"] = [
                        str(item.get("tenant_id") or item.get("tenantId") or "").strip()
                        for item in records
                    ]
                    params["tenant_labels"] = [
                        str(
                            item.get("display_name")
                            or item.get("displayName")
                            or item.get("name")
                            or item.get("tenant_id")
                            or item.get("tenantId")
                            or ""
                        ).strip()
                        for item in records
                    ]
                    requested = list(params["tenants"])
        if not records and not all_tenants:
            if not requested:
                return [], None, [], "没有可验证的客户范围，请重新选择客户"
            if any(not _SAFE_CUBE_ID_RE.fullmatch(value) for value in requested):
                return [], None, [], "客户身份无法再次验证，请重新生成选择器"
            records = [
                {
                    "tenant_id": value,
                    "display_name": str(
                        (params.get("tenant_labels") or [])[index]
                        if index < len(params.get("tenant_labels") or [])
                        else value
                    ),
                }
                for index, value in enumerate(requested)
            ]
        if all_tenants:
            loader = getattr(self._magik_tool, "list_tenant_catalog", None)
            if callable(loader):
                try:
                    loaded = await loader(limit=20)
                except Exception as exc:
                    logger.warning(
                        "Cube subscription confirmation catalog check failed: error_type={}",
                        type(exc).__name__,
                    )
                    return [], None, [], "Cube 客户目录在确认时不可用，请重新选择客户"
                if not isinstance(loaded, list):
                    return [], None, [], "Cube 客户目录在确认时无法验证，请重新选择客户"
                catalog = [item for item in loaded if isinstance(item, dict)]
                if not catalog:
                    return [], None, [], "Cube 客户目录在确认时为空，请重新选择客户"

        model_records: dict[str, list[str]] | None = None
        selected_models = [
            str(item).strip()
            for item in params.get("models") or []
            if str(item).strip()
        ]
        if str(params.get("model_scope") or "") == "selected" and selected_models:
            tenant_ids = requested or [
                str(item.get("tenant_id") or item.get("tenantId") or "").strip()
                for item in catalog
            ]
            model_resolver = getattr(self._magik_tool, "resolve_models_for_tenants", None)
            if tenant_ids and callable(model_resolver):
                try:
                    response = await model_resolver(tenant_ids, selected_models)
                except Exception as exc:
                    logger.warning(
                        "Cube subscription confirmation model check failed: error_type={}",
                        type(exc).__name__,
                    )
                    response = None
                if isinstance(response, tuple) and len(response) == 2:
                    resolved_models, unresolved_models = response
                    if unresolved_models:
                        return [], None, [], "模型范围在确认时已变化，请重新选择模型"
                    if isinstance(resolved_models, dict):
                        model_records = {
                            str(tenant_id): [
                                str(model).strip()
                                for model in values
                                if str(model).strip()
                            ]
                            for tenant_id, values in resolved_models.items()
                            if isinstance(values, (list, tuple, set))
                        }
                        params["models"] = list(
                            dict.fromkeys(
                                model
                                for values in model_records.values()
                                for model in values
                            )
                        )
                        for selection in params.get("report_selections") or []:
                            if isinstance(selection, dict):
                                tenant_id = str(selection.get("tenant_query") or "")
                                selection["models"] = list(model_records.get(tenant_id, ()))
        return records, model_records, catalog, None


    async def _subscribe_usage_via_service(
        self,
        *,
        period: str,
        report_params: dict[str, Any],
        channel: str,
        chat_id: str,
        user_id: str,
        send_time: str,
        daily_mode: str,
        weekday: int,
        month_day: int,
    ) -> ToolResult:
        """Create usage subscriptions through the shared guided service."""

        if period not in {"day", "week", "month", "recent1h"}:
            return ToolResult.error("Error: subscription period must be day, week, or month")
        params = dict(report_params)
        records, model_records, catalog, error = await self._revalidate_confirmed_usage_scope(
            params
        )
        if error:
            return ToolResult.error(f"Error: {error}")
        data_period = str(params.get("subscription_period") or period)
        if data_period not in {"day", "week", "month", "recent1h"}:
            return ToolResult.error("Error: invalid report data period")
        report_variant = str(params.get("report_variant") or "")
        report_template = str(params.get("report_template") or "brief")
        template_id = self._subscription_service_template_id(
            data_period=data_period,
            report_template=report_template,
            report_variant=report_variant,
        )
        # The guided service enforces the template policy itself; the retired
        # multi-scope runtime flag no longer gates this path.
        if period == "recent1h":
            recurrence = "hourly"
        elif period == "week":
            recurrence = "weekly"
        elif period == "month":
            recurrence = "monthly"
        else:
            recurrence = "workdays" if daily_mode == "workdays" else "every_day"
        tenant_scope = (
            "all"
            if params.get("all_tenants") is True or params.get("tenant_scope") == "all"
            else "selected"
        )
        tenants = [
            str(item).strip()
            for item in params.get("tenants") or []
            if str(item).strip()
        ]
        if not tenants and params.get("tenant_query"):
            tenants = [str(params["tenant_query"]).strip()]
        model_scope = str(params.get("model_scope") or "")
        if not model_scope:
            model_scope = "selected" if params.get("model") else "summary"
        models = [
            str(item).strip()
            for item in params.get("models") or []
            if str(item).strip()
        ]
        if not models and params.get("model"):
            models = [str(params["model"]).strip()]
        form = {
            "template_id": template_id,
            "channel": channel,
            "chat_id": chat_id,
            "user_id": user_id,
            "tenant_scope": tenant_scope,
            "tenants": tenants,
            "tenant_labels": params.get("tenant_labels") or [],
            "model_scope": model_scope,
            "models": models,
            "period": data_period,
            "recurrence": recurrence,
            "send_time": send_time,
            "weekday": weekday,
            "month_day": month_day,
            "timezone": self._config.timezone,
            "project": params.get("project", ""),
            "endpoint": params.get("endpoint", ""),
            "provider": params.get("provider", ""),
            "cluster": params.get("cluster", ""),
        }
        service = self._subscription_service_for_confirmed_scope(
            tenant_records=records,
            model_records=model_records,
            catalog_tenants=catalog,
        )
        try:
            subscription = service.create(form, updated_by=user_id)
        except SubscriptionServiceError as exc:
            if exc.status == 409 and "identical" in exc.message:
                return ToolResult("相同报表和发送计划的订阅已经存在。")
            return ToolResult.error(f"Error: {exc.message}")
        return self._result(subscription_created_document(subscription))


    async def _subscribe(
        self,
        *,
        period: str,
        report_family: str,
        report_params: Any,
        channel: str,
        chat_id: str,
        user_id: str,
        session_key: str,
        metadata: dict[str, Any],
        send_time: str,
        daily_mode: str,
        weekday: int,
        month_day: int,
    ) -> ToolResult:
        if self._cron is None:
            return ToolResult.error("Error: report subscriptions require the Gateway Cron service")
        if not user_id or not self._store.allowed(
            channel, user_id, "capability", "subscriptions"
        ):
            return ToolResult.error("Error: no permission to manage report subscriptions")
        params = self._safe_report_params(report_params)
        saved_family = str(params.get("report_family") or "")
        hourly_template = str(params.get("report_template_id") or params.get("template_id") or "")
        hourly_requested = period == "recent1h" or hourly_template == "usage_customer_model_hourly_tpm"
        if saved_family == "health" and report_family == "usage":
            report_family = saved_family
        report_family = report_family or saved_family or "usage"
        if report_family not in {"usage", "health", "cost", "provider_quality"}:
            return ToolResult.error("Error: unsupported report family")
        # Provider-quality and health subscription availability is governed by
        # the always-enforced template policy (subscription_mode), checked by
        # the policy denial below; the retired family flags no longer gate.
        if report_family == "provider_quality":
            if period not in {"day", "week"}:
                return ToolResult.error("Error: provider quality subscriptions support day or week only")
        elif report_family == "health":
            if period not in {"day", "week"}:
                return ToolResult.error("Error: health subscriptions support day or week only")
        elif report_family == "cost":
            if not self.cost_subscriptions_enabled:
                return ToolResult.error("Error: Cube cost/account subscription is not enabled")
            if period != "month":
                return ToolResult.error("Error: cost/account subscriptions support month only")
        elif report_family == "usage":
            if not self._flag("cube_subscription"):
                return ToolResult.error("Error: Cube usage subscription is not enabled")
            if hourly_requested:
                # Creation-time availability is enforced by the template
                # policy (subscription_mode) later in this action.
                period = "recent1h"
                params["report_template_id"] = "usage_customer_model_hourly_tpm"
            elif period not in PERIOD_TEMPLATES:
                return ToolResult.error("Error: subscription period must be day, week, or month")
            # Day/week/month confirmations use the same typed compiler as the
            # WebUI; multi-selection matrix subscriptions keep the bounded
            # legacy path for custom windows. Hourly (recent1h) always uses
            # the service — its schedule is clock-driven and carries no
            # legacy semantics.
            selections_count = len(
                [
                    item
                    for item in params.get("report_selections") or []
                    if isinstance(item, dict)
                ]
            )
            legacy_matrix_shape = (
                selections_count > 1
                and str(params.get("report_variant") or "") != "customer_model_daily_brief"
                and str(params.get("report_variant") or "") != "customer_model_hourly_tpm"
            )
            if (
                period in {"day", "week", "month"} and not legacy_matrix_shape
            ) or period == "recent1h":
                if not user_id or not self._authorized_for_magik(channel, user_id):
                    return ToolResult.error("Error: no permission for the Magik Cube connector")
                params.setdefault(
                    "report_template",
                    "brief" if self._usage_brief_default_enabled else "matrix_card",
                )
                return await self._subscribe_usage_via_service(
                    period=period,
                    report_params=params,
                    channel=channel,
                    chat_id=chat_id,
                    user_id=user_id,
                    send_time=send_time,
                    daily_mode=daily_mode,
                    weekday=weekday,
                    month_day=month_day,
                )
        # Delivery cadence and report data period are independent. For example,
        # a daily report can be delivered on workdays or once every Monday.
        data_period = str(params.get("subscription_period") or period)
        if data_period not in {"day", "week", "month", "recent1h"}:
            return ToolResult.error("Error: invalid report data period")
        if report_family == "provider_quality":
            if not user_id or not self.provider_quality_connector_enabled:
                return ToolResult.error("Error: no permission for the Cube provider quality connector")
            if not self._store.allowed(channel, user_id, "connector", "cube_provider_quality"):
                return ToolResult.error("Error: no permission for the Cube provider quality connector")
            if not self._store.allowed(channel, user_id, "template", "provider_quality"):
                return ToolResult.error("Error: no permission for the Cube provider quality template")
            params["report_family"] = report_family
            params["subscription_period"] = period
        elif not user_id or not self._authorized_for_magik(channel, user_id):
            return ToolResult.error("Error: no permission for the Magik Cube connector")
        if report_family in {"health", "cost"}:
            params["report_family"] = report_family
            params.setdefault("subscription_period", data_period)
        elif report_family == "usage" and params.get("report_template") == "brief":
            params.setdefault("subscription_period", data_period)
        denial = None if report_family == "provider_quality" else authorize_magik_params(
            self._store, channel=channel, user_id=user_id, params=params
        )
        if denial:
            return ToolResult.error(denial)
        try:
            cron_expr = build_subscription_schedule(
                period,
                send_time=send_time,
                daily_mode=daily_mode,
                weekday=weekday,
                month_day=month_day,
            )
        except ValueError as exc:
            return ToolResult.error(f"Error: invalid subscription schedule: {exc}")
        report_variant = str(params.get("report_variant") or "")
        if report_variant == "customer_model_daily_brief":
            if data_period != "day":
                return ToolResult.error(
                    "Error: multi-customer model subscriptions require the daily brief"
                )
            template_id = "usage_customer_model_daily_brief"
        else:
            template_id = (
                "usage_customer_model_hourly_tpm"
                if hourly_requested
                else "health_sre"
                if report_family == "health"
                else "cost_account"
                if report_family == "cost"
                else "provider_quality"
                if report_family == "provider_quality"
                else BRIEF_PERIOD_TEMPLATES[data_period]
                if params.get("report_template") == "brief"
                else PERIOD_TEMPLATES[data_period]
            )
        template = self._registry.template(template_id)
        if (
            report_family == "usage"
            and params.get("report_template") == "brief"
            and (
                template is None
                or template.manifest.lifecycle_state not in {"publish", "canary"}
            )
        ):
            return ToolResult.error("Error: Cube usage brief template is not available")
        if template is not None:
            params["calculation_version"] = template.manifest.version
        policy_denial = self._subscription_policy_denial(
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        if policy_denial:
            return ToolResult.error(policy_denial)
        if report_family == "health":
            params["threshold_version"] = "health-default-v1"
        # Legacy-path fingerprint uses the same single-source identity as the
        # guided service so cross-entry duplicates are detected.
        fingerprint = subscription_fingerprint(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            template_id=template_id,
            schedule=cron_expr,
            timezone_name=self._config.timezone,
            report_params=params,
        )
        subscription_id = uuid.uuid4().hex[:16]
        origin_metadata = {
            key: value
            for key, value in metadata.items()
            if key not in {OUTBOUND_META_AGENT_UI, INBOUND_META_DIRECT_TOOL}
        }
        origin_metadata[INBOUND_META_DIRECT_TOOL] = {
            "name": "report_center",
            "params": {"action": "run_subscription", "subscription_id": subscription_id},
        }
        origin_metadata["direct_request_text"] = "执行固定报表订阅"
        job = self._cron.add_job(
            name=f"固定{period}报订阅",
            schedule=CronSchedule(kind="cron", expr=cron_expr, tz=self._config.timezone),
            message="执行固定报表订阅",
            session_key=session_key,
            origin_channel=channel,
            origin_chat_id=chat_id,
            origin_metadata=origin_metadata,
        )
        now = datetime.now().astimezone().isoformat()
        subscription = ReportSubscription(
            subscription_id=subscription_id,
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            connector_id=("cube_provider_quality" if report_family == "provider_quality" else "magik_cube"),
            template_id=template_id,
            template_version=template.manifest.version if template is not None else "1.0",
            schedule=cron_expr,
            timezone=self._config.timezone,
            report_params=params,
            cron_job_id=job.id,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        if not self._store.add_subscription(subscription, fingerprint):
            self._cron.remove_job(job.id)
            return ToolResult("相同报表和发送计划的订阅已经存在。")
        return self._result(subscription_created_document(subscription))


    def _subscription_setup(
        self,
        *,
        period: str,
        report_family: str,
        report_params: Any,
        channel: str,
        user_id: str,
    ) -> ToolResult:
        if self._cron is None:
            return ToolResult.error("Error: report subscriptions require the Gateway Cron service")
        if not user_id or not self._store.allowed(
            channel, user_id, "capability", "subscriptions"
        ):
            return ToolResult.error("Error: no permission to manage report subscriptions")
        params = self._safe_report_params(report_params)
        # Compute the hourly marker once for every family so the template-id
        # selection below cannot reference an unbound variable when the
        # request is a health or cost subscription.
        hourly_template = str(params.get("report_template_id") or params.get("template_id") or "")
        hourly_requested = period == "recent1h" or hourly_template == "usage_customer_model_hourly_tpm"
        # Provider-quality and health subscription availability is governed by
        # the always-enforced template policy (subscription_mode), checked by
        # the policy denial below; the retired family flags no longer gate.
        if report_family == "provider_quality":
            if period not in {"day", "week"}:
                return ToolResult.error("Error: provider quality subscriptions support day or week only")
        elif report_family == "health":
            if period not in {"day", "week"}:
                return ToolResult.error("Error: health subscriptions support day or week only")
        elif report_family == "cost":
            if not self.cost_subscriptions_enabled:
                return ToolResult.error("Error: Cube cost/account subscription is not enabled")
            if period != "month":
                return ToolResult.error("Error: cost/account subscriptions support month only")
        elif report_family == "usage":
            if not self._flag("cube_subscription"):
                return ToolResult.error("Error: Cube usage subscription is not enabled")
            if hourly_requested:
                # Creation-time availability is enforced by the template
                # policy (subscription_mode) later in this action.
                period = "recent1h"
                params["report_template_id"] = "usage_customer_model_hourly_tpm"
            elif period not in PERIOD_TEMPLATES:
                return ToolResult.error("Error: subscription period must be day, week, or month")
        else:
            return ToolResult.error("Error: unsupported report family")
        if report_family == "provider_quality":
            authorized = bool(
                user_id
                and self.provider_quality_connector_enabled
                and self._store.allowed(channel, user_id, "connector", "cube_provider_quality")
                and self._store.allowed(channel, user_id, "template", "provider_quality")
            )
        else:
            authorized = bool(user_id and self._authorized_for_magik(channel, user_id))
        if not authorized:
            return ToolResult.error(
                "Error: no permission for the Cube provider quality connector"
                if report_family == "provider_quality"
                else "Error: no permission for the Magik Cube connector"
            )
        params = self._safe_report_params(report_params)
        if report_family in {"health", "cost", "provider_quality"}:
            params["report_family"] = report_family
            params["subscription_period"] = period
        elif params.get("report_template") == "brief":
            params["subscription_period"] = period
        report_variant = str(params.get("report_variant") or "")
        if report_variant in {
            "customer_model_daily_brief",
            "customer_model_weekly_brief",
        }:
            expected_period = (
                "day" if report_variant == "customer_model_daily_brief" else "week"
            )
            # Availability is enforced by the template policy; only the
            # period contract remains here.
            if period != expected_period:
                return ToolResult.error(
                    "Error: multi-customer model subscription period is not enabled"
                )
            template_id = (
                "usage_customer_model_daily_brief"
                if expected_period == "day"
                else "usage_customer_model_weekly_brief"
            )
        else:
            template_id = (
                "usage_customer_model_hourly_tpm"
                if hourly_requested
                else "health_sre"
                if report_family == "health"
                else "cost_account"
                if report_family == "cost"
                else "provider_quality"
                if report_family == "provider_quality"
                else BRIEF_PERIOD_TEMPLATES[period]
                if params.get("report_template") == "brief"
                else PERIOD_TEMPLATES[period]
            )
        policy_denial = self._subscription_policy_denial(
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        if policy_denial:
            return ToolResult.error(policy_denial)
        denial = None if report_family == "provider_quality" else authorize_magik_params(
            self._store, channel=channel, user_id=user_id, params=params
        )
        if denial:
            return ToolResult.error(denial)
        return ToolResult(
            "请选择报表发送周期和时间。",
            metadata={
                OUTBOUND_META_AGENT_UI: {
                    "kind": "report_subscription_form",
                    "version": 1,
                    "title": "设置报表订阅",
                    "default_period": period,
                    "default_time": "10:00",
                    "timezone": self._config.timezone,
                    "report_params": params,
                }
            },
        )
