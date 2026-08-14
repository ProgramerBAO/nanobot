from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.feishu_history import FeishuChatHistoryTool, _parse_time_expr


@pytest.mark.asyncio
async def test_history_tool_defaults_to_current_feishu_topic() -> None:
    channel = MagicMock()
    channel._client = object()
    channel._get_user_name_sync = None
    channel._download_image_sync = None
    channel._download_file_sync = None
    channel.get_chat_history_sync.return_value = [
        {
            "message_id": "om_topic_reply",
            "sender_id": "ou_alice",
            "sender_name": "张岩",
            "msg_type": "text",
            "text": "这是其他人话题里的上下文",
            "create_time_ms": 1786683600000,
            "image_keys": [],
            "file_key": None,
            "file_name": None,
        }
    ]

    ctx = RequestContext(
        channel="feishu",
        chat_id="oc_group",
        metadata={"thread_id": "omt_topic"},
    )
    with patch("nanobot.channels.runtime_registry.get_channel", return_value=channel):
        with request_context(ctx):
            result = await FeishuChatHistoryTool().execute()

    channel.get_chat_history_sync.assert_called_once_with(
        "oc_group",
        None,
        None,
        None,
        thread_id="omt_topic",
    )
    assert "话题 omt_topic 已完整抓取 1 条消息" in result
    assert "READ_COMPLETE" in result
    assert "张岩" in result
    assert "这是其他人话题里的上下文" in result


@pytest.mark.asyncio
async def test_explicit_chat_id_does_not_reuse_current_topic() -> None:
    channel = MagicMock()
    channel._client = object()
    channel._get_user_name_sync = None
    channel._download_image_sync = None
    channel._download_file_sync = None
    channel.get_chat_history_sync.return_value = []

    ctx = RequestContext(
        channel="feishu",
        chat_id="oc_current",
        metadata={"thread_id": "omt_current"},
    )
    with patch("nanobot.channels.runtime_registry.get_channel", return_value=channel):
        with request_context(ctx):
            await FeishuChatHistoryTool().execute(chat_id="oc_other")

    channel.get_chat_history_sync.assert_called_once_with(
        "oc_other",
        None,
        None,
        None,
        thread_id=None,
    )


@pytest.mark.asyncio
async def test_group_request_ignores_current_topic_and_infers_today() -> None:
    channel = MagicMock()
    channel._client = object()
    channel._get_user_name_sync = None
    channel._download_image_sync = None
    channel._download_file_sync = None
    channel.get_chat_history_sync.return_value = [{
        "message_id": "om_alert",
        "sender_id": "ou_alice",
        "sender_name": "张岩",
        "msg_type": "text",
        "text": "告警",
        "create_time_ms": _parse_time_expr("今天"),
        "image_keys": [],
        "file_key": None,
        "file_name": None,
    }]
    ctx = RequestContext(
        channel="feishu",
        chat_id="oc_group",
        session_key="feishu:oc_group:om_root",
        original_user_text="总结下群里今天有哪些告警",
        metadata={"thread_id": "omt_current"},
    )

    with patch("nanobot.channels.runtime_registry.get_channel", return_value=channel):
        with request_context(ctx):
            result = await FeishuChatHistoryTool().execute()

    channel.get_chat_history_sync.assert_called_once_with(
        "oc_group",
        None,
        _parse_time_expr("今天", is_end=False),
        _parse_time_expr("今天", is_end=True),
        thread_id=None,
    )
    assert "READ_COMPLETE" in result


@pytest.mark.asyncio
async def test_history_tool_requires_continuation_until_snapshot_is_complete() -> None:
    channel = MagicMock()
    channel._client = object()
    channel._get_user_name_sync = None
    channel._download_image_sync = None
    channel._download_file_sync = None
    channel.get_chat_history_sync.return_value = [
        {
            "message_id": f"om_{index}",
            "sender_id": "ou_alice",
            "sender_name": "张岩",
            "msg_type": "text",
            "text": f"消息 {index}",
            "create_time_ms": 1786683600000 + index,
            "image_keys": [],
            "file_key": None,
            "file_name": None,
        }
        for index in range(3)
    ]
    tool = FeishuChatHistoryTool()
    tool._RESULT_PAGE_MAX_CHARS = 1
    ctx = RequestContext(
        channel="feishu",
        chat_id="oc_group",
        session_key="feishu:oc_group",
        turn_id="turn-history",
    )

    with patch("nanobot.channels.runtime_registry.get_channel", return_value=channel):
        with request_context(ctx):
            first = await tool.execute()
            second = await tool.execute(offset=1)
            third = await tool.execute(offset=2)

    assert "CONTINUE_REQUIRED: next_offset=1" in first
    assert "CONTINUE_REQUIRED: next_offset=2" in second
    assert "READ_COMPLETE" in third
    channel.get_chat_history_sync.assert_called_once()
