"""Shared test fixtures.

The autouse isolation below keeps report-settings overrides (tenant
aliases, change-alert threshold) out of unit tests: without it the
process-wide store handle in ``magik_cube`` would resolve to the
developer's real state DB, making assertions depend on machine-local
page settings — e.g. lowering the change-alert threshold in the WebUI
would retroactively break percentage pins. Tests that exercise the
override behavior pass a store explicitly or set the sentinel themselves.

Since 2026-10-10 the same isolation covers the reporting-package store
singleton (``get_report_state_store``): the hourly capacity analysis and
the runtime feature flags both read it through bound imports, so the
patch targets the module-level factory the cached accessor resolves at
call time and clears the lru_cache — otherwise a WebUI flag toggle on
the developer machine would flip template behavior inside unit tests.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_report_settings_store(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    from nanobot.agent.tools import magik_cube
    from nanobot.reporting import store as reporting_store

    monkeypatch.setattr(magik_cube, "_tenant_mappings_store", False)
    # The isolated store lives in its own system temp dir on purpose:
    # dropping state.db/-wal/-shm into ``tmp_path`` would add files to
    # every test's workspace and break directory-listing assertions
    # (filesystem/search tool tests count the files they create).
    with tempfile.TemporaryDirectory(prefix="nanobot-report-store-") as temp_dir:
        isolated = reporting_store.ReportStateStore(Path(temp_dir) / "state.db")
        reporting_store.get_report_state_store.cache_clear()
        monkeypatch.setattr(
            reporting_store,
            "create_report_state_store",
            lambda **_kwargs: isolated,
        )
        yield
        reporting_store.get_report_state_store.cache_clear()
