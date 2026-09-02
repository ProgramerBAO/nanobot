"""Capability catalog and channel-neutral report center documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nanobot.reporting.contracts import ReportAction, ReportBlock, ReportDocument
from nanobot.reporting.registry import ReportPluginRegistry
from nanobot.reporting.schedules import (
    describe_subscription_schedule,
    report_data_period,
    report_template_label,
)
from nanobot.reporting.store import ReportStateStore

ONBOARDING_VERSION = 1


@dataclass(frozen=True, slots=True)
class Capability:
    capability_id: str
    title: str
    description: str
    action: ReportAction


def _allowed(
    store: ReportStateStore, channel: str, user_id: str, capability_id: str
) -> bool:
    return store.allowed(channel, user_id, "capability", capability_id)


def _template_enabled(
    store: ReportStateStore, template_id: str, *, policy_enforced: bool
) -> bool:
    """Hide administratively disabled templates when management policies are active."""

    if not policy_enforced:
        return True
    policy = store.template_policy(template_id)
    return policy is None or bool(policy["enabled"])


def capability_catalog(
    registry: ReportPluginRegistry,
    store: ReportStateStore,
    *,
    channel: str,
    user_id: str,
    health_enabled: bool = False,
    cost_enabled: bool = False,
    provider_quality_enabled: bool = False,
    brief_default: bool = False,
    template_policy_enforced: bool = False,
) -> tuple[Capability, ...]:
    has_cube = registry.connector("magik_cube") is not None and store.allowed(
        channel, user_id, "connector", "magik_cube"
    )
    if store.rbac_enabled() and not has_cube:
        return (
            Capability(
                "request_access",
                "申请权限",
                "联系管理员开通报表数据范围",
                ReportAction("request_access", "申请权限", style="primary"),
            ),
        )
    items: list[Capability] = []
    health_template = registry.template("health_sre")
    if (
        has_cube
        and health_enabled
        and health_template is not None
        and health_template in registry.compatible_templates("magik_cube")
        and store.allowed(channel, user_id, "template", "health_sre")
        and _template_enabled(store, "health_sre", policy_enforced=template_policy_enforced)
    ):
        items.append(
            Capability(
                "health_report",
                "Cube 健康报告",
                "近 15 分钟健康快照及日/周趋势",
                ReportAction("health_report", "Cube 健康报告", style="primary"),
            )
        )

    provider_quality_template = registry.template("provider_quality")
    has_provider_quality = registry.connector("cube_provider_quality") is not None and store.allowed(
        channel, user_id, "connector", "cube_provider_quality"
    )
    if (
        has_provider_quality
        and provider_quality_enabled
        and provider_quality_template is not None
        and provider_quality_template in registry.compatible_templates("cube_provider_quality")
        and store.allowed(channel, user_id, "template", "provider_quality")
        and _template_enabled(
            store, "provider_quality", policy_enforced=template_policy_enforced
        )
    ):
        items.append(
            Capability(
                "provider_quality_report",
                "Cube 供应商质量",
                "供应商错误率、延迟、吞吐、探测和测试结果",
                ReportAction("provider_quality_report", "供应商质量", style="primary"),
            )
        )
    cost_template = registry.template("cost_account")
    if (
        has_cube
        and cost_enabled
        and cost_template is not None
        and cost_template in registry.compatible_templates("magik_cube")
        and store.allowed(channel, user_id, "template", "cost_account")
        and _template_enabled(store, "cost_account", policy_enforced=template_policy_enforced)
    ):
        items.append(
            Capability(
                "cost_report",
                "Cube 成本与账户",
                "上月应付金额、钱包余额和未结算金额",
                ReportAction("cost_report", "成本与账户"),
            )
        )
    if has_cube and _allowed(store, channel, user_id, "generate"):
        multi_template = registry.template("usage_customer_model_daily_brief")
        if (
            multi_template is not None
            and multi_template in registry.compatible_templates("magik_cube")
            and store.allowed(channel, user_id, "template", multi_template.manifest.template_id)
            and _template_enabled(
                store,
                multi_template.manifest.template_id,
                policy_enforced=template_policy_enforced,
            )
        ):
            items.append(
                Capability(
                    "multi_scope_brief",
                    "多客户多模型简报",
                    "按客户分组查看模型同比和环比",
                    ReportAction("multi_scope_brief", "多客户多模型简报", style="default"),
                )
            )
        matrix_actions = {
            "usage_daily_matrix": ("day", "日报", "昨天对比前天"),
            "usage_weekly_matrix": ("week", "周报", "上周对比上上周"),
            "usage_monthly_matrix": ("month", "月报", "上月对比前一自然月"),
        }
        brief_actions = {
            "usage_daily_brief": ("day", "日报简报", "昨日概览，含同比和环比"),
            "usage_weekly_brief": ("week", "周报简报", "上周核心指标环比"),
            "usage_monthly_brief": ("month", "月报简报", "上月核心指标环比"),
        }
        period_actions = brief_actions if brief_default else matrix_actions
        for template in registry.compatible_templates("magik_cube"):
            manifest = template.manifest
            if manifest.lifecycle_state not in {"publish", "canary"}:
                continue
            action_info = period_actions.get(manifest.template_id)
            if action_info is None or not store.allowed(
                channel, user_id, "template", manifest.template_id
            ) or not _template_enabled(
                store, manifest.template_id, policy_enforced=template_policy_enforced
            ):
                continue
            period, title, description = action_info
            items.append(
                Capability(
                    f"generate:{period}",
                    title,
                    description,
                    ReportAction(
                        action_id=f"generate:{period}",
                        label=title,
                        style="primary" if period == "week" else "default",
                    ),
                )
            )
        recent_template = "usage_custom_brief" if brief_default else "usage_weekly_matrix"
        if store.allowed(channel, user_id, "template", recent_template) and _template_enabled(
            store, recent_template, policy_enforced=template_policy_enforced
        ):
            items.append(
                Capability(
                    "generate:recent7",
                    "区间对比",
                    "近 7 天对比前 7 天",
                    ReportAction("generate:recent7", "区间对比"),
                )
            )
        machine_template = registry.template("machine_tpm_peak")
        if (
            machine_template is not None
            and machine_template in registry.compatible_templates("magik_cube")
            and store.allowed(channel, user_id, "template", "machine_tpm_peak")
            and _template_enabled(
                store, "machine_tpm_peak", policy_enforced=template_policy_enforced
            )
        ):
            items.append(
                Capability(
                    "machine_tpm_report",
                    "单机 TPM 峰值",
                    "按模型、集群和卡型查看单机折算 TPM 峰值",
                    ReportAction("machine_tpm_report", "单机 TPM 峰值"),
                )
            )
    if _allowed(store, channel, user_id, "subscriptions"):
        items.append(
            Capability(
                "subscriptions",
                "我的订阅",
                "管理确定性定时报表",
                ReportAction("subscriptions", "我的订阅"),
            )
        )
    if _allowed(store, channel, user_id, "recent"):
        items.append(
            Capability(
                "recent",
                "最近报表",
                "查看运行记录并重新生成",
                ReportAction("recent", "最近报表"),
            )
        )
    items.append(
        Capability(
            "examples",
            "示例与帮助",
            "查看当前可用的固定问法",
            ReportAction("examples", "示例与帮助"),
        )
    )
    return tuple(items)


def home_document(
    registry: ReportPluginRegistry,
    store: ReportStateStore,
    *,
    channel: str,
    user_id: str,
    health_enabled: bool = False,
    cost_enabled: bool = False,
    provider_quality_enabled: bool = False,
    brief_default: bool = False,
    admin_skill_enabled: bool = False,
    management_enabled: bool = False,
) -> ReportDocument:
    has_cube = registry.connector("magik_cube") is not None and store.allowed(
        channel, user_id, "connector", "magik_cube"
    )
    capabilities = capability_catalog(
        registry,
        store,
        channel=channel,
        user_id=user_id,
        health_enabled=health_enabled,
        cost_enabled=cost_enabled,
        provider_quality_enabled=provider_quality_enabled,
        brief_default=brief_default,
        template_policy_enforced=management_enabled,
    )
    if capabilities and capabilities[0].capability_id == "request_access":
        intro = "当前账号尚未获得报表数据源权限，请联系管理员授权。"
    else:
        intro = "选择 Cube 报表功能，或直接发送客户、模型范围和报表周期。明确请求会直接生成。"
    flexible_help = (
        "\n\n**Cube 灵活查询**\n"
        "可用自然语言查询租户、模型、Endpoint、账单、集群、日志和只读配置。"
        if admin_skill_enabled and has_cube
        else ""
    )
    fallback_help = flexible_help.replace("**", "")
    management_help = (
        "\n\n**报表管理**\n管理员可在 WebUI 的 Report platform 管理报表启用状态、订阅和订阅权限。"
        if management_enabled
        else ""
    )
    fallback_management = management_help.replace("**", "")
    return ReportDocument(
        title="报表中心",
        subtitle="确定性报表 · 固定口径",
        document_id="report_home",
        fallback_text=(
            "报表中心\n"
            + intro
            + "\n可用功能："
            + "、".join(item.title for item in capabilities)
            + fallback_help
            + fallback_management
        ),
        blocks=(
            ReportBlock("markdown", {"content": intro + flexible_help + management_help}),
            ReportBlock(
                "actions",
                {
                    "actions": [
                        {
                            "action_id": item.action.action_id,
                            "label": item.action.label,
                            "style": item.action.style,
                            "description": item.description,
                        }
                        for item in capabilities
                    ]
                },
            ),
            ReportBlock(
                "note",
                {"content": "输入“帮助”“菜单”或“报表中心”可随时重新打开。"},
            ),
        ),
    )


def examples_document(
    authorized: bool,
    *,
    cost_enabled: bool = False,
    all_tenant_model_enabled: bool = False,
    provider_quality_enabled: bool = False,
    admin_skill_enabled: bool = False,
    multi_scope_enabled: bool = False,
    machine_tpm_enabled: bool = False,
) -> ReportDocument:
    examples = ["我要周报", "我要日报", "健康报告", "查看我的订阅", "查看最近报表"]
    if authorized:
        examples[:0] = [
            "客户英文标识所有模型的周报",
            "客户英文标识完整月报",
            "客户英文标识近7天使用情况",
        ]
        if all_tenant_model_enabled:
            examples.insert(0, "Kimi-K3模型的日报（全部客户）")
    if cost_enabled:
        examples.insert(0, "成本报告")
    if provider_quality_enabled:
        examples.insert(0, "供应商质量报告")
        examples.insert(1, "Kimi-K3 各供应商性能对比")
    if multi_scope_enabled:
        examples.insert(0, "多客户多模型日报简报")
    if machine_tpm_enabled:
        examples.insert(0, "Kimi-K3 单机 TPM 峰值")
    if authorized and admin_skill_enabled:
        examples.extend(
            [
                "tencent_token_hub 有哪些 Endpoint",
                "Kimi-K3 配置在哪些集群",
                "查看某客户最近的账单",
                "查询某 Endpoint 的路由链",
                "查看某模型的价格和配置",
            ]
        )
    return ReportDocument(
        title="报表问法示例",
        fallback_text="可直接发送：\n" + "\n".join(f"- {item}" for item in examples),
        blocks=(
            ReportBlock(
                "markdown",
                {"content": "**可直接发送**\n" + "\n".join(f"• {item}" for item in examples)},
            ),
        ),
    )


def recent_document(rows: list[dict[str, Any]]) -> ReportDocument:
    if not rows:
        content = "暂无报表运行记录。生成第一张报表后会显示在这里。"
    else:
        lines: list[str] = []
        for row in rows:
            request = row.get("request") if isinstance(row.get("request"), dict) else {}
            report_context = request.get("report_context")
            window = ""
            if isinstance(report_context, dict):
                current = report_context.get("current_window")
                if isinstance(current, dict):
                    window = f"｜窗口 {current.get('start', '')} - {current.get('end', '')}"
            lines.append(
                f"• {row['created_at'][:19]}｜{row['template_id']}｜"
                f"{row['status']}｜质量 {row.get('quality', 'unknown')}｜"
                f"{row['duration_ms']}ms{window}"
            )
        content = "\n".join(lines)
    return ReportDocument(
        title="最近报表",
        fallback_text=content,
        blocks=(ReportBlock("markdown", {"content": content}),),
    )


def subscriptions_document(rows: list[Any]) -> ReportDocument:
    if not rows:
        content = "暂无订阅。请在 WebUI 的 Report platform 中创建并管理订阅。"
        blocks = [ReportBlock("markdown", {"content": content})]
    else:
        enabled_count = sum(1 for row in rows if row.enabled)
        blocks = [
            ReportBlock(
                "note",
                {
                    "content": (
                        f"共 {len(rows)} 个订阅｜启用 {enabled_count}｜"
                        f"停用 {len(rows) - enabled_count}"
                    )
                },
            )
        ]
        fallback_sections: list[str] = []
        for index, row in enumerate(rows, start=1):
            report_label = report_template_label(row.template_id)
            schedule = describe_subscription_schedule(row.schedule)
            data_period = report_data_period(row.template_id)
            calculation_version = row.report_params.get(
                "calculation_version", row.template_version
            )
            status = "启用" if row.enabled else "停用"
            scope = _subscription_scope_text(row.report_params)
            content = (
                f"**订阅 {index} · {report_label}**｜{status}\n"
                f"发送计划：{schedule}｜时区：{row.timezone}\n"
                f"统计范围：{scope}\n"
                f"数据周期：{data_period}｜口径版本：{calculation_version}"
            )
            fallback_sections.append(content)
            operation = "disable" if row.enabled else "enable"
            operation_label = "停用" if row.enabled else "启用"
            blocks.extend(
                (
                    ReportBlock(
                        "markdown",
                        {
                            "content": content,
                            "variant": "subscription",
                            "index": index,
                            "title": report_label,
                            "status": status,
                            "schedule": schedule,
                            "timezone": row.timezone,
                            "scope": scope,
                            "data_period": data_period,
                            "calculation_version": str(calculation_version),
                        },
                    ),
                    ReportBlock(
                        "actions",
                        {
                            "actions": [
                                {
                                    "action_id": (
                                        f"subscription:{operation}:{row.subscription_id}"
                                    ),
                                    "label": f"{operation_label}订阅 {index}",
                                    "style": "default",
                                    "command": f"{operation_label}订阅：{row.subscription_id}",
                                }
                            ]
                        },
                    ),
                )
            )
        content = "\n\n".join(fallback_sections)
    return ReportDocument(
        title="我的订阅",
        fallback_text=content,
        blocks=tuple(blocks),
    )


def _subscription_scope_text(params: dict[str, Any]) -> str:
    """Describe only persisted, non-sensitive report scope fields."""

    selections = params.get("report_selections")
    selection = (
        next((item for item in selections if isinstance(item, dict)), {})
        if isinstance(selections, list)
        else {}
    )
    all_tenants = params.get("all_tenants") is True
    tenant = (
        "全部客户"
        if all_tenants
        else str(
            params.get("tenant_query")
            or selection.get("tenant_query")
            or "默认客户"
        ).strip()
    )
    selected_values = params.get("models") or selection.get("models") or []
    models = [str(item).strip() for item in selected_values if str(item).strip()]
    legacy_model = str(params.get("model") or "").strip()
    if not models and legacy_model:
        models = [legacy_model]
    model_scope = str(
        params.get("model_scope")
        or selection.get("model_scope")
        or ("selected" if models else "summary")
    )
    if model_scope == "all":
        model_text = "全部模型"
    elif model_scope == "selected" and models:
        model_text = "、".join(models[:3])
        if len(models) > 3:
            model_text += f" 等 {len(models)} 个模型"
    else:
        model_text = "汇总"
    return f"{tenant}｜{model_text}"


def subscription_created_document(row: Any) -> ReportDocument:
    schedule = describe_subscription_schedule(row.schedule)
    report_label = report_template_label(row.template_id)
    data_period = report_data_period(row.template_id)
    content = (
        f"**报表**：{report_label}\n"
        f"**发送计划**：{schedule}\n"
        f"**时区**：{row.timezone}\n"
        f"**数据周期**：{data_period}\n"
        f"**口径版本**：{row.report_params.get('calculation_version', row.template_version)}"
    )
    return ReportDocument(
        title="订阅已创建",
        subtitle=f"{report_label}｜{schedule}",
        fallback_text=(
            f"订阅已创建：{report_label}，{schedule}（{row.timezone}）；"
            f"数据周期：{data_period}。"
        ),
        blocks=(
            ReportBlock("markdown", {"content": content}),
            ReportBlock(
                "actions",
                {
                    "actions": [
                        {
                            "action_id": "subscriptions",
                            "label": "查看我的订阅",
                            "style": "default",
                        }
                    ]
                },
            ),
        ),
    )
