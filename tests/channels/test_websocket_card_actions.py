"""Card-action tests for the websocket runtime (report-document buttons).

Report-document action buttons that name a tool plus params (the MCP
confirmation gate, subscription confirm cards) are rewritten to opaque
single-use tokens before the wire; clicking resolves server-side state and
re-injects the direct-tool resume. The channel here is a lightweight
``object.__new__`` stand-in — only the collaborators the card-action paths
touch are wired.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from nanobot.bus.events import INBOUND_META_DIRECT_TOOL
from nanobot.channels.websocket.runtime import WebSocketChannel


def _channel() -> tuple[WebSocketChannel, dict[str, Any]]:
    channel = object.__new__(WebSocketChannel)
    channel._card_interactions = {}
    channel._card_interaction_lock = threading.Lock()
    channel._subs = {}
    channel._conn_chats = {}
    sent: list[dict[str, Any]] = []
    handled: list[dict[str, Any]] = []

    async def _send_event(_connection: Any, event: str, **kwargs: Any) -> None:
        sent.append({"event": event, **kwargs})

    async def _hydrate(_chat_id: str) -> None:
        return None

    async def _handle_message(**kwargs: Any) -> None:
        handled.append(kwargs)

    channel._send_event = _send_event  # type: ignore[method-assign]
    channel._hydrate_after_subscribe = _hydrate  # type: ignore[method-assign]
    channel._handle_message = _handle_message  # type: ignore[method-assign]
    channel._transcripts = SimpleNamespace(
        client_turn_metadata=lambda _turn_id: {},
        append_user_message=lambda *_args, **_kwargs: None,
    )
    return channel, {"sent": sent, "handled": handled}


def _confirm_document() -> dict[str, Any]:
    return {
        "kind": "report_document",
        "version": 1,
        "title": "确认 MCP 写操作",
        "blocks": [
            {"kind": "markdown", "data": {"content": "**工具**：demo"}},
            {
                "kind": "actions",
                "data": {
                    "actions": [
                        {
                            "action_id": "mcp_write_confirm",
                            "label": "确认执行",
                            "style": "danger",
                            "tool_name": "mcp_grafana-prod_update_dashboard",
                            "params": {"dashboard": "D-1", "_nanobot_confirm": "nonce-1"},
                            "content": "确认执行该 MCP 写操作",
                        },
                        # Command-style buttons stay untouched.
                        {"action_id": "open_center", "label": "打开", "command": "/订阅"},
                    ]
                },
            },
        ],
    }


def test_rewrite_swaps_direct_tool_actions_for_opaque_tokens():
    channel, _recorders = _channel()
    document = _confirm_document()
    original = _confirm_document()

    wire = channel._rewrite_agent_ui_actions(document, "chat-1")

    assert wire is not document
    # The input blob itself is never mutated (msg.metadata stays intact).
    assert document == original
    actions = wire["blocks"][1]["data"]["actions"]
    gated = actions[0]
    assert "tool_name" not in gated
    assert "params" not in gated
    assert gated["webui_token"]
    assert gated["label"] == "确认执行"
    # Command-style action and non-actions blocks pass through unchanged.
    assert actions[1] == {"action_id": "open_center", "label": "打开", "command": "/订阅"}
    assert wire["blocks"][0] == document["blocks"][0]
    # The server retained the executable payload behind the token.
    token = gated["webui_token"]
    item = channel._resolve_card_action(token, chat_id="chat-1")
    assert item is not None
    assert item.tool_name == "mcp_grafana-prod_update_dashboard"
    assert item.params == {"dashboard": "D-1", "_nanobot_confirm": "nonce-1"}


def test_rewrite_passes_through_non_report_documents_and_plain_blobs():
    channel, _recorders = _channel()
    selector = {"kind": "magik_report_form", "version": 1, "blocks": [
        {"kind": "actions", "data": {"actions": [
            {"action_id": "x", "tool_name": "t", "params": {"a": 1}},
        ]}},
    ]}
    assert channel._rewrite_agent_ui_actions(selector, "chat-1") is selector

    document = _confirm_document()
    document["blocks"] = [{"kind": "markdown", "data": {"content": "no actions"}}]
    assert channel._rewrite_agent_ui_actions(document, "chat-1") is document


def test_token_is_single_use_and_chat_bound():
    channel, _recorders = _channel()
    wire = channel._rewrite_agent_ui_actions(_confirm_document(), "chat-1")
    token = wire["blocks"][1]["data"]["actions"][0]["webui_token"]

    assert channel._resolve_card_action(token, chat_id="chat-other") is None
    assert channel._resolve_card_action(token, chat_id="chat-1") is not None
    assert channel._resolve_card_action(token, chat_id="chat-1") is None


async def test_dispatch_card_action_reinjects_direct_tool():
    channel, recorders = _channel()
    wire = channel._rewrite_agent_ui_actions(_confirm_document(), "chat-1")
    token = wire["blocks"][1]["data"]["actions"][0]["webui_token"]
    connection = object()  # hashable stand-in; _attach stores it in a set

    await channel._dispatch_card_action(connection, "client-1", {
        "chat_id": "chat-1",
        "token": token,
    })

    assert len(recorders["handled"]) == 1
    call = recorders["handled"][0]
    assert call["sender_id"] == "client-1"
    assert call["chat_id"] == "chat-1"
    direct = call["metadata"][INBOUND_META_DIRECT_TOOL]
    assert direct["name"] == "mcp_grafana-prod_update_dashboard"
    assert direct["params"] == {"dashboard": "D-1", "_nanobot_confirm": "nonce-1"}
    assert call["metadata"]["report_action_validated"] is True
    assert recorders["sent"] == []


async def test_dispatch_card_action_rejects_replay_and_bad_shapes():
    channel, recorders = _channel()
    wire = channel._rewrite_agent_ui_actions(_confirm_document(), "chat-1")
    token = wire["blocks"][1]["data"]["actions"][0]["webui_token"]
    connection = object()  # hashable stand-in; _attach stores it in a set

    # First click executes; the replay hits the consumed token.
    await channel._dispatch_card_action(connection, "client-1", {
        "chat_id": "chat-1", "token": token,
    })
    await channel._dispatch_card_action(connection, "client-1", {
        "chat_id": "chat-1", "token": token,
    })
    assert len(recorders["handled"]) == 1
    assert recorders["sent"][-1]["event"] == "error"
    assert recorders["sent"][-1]["detail"] == "invalid_or_expired_card_action"

    # Malformed frames never reach the resume path.
    await channel._dispatch_card_action(connection, "client-1", {"chat_id": "chat-1"})
    await channel._dispatch_card_action(connection, "client-1", {"token": "x"})
    assert len(recorders["handled"]) == 1
    assert recorders["sent"][-2]["detail"] == "invalid card action"
    assert recorders["sent"][-1]["detail"] == "invalid chat_id"


async def test_dispatch_card_action_rejects_wrong_chat_token():
    channel, recorders = _channel()
    wire = channel._rewrite_agent_ui_actions(_confirm_document(), "chat-1")
    token = wire["blocks"][1]["data"]["actions"][0]["webui_token"]
    connection = object()  # hashable stand-in; _attach stores it in a set

    await channel._dispatch_card_action(connection, "client-1", {
        "chat_id": "chat-2", "token": token,
    })

    assert recorders["handled"] == []
    assert recorders["sent"][-1]["detail"] == "invalid_or_expired_card_action"
