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
        change = item.get("change")
        suffix = f" ({change})" if change not in (None, "") else ""
        baseline = ""
        if item.get("baseline_value") is not None:
            baseline = f"；基准：{item.get('baseline') or '-'}"
        samples = ""
        if item.get("valid_sample_count") not in (None, ""):
            sample_label = "有效请求" if item.get("metric") == "ai.ttft" else "时间桶"
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
    return (
        "报表说明\n"
        f"基准：前一等长窗口；当前：{current_text}；基准：{baseline_text}\n"
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
        elif block.kind == "actions":
            actions = block.data.get("actions") or []
            content = "\n".join(
                f"- {item.get('label', item.get('action_id', '操作'))}"
                for item in actions
                if isinstance(item, dict)
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
