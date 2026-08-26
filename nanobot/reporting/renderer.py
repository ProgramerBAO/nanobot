"""Channel renderer extension contract and text fallback."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from nanobot.reporting.contracts import ReportDocument


@dataclass(frozen=True, slots=True)
class RenderedReport:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ChannelRenderer(ABC):
    channel_id: str
    version: str = "1.0"

    @abstractmethod
    def render(self, document: ReportDocument) -> RenderedReport:
        ...


class TextChannelRenderer(ChannelRenderer):
    channel_id = "text"

    def render(self, document: ReportDocument) -> RenderedReport:
        return RenderedReport(document.fallback_text or document.title)
