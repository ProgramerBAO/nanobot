"""Runtime registry for live channel instances.

Lets agent tools reach a running channel (e.g. to call its platform API)
without threading the :class:`~nanobot.channels.manager.ChannelManager`
through the whole tool-construction chain. Entries are registered when a
channel starts and cleared when it stops.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.channels.base import BaseChannel

_live_channels: dict[str, "BaseChannel"] = {}


def register_channel(channel: "BaseChannel") -> None:
    """Register a started channel under its runtime name."""
    _live_channels[channel.name] = channel


def unregister_channel(name: str) -> None:
    """Drop a channel from the registry (on stop)."""
    _live_channels.pop(name, None)


def get_channel(name: str) -> "BaseChannel | None":
    """Return the live channel instance for *name*, or ``None``."""
    return _live_channels.get(name)


def all_channels() -> dict[str, "BaseChannel"]:
    """Snapshot of all live channels (name → instance)."""
    return dict(_live_channels)
