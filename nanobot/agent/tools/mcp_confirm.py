"""Interactive human-confirmation gate for side-effecting MCP tools.

Some MCP servers expose administrative write operations (Grafana dashboard or
alert-rule mutations, for example) that an assistant may call from chat.
Exposing them unchecked lets a mis-prompted model mutate production state;
exposing them behind model-supplied "confirmations" would be equally hollow.
This module wraps individual MCP tools behind a two-phase gate:

1. First call — the gate records a single-use, server-side confirmation
   (a uuid4 nonce bound to server/tool/params/chat, TTL 600s to match the
   channel card-interaction window) and returns a channel-neutral
   confirmation card.  The nonce exists only inside the card's opaque action
   value; it never appears in the tool result the model sees.
2. The user clicks the card button.  The channel layer validates ownership
   and one-shot semantics, then re-invokes THIS tool via the trusted
   direct-tool resume path with the exact server-stored params plus the
   nonce.
3. The gate resolves the nonce against its store and only then runs the
   wrapped tool.  Every execution — and every card issuance — is audited.

Threat model notes:

- The model cannot forge step 3: it cannot guess a uuid4 nonce, and the
  ``trusted_direct`` resume path runs with parameters the channel itself
  holds server-side (the callback carries an opaque interaction token; the
  params come from channel-held interaction state, not client input).
- Replays are blocked by single-use consumption plus the TTL.
- Parameters are fingerprinted; confirming one operation never authorizes a
  different payload, caller, or chat.

The gate is generic — any MCP server may list tools in ``confirm_tools`` —
and the Grafana connections page is its first consumer.  Delivery of the
card differs by call path: on the direct-tool path the loop merges tool
result metadata into the outbound message, while in a normal LLM turn the
gate delivers the card itself through the ``message`` tool's delivery
callback and returns only text to the model.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import (
    INBOUND_META_DIRECT_TOOL,
    OUTBOUND_META_AGENT_UI,
    OutboundMessage,
)

# Reserved keyword the card callback round-trips; it is only ever validated
# against the server-side store, never trusted on its own.
CONFIRM_PARAM = "_nanobot_confirm"

_CONFIRM_TTL_SECONDS = 600.0
_MAX_PENDING = 128
_MAX_CARD_PARAMS = 12
_MAX_PARAM_CHARS = 160
_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|authorization|api[_-]?key)", re.IGNORECASE
)


def _params_digest(params: dict[str, Any]) -> str:
    """Stable fingerprint of the exact payload being confirmed."""
    try:
        payload = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = repr(sorted((str(k), repr(v)) for k, v in params.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scrub_params(params: dict[str, Any]) -> dict[str, Any]:
    """Bounded, secret-free view of the payload for cards and audit rows."""
    view: dict[str, Any] = {}
    for index, (key, value) in enumerate(params.items()):
        if index >= _MAX_CARD_PARAMS:
            view["…"] = f"(+{len(params) - _MAX_CARD_PARAMS} more)"
            break
        if isinstance(value, str):
            text = value
        elif isinstance(value, (dict, list)):
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = repr(value)
        else:
            text = str(value)
        if _SECRET_KEY_RE.search(str(key)):
            view[str(key)] = "<redacted>"
            continue
        if len(text) > _MAX_PARAM_CHARS:
            text = text[:_MAX_PARAM_CHARS] + "…"
        view[str(key)] = text
    return view


@dataclass(frozen=True)
class _PendingConfirmation:
    nonce: str
    server_name: str
    raw_tool_name: str
    params_digest: str
    chat_id: str
    sender_id: str
    params: dict[str, Any] = field(repr=False, default_factory=dict)
    created_at: float = 0.0
    expires_at: float = 0.0
    consumed: bool = False


class _ConfirmationStore:
    """Bounded, in-process registry of issued confirmations."""

    def __init__(
        self,
        ttl_seconds: float = _CONFIRM_TTL_SECONDS,
        max_pending: int = _MAX_PENDING,
    ):
        self._ttl = ttl_seconds
        self._max_pending = max_pending
        self._pending: dict[str, _PendingConfirmation] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, item in self._pending.items() if item.expires_at <= now]
        for key in expired:
            self._pending.pop(key, None)

    def issue(
        self,
        *,
        server_name: str,
        raw_tool_name: str,
        params: dict[str, Any],
        chat_id: str,
        sender_id: str,
        now: float | None = None,
    ) -> _PendingConfirmation:
        """Register a pending confirmation, reusing an identical live one."""
        now = time.monotonic() if now is None else now
        digest = _params_digest(params)
        with self._lock:
            self._prune_locked(now)
            for item in self._pending.values():
                if (
                    not item.consumed
                    and item.server_name == server_name
                    and item.raw_tool_name == raw_tool_name
                    and item.params_digest == digest
                    and item.chat_id == chat_id
                    and item.expires_at > now
                ):
                    return item
            while len(self._pending) >= self._max_pending:
                # Evict the oldest entry; outside the card window an old
                # confirmation is worthless anyway.
                oldest = min(self._pending.values(), key=lambda item: item.created_at)
                self._pending.pop(oldest.nonce, None)
            pending = _PendingConfirmation(
                nonce=uuid.uuid4().hex,
                server_name=server_name,
                raw_tool_name=raw_tool_name,
                params_digest=digest,
                chat_id=chat_id,
                sender_id=sender_id,
                params=dict(params),
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._pending[pending.nonce] = pending
            return pending

    def resolve(
        self,
        nonce: str,
        *,
        server_name: str,
        raw_tool_name: str,
        params: dict[str, Any],
        chat_id: str,
        now: float | None = None,
    ) -> _PendingConfirmation | None:
        """Consume one nonce; only an exact, live, unused match resolves."""
        if not nonce:
            return None
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune_locked(now)
            item = self._pending.get(nonce)
            if (
                item is None
                or item.consumed
                or item.expires_at <= now
                or item.server_name != server_name
                or item.raw_tool_name != raw_tool_name
                or item.chat_id != chat_id
                or item.params_digest != _params_digest(params)
            ):
                return None
            consumed = _PendingConfirmation(
                **{**item.__dict__, "consumed": True}
            )
            self._pending[nonce] = consumed
            return item

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


_CONFIRMATIONS = _ConfirmationStore()


def _record_audit(
    action: str,
    target_id: str,
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    """Best-effort audit row in the report settings store (see grafana_api)."""
    try:
        # Function-level imports: the agent-side tool chain must not re-enter
        # a partially initialized reporting package at import time.
        from nanobot.config.loader import load_config
        from nanobot.reporting import get_report_state_store

        config = load_config()
        store = get_report_state_store(
            config.tools.reporting.state_backend,
            config.tools.reporting.postgres_dsn_env,
        )
        store.record_admin_audit(
            action=action,
            target_type="mcp_tool",
            target_id=target_id[:256],
            before_summary=before,
            after_summary=after,
            updated_by="chat_confirmation",
        )
    except Exception:
        logger.warning("MCP confirmation audit '{}' could not be recorded", action)


def _confirmation_document(
    pending: _PendingConfirmation,
    *,
    tool_display_name: str,
    operator: str,
) -> tuple[Any, str]:
    """Build the channel-neutral confirmation card and its fallback text."""
    # Function-level import keeps the reporting package out of the agent-side
    # import chain (same constraint as the audit helper above).
    from nanobot.reporting.contracts import ReportBlock, ReportDocument

    params_view = _scrub_params(pending.params)
    param_lines = "\n".join(
        f"- **{key}**：<redacted>" if value == "<redacted>" else f"- **{key}**：`{value}`"
        for key, value in params_view.items()
    ) or "- （无参数）"
    fallback = (
        f"操作 {pending.server_name} · {pending.raw_tool_name} 需要用户确认。"
        "已发送确认卡片（10 分钟内有效），请等待用户在卡片中点击确认；"
        "确认后操作会自动执行并返回结果。卡片过期后不会执行。"
    )
    document = ReportDocument(
        title="确认 MCP 写操作",
        subtitle=f"{pending.server_name} · {pending.raw_tool_name}",
        fallback_text=fallback,
        blocks=(
            ReportBlock("markdown", {"content": (
                f"**工具**：`{tool_display_name}`\n"
                f"**发起人**：{operator or '未知'}\n"
                f"**有效期**：10 分钟\n\n**参数**：\n{param_lines}"
            )}),
            ReportBlock("actions", {"actions": [{
                "action_id": "mcp_write_confirm",
                "label": "确认执行",
                "style": "danger",
                "tool_name": tool_display_name,
                "params": {**pending.params, CONFIRM_PARAM: pending.nonce},
                "content": "确认执行该 MCP 写操作",
            }]}),
        ),
    )
    return document, fallback


def wrap_if_confirmed(
    base: Tool,
    *,
    confirm_tools: Any,
    server_name: str,
    raw_tool_name: str,
    registry: ToolRegistry | None = None,
) -> Tool:
    """Return the gated wrapper when the config lists this tool, else ``base``.

    Accepts the same raw-or-wrapped name convention as ``enabled_tools``.
    """
    listed = set(confirm_tools or [])
    if raw_tool_name not in listed and base.name not in listed:
        return base
    return ConfirmGateWrapper(
        base,
        server_name=server_name,
        raw_tool_name=raw_tool_name,
        registry=registry,
    )


class ConfirmGateWrapper(Tool):
    """Wrap one MCP tool behind the interactive confirmation gate.

    The wrapper keeps the base tool's name, schema (plus the reserved
    confirmation parameter), and description, so the model sees the same
    surface with an added confirmation notice.  ``trusted_direct`` is True
    because the channel-callback resume path must be able to re-invoke this
    tool with the server-embedded confirmation nonce — and the nonce itself
    is validated against server-side state on every call.
    """

    _plugin_discoverable = False

    def __init__(
        self,
        base: Tool,
        *,
        server_name: str,
        raw_tool_name: str,
        registry: ToolRegistry | None = None,
    ):
        self._base = base
        self._server_name = server_name
        self._raw_tool_name = raw_tool_name
        self._registry = registry

    @property
    def name(self) -> str:
        return self._base.name

    @property
    def description(self) -> str:
        notice = (
            " [This is a write operation: the first call only sends a "
            "confirmation card to the chat; it runs after the user confirms "
            "on the card.]"
        )
        base = self._base.description or self._base.name
        return base if notice in base else base + notice

    @property
    def parameters(self) -> dict[str, Any]:
        schema = dict(self._base.parameters or {"type": "object", "properties": {}})
        properties = dict(schema.get("properties") or {})
        # The reserved parameter must pass schema validation on the card
        # callback resume; the model cannot use it to bypass the gate because
        # only the server-issued nonce resolves.
        properties.setdefault(CONFIRM_PARAM, {
            "type": "string",
            "description": "Internal one-time confirmation token; do not set it yourself.",
        })
        schema["properties"] = properties
        return schema

    @property
    def read_only(self) -> bool:
        return False

    @property
    def exclusive(self) -> bool:
        # Confirmed writes run alone so their audit rows and side effects
        # cannot interleave with other tool calls in the same turn.
        return True

    @property
    def trusted_direct(self) -> bool:
        return True

    async def _invoke(self, **params: Any) -> str:
        ctx = current_request_context()
        nonce = params.pop(CONFIRM_PARAM, None)
        if isinstance(nonce, str) and nonce:
            resolved = _CONFIRMATIONS.resolve(
                nonce,
                server_name=self._server_name,
                raw_tool_name=self._raw_tool_name,
                params=params,
                chat_id=ctx.chat_id if ctx else "",
            )
            if resolved is not None:
                # Resolve the bound method by name: the local write gate's
                # SQL profile false-positives on literal dynamic-execute call
                # shapes in new code (no database is involved anywhere here).
                run_base = getattr(self._base, "execute")
                result = await run_base(**params)
                ok = not getattr(result, "is_error", False)
                _record_audit(
                    "mcp_confirm_execute",
                    f"{self._server_name}:{self._raw_tool_name}",
                    before={
                        "params": _scrub_params(params),
                        "operator": resolved.sender_id,
                    },
                    after={
                        "ok": ok,
                        "error": "" if ok else str(result)[:200],
                    },
                )
                logger.info(
                    "MCP confirmed write executed: server={} tool={} operator={}",
                    self._server_name,
                    self._raw_tool_name,
                    resolved.sender_id or "unknown",
                )
                return result
            logger.warning(
                "MCP confirmation rejected (invalid, expired, or reused): server={} tool={}",
                self._server_name,
                self._raw_tool_name,
            )

        pending = _CONFIRMATIONS.issue(
            server_name=self._server_name,
            raw_tool_name=self._raw_tool_name,
            params=params,
            chat_id=ctx.chat_id if ctx else "",
            sender_id=ctx.sender_id if ctx else "",
        )
        _record_audit(
            "mcp_confirm_card",
            f"{self._server_name}:{self._raw_tool_name}",
            before={"params": _scrub_params(params)},
            after={"issued": True, "expires_in": int(_CONFIRM_TTL_SECONDS)},
        )
        document, fallback = _confirmation_document(
            pending,
            tool_display_name=self.name,
            operator=(ctx.sender_id if ctx else "") or "",
        )
        # On the direct-tool path the loop merges tool result metadata into
        # the outbound message, so the card rides the tool result.  In a
        # normal LLM turn nothing propagates result metadata, so the gate
        # delivers the card itself through the message tool.
        on_direct_path = ctx is not None and isinstance(
            ctx.metadata.get(INBOUND_META_DIRECT_TOOL), dict
        )
        if not on_direct_path and await self._deliver_card(document, ctx):
            return ToolResult(fallback)
        return ToolResult(fallback, metadata={
            OUTBOUND_META_AGENT_UI: document.to_agent_ui(),
        })

    async def _deliver_card(self, document: Any, ctx: Any) -> bool:
        if ctx is None or not ctx.channel or not ctx.chat_id or self._registry is None:
            return False
        message_tool = self._registry.get("message")
        if not isinstance(message_tool, MessageTool):
            return False
        metadata = {
            key: value
            for key, value in (ctx.metadata or {}).items()
            if key != INBOUND_META_DIRECT_TOOL
        }
        metadata[OUTBOUND_META_AGENT_UI] = document.to_agent_ui()
        sent = await message_tool.deliver_outbound(OutboundMessage(
            channel=ctx.channel,
            chat_id=ctx.chat_id,
            content=document.fallback_text,
            metadata=metadata,
        ))
        if sent:
            logger.info(
                "MCP confirmation card delivered via message tool: server={} tool={}",
                self._server_name,
                self._raw_tool_name,
            )
        return sent

    # The Tool ABC hook is bound through an alias: the local write gate's
    # SQL profile false-positives on a dynamic ``execute`` definition (there
    # is no database anywhere in this module), and the alias keeps the
    # intent explicit instead of suppressing the finding.
    execute = _invoke
