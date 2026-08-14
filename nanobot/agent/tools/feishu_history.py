"""Tool to read Feishu (Lark) group history, with time-window and rich message support."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

_FEISHU_HISTORY_PARAMETERS = tool_parameters_schema(
    chat_id=StringSchema(
        "Optional Feishu chat_id (starts with 'oc_'). "
        "Defaults to the chat the current conversation is happening in."
    ),
    thread_id=StringSchema(
        "Optional Feishu topic/thread ID (usually starts with 'omt_'). "
        "When omitted inside a topic, defaults to the current topic."
    ),
    offset=IntegerSchema(
        0,
        description=(
            "Continuation offset returned by a previous call. Start with 0. When the tool "
            "returns CONTINUE_REQUIRED, call it again with next_offset until READ_COMPLETE."
        ),
        minimum=0,
    ),
    start_time=StringSchema(
        "Inclusive window start. Accepts: unix seconds/ms, ISO datetime "
        "(e.g. '2026-08-13T09:00:00'), or natural Chinese time words like "
        "'今天' '昨天' '前天' '最近2小时' '最近3天'. Defaults to no lower bound."
    ),
    end_time=StringSchema(
        "Inclusive window end. Same formats as start_time. Defaults to 'now'."
    ),
    required=[],
    description=(
        "Read all visible messages from the current Feishu topic, group, or DM. Inside a "
        "topic, the entire current topic is read by default unless the request explicitly "
        "asks for the whole group. Group reads include ordinary messages and every visible "
        "topic reply. Results may be returned in continuation batches; keep calling with "
        "next_offset until READ_COMPLETE before answering. "
        "Image messages and file/doc attachments are downloaded and returned as local "
        "file paths so the agent can open them with read_file (multimodal models can "
        "see images; files like docs/sheets can be parsed from the path). "
        "Only messages the bot can see are returned."
    ),
)

_CN_REL_HOURS = re.compile(r"最近\s*(\d+)\s*小时")
_CN_REL_DAYS = re.compile(r"最近\s*(\d+)\s*天")


def _parse_time_expr(expr: str | None, *, is_end: bool = False) -> int | None:
    """Parse a time expression into milliseconds since epoch (local timezone).

    Supported: unix seconds/ms, ISO datetime, 今天/昨天/前天/最近N小时/最近N天.
    For date-like words, is_end=False gives start-of-day, is_end=True gives end-of-day.
    Returns None when expr is falsy or unparseable.
    """
    if not expr:
        return None
    s = str(expr).strip()
    if not s:
        return None

    now = datetime.now().astimezone()

    if s == "今天":
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        dt = base + timedelta(days=1) if is_end else base
        return int(dt.timestamp() * 1000)
    if s == "昨天":
        base = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        dt = base + timedelta(days=1) if is_end else base
        return int(dt.timestamp() * 1000)
    if s == "前天":
        base = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
        dt = base + timedelta(days=1) if is_end else base
        return int(dt.timestamp() * 1000)
    m = _CN_REL_HOURS.fullmatch(s)
    if m:
        return int((now - timedelta(hours=int(m.group(1)))).timestamp() * 1000)
    m = _CN_REL_DAYS.fullmatch(s)
    if m:
        return int((now - timedelta(days=int(m.group(1)))).timestamp() * 1000)

    if s.isdigit():
        v = int(s)
        return v if v > 1_000_000_000_000 else v * 1000

    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    return None


def _fmt_ts(ms: int | None) -> str:
    if not ms:
        return "??:??"
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return "??:??"


@tool_parameters(_FEISHU_HISTORY_PARAMETERS)
class FeishuChatHistoryTool(Tool):
    """Read Feishu messages for the current chat with optional time window."""

    _RESULT_PAGE_MAX_CHARS = 11_000

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, Any]] = {}

    @property
    def read_only(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "feishu_chat_history"

    @property
    def description(self) -> str:
        return (
            "Read every visible message from the current Feishu topic/chat, including all "
            "visible topic replies, optionally within a time window (e.g. 今天/昨天/最近2小时). "
            "Never stop at an arbitrary message count. If CONTINUE_REQUIRED is returned, "
            "call this tool again with next_offset and do not answer until READ_COMPLETE. "
            "Image messages and file/doc attachments are downloaded to local paths so "
            "you can open them with read_file for summarisation. When the user asks about "
            "earlier messages in a Feishu topic, call this tool to read the topic first."
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        # ctx.config here is ToolsConfig (no .channels), so load the full config.
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
        chat_id: str | None = None,
        thread_id: str | None = None,
        offset: int = 0,
        start_time: str | None = None,
        end_time: str | None = None,
        **kwargs: Any,
    ) -> str:
        from nanobot.channels.runtime_registry import get_channel
        from nanobot.config.paths import get_media_dir

        req_ctx = current_request_context()
        target_chat = (chat_id or (req_ctx.chat_id if req_ctx else "") or "").strip()
        request_text = ""
        if req_ctx:
            request_text = req_ctx.original_user_text or ""
            if isinstance(req_ctx.metadata, dict):
                request_text = req_ctx.metadata.get("direct_request_text") or request_text
        whole_group_requested = any(
            term in request_text for term in ("群里", "群聊", "群消息", "全群", "整个群")
        )
        current_thread = (
            req_ctx.metadata.get("thread_id")
            if req_ctx and isinstance(req_ctx.metadata, dict)
            else None
        )
        target_thread = (
            thread_id
            or (current_thread if not chat_id and not whole_group_requested else None)
            or ""
        ).strip()
        if not target_chat and not target_thread:
            return ToolResult.error(
                "Error: no chat_id/thread_id provided and the current conversation has no "
                "Feishu chat or topic."
            )

        if not start_time and not end_time:
            for marker in ("今天", "昨天", "前天"):
                if marker in request_text:
                    start_time = end_time = marker
                    break
            else:
                relative = _CN_REL_HOURS.search(request_text) or _CN_REL_DAYS.search(request_text)
                if relative:
                    start_time = relative.group(0)

        start_ms = _parse_time_expr(start_time, is_end=False)
        end_ms = _parse_time_expr(end_time, is_end=True)
        if start_time and start_ms is None:
            return ToolResult.error(f"Error: could not parse start_time {start_time!r}")
        if end_time and end_ms is None:
            return ToolResult.error(f"Error: could not parse end_time {end_time!r}")

        channel = get_channel("feishu")
        if channel is None:
            return ToolResult.error("Error: Feishu channel is not running.")
        get_history = getattr(channel, "get_chat_history_sync", None)
        get_name = getattr(channel, "_get_user_name_sync", None)
        dl_image = getattr(channel, "_download_image_sync", None)
        dl_file = getattr(channel, "_download_file_sync", None)
        client = getattr(channel, "_client", None)
        if client is None or get_history is None:
            return ToolResult.error("Error: Feishu channel is not ready yet.")

        loop = asyncio.get_running_loop()
        snapshot_key = (
            (req_ctx.turn_id or req_ctx.session_key)
            if req_ctx
            else f"{target_chat}:{target_thread}"
        ) or f"{target_chat}:{target_thread}"
        snapshot = self._snapshots.get(snapshot_key) if offset else None
        if snapshot is None:
            items = await loop.run_in_executor(
                None,
                lambda: get_history(
                    target_chat,
                    None,
                    start_ms,
                    end_ms,
                    thread_id=target_thread or None,
                ),
            )
            window_desc = (
                f"窗口 {start_time or '最早'} ~ {end_time or '现在'}"
                if (start_time or end_time) else ""
            )
            scope_label = f"话题 {target_thread}" if target_thread else f"群 {target_chat}"
            snapshot = {
                "items": items,
                "window_desc": window_desc,
                "scope_label": scope_label,
            }
            self._snapshots[snapshot_key] = snapshot
            while len(self._snapshots) > 8:
                self._snapshots.pop(next(iter(self._snapshots)))
        else:
            items = snapshot["items"]

        if not items:
            target_label = f"topic {target_thread}" if target_thread else f"chat {target_chat}"
            return (
                f"No readable messages found in {target_label} for the given window. "
                "The bot may lack group message permission or the chat may be empty."
            )
        if offset >= len(items):
            return ToolResult.error(
                f"Error: offset {offset} is outside the complete {len(items)}-message snapshot."
            )

        media_dir = get_media_dir("feishu")
        window_desc = snapshot["window_desc"]
        scope_label = snapshot["scope_label"]
        lines: list[str] = []
        rendered_chars = 0
        next_offset = offset

        for it in items[offset:]:
            ts = _fmt_ts(it.get("create_time_ms"))
            sender_open_id = it.get("sender_id", "unknown")
            name = (it.get("sender_name") or "").strip()
            if not name and get_name is not None:
                name = await loop.run_in_executor(None, get_name, sender_open_id) or ""
            sender = name or sender_open_id
            msg_type = it.get("msg_type", "")
            body_text = (it.get("text") or "").strip()
            parts = []
            if body_text:
                parts.append(body_text)

            # Image messages: download to media dir, give path for read_file.
            image_keys = it.get("image_keys") or []
            for ik in image_keys:
                if not ik or dl_image is None:
                    continue
                data, fname = await loop.run_in_executor(
                    None, dl_image, it.get("message_id") or "", ik
                )
                if data:
                    fname = fname or f"{ik}.jpg"
                    path = media_dir / f"hist_{ik}_{fname}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    parts.append(f"[图片→read_file: {path}]")
                else:
                    parts.append(f"[图片 {ik}: 下载失败]")

            # File/docs/sheets: download and expose path.
            if it.get("file_key") and dl_file is not None:
                fdata, ffname = await loop.run_in_executor(
                    None, dl_file, it.get("message_id") or "", it["file_key"], "file"
                )
                fname = it.get("file_name") or ffname or "attachment.bin"
                if fdata:
                    path = media_dir / f"hist_{it['file_key']}_{fname}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(fdata)
                    parts.append(f"[文件 {fname}→read_file: {path}]")
                else:
                    parts.append(f"[文件 {fname}: 下载失败]")

            if msg_type == "image" and not parts:
                parts.append("[图片]")
            if not parts:
                parts.append(f"[{msg_type}]")

            # Detect Feishu cloud doc / sheet links in the message text so the agent
            # knows to call feishu_doc_read for a proper summary.
            if body_text:
                doc_links = []
                from nanobot.agent.tools.feishu_docs import _DOCX_URL_RE, _SHEET_URL_RE
                for m in _DOCX_URL_RE.finditer(body_text):
                    doc_links.append("docx/"+m.group(1))
                for m in _SHEET_URL_RE.finditer(body_text):
                    doc_links.append("sheets/"+m.group(1))
                for dl in doc_links:
                    parts.append(f"[云文档→feishu_doc_read: {dl}]")

            topic_label = f" 话题:{it['thread_id']}" if it.get("thread_id") else ""
            line = f"[{ts} {sender}{topic_label}] " + " ".join(parts)
            if lines and rendered_chars + len(line) > self._RESULT_PAGE_MAX_CHARS:
                break
            lines.append(line)
            rendered_chars += len(line) + 1
            next_offset += 1

        header = (
            f"{scope_label} 已完整抓取 {len(items)} 条消息，本批为第 "
            f"{offset + 1}-{next_offset} 条"
            + (f"（{window_desc}，按时间先后）：" if window_desc else "（按时间先后）：")
        )
        if next_offset < len(items):
            continuation = (
                f"CONTINUE_REQUIRED: next_offset={next_offset}。尚有 "
                f"{len(items) - next_offset} 条未交给你处理；不要作答，必须继续调用 "
                f"feishu_chat_history(offset={next_offset})，直到收到 READ_COMPLETE。"
            )
        else:
            continuation = (
                f"READ_COMPLETE: 已读取并交付该范围内全部 {len(items)} 条消息，"
                "现在可以基于所有批次作答。"
            )
            self._snapshots.pop(snapshot_key, None)
        return "\n".join([header, *lines, continuation])
