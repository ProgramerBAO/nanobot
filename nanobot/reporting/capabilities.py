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


def capability_catalog(
    registry: ReportPluginRegistry,
    store: ReportStateStore,
    *,
    channel: str,
    user_id: str,
) -> tuple[Capability, ...]:
    has_magik = registry.connector("magik_cube") is not None and store.allowed(
        channel, user_id, "connector", "magik_cube"
    )
    if store.rbac_enabled() and not has_magik:
        return (
            Capability(
                "request_access",
                "申请权限",
                "联系管理员开通报表数据范围",
                ReportAction("request_access", "申请权限", style="primary"),
            ),
        )
    items: list[Capability] = []
    if has_magik and _allowed(store, channel, user_id, "generate"):
        for period, template_id, title, description in (
            ("day", "usage_daily_matrix", "日报", "昨天对比前天"),
            ("week", "usage_weekly_matrix", "周报", "上周对比上上周"),
            ("month", "usage_monthly_matrix", "月报", "上月对比前一自然月"),
            ("recent7", "usage_weekly_matrix", "区间对比", "近 7 天对比前 7 天"),
        ):
            if not store.allowed(channel, user_id, "template", template_id):
                continue
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
) -> ReportDocument:
    capabilities = capability_catalog(
        registry, store, channel=channel, user_id=user_id
    )
    if capabilities and capabilities[0].capability_id == "request_access":
        intro = "当前账号尚未获得报表数据源权限，请联系管理员授权。"
    else:
        intro = "选择功能，或直接发送客户、模型范围和报表周期。明确请求会直接生成。"
    return ReportDocument(
        title="报表中心",
        subtitle="确定性报表 · 固定口径 · 0 LLM",
        document_id="report_home",
        fallback_text=(
            "报表中心\n"
            + intro
            + "\n可用功能："
            + "、".join(item.title for item in capabilities)
        ),
        blocks=(
            ReportBlock("markdown", {"content": intro}),
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


def examples_document(authorized: bool) -> ReportDocument:
    examples = ["我要周报", "我要日报", "查看我的订阅", "查看最近报表"]
    if authorized:
        examples[:0] = [
            "客户英文标识所有模型的周报",
            "客户英文标识完整月报",
            "客户英文标识近7天使用情况",
        ]
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
        content = "\n".join(
            f"• {row['created_at'][:19]}｜{row['template_id']}｜{row['status']}｜{row['duration_ms']}ms"
            for row in rows
        )
    return ReportDocument(
        title="最近报表",
        fallback_text=content,
        blocks=(ReportBlock("markdown", {"content": content}),),
    )


def subscriptions_document(rows: list[Any]) -> ReportDocument:
    if not rows:
        content = "暂无订阅。生成报表后，可在结果卡片底部创建日报、周报或月报订阅。"
        actions: list[dict[str, Any]] = []
    else:
        content = "\n".join(
            (
                f"• **{report_template_label(row.template_id)}**｜"
                f"{describe_subscription_schedule(row.schedule)}｜"
                f"{row.timezone}｜{'启用' if row.enabled else '停用'}\n"
                f"  数据周期：{report_data_period(row.template_id)}"
            )
            for row in rows
        )
        actions = [
            {
                "action_id": f"subscription:{'disable' if row.enabled else 'enable'}:{row.subscription_id}",
                "label": "停用" if row.enabled else "启用",
                "style": "default",
            }
            for row in rows
        ]
    blocks = [ReportBlock("markdown", {"content": content})]
    if actions:
        blocks.append(ReportBlock("actions", {"actions": actions}))
    return ReportDocument(
        title="我的订阅",
        fallback_text=content,
        blocks=tuple(blocks),
    )


def subscription_created_document(row: Any) -> ReportDocument:
    schedule = describe_subscription_schedule(row.schedule)
    report_label = report_template_label(row.template_id)
    data_period = report_data_period(row.template_id)
    content = (
        f"**报表**：{report_label}\n"
        f"**发送计划**：{schedule}\n"
        f"**时区**：{row.timezone}\n"
        f"**数据周期**：{data_period}"
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
