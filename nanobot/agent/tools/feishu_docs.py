"""Tool to read Feishu cloud docs (docx) and spreadsheets (sheets) by URL or token."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

# Match Feishu cloud doc & sheet URLs (feishu.cn or larksuite.com).
_DOCX_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9-]+\.(?:feishu\.cn|larksuite\.com)/docx/([A-Za-z0-9]+)"
)
_SHEET_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9-]+\.(?:feishu\.cn|larksuite\.com)/sheets/([A-Za-z0-9]+)"
)
_WIKI_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9-]+\.(?:feishu\.cn|larksuite\.com)/wiki/([A-Za-z0-9]+)"
)

_FEISHU_DOC_PARAMETERS = tool_parameters_schema(
    url_or_token=StringSchema(
        "Feishu cloud doc URL (…/docx/<token>, …/sheets/<token>, …/wiki/<token>) "
        "or a bare doc/sheet token."
    ),
    range=StringSchema(
        "Optional sheet range like '<sheetId>!A1:D200'. Only used for spreadsheets. "
        "When omitted we fetch the first 200 rows × 26 columns of the first sheet."
    ),
    required=["url_or_token"],
    description=(
        "Fetch the text content of a Feishu cloud document (docx) or spreadsheet (sheets). "
        "Use this when the user shares a Feishu doc link, or when chat history contains "
        "such a link. Content is returned as plain text (docs) or tab-separated sheet "
        "cells that you can then summarise."
    ),
)


def _parse_doc_ref(value: str) -> tuple[str, str]:
    """Return ``(kind, token)`` where kind is 'docx' / 'sheet' / 'wiki' / 'unknown'."""
    s = (value or "").strip()
    if not s:
        return "unknown", ""
    m = _DOCX_URL_RE.search(s)
    if m:
        return "docx", m.group(1)
    m = _SHEET_URL_RE.search(s)
    if m:
        return "sheet", m.group(1)
    m = _WIKI_URL_RE.search(s)
    if m:
        return "wiki", m.group(1)
    # Bare token heuristic: only letters+digits, reasonable length.
    if re.fullmatch(r"[A-Za-z0-9]{10,}", s):
        # Can't tell docx vs sheet from a bare token — default to docx and let
        # caller retry as sheet if that fails.
        return "docx", s
    return "unknown", ""


@tool_parameters(_FEISHU_DOC_PARAMETERS)
class FeishuDocReadTool(Tool):
    """Read Feishu cloud documents and spreadsheets."""

    @property
    def read_only(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "feishu_doc_read"

    @property
    def description(self) -> str:
        return (
            "Read the text content of a Feishu cloud document (docx) or the cells of "
            "a Feishu spreadsheet (sheets). Pass the full URL like "
            "https://xxx.feishu.cn/docx/<token> or https://xxx.feishu.cn/sheets/<token>. "
            "Returns plain text suitable for summarisation."
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        try:
            from nanobot.config.loader import load_config

            feishu_cfg = getattr(load_config().channels, "feishu", None)
        except Exception:
            return False
        if feishu_cfg is None:
            return False
        instances = feishu_cfg.get("instances") if isinstance(feishu_cfg, dict) else getattr(
            feishu_cfg, "instances", None
        )
        if not instances:
            return False
        return any(
            (inst.get("enabled") if isinstance(inst, dict) else getattr(inst, "enabled", False))
            for inst in instances
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls()

    async def execute(
        self,
        url_or_token: str,
        range: str = "",
        **kwargs: Any,
    ) -> str:
        from nanobot.channels.runtime_registry import get_channel

        channel = get_channel("feishu")
        if channel is None:
            return ToolResult.error("Error: Feishu channel is not running.")
        client = getattr(channel, "_client", None)
        if client is None:
            return ToolResult.error("Error: Feishu client is not ready.")

        kind, token = _parse_doc_ref(url_or_token)
        if not token:
            return ToolResult.error(
                f"Error: could not extract a Feishu doc/sheet token from {url_or_token!r}."
            )

        loop = asyncio.get_running_loop()
        if kind == "sheet":
            fetch = getattr(channel, "get_sheet_values_sync", None)
            if fetch is None:
                return ToolResult.error("Error: channel lacks get_sheet_values_sync.")
            title, text = await loop.run_in_executor(None, fetch, token, range or "")
            if not text or text.startswith(("error", "exception")):
                return ToolResult.error(f"Feishu sheet read failed: {text or 'empty'}")
            return f"# 表格 {title}\n\n```\n{text}\n```"

        # docx (and wiki fallback — wiki tokens often wrap a docx node).
        fetch_doc = getattr(channel, "get_docx_raw_content_sync", None)
        if fetch_doc is None:
            return ToolResult.error("Error: channel lacks get_docx_raw_content_sync.")

        if kind == "wiki":
            # /wiki/<token> wraps a node; obtain the real doc token via the node endpoint.
            # We attempt direct docx fetch first — works when token coincides with docx.
            title, text = await loop.run_in_executor(None, fetch_doc, token)
            if text and not text.startswith(("error", "exception")):
                return f"# 文档 {title}\n\n{text}"
            return ToolResult.error(
                f"Feishu wiki read failed for {token}. Wiki nodes usually require a separate "
                f"`wiki.v2.spaces.get_node` call to unwrap — not yet implemented."
            )

        title, text = await loop.run_in_executor(None, fetch_doc, token)
        if not text or text.startswith(("error", "exception")):
            return ToolResult.error(f"Feishu doc read failed: {text or 'empty'}")
        return f"# 文档 {title}\n\n{text}"
