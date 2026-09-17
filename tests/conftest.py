"""Shared test fixtures.

The autouse isolation below keeps report-settings overrides (tenant
aliases, change-alert threshold) out of unit tests: without it the
process-wide store handle in ``magik_cube`` would resolve to the
developer's real state DB, making assertions depend on machine-local
page settings — e.g. lowering the change-alert threshold in the WebUI
would retroactively break percentage pins. Tests that exercise the
override behavior pass a store explicitly or set the sentinel themselves.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_report_settings_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from nanobot.agent.tools import magik_cube

    monkeypatch.setattr(magik_cube, "_tenant_mappings_store", False)
