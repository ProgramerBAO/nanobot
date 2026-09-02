"""Channel renderer extension contract and text fallback."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from nanobot.bus.events import OUTBOUND_META_AGENT_UI
from nanobot.reporting.contracts import ReportDocument


@dataclass(frozen=True, slots=True)
class RendererCapabilities:
    """Features a channel renderer can represent without losing semantics."""

    markdown: bool = True
    table: bool = False
    actions: bool = False
    pagination: bool = False
    message_update: bool = False
    max_message_length: int = 4000
    threads: bool = False
    fallback_channel_id: str = "text"


@dataclass(frozen=True, slots=True)
class RendererManifest:
    channel_id: str
    version: str
    capabilities: RendererCapabilities


@dataclass(frozen=True, slots=True)
class RenderedReport:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ChannelRenderer(ABC):
    channel_id: str
    version: str = "1.0"
    capabilities = RendererCapabilities()

    @property
    def manifest(self) -> RendererManifest:
        return RendererManifest(self.channel_id, self.version, self.capabilities)

    @abstractmethod
    def render(self, document: ReportDocument) -> RenderedReport:
        ...


class TextChannelRenderer(ChannelRenderer):
    channel_id = "text"
    capabilities = RendererCapabilities(markdown=True, max_message_length=8000)

    def render(self, document: ReportDocument) -> RenderedReport:
        return RenderedReport(
            document.fallback_text or document.title,
            metadata={"content_type": "text", "renderer": self.channel_id},
        )


def _block_content(data: dict[str, Any], key: str = "content") -> str:
    value = data.get(key, "")
    return str(value).strip() if value is not None else ""


def _format_metric_items(data: dict[str, Any]) -> str:
    items = data.get("items") or data.get("metrics") or []
    if isinstance(items, dict):
        items = [{"label": key, "value": value} for key, value in items.items()]
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("metric") or "指标")
        value = item.get("value", "-")
        comparisons = [
            value for value in item.get("comparisons") or [] if isinstance(value, dict)
        ]
        if comparisons:
            comparison_text = "".join(
                f"；{value.get('label') or '对比'}："
                f"{value.get('change') or '暂无可比基准'}"
                for value in comparisons
            )
            suffix = ""
            baseline = comparison_text
        else:
            change = item.get("change")
            suffix = f" ({change})" if change not in (None, "") else ""
            baseline = ""
        samples = ""
        if item.get("valid_sample_count") not in (None, ""):
            sample_label = str(
                item.get("sample_label")
                or ("有效请求" if item.get("metric") == "ai.ttft" else "时间桶")
            )
            samples = f"；{sample_label}：{item['valid_sample_count']}"
        aggregation = str(item.get("aggregation") or "").strip()
        semantics = f"；口径：{aggregation}" if aggregation else ""
        detail = str(item.get("detail") or "").strip()
        detail_text = f"；{detail}" if detail else ""
        lines.append(
            f"- **{label}**：{value}{baseline}{suffix}{samples}{semantics}{detail_text}"
        )
    return "\n".join(lines)


def _format_table(data: dict[str, Any]) -> str:
    headers = [str(item) for item in data.get("headers") or []]
    columns = [
        str(item.get("name"))
        for item in data.get("columns") or []
        if isinstance(item, dict) and item.get("name")
    ]
    if not headers and columns:
        headers = [
            str(item.get("display_name") or item.get("name"))
            for item in data.get("columns") or []
            if isinstance(item, dict) and item.get("name")
        ]
    rows = data.get("rows") or []
    if not headers and rows and isinstance(rows[0], dict):
        headers = [str(key) for key in rows[0]]
    if not headers:
        return ""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        if isinstance(row, dict):
            if columns and any(column in row for column in columns):
                values = [row.get(column, "") for column in columns]
            else:
                values = [row.get(header, "") for header in headers]
        elif isinstance(row, (list, tuple)):
            values = list(row)
        else:
            continue
        lines.append("| " + " | ".join(str(value) for value in values) + " |")
    return "\n".join(lines)


def _format_grouped_metrics(data: dict[str, Any]) -> str:
    """Render customer/model groups without introducing template-specific Markdown."""

    sections: list[str] = []
    hidden_groups: list[str] = []
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        label = str(group.get("label") or "未命名客户")
        items = [item for item in group.get("items") or [] if isinstance(item, dict)]
        has_visible_data = any(str(item.get("status") or "") != "no_usage" for item in items)
        if data.get("collapse_no_usage") is True and items and not has_visible_data:
            hidden_groups.append(label)
            continue
        lines = [f"## {label}"]
        for item in items:
            comparisons = "｜".join(
                f"{value.get('label') or '对比'}：{value.get('change') or '暂无可比基准'}"
                for value in item.get("comparisons") or []
                if isinstance(value, dict)
            )
            status = str(item.get("status") or "")
            suffix = "｜暂无用量" if status == "no_usage" else ""
            lines.append(f"- {item.get('label') or '未命名模型'}｜{comparisons}{suffix}")
        sections.append("\n".join(lines))
    if hidden_groups:
        sections.append(f"无用量客户 {len(hidden_groups)} 个（默认收起）：" + "、".join(hidden_groups))
    return "\n\n".join(sections)


def _format_report_context(document: ReportDocument) -> str:
    context = document.context
    if context is None:
        return ""
    current = context.current_window
    baseline = context.baseline_window
    current_text = f"{current.start} - {current.end}" if current else "暂无"
    baseline_text = f"{baseline.start} - {baseline.end}" if baseline else "暂无可比基准"
    sources = "; ".join(
        dict.fromkeys(f"{item.system} / {item.route}" for item in context.sources)
    ) or "已配置报表数据源"
    aggregations = ", ".join(
        dict.fromkeys(item.aggregation for item in context.metric_definitions)
    ) or "按指标定义聚合"
    quality = context.quality or "未标记"
    reasons = "；".join(context.quality_reasons[:5]) if context.quality_reasons else "无"
    freshness = context.freshness or "未提供"
    if document.document_id.endswith("_brief"):
        named = {item.key: item for item in context.comparison_windows}
        previous = named.get("previous_period")
        weekly = named.get("previous_week_same_day")
        previous_text = (
            f"{previous.window.start} - {previous.window.end}"
            if previous is not None
            else "暂无可比基准"
        )
        if weekly is not None:
            comparison_text = (
                f"环比基准：前一日 {previous_text}\n"
                f"同比基准：上周同期 {weekly.window.start} - {weekly.window.end}"
            )
        else:
            comparison_text = f"环比基准：前一等长周期 {previous_text}"
        baseline_policy_text = "简报命名基准"
    elif context.comparison_windows:
        comparison_text = "\n".join(
            f"对比（{item.label}）：{item.window.start} - {item.window.end}"
            for item in context.comparison_windows
        )
        baseline_policy_text = "对比周期已按名称列出"
    else:
        comparison_text = f"对比基准：{baseline_text}"
        baseline_policy_text = "前一等长窗口"
    return (
        "报表说明\n"
        f"当前：{current_text}\n"
        f"{comparison_text}\n"
        f"基准规则：{baseline_policy_text}\n"
        f"时区：{context.timezone}\n"
        f"来源：{sources}\n"
        f"口径：{aggregations}\n"
        f"数据质量：{quality}；最近样本：{freshness}\n"
        f"质量原因：{reasons}\n"
        "读法：错误率、延迟和 TTFT 越低越好；RPM/TPM 表示流量，不能单独判断故障。"
    )


def document_to_markdown(document: ReportDocument) -> str:
    """Render channel-neutral blocks into conservative Markdown/text syntax."""

    sections = [f"# {document.title}"]
    if document.subtitle:
        sections.append(document.subtitle)
    for block in document.blocks:
        if block.kind in {"markdown", "note"}:
            content = _block_content(block.data)
        elif block.kind == "metrics":
            content = _format_metric_items(block.data)
        elif block.kind == "table":
            content = _format_table(block.data)
        elif block.kind == "grouped_metrics":
            content = _format_grouped_metrics(block.data)
        elif block.kind == "actions":
            actions = block.data.get("actions") or []
            content = "\n".join(
                f"- {item.get('label', item.get('action_id', '操作'))}"
                for item in actions
                if isinstance(item, dict)
            )
        elif block.kind == "selector":
            options = [
                item
                for item in block.data.get("options") or []
                if isinstance(item, dict)
            ]
            labels = [str(item.get("label") or "未命名供应商") for item in options]
            content = "\n".join(
                [
                    "请选择供应商和统计周期后生成报告。",
                    "可选供应商：" + ("、".join(labels) if labels else "暂无可用供应商"),
                    "默认范围：全部供应商。",
                ]
            )
        else:
            content = ""
        if content:
            sections.append(content)
    context_text = _format_report_context(document)
    if context_text:
        sections.append(context_text)
    if not document.blocks and document.fallback_text:
        sections.append(document.fallback_text)
    if document.quality and document.quality != "complete":
        sections.append(f"数据质量：{document.quality}")
    if document.warnings:
        sections.append("数据提示：" + "；".join(document.warnings))
    return "\n\n".join(section for section in sections if section).strip()


class MarkdownChannelRenderer(ChannelRenderer):
    """Renderer for channels whose report transport accepts Markdown text."""

    def __init__(self, channel_id: str, *, max_message_length: int) -> None:
        if not channel_id or max_message_length < 256:
            raise ValueError("invalid Markdown renderer configuration")
        self.channel_id = channel_id
        self.capabilities = RendererCapabilities(
            markdown=True,
            max_message_length=max_message_length,
            fallback_channel_id="text",
        )

    def render(self, document: ReportDocument) -> RenderedReport:
        return RenderedReport(
            document_to_markdown(document),
            metadata={
                "content_type": "markdown",
                "renderer": self.channel_id,
                "capabilities": asdict(self.capabilities),
            },
        )


class WeComReportRenderer(MarkdownChannelRenderer):
    def __init__(self) -> None:
        super().__init__("wecom", max_message_length=4000)


class DingTalkReportRenderer(MarkdownChannelRenderer):
    def __init__(self) -> None:
        super().__init__("dingtalk", max_message_length=6000)


class FeishuReportRenderer(ChannelRenderer):
    """Keep structured documents on the existing Feishu card compatibility path."""

    channel_id = "feishu"
    capabilities = RendererCapabilities(
        markdown=True,
        table=True,
        actions=True,
        pagination=True,
        max_message_length=8000,
    )

    def render(self, document: ReportDocument) -> RenderedReport:
        return RenderedReport(
            document.fallback_text or document.title,
            metadata={
                "content_type": "interactive",
                "renderer": self.channel_id,
                OUTBOUND_META_AGENT_UI: document.to_agent_ui(),
            },
        )
