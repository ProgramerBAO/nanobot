"""Manual report execution adapters over the shared ReportRunner."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.agent.tools.base import ToolResult
from nanobot.reporting import (
    CubeProviderQualityConnector,
    ReportIntent,
    ReportRunContext,
    ReportRunner,
)
from nanobot.reporting.capabilities import (
    template_enabled,
)
from nanobot.reporting.interactions import report_interactions
from nanobot.reporting.provider_quality import provider_quality_selector_document
from nanobot.reporting.schedules import (
    BRIEF_PERIOD_TEMPLATES,
    PERIOD_TEMPLATES,
)


class _ReportExecutionMixin:

    def _translate_legacy_usage_request(self, raw: str) -> dict[str, Any] | None:
        """Reuse the mature Cube parser, then execute standard reports through ReportRunner."""

        if self._magik_tool is None:
            return None
        params = self._magik_tool.match_direct_request(raw)
        if not isinstance(params, dict):
            return None
        legacy_template = str(params.get("report_template") or "")
        if legacy_template in {"full", "usage_total"}:
            return None
        start_date = str(params.get("start_date") or "")
        end_date = str(params.get("end_date") or "")
        if not start_date or not end_date:
            return None
        if "月报" in raw or "上月" in raw or "上个月" in raw:
            period = "month"
        elif "周报" in raw or "上周" in raw:
            period = "week"
        elif start_date == end_date:
            period = "day"
        else:
            period = "range"
        result = {
            "action": "cube_report",
            "period": period,
            "report_template": self._requested_usage_template(
                "详细" if re.search(r"(?:详细|明细)", raw) else None
            ),
            "tenant_query": str(params.get("tenant_query") or ""),
            "model": str(params.get("model") or ""),
            "models": list(params.get("models") or []),
            "breakdown": str(params.get("breakdown") or "summary"),
            "start_date": start_date,
            "end_date": end_date,
            "interactive": bool(params.get("interactive", False)),
            "report_selections": list(params.get("report_selections") or []),
        }
        return result


    def _cube_period_dates(self, period: str, today: date) -> tuple[date, date]:
        yesterday = today - timedelta(days=1)
        if period == "day":
            return yesterday, yesterday
        if period == "week":
            start = today - timedelta(days=today.weekday() + 7)
            return start, start + timedelta(days=6)
        if period == "month":
            end = today.replace(day=1) - timedelta(days=1)
            return end.replace(day=1), end
        if period == "recent7":
            return yesterday - timedelta(days=6), yesterday
        raise ValueError("unsupported Cube report period")


    async def _run_health_report(self, *, period: str) -> ToolResult:
        if not self.health_reports_enabled:
            return ToolResult.error("Error: Cube health report is not enabled")
        if period not in {"recent15m", "day", "week"}:
            return ToolResult.error("Error: health report period must be recent15m, day, or week")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        timezone_info = ZoneInfo(self._config.timezone)
        now = datetime.now(timezone_info)
        intent_kwargs: dict[str, Any] = {
            "connector_id": "magik_cube",
            "template_id": "health_sre",
            "period": period,
            "filters": {},
        }
        if period == "recent15m":
            end_time = now.replace(second=0, microsecond=0)
            start_time = end_time - timedelta(minutes=15)
            intent_kwargs.update(
                start_date=start_time.date(),
                end_date=end_time.date(),
                start_time=start_time,
                end_time=end_time,
                comparison_start_time=start_time - timedelta(minutes=15),
                comparison_end_time=end_time - timedelta(minutes=15),
            )
        else:
            start_date, end_date = self._cube_period_dates(period, now.date())
            intent_kwargs.update(start_date=start_date, end_date=end_date)
        intent = ReportIntent(**intent_kwargs)
        template = self._registry.template("health_sre")
        if template is None:
            return ToolResult.error("Error: Cube health template is unavailable")
        trace_id = uuid.uuid4().hex
        context = ReportRunContext(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            timezone=self._config.timezone,
            trace_id=trace_id,
            template_version=template.manifest.version,
            metadata=metadata,
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
            ).run(intent, context)
        except PermissionError:
            return ToolResult.error("当前账号没有执行 Cube 健康报告的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: Cube health report unavailable: {exc}")
        return self._result(outcome.document)


    async def _run_provider_quality_report(
        self,
        *,
        period: str,
        provider: str,
        providers: list[str] | None,
        provider_id: str,
        model: str,
        endpoint: str,
        selection_confirmed: bool = False,
        include_empty: bool = False,
        start_date: str = "",
        end_date: str = "",
    ) -> ToolResult:
        if not self.provider_quality_reports_enabled:
            return ToolResult.error("Error: Cube provider quality report is not enabled")
        if period not in {"recent15m", "day", "week", "range"}:
            return ToolResult.error("Error: provider quality supports recent15m, day, week, or range")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        if (
            self._config.cube_provider_quality_selector
            and not selection_confirmed
            and not provider.strip()
            and not providers
            and not provider_id.strip()
            and not model.strip()
            and not endpoint.strip()
        ):
            return await self._run_provider_quality_selector(
                channel=channel,
                chat_id=chat_id,
                user_id=user_id,
                timezone=self._config.timezone,
            )
        timezone_info = ZoneInfo(self._config.timezone)
        now = datetime.now(timezone_info)
        intent_kwargs: dict[str, Any] = {
            "connector_id": "cube_provider_quality",
            "template_id": "provider_quality",
            "period": period,
            "provider": provider.strip(),
            "endpoint": endpoint.strip(),
            "models": self._canonical_cube_models((model,)) if model.strip() else (),
            "filters": {
                "provider": provider.strip(),
                "providers": [item.strip() for item in (providers or []) if item.strip()],
                "provider_id": provider_id.strip(),
                "model": model.strip(),
                "endpoint": endpoint.strip(),
                "include_empty": include_empty,
                "start_date": start_date,
                "end_date": end_date,
            },
        }
        if period == "recent15m":
            end_time = now.replace(second=0, microsecond=0)
            start_time = end_time - timedelta(minutes=15)
            intent_kwargs.update(
                start_date=start_time.date(),
                end_date=end_time.date(),
                start_time=start_time,
                end_time=end_time,
                comparison_start_time=start_time - timedelta(minutes=15),
                comparison_end_time=end_time - timedelta(minutes=15),
            )
        else:
            if period == "range":
                try:
                    period_start_date = date.fromisoformat(start_date)
                    period_end_date = date.fromisoformat(end_date)
                except ValueError:
                    return ToolResult.error("自定义区间需要使用 YYYY-MM-DD 日期格式。")
                if period_end_date < period_start_date:
                    return ToolResult.error("自定义区间的结束日期不能早于开始日期。")
                if (period_end_date - period_start_date).days >= 90:
                    return ToolResult.error("自定义区间最多支持 90 天。")
            else:
                period_start_date, period_end_date = self._cube_period_dates(period, now.date())
            intent_kwargs.update(
                start_date=period_start_date,
                end_date=period_end_date,
                comparison_start_time=None,
                comparison_end_time=None,
            )
        intent = ReportIntent(**intent_kwargs)
        template = self._registry.template("provider_quality")
        if template is None:
            return ToolResult.error("Error: Cube provider quality template is unavailable")
        context = ReportRunContext(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            timezone=self._config.timezone,
            trace_id=uuid.uuid4().hex,
            template_version=template.manifest.version,
            metadata=metadata,
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
            ).run(intent, context)
        except PermissionError:
            return ToolResult.error("当前账号没有执行 Cube 供应商质量报告的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: Cube provider quality report unavailable: {exc}")
        return self._result(outcome.document)


    async def _run_provider_quality_selector(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
        timezone: str,
    ) -> ToolResult:
        connector = self._registry.connector("cube_provider_quality")
        if not isinstance(connector, CubeProviderQualityConnector):
            return ToolResult.error("Error: Cube provider quality connector is unavailable")
        if not self._store.allowed(channel, user_id, "connector", "cube_provider_quality"):
            return ToolResult.error("当前账号没有执行 Cube 供应商质量报告的权限，请联系管理员授权。")
        if not self._store.allowed(channel, user_id, "template", "provider_quality"):
            return ToolResult.error("当前账号没有执行 Cube 供应商质量报告的权限，请联系管理员授权。")
        catalog, warnings = await connector.list_provider_catalog()
        providers = sorted(
            {
                str(item.get("provider") or "").strip()
                for item in catalog
                if str(item.get("provider") or "").strip()
            },
            key=str.casefold,
        )
        if not providers:
            reason = "；".join(warnings[:2]) if warnings else "Cube 未返回供应商目录"
            return ToolResult.error(f"暂时无法加载供应商列表：{reason}")
        interaction = report_interactions().create(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            options={
                secrets.token_urlsafe(12): provider
                for provider in providers
            },
        )
        return self._result(
            provider_quality_selector_document(
                interaction,
                catalog,
                timezone=timezone,
                warnings=warnings,
            )
        )


    async def _run_cost_report(
        self,
        *,
        period: str,
        tenant_query: str,
        project: str,
        model: str,
        endpoint: str,
        interactive: bool,
    ) -> ToolResult:
        if not self.cost_reports_enabled:
            return ToolResult.error("Error: Cube cost/account report is not enabled")
        if period != "month":
            return ToolResult.error("Error: Cube cost/account reports support month only")
        if interactive and not tenant_query.strip():
            return await self._run_scope_selector(period=period, report_family="cost")
        if not tenant_query.strip():
            return ToolResult.error("请选择一个已授权客户后再查询成本与账户。")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        today = datetime.now(ZoneInfo(self._config.timezone)).date()
        start_date, end_date = self._cube_period_dates("month", today)
        intent = ReportIntent(
            connector_id="magik_cube",
            template_id="cost_account",
            period="month",
            tenant=tenant_query.strip(),
            project=project.strip(),
            endpoint=endpoint.strip(),
            model_scope="selected" if model.strip() else "summary",
            models=(model.strip(),) if model.strip() else (),
            start_date=start_date,
            end_date=end_date,
            filters={
                "tenant": tenant_query.strip(),
                "project": project.strip(),
                "endpoint": endpoint.strip(),
                "models": [model.strip()] if model.strip() else [],
                "model_scope": "selected" if model.strip() else "summary",
            },
        )
        template = self._registry.template("cost_account")
        if template is None:
            return ToolResult.error("Error: Cube cost/account template is unavailable")
        context = ReportRunContext(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            timezone=self._config.timezone,
            trace_id=uuid.uuid4().hex,
            template_version=template.manifest.version,
            metadata=metadata,
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
            ).run(intent, context)
        except PermissionError:
            return ToolResult.error("当前账号没有执行 Cube 成本与账户报表的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: Cube cost/account report unavailable: {exc}")
        return self._result(outcome.document)


    async def _run_cube_report(
        self,
        *,
        period: str,
        tenant_query: str,
        model: str,
        models: list[str] | None,
        breakdown: str,
        project: str,
        endpoint: str,
        provider: str,
        interactive: bool,
        all_tenants: bool,
        report_template: str,
        start_date: str = "",
        end_date: str = "",
        report_selections: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        if (
            not self._config.cube_report_runner
            or self._magik_tool is None
            or self._registry.connector("magik_cube") is None
        ):
            return ToolResult.error("Error: Magik Cube connector is unavailable")
        if period not in {"day", "week", "month", "recent7", "range"}:
            return ToolResult.error("Error: unsupported Cube report period")
        if report_template not in {"brief", "matrix_card", "full"}:
            return ToolResult.error("Error: unsupported Cube report template")
        if breakdown not in {"summary", "model"}:
            return ToolResult.error("Error: breakdown must be summary or model")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        selections = [item for item in report_selections or [] if isinstance(item, dict)]
        if len(selections) > 1:
            return ToolResult.error("统一选择器当前一次仅支持一个客户，请分开生成报表。")
        selected = selections[0] if selections else {}
        selected_tenant = str(selected.get("tenant_query") or tenant_query).strip()
        selected_scope = str(selected.get("model_scope") or "").strip()
        selected_values = selected.get("models") if isinstance(selected.get("models"), list) else []
        selected_models = self._canonical_cube_models(tuple(
            dict.fromkeys(
                item.strip()
                for item in (
                    ([model] if model else (models or []))
                    if not selected_values
                    else selected_values
                )
                if isinstance(item, str) and item.strip()
            )
        ))
        if all_tenants:
            if not self._config.cube_model_all_tenant_report:
                return ToolResult.error("管理员尚未开启全部客户模型报表。")
            if selected_tenant:
                return ToolResult.error("全部客户模型报表不能同时选择单个客户。")
            if len(selected_models) != 1:
                return ToolResult.error("请选择一个模型后再生成全部客户报表。")
        if interactive and not selected_tenant and not selected_models:
            return await self._run_scope_selector(
                period=period,
                report_family="usage",
                report_template=report_template,
            )
        if interactive and selected_scope == "selected" and not selected_models:
            return await self._run_selector_model_stage(
                period=period,
                tenant_query=selected_tenant,
                report_template=report_template,
            )
        today = datetime.now(ZoneInfo(self._config.timezone)).date()
        if start_date or end_date:
            try:
                period_start = date.fromisoformat(start_date)
                period_end = date.fromisoformat(end_date)
            except ValueError:
                return ToolResult.error("报表日期需要使用 YYYY-MM-DD 格式。")
            if period_end < period_start or (period_end - period_start).days >= 365:
                return ToolResult.error("报表日期范围无效或超过 365 天。")
        else:
            if period == "range":
                return ToolResult.error("自定义区间必须提供开始和结束日期。")
            period_start, period_end = self._cube_period_dates(period, today)
        if report_template == "full":
            legacy_params: dict[str, Any] = {
                "start_date": period_start.isoformat(),
                "end_date": period_end.isoformat(),
                "comparison": "previous_period",
                "report_template": "full",
                "tenant_query": selected_tenant,
                "model": selected_models[0] if len(selected_models) == 1 else "",
                "breakdown": "model" if selected_scope in {"all", "selected"} else breakdown,
                "include_tpm": True,
                "save_snapshot": False,
            }
            return await self._magik_tool.execute(**legacy_params)
        template_id = (
            BRIEF_PERIOD_TEMPLATES[period]
            if report_template == "brief"
            else PERIOD_TEMPLATES[period]
        )
        intent = ReportIntent(
            connector_id="magik_cube",
            template_id=template_id,
            period=period,  # type: ignore[arg-type]
            tenant=selected_tenant,
            project=project.strip(),
            endpoint=endpoint.strip(),
            provider=provider.strip(),
            model_scope=(selected_scope if selected_scope in {"summary", "all", "selected"} else "selected" if selected_models else "summary"),
            models=selected_models,
            start_date=period_start,
            end_date=period_end,
            filters={
                "tenant": selected_tenant,
                "project": project.strip(),
                "endpoint": endpoint.strip(),
                "provider": provider.strip(),
                "models": list(selected_models),
                "all_tenants": all_tenants,
                "model_scope": (
                    selected_scope
                    if selected_scope in {"summary", "all", "selected"}
                    else "selected" if selected_models else "summary"
                ),
            },
        )
        template = self._registry.template(template_id)
        if template is None:
            return ToolResult.error(f"Error: Cube report template unavailable: {template_id}")
        trace_id = uuid.uuid4().hex
        context = ReportRunContext(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            timezone=self._config.timezone,
            trace_id=trace_id,
            template_version=template.manifest.version,
            metadata=metadata,
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
            ).run(intent, context)
        except PermissionError:
            return ToolResult.error("当前账号没有执行该 Cube 报表的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: Cube report unavailable: {exc}")
        document = self._filter_usage_subscription_actions(
            outcome.document,
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        return self._result(
            document,
            report_reference=self._report_reference_payload(
                intent, document=document, run_id=trace_id
            ),
        )


    async def _run_multi_scope_brief(
        self,
        *,
        period: str,
        tenants: list[str],
        models: list[str],
        all_tenants: bool,
        interactive: bool,
        start_date: str,
        end_date: str,
        report_selections: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        """Run the explicit multi-customer/model brief after scope selection."""

        if period == "day" and not template_enabled(
            self._store, "usage_customer_model_daily_brief"
        ):
            return ToolResult.error("Error: multi-customer model brief is not enabled")
        if period not in {"day", "week"}:
            return ToolResult.error("Error: multi-customer model brief supports day and week")
        if period == "week" and not template_enabled(
            self._store, "usage_customer_model_weekly_brief"
        ):
            return ToolResult.error("Error: multi-customer weekly brief is not enabled")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        if interactive and not tenants and not report_selections:
            if self._magik_tool is None:
                return ToolResult.error("Error: Cube scope selector is unavailable")
            today = datetime.now(ZoneInfo(self._config.timezone)).date()
            selected_day, selected_end = self._cube_period_dates(period, today)
            result = await self._magik_tool.execute(
                start_date=selected_day.isoformat(),
                end_date=selected_end.isoformat(),
                comparison="none",
                include_tpm=False,
                report_template="matrix_card",
                granularity="day",
                interactive=True,
                save_snapshot=False,
            )
            ui = self._agent_ui_of(result)
            if isinstance(ui, dict) and ui.get("kind") == "magik_report_form":
                ui["title"] = "选择多客户多模型日报范围"
                ui["base_params"] = {
                    "action": "multi_scope_brief",
                    "period": period,
                    "interactive": False,
                    "start_date": selected_day.isoformat(),
                    "end_date": selected_end.isoformat(),
                    # The form UI comes from the legacy Cube tool, but every callback
                    # in this workflow must resume the multi-scope ReportRunner action.
                    "_report_center_selector": True,
                }
                ui["max_tenants"] = 20
            return result
        selections = [item for item in report_selections or [] if isinstance(item, dict)]
        needs_model_selection = interactive and any(
            str(item.get("model_scope") or "") == "selected" and not item.get("models")
            for item in selections
        )
        if needs_model_selection:
            if self._magik_tool is None:
                return ToolResult.error("Error: Cube model selector is unavailable")
            result = await self._magik_tool.execute(
                start_date=start_date,
                end_date=end_date,
                comparison="none",
                include_tpm=False,
                report_template="matrix_card",
                granularity="day",
                interactive=True,
                report_selections=selections,
                save_snapshot=False,
                # Tool-schema callers cannot submit this private control. It only
                # raises the internal selector bound for the explicitly gated
                # multi-scope workflow; ordinary compatibility calls keep their cap.
                _trusted_selection_limit=20,
            )
            ui = self._agent_ui_of(result)
            if isinstance(ui, dict) and ui.get("kind") == "magik_report_form":
                ui["title"] = "选择多客户日报模型" if period == "day" else "选择多客户周报模型"
                ui["base_params"] = {
                    "action": "multi_scope_brief",
                    "period": period,
                    "interactive": False,
                    "start_date": start_date,
                    "end_date": end_date,
                    "_report_center_selector": True,
                }
                ui["max_tenants"] = 20
            return result
        if selections:
            tenants = [
                str(item.get("tenant_query") or "").strip()
                for item in selections
                if str(item.get("tenant_query") or "").strip()
            ]
            tenant_models = {
                tenant: [str(model).strip() for model in item.get("models") or [] if str(model).strip()]
                for item in selections
                for tenant in [str(item.get("tenant_query") or "").strip()]
                if tenant and str(item.get("model_scope") or "") == "selected"
            }
            all_model_tenants = [
                str(item.get("tenant_query") or "").strip()
                for item in selections
                if str(item.get("model_scope") or "") == "all"
                and str(item.get("tenant_query") or "").strip()
            ]
            all_models = bool(all_model_tenants)
        else:
            tenant_models = {}
            all_model_tenants = []
            all_models = False
        # Resolve display aliases to authoritative Cube tenant IDs before model
        # discovery. The connector and the live model catalog both key by tenant
        # ID; passing aliases here silently produces aggregate rows without model
        # names for an all-model report.
        resolver = getattr(self._magik_tool, "resolve_tenant_queries", None)
        native_resolver = getattr(type(self._magik_tool), "resolve_tenant_queries", None)
        if callable(resolver) and native_resolver is not None and tenants:
            try:
                resolved_tenants, unresolved_tenants = await resolver(tenants)
            except Exception as exc:
                return ToolResult.error(f"Cube 客户目录当前不可用，请稍后重试：{type(exc).__name__}")
            if unresolved_tenants:
                unresolved = "、".join(
                    str(item.get("query") or "")
                    for item in unresolved_tenants
                    if isinstance(item, dict) and str(item.get("query") or "")
                )
                return ToolResult.error(
                    f"以下客户无法在 Cube 实时目录中确认：{unresolved or '未知客户'}。"
                )
            tenant_id_by_query = {
                str(item.get("query") or "").strip(): str(item.get("tenant_id") or "").strip()
                for item in resolved_tenants
                if isinstance(item, dict)
                and str(item.get("query") or "").strip()
                and str(item.get("tenant_id") or "").strip()
            }
            if len(tenant_id_by_query) != len(set(tenants)):
                return ToolResult.error("Cube 客户目录返回的客户映射不完整，请重新选择客户。")
            tenants = [tenant_id_by_query.get(item, item) for item in tenants]
            all_model_tenants = [tenant_id_by_query.get(item, item) for item in all_model_tenants]
            tenant_models = {
                tenant_id_by_query.get(str(tenant_id).strip(), str(tenant_id).strip()): values
                for tenant_id, values in tenant_models.items()
            }
        if not tenants and not all_tenants:
            return ToolResult.error("请选择至少一个客户")
        if not all_models and not models and not tenant_models:
            return ToolResult.error("请选择至少一个模型")
        try:
            target_start = date.fromisoformat(start_date) if start_date else None
            target_end = date.fromisoformat(end_date) if end_date else None
        except ValueError:
            return ToolResult.error("报表日期需要使用 YYYY-MM-DD 格式。")
        if target_start is None or target_end is None:
            target_start, target_end = self._cube_period_dates(
                period, datetime.now(ZoneInfo(self._config.timezone)).date()
            )
        expected_days = 1 if period == "day" else 7
        if (target_end - target_start).days + 1 != expected_days:
            return ToolResult.error("多客户多模型简报需要完整的日报或自然周周期")
        if all_model_tenants:
            try:
                catalog_models = await self._load_tenant_model_catalog(
                    all_model_tenants,
                    start_date=target_start,
                    end_date=target_end,
                )
            except (LookupError, ValueError) as exc:
                return ToolResult.error(
                    f"{exc}。"
                )
            tenant_models.update(
                {tenant_id: catalog_models[tenant_id] for tenant_id in all_model_tenants}
            )
        template_id = (
            "usage_customer_model_daily_brief"
            if period == "day"
            else "usage_customer_model_weekly_brief"
        )
        template = self._registry.template(template_id)
        if template is None:
            return ToolResult.error("Error: multi-customer model brief template is unavailable")
        scoped_model_values = tuple(
            dict.fromkeys(
                [*models]
                + [
                    model_name
                    for tenant_values in tenant_models.values()
                    for model_name in tenant_values
                ]
            )
        )
        selected_models = self._canonical_cube_models(scoped_model_values)
        if len(selected_models) > 20 and not all_models:
            return ToolResult.error("多客户多模型简报最多支持 20 个模型，请缩小范围。")
        canonical_by_name = {item.casefold(): item for item in selected_models}
        tenant_models = {
            tenant_id: list(
                dict.fromkeys(
                    canonical_by_name.get(model_name.casefold(), model_name)
                    for model_name in tenant_values
                )
            )
            for tenant_id, tenant_values in tenant_models.items()
        }
        # Keep the shared Intent's explicit-selection cap intact. Catalog-expanded
        # "all" models remain in tenant_models, which ReportRunner authorizes item
        # by item and CubeConnector bounds to 200 tenant/model combinations.
        intent_models = () if all_models else selected_models
        intent = ReportIntent(
            connector_id="magik_cube",
            template_id=template.manifest.template_id,
            period=period,
            tenant_scope="all" if all_tenants else "selected",
            tenants=tuple(dict.fromkeys(tenants)),
            models=intent_models,
            start_date=target_start,
            end_date=target_end,
            filters={
                "tenants": list(dict.fromkeys(tenants)),
                "tenant_scope": "all" if all_tenants else "selected",
                "models": list(intent_models),
                "model_scope": "all" if all_models else "selected",
                "all_tenants": all_tenants,
                "tenant_models": tenant_models,
                "multi_scope": True,
            },
        )
        trace_id = uuid.uuid4().hex
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
            ).run(
                intent,
                ReportRunContext(
                    channel=channel,
                    chat_id=chat_id,
                    user_id=user_id,
                    timezone=self._config.timezone,
                    trace_id=trace_id,
                    template_version=template.manifest.version,
                    metadata=metadata,
                ),
            )
        except PermissionError:
            return ToolResult.error("当前账号没有执行多客户多模型简报的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: multi-customer model brief unavailable: {exc}")
        return self._result(
            outcome.document,
            report_reference=self._report_reference_payload(
                intent, document=outcome.document, run_id=trace_id
            ),
        )


    async def _run_customer_model_hourly_tpm(
        self,
        *,
        tenants: list[str],
        models: list[str],
        all_tenants: bool,
        interactive: bool = False,
        report_selections: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        """Run the read-only hourly model TPM report for validated customer scope.

        Customer/model discovery deliberately reuses the daily-report catalog
        resolver.  The hourly Cube endpoint provides model-level TPM, so this
        method carries customer relationships as scope metadata and never
        claims customer-exclusive machine resources.
        """
        if not template_enabled(self._store, "usage_customer_model_hourly_tpm"):
            return ToolResult.error("Error: hourly customer/model TPM report is not enabled")
        if self._magik_tool is None:
            return ToolResult.error("Cube 客户目录当前不可用，请稍后重试")
        selections = [
            item for item in report_selections or [] if isinstance(item, dict)
        ]
        if interactive and not tenants and not selections:
            # No scope yet: open the shared customer/model selector and rewire
            # every callback back into this action, mirroring the multi-scope
            # brief workflow. Scope validation stays server-side.
            now = datetime.now(ZoneInfo(self._config.timezone))
            previous_hour = self._previous_complete_hour(now)
            result = await self._magik_tool.execute(
                start_date=previous_hour.date().isoformat(),
                end_date=previous_hour.date().isoformat(),
                comparison="none",
                include_tpm=False,
                report_template="matrix_card",
                granularity="day",
                interactive=True,
                save_snapshot=False,
            )
            ui = self._agent_ui_of(result)
            if isinstance(ui, dict) and ui.get("kind") == "magik_report_form":
                ui["title"] = "选择小时 TPM 客户与模型范围"
                ui["base_params"] = {
                    "action": "customer_model_hourly_tpm",
                    "period": "recent1h",
                    "interactive": False,
                    "_report_center_selector": True,
                }
                ui["max_tenants"] = 20
            return result
        raw_tenants = [str(item).strip() for item in tenants if str(item).strip()]
        if not raw_tenants and selections:
            raw_tenants = list(
                dict.fromkeys(
                    str(item.get("tenant_query") or "").strip()
                    for item in selections
                    if str(item.get("tenant_query") or "").strip()
                )
            )
        if all_tenants and not raw_tenants:
            loader = getattr(self._magik_tool, "list_tenant_catalog", None)
            if not callable(loader):
                return ToolResult.error("Cube 客户目录当前不可用，请稍后重试")
            try:
                catalog = await loader(limit=20)
            except Exception:
                return ToolResult.error("Cube 客户目录当前不可用，请稍后重试")
            raw_tenants = [
                str(item.get("tenant_id") or item.get("tenantId") or "").strip()
                for item in catalog or []
                if isinstance(item, dict)
            ]
        if not raw_tenants:
            return ToolResult.error("请指定至少一个客户")
        resolver = getattr(self._magik_tool, "resolve_tenant_queries", None)
        resolved: list[dict[str, Any]] = []
        if callable(resolver):
            try:
                resolved, unresolved = await resolver(raw_tenants)
            except Exception:
                return ToolResult.error("Cube 客户目录当前不可用，请稍后重试")
            if unresolved:
                names = "、".join(str(item.get("query") or "") for item in unresolved)
                return ToolResult.error(f"以下客户无法在 Cube 实时目录中确认：{names or '未知客户'}")
            raw_tenants = [
                str(item.get("tenant_id") or "").strip()
                for item in resolved
                if isinstance(item, dict) and str(item.get("tenant_id") or "").strip()
            ]
        if len(raw_tenants) > 20:
            return ToolResult.error("最多支持 20 个客户，请缩小范围")
        selected_models = self._canonical_cube_models(tuple(models))
        discovered: dict[str, list[str]] = {}
        if selections:
            # Selector submissions already carry catalog-validated scope:
            # all-scope tenants get live active discovery for the hour's date,
            # selected-scope tenants keep the models their selector stage
            # validated against the live catalog.
            discovery_tenants = list(
                dict.fromkeys(
                    str(item.get("tenant_query") or "").strip()
                    for item in selections
                    if str(item.get("model_scope") or "") == "all"
                    and str(item.get("tenant_query") or "").strip()
                )
            )
            if discovery_tenants:
                now = datetime.now(ZoneInfo(self._config.timezone))
                previous_hour = self._previous_complete_hour(now)
                try:
                    discovered.update(
                        await self._load_tenant_model_catalog(
                            discovery_tenants,
                            start_date=previous_hour.date(),
                            end_date=previous_hour.date(),
                        )
                    )
                except (LookupError, ValueError) as exc:
                    return ToolResult.error(str(exc))
            for item in selections:
                if str(item.get("model_scope") or "") != "selected":
                    continue
                tenant_id = str(item.get("tenant_query") or "").strip()
                model_values = [
                    str(model).strip()
                    for model in item.get("models") or []
                    if str(model).strip()
                ]
                if tenant_id and model_values:
                    discovered[tenant_id] = list(dict.fromkeys(model_values))
            selected_models = self._canonical_cube_models(
                tuple(model for values in discovered.values() for model in values)
            )
        elif selected_models:
            # Explicit model scope must be resolved per tenant. Reusing one
            # global model list for every tenant would claim a customer/model
            # relationship that the live Cube catalog never confirmed.
            model_resolver = getattr(self._magik_tool, "resolve_models_for_tenants", None)
            if not callable(model_resolver):
                return ToolResult.error("Cube 实时模型目录当前不可用，请稍后重试")
            try:
                discovered, unresolved_models = await model_resolver(
                    raw_tenants, list(selected_models)
                )
            except (LookupError, ValueError) as exc:
                return ToolResult.error(str(exc))
            if unresolved_models:
                details = "、".join(
                    f"{item.get('tenant_id') or item.get('tenant')}: {item.get('model')}"
                    for item in unresolved_models
                    if isinstance(item, dict)
                )
                return ToolResult.error(
                    f"以下客户未确认指定模型，请检查客户与模型范围：{details or '未知范围'}"
                )
            selected_models = self._canonical_cube_models(
                tuple(model for values in discovered.values() for model in values)
            )
        else:
            # The existing catalog path is the source of truth for all-model
            # selection; it excludes configured models that have no live data.
            now = datetime.now(ZoneInfo(self._config.timezone))
            current_hour = now.replace(minute=0, second=0, microsecond=0)
            previous_hour = current_hour - timedelta(hours=1)
            try:
                discovered = await self._load_tenant_model_catalog(
                    raw_tenants,
                    start_date=previous_hour.date(),
                    end_date=previous_hour.date(),
                )
            except (LookupError, ValueError) as exc:
                return ToolResult.error(str(exc))
            selected_models = self._canonical_cube_models(
                tuple(model for values in discovered.values() for model in values)
            )
        if not selected_models:
            return ToolResult.error("所选客户当前没有可查询的有用量模型")
        if len(selected_models) > 20:
            return ToolResult.error("最多支持 20 个模型，请缩小范围")
        tenant_models = {
            tenant_id: list(dict.fromkeys(discovered.get(tenant_id, [])))
            for tenant_id in raw_tenants
        }
        combinations = sum(len(values) for values in tenant_models.values())
        if combinations > 200:
            return ToolResult.error("客户模型组合超过 200 个，请缩小范围")
        template = self._registry.template("usage_customer_model_hourly_tpm")
        if template is None:
            return ToolResult.error("Error: hourly customer/model TPM report template is unavailable")
        # The intent's model scope must reflect how the scope was chosen: a
        # discovery-expanded all-model run keeps model_scope="all" with an
        # empty model tuple (the daily brief pattern), so quoted-card
        # subscriptions inherit per-run discovery semantics instead of a flat
        # cross-tenant model list that no catalog relationship confirms.
        explicit_model_selection = any(str(item).strip() for item in models) or any(
            str(item.get("model_scope") or "") == "selected" and item.get("models")
            for item in selections
        )
        intent_model_scope = "selected" if explicit_model_selection else "all"
        intent_models = selected_models if explicit_model_selection else ()
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        tenant_names = {
            str(item.get("tenant_id") or ""): str(
                item.get("display_name") or item.get("tenant_name") or item.get("name") or item.get("tenant_id") or ""
            )
            for item in (resolved if callable(resolver) else [])
            if isinstance(item, dict) and str(item.get("tenant_id") or "").strip()
        }
        intent = ReportIntent(
            connector_id="magik_cube",
            template_id=template.manifest.template_id,
            period="recent1h",
            tenant_scope="all" if all_tenants else "selected",
            tenants=tuple(dict.fromkeys(raw_tenants)),
            models=intent_models,
            model_scope=intent_model_scope,
            filters={
                "tenants": list(dict.fromkeys(raw_tenants)),
                "models": list(intent_models),
                "model_scope": intent_model_scope,
                "multi_scope": True,
                "tenant_models": tenant_models,
                "tenant_names": tenant_names,
            },
        )
        trace_id = uuid.uuid4().hex
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
            ).run(
                intent,
                ReportRunContext(
                    channel=channel,
                    chat_id=chat_id,
                    user_id=user_id,
                    timezone=self._config.timezone,
                    trace_id=trace_id,
                    template_version=template.manifest.version,
                    metadata=metadata,
                ),
            )
        except PermissionError:
            return ToolResult.error("当前账号没有执行小时 TPM 报表的权限，请联系管理员授权")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: hourly TPM report unavailable: {exc}")
        return self._result(
            outcome.document,
            report_reference=self._report_reference_payload(
                intent, document=outcome.document, run_id=trace_id
            ),
        )


    async def _run_machine_tpm_report(
        self,
        *,
        period: str,
        model: str,
        cluster: str,
        start_date: str,
        end_date: str,
    ) -> ToolResult:
        """Generate the read-only machine TPM report for one validated model."""

        if not template_enabled(self._store, "machine_tpm_peak"):
            return ToolResult.error("Error: machine TPM report is not enabled")
        if not model.strip():
            return ToolResult("请指定一个模型，例如：Kimi-K3 单机 TPM 峰值。")
        if period not in {"day", "week", "range"}:
            return ToolResult.error("Error: machine TPM report supports day, week, or range")
        try:
            if start_date or end_date:
                target_start = date.fromisoformat(start_date)
                target_end = date.fromisoformat(end_date)
            else:
                target_start, target_end = self._cube_period_dates(
                    period, datetime.now(ZoneInfo(self._config.timezone)).date()
                )
        except ValueError:
            return ToolResult.error("报表日期需要使用 YYYY-MM-DD 格式。")
        if target_end < target_start or (target_end - target_start).days >= 90:
            return ToolResult.error("机器 TPM 报表日期范围无效或超过 90 天。")
        selected_model = self._canonical_cube_models((model.strip(),))
        if len(selected_model) != 1:
            return ToolResult.error("请选择一个有效模型")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        template = self._registry.template("machine_tpm_peak")
        if template is None:
            return ToolResult.error("Error: machine TPM report template is unavailable")
        intent = ReportIntent(
            connector_id="magik_cube",
            template_id=template.manifest.template_id,
            period=period,  # type: ignore[arg-type]
            models=selected_model,
            start_date=target_start,
            end_date=target_end,
            filters={"models": list(selected_model), "cluster": cluster.strip()},
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
            ).run(
                intent,
                ReportRunContext(
                    channel=channel,
                    chat_id=chat_id,
                    user_id=user_id,
                    timezone=self._config.timezone,
                    trace_id=uuid.uuid4().hex,
                    template_version=template.manifest.version,
                    metadata=metadata,
                ),
            )
        except PermissionError:
            return ToolResult.error("当前账号没有执行机器 TPM 报表的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: machine TPM report unavailable: {exc}")
        return self._result(outcome.document)


    async def _run_scope_selector(
        self,
        *,
        period: str,
        report_family: str,
        report_template: str = "matrix_card",
    ) -> ToolResult:
        """Reuse the proven catalog card while returning to ReportRunner for execution."""

        if self._magik_tool is None:
            return ToolResult.error("Error: Cube scope selector is unavailable")
        today = datetime.now(ZoneInfo(self._config.timezone)).date()
        start_date, end_date = self._cube_period_dates(period, today)
        granularity = "week" if period == "month" else "day"
        result = await self._magik_tool.execute(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            comparison="previous_period",
            include_tpm=True,
            report_template="matrix_card",
            granularity=granularity,
            interactive=True,
            save_snapshot=False,
        )
        if (
            report_family == "usage"
            and report_template != "brief"
            and not self._config.cube_scope_selector_v2
        ):
            return result
        ui = self._agent_ui_of(result)
        if not isinstance(ui, dict) or ui.get("kind") != "magik_report_form":
            return result
        ui["title"] = "选择成本账户范围" if report_family == "cost" else "选择 Cube 报表范围"
        ui["base_params"] = {
            "action": "cost_report" if report_family == "cost" else "cube_report",
            "period": period,
            "report_family": report_family,
            "report_template": report_template,
            "_report_center_selector": True,
        }
        ui["max_tenants"] = 1
        if report_family == "cost":
            ui["scope_options"] = [{"value": "summary", "label": "账务汇总"}]
        elif report_family == "usage":
            ui["report_template_options"] = [
                {"value": "brief", "label": "简报（默认）"},
                {"value": "matrix_card", "label": "详细分析"},
                {"value": "full", "label": "完整报表"},
            ]
        return result


    async def _run_selector_model_stage(
        self, *, period: str, tenant_query: str, report_template: str
    ) -> ToolResult:
        """Load one authorized tenant's model catalog, then resume ReportRunner."""

        if not tenant_query or self._magik_tool is None:
            return ToolResult.error("Error: Cube model selector is unavailable")
        today = datetime.now(ZoneInfo(self._config.timezone)).date()
        start_date, end_date = self._cube_period_dates(period, today)
        result = await self._magik_tool.execute(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            comparison="previous_period",
            include_tpm=True,
            report_template="matrix_card",
            granularity="week" if period == "month" else "day",
            interactive=True,
            report_selections=[
                {"tenant_query": tenant_query, "model_scope": "selected", "models": []}
            ],
            save_snapshot=False,
        )
        ui = self._agent_ui_of(result)
        if isinstance(ui, dict) and ui.get("kind") == "magik_report_form":
            ui["base_params"] = {
                "action": "cube_report",
                "period": period,
                "report_family": "usage",
                "report_template": report_template,
                "_report_center_selector": True,
            }
            ui["max_tenants"] = 1
        return result
