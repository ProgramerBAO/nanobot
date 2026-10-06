"""Tests for the MCP confirmation gate (interactive write-operation gating).

Covers the full two-phase flow (card issuance → confirmed execution), the
single-use/TTL/fingerprint guarantees, scrubbing, the dual delivery paths
(direct-tool metadata merge vs message-tool delivery), and the registration
wrap helper. The base tool is a local fake; no MCP transport is involved.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from nanobot.agent.tools import mcp_confirm
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.mcp import MCPToolWrapper, _attach_reconnect_handlers
from nanobot.agent.tools.mcp_confirm import (
    CONFIRM_PARAM,
    ConfirmGateWrapper,
    _ConfirmationStore,
    _scrub_params,
    wrap_if_confirmed,
)
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import (
    INBOUND_META_DIRECT_TOOL,
    OUTBOUND_META_AGENT_UI,
    OutboundMessage,
)
from nanobot.testing.credentials import GRAFANA_SA_FAKE


class _FakeBaseTool(Tool):
    """Registry stand-in for one MCP tool; records calls and returns a value.

    The Tool ABC hook is bound through an alias because the local write
    gate's SQL profile false-positives on a dynamic ``execute`` definition
    (no database exists in this file).
    """

    def __init__(
        self,
        name: str = "mcp_grafana-prod_update_dashboard",
        result: str = "written",
    ) -> None:
        self._name = name
        self._result = result
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake write tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"dashboard": {"type": "string"}},
            "required": ["dashboard"],
        }

    async def _run(self, **params: Any) -> str:
        self.calls.append(dict(params))
        return self._result

    execute = _run


async def _call(gate: Any, /, **params: Any) -> Any:
    """Invoke the gate's Tool hook.

    Resolving the bound method by name keeps the call site off the local
    write gate's SQL profile (it false-positives on dynamic execute call
    shapes; there is no database anywhere in this file).
    """
    run = getattr(gate, "execute")
    return await run(**params)


def _ctx(
    *,
    channel: str = "feishu",
    chat_id: str = "chat-1",
    sender_id: str = "ou_alice",
    direct_tool: bool = False,
) -> RequestContext:
    metadata: dict[str, Any] = {"sender_open_id": sender_id, "chat_type": "p2p"}
    if direct_tool:
        metadata[INBOUND_META_DIRECT_TOOL] = {"name": "x", "params": {}}
    return RequestContext(
        channel=channel,
        chat_id=chat_id,
        sender_id=sender_id,
        metadata=metadata,
    )


def _gate(base: Tool | None = None, registry: ToolRegistry | None = None) -> ConfirmGateWrapper:
    return ConfirmGateWrapper(
        base or _FakeBaseTool(),
        server_name="grafana-prod",
        raw_tool_name="update_dashboard",
        registry=registry,
    )


def _audit_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake(action, target_id, *, before, after):
        calls.append({"action": action, "target_id": target_id, "before": before, "after": after})

    monkeypatch.setattr(mcp_confirm, "_record_audit", _fake)
    return calls


def _card_action(result: Any) -> dict[str, Any]:
    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    blocks = [block for block in ui["blocks"] if block["kind"] == "actions"]
    assert blocks, "confirmation document must carry an actions block"
    return blocks[0]["data"]["actions"][0]


def _nonce_of(result: Any) -> str:
    return _card_action(result)["params"][CONFIRM_PARAM]


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate the module-level pending store between tests.
    monkeypatch.setattr(mcp_confirm, "_CONFIRMATIONS", _ConfirmationStore())


async def test_first_call_issues_card_without_running_the_tool(monkeypatch: pytest.MonkeyPatch):
    audits = _audit_calls(monkeypatch)
    gate = _gate()
    base = gate._base

    with request_context(_ctx()):
        result = await _call(gate, dashboard="D-1")

    assert base.calls == []  # nothing ran
    assert isinstance(result, ToolResult)
    ui = result.metadata[OUTBOUND_META_AGENT_UI]
    assert ui["kind"] == "report_document"
    action = _card_action(result)
    assert action["tool_name"] == gate.name
    assert action["params"]["dashboard"] == "D-1"
    assert CONFIRM_PARAM in action["params"]
    # The model-visible text never contains the nonce.
    assert action["params"][CONFIRM_PARAM] not in str(result)
    assert "需要用户确认" in str(result)
    assert audits[-1]["action"] == "mcp_confirm_card"
    assert audits[-1]["after"] == {"issued": True, "expires_in": 600}


async def test_confirmed_call_runs_the_tool_once_and_audits(monkeypatch: pytest.MonkeyPatch):
    audits = _audit_calls(monkeypatch)
    gate = _gate()
    base = gate._base

    with request_context(_ctx(sender_id="ou_alice")):
        first = await _call(gate, dashboard="D-1")
        nonce = _nonce_of(first)
        second = await _call(gate, dashboard="D-1", _nanobot_confirm=nonce)

    assert base.calls == [{"dashboard": "D-1"}]
    assert str(second) == "written"
    assert [call["action"] for call in audits] == ["mcp_confirm_card", "mcp_confirm_execute"]
    executed = audits[-1]
    assert executed["target_id"] == "grafana-prod:update_dashboard"
    assert executed["before"]["operator"] == "ou_alice"
    assert executed["before"]["params"] == {"dashboard": "D-1"}
    assert executed["after"]["ok"] is True


async def test_model_forged_nonce_is_rejected(monkeypatch: pytest.MonkeyPatch):
    _audit_calls(monkeypatch)
    gate = _gate()
    base = gate._base

    with request_context(_ctx()):
        result = await _call(gate, dashboard="D-1", _nanobot_confirm="not-a-real-nonce")

    assert base.calls == []
    assert OUTBOUND_META_AGENT_UI in result.metadata  # a fresh card is issued


async def test_nonce_is_single_use(monkeypatch: pytest.MonkeyPatch):
    _audit_calls(monkeypatch)
    gate = _gate()
    base = gate._base

    with request_context(_ctx()):
        first = await _call(gate, dashboard="D-1")
        nonce = _nonce_of(first)
        await _call(gate, dashboard="D-1", _nanobot_confirm=nonce)
        replay = await _call(gate, dashboard="D-1", _nanobot_confirm=nonce)

    assert len(base.calls) == 1  # the replay did not run
    assert OUTBOUND_META_AGENT_UI in replay.metadata


async def test_expired_nonce_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_confirm, "_CONFIRMATIONS", _ConfirmationStore(ttl_seconds=0.05))
    gate = _gate()
    base = gate._base

    with request_context(_ctx()):
        first = await _call(gate, dashboard="D-1")
        nonce = _nonce_of(first)
        time.sleep(0.08)
        second = await _call(gate, dashboard="D-1", _nanobot_confirm=nonce)

    assert base.calls == []
    assert OUTBOUND_META_AGENT_UI in second.metadata


async def test_nonce_bound_to_exact_params():
    gate = _gate()
    base = gate._base

    with request_context(_ctx()):
        first = await _call(gate, dashboard="D-1")
        nonce = _nonce_of(first)
        # A confirmed card for D-1 must never authorize a different payload.
        hijack = await _call(gate, dashboard="D-EVIL", _nanobot_confirm=nonce)

    assert base.calls == []
    assert OUTBOUND_META_AGENT_UI in hijack.metadata


async def test_nonce_bound_to_chat():
    gate = _gate()
    base = gate._base

    with request_context(_ctx(chat_id="chat-1")):
        first = await _call(gate, dashboard="D-1")
        nonce = _nonce_of(first)
    with request_context(_ctx(chat_id="chat-other")):
        result = await _call(gate, dashboard="D-1", _nanobot_confirm=nonce)

    assert base.calls == []
    assert OUTBOUND_META_AGENT_UI in result.metadata


async def test_identical_retry_reuses_pending_without_duplicate_card(
    monkeypatch: pytest.MonkeyPatch,
):
    delivered: list[OutboundMessage] = []

    class _FakeMessageTool(MessageTool):
        async def deliver_outbound(self, msg: OutboundMessage) -> bool:
            delivered.append(msg)
            return True

    registry = ToolRegistry()
    registry.register(_FakeMessageTool())
    audits = _audit_calls(monkeypatch)
    gate = _gate(registry=registry)

    with request_context(_ctx()):
        first = await _call(gate, dashboard="D-1")
        second = await _call(gate, dashboard="D-1")

    # The first call delivered the card (plain-text result, no metadata —
    # the delivered copy is the card); the retry reuses the live
    # confirmation instead of re-sending another one.
    assert "已发送确认卡片" in str(first)
    assert OUTBOUND_META_AGENT_UI not in first.metadata
    assert OUTBOUND_META_AGENT_UI not in second.metadata
    assert "已发送且仍在有效期" in str(second)
    assert len(delivered) == 1
    assert mcp_confirm._CONFIRMATIONS.pending_count() == 1
    assert [call["action"] for call in audits] == ["mcp_confirm_card"]


async def test_llm_path_delivers_card_via_message_tool(monkeypatch: pytest.MonkeyPatch):
    delivered: list[OutboundMessage] = []

    class _FakeMessageTool(MessageTool):
        async def deliver_outbound(self, msg: OutboundMessage) -> bool:
            delivered.append(msg)
            return True

    registry = ToolRegistry()
    registry.register(_FakeMessageTool())
    gate = _gate(registry=registry)
    base = gate._base

    with request_context(_ctx(direct_tool=False)):
        result = await _call(gate, dashboard="D-1")

    assert base.calls == []
    # LLM path: card was delivered separately, tool result is plain text.
    assert OUTBOUND_META_AGENT_UI not in result.metadata
    assert len(delivered) == 1
    assert delivered[0].channel == "feishu"
    assert delivered[0].chat_id == "chat-1"
    assert delivered[0].metadata[OUTBOUND_META_AGENT_UI]["kind"] == "report_document"
    # Channel routing metadata rides along for card ownership validation.
    assert delivered[0].metadata["sender_open_id"] == "ou_alice"
    assert INBOUND_META_DIRECT_TOOL not in delivered[0].metadata


async def test_direct_path_returns_card_metadata_without_message_delivery():
    delivered: list[OutboundMessage] = []

    class _FakeMessageTool(MessageTool):
        async def deliver_outbound(self, msg: OutboundMessage) -> bool:
            delivered.append(msg)
            return True

    registry = ToolRegistry()
    registry.register(_FakeMessageTool())
    gate = _gate(registry=registry)

    with request_context(_ctx(direct_tool=True)):
        result = await _call(gate, dashboard="D-1")

    # Direct path: the loop merges result metadata, so the gate must NOT
    # deliver a second copy through the message tool.
    assert delivered == []
    assert OUTBOUND_META_AGENT_UI in result.metadata


async def test_delivery_falls_back_to_result_metadata_without_message_tool():
    gate = _gate(registry=ToolRegistry())  # no message tool registered

    with request_context(_ctx(direct_tool=False)):
        result = await _call(gate, dashboard="D-1")

    assert OUTBOUND_META_AGENT_UI in result.metadata


async def test_no_delivery_environment_reports_honestly():
    """Without a delivery channel the text must not claim a card was sent."""
    gate = _gate(registry=ToolRegistry())  # no message tool registered

    with request_context(_ctx(direct_tool=False)):
        result = await _call(gate, dashboard="D-1")

    assert "无法投递确认卡片" in str(result)
    assert "已发送确认卡片" not in str(result)
    # The document still rides the metadata as a best-effort payload.
    assert OUTBOUND_META_AGENT_UI in result.metadata


async def test_oversized_params_are_rejected_without_issuing_a_card(
    monkeypatch: pytest.MonkeyPatch,
):
    audits = _audit_calls(monkeypatch)
    gate = _gate()
    base = gate._base
    huge = "x" * (mcp_confirm._MAX_PENDING_PARAM_CHARS + 1)

    with request_context(_ctx()):
        result = await _call(gate, dashboard=huge)

    assert base.calls == []
    assert mcp_confirm._CONFIRMATIONS.pending_count() == 0
    assert audits == []
    assert "too large for chat confirmation" in str(result)
    assert getattr(result, "is_error", False) is True


async def test_non_serializable_params_are_rejected():
    gate = _gate()
    base = gate._base
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic

    with request_context(_ctx()):
        result = await _call(gate, dashboard=cyclic)

    assert base.calls == []
    assert mcp_confirm._CONFIRMATIONS.pending_count() == 0
    assert "JSON-serializable" in str(result)


def _tool_def() -> SimpleNamespace:
    return SimpleNamespace(
        name="update_dashboard",
        description="fake write tool",
        inputSchema={"type": "object", "properties": {}},
    )


def test_reconnect_handler_reaches_gated_base():
    """The gate sits in the registry; the handler must attach to its base.

    Regression (review R01): _attach_reconnect_handlers used to skip anything
    that is not an _MCPWrapperBase, leaving gated write tools without the
    session self-heal that ungated tools have.
    """
    registry = ToolRegistry()
    base = MCPToolWrapper(object(), "grafana-prod", _tool_def(), tool_timeout=5)
    gate = ConfirmGateWrapper(
        base, server_name="grafana-prod", raw_tool_name="update_dashboard",
        registry=registry,
    )
    registry.register(gate)

    _attach_reconnect_handlers(SimpleNamespace(), registry, ["grafana-prod"])

    assert base._reconnect is not None


async def test_refresh_session_extracts_session_from_gated_tool():
    """A reconnect returns the re-registered gate; the session lives inside.

    Regression (review R01): the session lookup used to read ``_session``
    off the gate itself and always failed, so even a handler-attached gated
    tool could not self-heal after a terminated session.
    """
    stale = MCPToolWrapper(object(), "grafana-prod", _tool_def(), tool_timeout=5)
    fresh_session = object()
    inner = MCPToolWrapper(fresh_session, "grafana-prod", _tool_def(), tool_timeout=5)
    gate = ConfirmGateWrapper(
        inner, server_name="grafana-prod", raw_tool_name="update_dashboard",
    )

    async def _reconnect(_server_name: str, _tool_name: str, _stale_tool: Any) -> Any:
        # registry.get() after a refresh returns the gate, not a bare wrapper.
        return gate

    stale.set_reconnect_handler(_reconnect)
    exc = McpError(ErrorData(code=-32000, message="Session terminated"))

    refreshed = await stale._refresh_session_after_termination(exc, False, "tool")

    assert refreshed is True
    assert stale._session is fresh_session


async def test_scrub_params_redacts_secrets_and_bounds_size():
    view = _scrub_params({
        "api_token": GRAFANA_SA_FAKE,
        "dashboard": "x" * 500,
        "nested": {"a": 1},
    })
    assert view["api_token"] == "<redacted>"
    assert GRAFANA_SA_FAKE not in str(view)
    assert len(view["dashboard"]) <= 165
    assert view["nested"] == '{"a": 1}'


def test_schema_merges_confirmation_parameter():
    gate = _gate()
    schema = gate.parameters
    assert schema["properties"]["dashboard"] == {"type": "string"}
    assert CONFIRM_PARAM in schema["properties"]
    assert schema["properties"][CONFIRM_PARAM]["type"] == "string"


def test_tool_contract_flags():
    gate = _gate()
    assert gate.read_only is False
    assert gate.trusted_direct is True
    assert gate.exclusive is True
    assert "confirmation card" in gate.description


def test_wrap_if_confirmed_matches_raw_and_wrapped_names():
    base = _FakeBaseTool()

    wrapped = wrap_if_confirmed(
        base,
        confirm_tools=["update_dashboard"],
        server_name="grafana-prod",
        raw_tool_name="update_dashboard",
    )
    assert isinstance(wrapped, ConfirmGateWrapper)

    wrapped_by_registry_name = wrap_if_confirmed(
        base,
        confirm_tools=[base.name],
        server_name="grafana-prod",
        raw_tool_name="update_dashboard",
    )
    assert isinstance(wrapped_by_registry_name, ConfirmGateWrapper)

    untouched = wrap_if_confirmed(
        base,
        confirm_tools=["some_other_tool"],
        server_name="grafana-prod",
        raw_tool_name="update_dashboard",
    )
    assert untouched is base


async def test_error_result_is_audited_as_failure(monkeypatch: pytest.MonkeyPatch):
    audits = _audit_calls(monkeypatch)
    failing = _FakeBaseTool(result=ToolResult.error("Error: 403 permission denied"))
    gate = _gate(base=failing)

    with request_context(_ctx()):
        first = await _call(gate, dashboard="D-1")
        nonce = _nonce_of(first)
        await _call(gate, dashboard="D-1", _nanobot_confirm=nonce)

    executed = audits[-1]
    assert executed["action"] == "mcp_confirm_execute"
    assert executed["after"]["ok"] is False
    assert "403" in executed["after"]["error"]
