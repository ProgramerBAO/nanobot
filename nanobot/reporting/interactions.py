"""Short-lived server-side state for structured report interactions."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(slots=True)
class ReportInteraction:
    interaction_id: str
    channel: str
    chat_id: str
    user_id: str
    options: dict[str, str]
    all_option: str
    submit_token: str
    expires_at: float
    consumed: bool = False


class ReportInteractionStore:
    """In-memory opaque option store shared by WebSocket and Feishu renderers."""

    def __init__(self, *, ttl_seconds: int = 600) -> None:
        self._ttl_seconds = max(60, min(ttl_seconds, 3600))
        self._items: dict[str, ReportInteraction] = {}
        self._lock = threading.RLock()

    def create(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
        options: dict[str, str],
    ) -> ReportInteraction:
        self.prune()
        interaction = ReportInteraction(
            interaction_id=secrets.token_urlsafe(18),
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            options=dict(options),
            all_option=secrets.token_urlsafe(12),
            submit_token=secrets.token_urlsafe(12),
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        with self._lock:
            self._items[interaction.interaction_id] = interaction
        return interaction

    def resolve(
        self,
        *,
        interaction_id: str,
        channel: str,
        chat_id: str,
        user_id: str,
        submit_token: str,
        selected_options: list[str],
        period: str,
        start_date: str = "",
        end_date: str = "",
    ) -> dict[str, Any] | None:
        self.prune()
        with self._lock:
            item = self._items.get(interaction_id)
            if item is None or item.consumed:
                return None
            if (
                item.channel != channel
                or item.chat_id != chat_id
                or item.user_id != user_id
                or item.submit_token != submit_token
            ):
                return None
            selected = list(dict.fromkeys(str(value) for value in selected_options))
            if len(selected) > 50:
                return None
            if not selected:
                selected = [item.all_option]
            if item.all_option in selected and len(selected) > 1:
                return None
            if any(value not in item.options and value != item.all_option for value in selected):
                return None
            if period not in {"recent15m", "day", "week", "range"}:
                return None
            if period == "range":
                try:
                    start = date.fromisoformat(start_date)
                    end = date.fromisoformat(end_date)
                except ValueError:
                    return None
                if end < start or (end - start).days >= 90:
                    return None
            elif start_date or end_date:
                return None
            providers = [] if selected == [item.all_option] else [item.options[value] for value in selected]
            item.consumed = True
            result = {
                "action": "provider_quality_report",
                "period": period,
                "providers": providers,
            }
            if period == "range":
                result["start_date"] = start_date
                result["end_date"] = end_date
            return result

    def prune(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [key for key, value in self._items.items() if value.expires_at <= now]
            for key in expired:
                self._items.pop(key, None)


_STORE = ReportInteractionStore()


def report_interactions() -> ReportInteractionStore:
    return _STORE
