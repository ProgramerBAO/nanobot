"""Cross-suite test infrastructure."""

from __future__ import annotations

import os
import ssl
import sys
from collections.abc import Iterator

import certifi
import pytest


@pytest.fixture(scope="session", autouse=True)
def _use_windows_system_ca_for_default_http_clients() -> Iterator[None]:
    """Avoid reparsing certifi's CA bundle for every offline HTTP client.

    Loading certifi takes roughly 0.7 seconds per client on Windows. The test
    suite constructs hundreds of clients while mocking their I/O. System roots
    preserve certificate verification for accidental local requests; explicit
    ``cafile``, ``capath``, and ``cadata`` arguments still use the real loader.
    """
    if sys.platform != "win32":
        yield
        return

    original = ssl.create_default_context
    certifi_path = os.path.normcase(os.path.abspath(certifi.where()))

    def create_default_context(
        purpose: ssl.Purpose = ssl.Purpose.SERVER_AUTH,
        *,
        cafile: str | None = None,
        capath: str | None = None,
        cadata: str | bytes | None = None,
    ) -> ssl.SSLContext:
        requested_path = os.path.normcase(os.path.abspath(cafile)) if cafile else None
        if requested_path == certifi_path and capath is None and cadata is None:
            return original(purpose)
        return original(
            purpose,
            cafile=cafile,
            capath=capath,
            cadata=cadata,
        )

    ssl.create_default_context = create_default_context
    try:
        yield
    finally:
        ssl.create_default_context = original


@pytest.fixture(autouse=True)
def _isolated_tenant_mapping_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tenant-alias resolution away from the machine's live report store.

    ``effective_tenant_mappings`` resolves the process-configured report
    state store when no explicit store is passed, so a ``tenant_mappings``
    override saved through the WebUI alias page on this machine would shadow
    every config-level alias in tests (observed 2026-09-17: thirteen tests
    failed the moment the live acceptance run saved a real alias table).
    Pinning the process-local cache to ``False`` — the "resolution attempted
    and failed" state — makes the default path fall back to the configured
    mapping. Tests that exercise the store-override path pass their store
    explicitly and are unaffected.
    """

    from nanobot.agent.tools import magik_cube

    monkeypatch.setattr(magik_cube, "_tenant_mappings_store", False)
