"""Channel-neutral report delivery with retries, fallback, and idempotency."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.reporting.contracts import ReportDocument
from nanobot.reporting.registry import ReportPluginRegistry
from nanobot.reporting.store import ReportStateStore


class ChannelTransport(Protocol):
    async def send(self, msg: OutboundMessage) -> None:
        """Send one already-rendered message through the existing channel runtime."""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: str
    channel_id: str
    renderer_id: str
    parts: int
    attempts: int
    degraded: bool = False
    error_type: str = ""


def split_message(content: str, max_length: int) -> tuple[str, ...]:
    """Split long Markdown while preferring line boundaries."""

    if max_length < 1:
        raise ValueError("max_length must be positive")
    content = content.strip()
    if not content:
        return ("",)
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in content.splitlines():
        pieces = [line[index:index + max_length] for index in range(0, len(line), max_length)] or [""]
        for piece in pieces:
            if current and current_length + len(piece) + 1 > max_length:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            current.append(piece)
            current_length += len(piece) + (1 if current_length else 0)
    if current:
        chunks.append("\n".join(current))
    return tuple(chunk.strip() for chunk in chunks if chunk.strip()) or ("",)


class DeliveryRouter:
    """Deliver one ReportDocument through an existing channel transport."""

    def __init__(
        self,
        registry: ReportPluginRegistry,
        store: ReportStateStore,
        transports: Mapping[str, ChannelTransport],
        *,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        self._registry = registry
        self._store = store
        self._transports = transports
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds

    async def deliver(
        self,
        document: ReportDocument,
        *,
        channel_id: str,
        chat_id: str,
        user_id: str = "",
        trace_id: str = "",
        idempotency_key: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> DeliveryResult:
        transport = self._transports.get(channel_id)
        if transport is None and "." in channel_id:
            transport = self._transports.get(channel_id.split(".", 1)[0])
        if transport is None:
            return DeliveryResult("error", channel_id, "", 0, 0, error_type="channel_unavailable")

        renderer = self._registry.exact_renderer(channel_id)
        if renderer is None and "." in channel_id:
            renderer = self._registry.exact_renderer(channel_id.split(".", 1)[0])
        degraded = renderer is None
        if renderer is None:
            renderer = self._registry.renderer("text")
        if renderer is None:
            return DeliveryResult("error", channel_id, "", 0, 0, error_type="renderer_unavailable")
        rendered = renderer.render(document)
        parts = split_message(rendered.content, renderer.capabilities.max_message_length)
        delivery_key = idempotency_key.strip()
        if delivery_key and not self._store.claim_delivery(delivery_key):
            return DeliveryResult("duplicate", channel_id, renderer.channel_id, len(parts), 0, degraded)

        attempts = 0
        base_metadata = {
            "report_id": document.document_id,
            "report_version": document.version,
            "renderer": renderer.channel_id,
            "trace_id": trace_id,
            "user_id": user_id,
            **rendered.metadata,
            **dict(metadata or {}),
        }
        try:
            for index, content in enumerate(parts, start=1):
                part_key = (
                    hashlib.sha256(f"{delivery_key}:part:{index}".encode("utf-8")).hexdigest()
                    if delivery_key
                    else ""
                )
                if part_key and not self._store.claim_delivery(part_key):
                    continue
                sent = False
                for attempt in range(1, self._max_attempts + 1):
                    attempts += 1
                    try:
                        await transport.send(
                            OutboundMessage(
                                channel=channel_id,
                                chat_id=chat_id,
                                content=content,
                                metadata={
                                    **base_metadata,
                                    "delivery_part": index,
                                    "delivery_parts": len(parts),
                                },
                            )
                        )
                    except Exception as exc:
                        if delivery_key:
                            self._store.record_delivery_attempt(
                                delivery_key,
                                part_index=index,
                                attempt=attempt,
                                status="error",
                                error_type=type(exc).__name__,
                            )
                        logger.warning(
                            "Report delivery attempt failed: channel={} part={} attempt={} error_type={}",
                            channel_id,
                            index,
                            attempt,
                            type(exc).__name__,
                        )
                        if attempt < self._max_attempts:
                            await asyncio.sleep(self._retry_backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    sent = True
                    if delivery_key:
                        self._store.record_delivery_attempt(
                            delivery_key,
                            part_index=index,
                            attempt=attempt,
                            status="ok",
                        )
                    break
                if not sent:
                    if part_key:
                        self._store.complete_delivery(part_key, status="error")
                    raise RuntimeError("channel delivery failed")
                if part_key:
                    self._store.complete_delivery(part_key, status="ok")
            if delivery_key:
                self._store.complete_delivery(delivery_key, status="ok")
            return DeliveryResult("ok", channel_id, renderer.channel_id, len(parts), attempts, degraded)
        except Exception as exc:
            if delivery_key:
                self._store.complete_delivery(delivery_key, status="error")
            return DeliveryResult(
                "error",
                channel_id,
                renderer.channel_id,
                len(parts),
                attempts,
                degraded,
                type(exc).__name__,
            )

    @staticmethod
    def default_idempotency_key(
        document: ReportDocument, *, channel_id: str, chat_id: str, period_key: str
    ) -> str:
        raw = "|".join(
            (channel_id, chat_id, document.document_id, str(document.version), period_key)
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
