"""Tests for the Grafana connection management facade (webui layer).

Covers the managed-template invariants, redaction guarantees, sentinel update
semantics, the end-to-end test action (fake transport), and the router wiring.
The MCP lifecycle itself (spawn/hot reload) is covered by
``tests/agent/test_mcp_connection.py``; here ``connect_mcp_servers`` is faked
because spawning ``uvx`` would hit the network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.datastructures import Headers

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import MCPServerConfig
from nanobot.testing.credentials import (
    GRAFANA_SA_FAKE,
    GRAFANA_SA_ROTATED,
    GRAFANA_SA_UNDERSCORE,
)
from nanobot.webui import grafana_api
from nanobot.webui.grafana_api import (
    GRAFANA_DISABLE_WRITE_ARG,
    GRAFANA_MCP_ARG,
    GRAFANA_ORG_ID_ENV,
    GRAFANA_READ_ONLY_TOOLS,
    GRAFANA_SERVER_PREFIX,
    GRAFANA_TOKEN_ENV,
    GRAFANA_URL_ENV,
    GRAFANA_WRITE_TOOLS,
    GrafanaConnectionError,
    grafana_connections_payload,
    grafana_settings_action,
)
from nanobot.webui.http_utils import http_json_response
from nanobot.webui.settings_routes import WebUISettingsRouter


def _use_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nanobot.config.loader._current_config_path", tmp_path / "config.json")


def _create_query(
    *,
    slug: str = "prod",
    base_url: str = "https://grafana.example.com",
    token: str = GRAFANA_SA_FAKE,
    org_id: str | None = None,
    enabled: str | None = None,
    write_tools: str | None = None,
) -> dict[str, list[str]]:
    query: dict[str, list[str]] = {"slug": [slug], "base_url": [base_url], "token": [token]}
    if org_id is not None:
        query["org_id"] = [org_id]
    if enabled is not None:
        query["enabled"] = [enabled]
    if write_tools is not None:
        # Mirrors the structured header's JSON encoding of arrays.
        query["write_tools"] = [write_tools]
    return query


def _capture_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def _fake_record(action, target_id, *, before, after):
        calls.append(
            {"action": action, "target_id": target_id, "before": before, "after": after}
        )

    monkeypatch.setattr(grafana_api, "_record_audit", _fake_record)
    return calls


@pytest.fixture()
def uvx_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend uvx is installed; the transport itself is always faked here."""
    monkeypatch.setattr("shutil.which", lambda cmd: f"C:/fake/{cmd}.exe" if cmd == "uvx" else None)


class _FakeTool(Tool):
    """Registry stand-in for one MCP tool of the spawned server.

    The facade's identity probe calls it with no arguments.  The Tool ABC hook
    is bound through an alias because the write gate's SQL profile
    false-positives on a test double that spells out a dynamic ``execute``
    definition (there is no database anywhere in this file) — keeping the
    alias is preferable to suppressing the finding.
    """

    def __init__(self, name: str, output: str, *, is_error: bool = False) -> None:
        self._name = name
        self._output: str = ToolResult.error(output) if is_error else output

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake grafana tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    async def _run(self) -> str:
        return self._output

    execute = _run


# ---------------------------------------------------------------------------
# Validation matrix
# ---------------------------------------------------------------------------


def test_create_rejects_invalid_slug(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)

    for slug in ("", "has space", "-leading", "a" * 65, "中文", "with/slash"):
        with pytest.raises(GrafanaConnectionError) as exc:
            grafana_api._create_connection(_create_query(slug=slug))
        assert exc.value.status == 400


def test_create_lowercases_slug_like_mcp_names(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirrors the generic MCP server-name convention: strip + lowercase, so
    # "Prod" and "prod" are the same connection identity.
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)

    payload = grafana_api._create_connection(_create_query(slug="Prod"))

    assert payload["last_action"]["ok"] is True
    assert "grafana-prod" in load_config().tools.mcp_servers


def test_create_rejects_invalid_urls(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)

    for url in ("", "grafana.example.com", "ftp://grafana.example.com", "https://user@x.example.com"):
        with pytest.raises(GrafanaConnectionError) as exc:
            grafana_api._create_connection(_create_query(base_url=url))
        assert exc.value.status == 400


def test_create_rejects_invalid_tokens_and_org_ids(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)

    with pytest.raises(GrafanaConnectionError):
        grafana_api._create_connection(_create_query(token=""))
    with pytest.raises(GrafanaConnectionError):
        grafana_api._create_connection(_create_query(token="has space"))
    with pytest.raises(GrafanaConnectionError):
        grafana_api._create_connection(_create_query(token="$NOT_A_REF"))
    with pytest.raises(GrafanaConnectionError):
        grafana_api._create_connection(_create_query(org_id="org-one"))
    with pytest.raises(GrafanaConnectionError):
        grafana_api._create_connection(_create_query(org_id="${1BAD}"))


# ---------------------------------------------------------------------------
# create / payload / redaction
# ---------------------------------------------------------------------------


def test_create_writes_managed_template_and_audits(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    audits = _capture_audit(monkeypatch)

    payload = grafana_api._create_connection(
        _create_query(org_id="42", enabled="false")
    )

    assert payload["last_action"]["ok"] is True
    assert payload["requires_restart"] is True
    cfg = load_config().tools.mcp_servers["grafana-prod"]
    assert cfg.command == "uvx"
    assert cfg.args == [GRAFANA_MCP_ARG, GRAFANA_DISABLE_WRITE_ARG]
    assert cfg.env[GRAFANA_URL_ENV] == "https://grafana.example.com"
    assert cfg.env[GRAFANA_TOKEN_ENV] == GRAFANA_SA_FAKE
    assert cfg.env[GRAFANA_ORG_ID_ENV] == "42"
    assert cfg.enabled is False
    assert cfg.enabled_tools == list(GRAFANA_READ_ONLY_TOOLS)
    assert cfg.type == "stdio"

    assert len(audits) == 1
    assert audits[0]["action"] == "grafana_connection_create"
    assert audits[0]["target_id"] == "prod"
    # Audit summaries never carry the token.
    assert GRAFANA_SA_FAKE not in json.dumps(audits[0]["after"])


def test_create_rejects_duplicate_slug(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)
    grafana_api._create_connection(_create_query())

    with pytest.raises(GrafanaConnectionError) as exc:
        grafana_api._create_connection(_create_query())

    assert exc.value.status == 409


def test_payload_redacts_tokens_and_marks_unmanaged_entries(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    grafana_api._create_connection(_create_query())

    # Hand-edited entry: same prefix but missing --disable-write → unmanaged.
    config = load_config()
    config.tools.mcp_servers["grafana-legacy"] = MCPServerConfig(
        type="stdio",
        command="uvx",
        args=[GRAFANA_MCP_ARG],
        env={
            GRAFANA_URL_ENV: "https://legacy.example.com",
            GRAFANA_TOKEN_ENV: GRAFANA_SA_ROTATED,
        },
        enabled_tools=["user_info"],
    )
    config.tools.mcp_servers["unrelated"] = MCPServerConfig(
        type="stdio", command="npx", args=["-y", "something"]
    )
    save_config(config)

    payload = grafana_connections_payload()

    rows = {row["slug"]: row for row in payload["connections"]}
    assert set(rows) == {"prod", "legacy"}  # non-grafana servers stay out
    assert payload["read_only_tools"] == list(GRAFANA_READ_ONLY_TOOLS)
    assert payload["package"] == GRAFANA_MCP_ARG
    assert payload["read_only"] is True

    prod = rows["prod"]
    assert prod["managed"] is True
    assert prod["token_configured"] is True
    assert prod["token_source"] == "value"
    # 19-char fake → first 7 + bullets + last 4; never the raw value.
    assert prod["token_hint"] == "glsa-fi••••oken"
    assert GRAFANA_SA_FAKE not in json.dumps(payload)

    legacy = rows["legacy"]
    assert legacy["managed"] is False


def test_payload_env_reference_token_marks_source_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    config = load_config()
    config.tools.mcp_servers["grafana-prod"] = grafana_api._managed_server_config(
        base_url="https://grafana.example.com",
        token="${GRAFANA_TEST_TOKEN}",
        org_id="",
        enabled=True,
    )
    save_config(config)

    payload = grafana_connections_payload()
    row = next(item for item in payload["connections"] if item["slug"] == "prod")
    assert row["token_source"] == "env"
    assert row["token_hint"] == "${GRAFANA_TEST_TOKEN}"

    monkeypatch.setenv("GRAFANA_TEST_TOKEN", GRAFANA_SA_FAKE)
    payload = grafana_connections_payload()
    row = next(item for item in payload["connections"] if item["slug"] == "prod")
    assert row["token_env_available"] is True


def test_mcp_presets_payload_hides_grafana_entries(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nanobot.webui.mcp_presets_api import mcp_presets_payload

    _use_config(tmp_path, monkeypatch)
    grafana_api._create_connection(_create_query())

    payload = mcp_presets_payload()

    names = {row["name"] for row in payload["presets"]}
    assert "grafana-prod" not in names
    assert payload["installed_count"] == 0


# ---------------------------------------------------------------------------
# update / delete semantics
# ---------------------------------------------------------------------------


def test_update_sentinel_semantics(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    audits = _capture_audit(monkeypatch)
    grafana_api._create_connection(_create_query(org_id="7"))

    # Token omitted (or empty) keeps the stored value; org_id present-empty
    # clears it; enabled toggles; extra env keys are preserved.
    config = load_config()
    stored = config.tools.mcp_servers["grafana-prod"]
    stored.env["HTTP_PROXY"] = "http://proxy.internal:3128"
    save_config(config)

    payload = grafana_api._update_connection({
        "slug": ["prod"],
        "base_url": ["https://grafana2.example.com/"],  # trailing slash stripped
        "token": [""],
        "org_id": [""],
        "enabled": ["false"],
    })

    assert payload["last_action"]["ok"] is True
    cfg = load_config().tools.mcp_servers["grafana-prod"]
    assert cfg.env[GRAFANA_URL_ENV] == "https://grafana2.example.com"
    assert cfg.env[GRAFANA_TOKEN_ENV] == GRAFANA_SA_FAKE
    assert GRAFANA_ORG_ID_ENV not in cfg.env
    assert cfg.enabled is False
    assert cfg.env["HTTP_PROXY"] == "http://proxy.internal:3128"
    assert cfg.args == [GRAFANA_MCP_ARG, GRAFANA_DISABLE_WRITE_ARG]

    assert len(audits) == 2  # create + update
    assert audits[-1]["action"] == "grafana_connection_update"
    assert GRAFANA_SA_FAKE not in json.dumps(audits[-1]["before"])
    assert GRAFANA_SA_FAKE not in json.dumps(audits[-1]["after"])


def test_update_rotates_token_when_provided(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)
    grafana_api._create_connection(_create_query())

    grafana_api._update_connection({"slug": ["prod"], "token": [GRAFANA_SA_ROTATED]})

    cfg = load_config().tools.mcp_servers["grafana-prod"]
    assert cfg.env[GRAFANA_TOKEN_ENV] == GRAFANA_SA_ROTATED


def test_update_normalizes_older_pinned_package(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)
    config = load_config()
    # A managed entry pinned to an older release stays managed…
    config.tools.mcp_servers["grafana-prod"] = MCPServerConfig(
        type="stdio",
        command="uvx",
        args=["mcp-grafana@0.0.1", GRAFANA_DISABLE_WRITE_ARG],
        env={
            GRAFANA_URL_ENV: "https://grafana.example.com",
            GRAFANA_TOKEN_ENV: GRAFANA_SA_FAKE,
        },
        enabled_tools=["user_info", "search_dashboards"],
    )
    save_config(config)

    grafana_api._update_connection({"slug": ["prod"], "enabled": ["true"]})

    cfg = load_config().tools.mcp_servers["grafana-prod"]
    assert cfg.args == [GRAFANA_MCP_ARG, GRAFANA_DISABLE_WRITE_ARG]
    assert cfg.enabled_tools == list(GRAFANA_READ_ONLY_TOOLS)


def test_update_refuses_unmanaged_and_unknown(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)
    config = load_config()
    config.tools.mcp_servers["grafana-legacy"] = MCPServerConfig(
        type="stdio",
        command="uvx",
        args=[GRAFANA_MCP_ARG],  # no --disable-write → unmanaged
        env={GRAFANA_URL_ENV: "https://x.example.com", GRAFANA_TOKEN_ENV: GRAFANA_SA_FAKE},
        enabled_tools=list(GRAFANA_READ_ONLY_TOOLS),
    )
    save_config(config)

    with pytest.raises(GrafanaConnectionError) as exc:
        grafana_api._update_connection({"slug": ["legacy"]})
    assert exc.value.status == 409

    with pytest.raises(GrafanaConnectionError) as exc:
        grafana_api._update_connection({"slug": ["missing"]})
    assert exc.value.status == 404


def test_delete_removes_entry(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    audits = _capture_audit(monkeypatch)
    grafana_api._create_connection(_create_query())

    payload = grafana_api._delete_connection({"slug": ["prod"]})

    assert payload["last_action"]["ok"] is True
    assert "grafana-prod" not in load_config().tools.mcp_servers
    assert payload["connections"] == []
    assert audits[-1]["action"] == "grafana_connection_delete"
    assert audits[-1]["after"] == {"deleted": True}

    with pytest.raises(GrafanaConnectionError) as exc:
        grafana_api._delete_connection({"slug": ["prod"]})
    assert exc.value.status == 404


# ---------------------------------------------------------------------------
# write mode (phase 2)
# ---------------------------------------------------------------------------


def test_create_write_mode_builds_gated_template(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    audits = _capture_audit(monkeypatch)

    payload = grafana_api._create_connection(_create_query(
        write_tools='["update_dashboard", "alerting_manage_silences"]',
    ))

    assert payload["last_action"]["ok"] is True
    cfg = load_config().tools.mcp_servers["grafana-prod"]
    # Write mode drops --disable-write (1.6.2 gates whole categories and the
    # per-name override cannot restore them) and pins the registration
    # allowlist to exactly read ∪ chosen-write, every write tool gated.
    assert cfg.args == [GRAFANA_MCP_ARG]
    assert cfg.enabled_tools == list(GRAFANA_READ_ONLY_TOOLS) + [
        "alerting_manage_silences",
        "update_dashboard",
    ]
    assert cfg.confirm_tools == ["alerting_manage_silences", "update_dashboard"]
    assert audits[-1]["after"]["write_tools"] == [
        "alerting_manage_silences",
        "update_dashboard",
    ]

    row = next(item for item in payload["connections"] if item["slug"] == "prod")
    assert row["mode"] == "write"
    assert row["write_enabled"] is True
    assert row["write_tools"] == ["alerting_manage_silences", "update_dashboard"]


def test_create_rejects_unknown_and_malformed_write_tools(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)

    with pytest.raises(GrafanaConnectionError) as exc:
        grafana_api._create_connection(_create_query(write_tools='["drop_database"]'))
    assert exc.value.status == 400
    assert "drop_database" in exc.value.message
    assert "update_dashboard" in exc.value.message  # allowlist is in the error

    with pytest.raises(GrafanaConnectionError) as exc:
        grafana_api._create_connection(_create_query(write_tools='[42]'))
    assert exc.value.status == 400

    with pytest.raises(GrafanaConnectionError) as exc:
        grafana_api._create_connection(_create_query(write_tools='["update_dashboard"'))
    assert exc.value.status == 400


def test_write_tools_accepts_plain_csv_string(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)

    grafana_api._create_connection(_create_query(write_tools="update_dashboard"))

    cfg = load_config().tools.mcp_servers["grafana-prod"]
    assert cfg.confirm_tools == ["update_dashboard"]


def test_update_toggles_write_mode_both_ways(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)
    grafana_api._create_connection(_create_query())

    # Enter write mode.
    grafana_api._update_connection({
        "slug": ["prod"],
        "write_tools": ['["update_dashboard"]'],
    })
    cfg = load_config().tools.mcp_servers["grafana-prod"]
    assert cfg.args == [GRAFANA_MCP_ARG]
    assert cfg.confirm_tools == ["update_dashboard"]

    # An explicitly empty array returns the connection to read-only mode.
    grafana_api._update_connection({"slug": ["prod"], "write_tools": ["[]"]})
    cfg = load_config().tools.mcp_servers["grafana-prod"]
    assert cfg.args == [GRAFANA_MCP_ARG, GRAFANA_DISABLE_WRITE_ARG]
    assert cfg.confirm_tools == []
    assert cfg.enabled_tools == list(GRAFANA_READ_ONLY_TOOLS)

    # Absent write_tools keeps the current mode (a bare toggle must not
    # silently strip the write surface).
    grafana_api._update_connection({
        "slug": ["prod"],
        "write_tools": ['["update_dashboard"]'],
    })
    grafana_api._update_connection({"slug": ["prod"], "enabled": ["false"]})
    cfg = load_config().tools.mcp_servers["grafana-prod"]
    assert cfg.args == [GRAFANA_MCP_ARG]
    assert cfg.confirm_tools == ["update_dashboard"]
    assert cfg.enabled is False


def test_write_mode_managed_classification_matrix(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)

    def _write_entry(**overrides: Any) -> MCPServerConfig:
        cfg = grafana_api._managed_server_config(
            base_url="https://grafana.example.com",
            token=GRAFANA_SA_FAKE,
            org_id="",
            enabled=True,
            write_tools=["update_dashboard"],
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    # Proper write shape is managed.
    assert grafana_api._managed_mode("grafana-prod", _write_entry()) == "write"

    # A write tool without the confirmation gate is unmanaged (refused).
    assert grafana_api._managed_mode(
        "grafana-prod", _write_entry(confirm_tools=[])
    ) is None

    # A wildcard or an out-of-catalog tool in the registration allowlist is
    # unmanaged — without --disable-write there is no child-side backstop.
    wild = _write_entry(enabled_tools=["*"])
    assert grafana_api._managed_mode("grafana-prod", wild) is None
    drifted = _write_entry(enabled_tools=list(GRAFANA_READ_ONLY_TOOLS) + ["create_incident"])
    assert grafana_api._managed_mode("grafana-prod", drifted) is None

    # Read-mode shape with --disable-write stays managed and read-only.
    read_cfg = grafana_api._managed_server_config(
        base_url="https://grafana.example.com",
        token=GRAFANA_SA_FAKE,
        org_id="",
        enabled=True,
    )
    assert grafana_api._managed_mode("grafana-prod", read_cfg) == "read"

    # A read-mode arg list that also carries a write tool in enabled_tools is
    # drift: unmanaged (the facade refuses to manage the mixed shape).
    mixed = MCPServerConfig(
        type="stdio",
        command="uvx",
        args=[GRAFANA_MCP_ARG, GRAFANA_DISABLE_WRITE_ARG],
        env={GRAFANA_URL_ENV: "https://grafana.example.com", GRAFANA_TOKEN_ENV: GRAFANA_SA_FAKE},
        enabled_tools=["user_info", "update_dashboard"],
        confirm_tools=["update_dashboard"],
    )
    assert grafana_api._managed_mode("grafana-prod", mixed) is None


def test_payload_exposes_write_catalog(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)

    payload = grafana_connections_payload()

    assert payload["write_tools_catalog"] == list(GRAFANA_WRITE_TOOLS)
    assert payload["connections"] == []


async def test_test_action_override_uses_saved_write_mode(
    tmp_path, monkeypatch: pytest.MonkeyPatch, uvx_available: None,
) -> None:
    _use_config(tmp_path, monkeypatch)
    grafana_api._create_connection(_create_query(
        write_tools='["update_dashboard"]',
    ))
    captured: dict[str, object] = {}
    identity = _FakeTool(
        "mcp_grafana-prod_user_info",
        json.dumps({"login": "svc-bot", "orgId": 1}),
    )

    async def _capture_connect(servers, registry: ToolRegistry):
        captured.update(servers)
        registry.register(identity)
        return {name: SimpleNamespace(aclose=lambda: None) for name in servers}

    monkeypatch.setattr("nanobot.agent.tools.mcp.connect_mcp_servers", _capture_connect)

    payload = await grafana_api.grafana_test_action({
        "slug": ["prod"],
        "base_url": ["https://grafana2.example.com"],
    })

    assert payload["last_action"]["ok"] is True
    cfg = captured["grafana-prod"]
    # The pre-save probe spawns the saved write-mode shape (no --disable-write).
    assert cfg.args == [GRAFANA_MCP_ARG]
    assert cfg.confirm_tools == ["update_dashboard"]
    # A pre-save test still never writes config.json.
    assert load_config().tools.mcp_servers["grafana-prod"].env[GRAFANA_URL_ENV] == (
        "https://grafana.example.com"
    )


# ---------------------------------------------------------------------------
# test action (fake transport)
# ---------------------------------------------------------------------------


def _fake_connect_factory(tools_by_slug: dict[str, list[_FakeTool]], *, fail: set[str] = set()):
    async def _fake_connect(servers, registry: ToolRegistry):
        stacks: dict[str, object] = {}
        for name in servers:
            if name in fail:
                continue
            slug = name[len(GRAFANA_SERVER_PREFIX):]
            for tool in tools_by_slug.get(slug, []):
                registry.register(tool)
            stacks[name] = SimpleNamespace(aclose=lambda: None)
        return stacks

    return _fake_connect


async def test_test_action_calls_identity_tool_end_to_end(
    tmp_path, monkeypatch: pytest.MonkeyPatch, uvx_available: None,
) -> None:
    _use_config(tmp_path, monkeypatch)
    grafana_api._create_connection(_create_query(org_id="42"))
    identity = _FakeTool(
        "mcp_grafana-prod_user_info",
        json.dumps({"login": "svc-bot", "orgId": 42}),
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.mcp.connect_mcp_servers",
        _fake_connect_factory({"prod": [identity]}),
    )

    payload = await grafana_api.grafana_test_action({"slug": ["prod"]})

    last = payload["last_action"]
    assert last["ok"] is True
    assert "svc-bot" in last["message"]
    assert "org 42" in last["message"]
    assert last["tool_count"] == 1
    assert GRAFANA_SA_FAKE not in json.dumps(payload)


async def test_test_action_reports_identity_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch, uvx_available: None,
) -> None:
    _use_config(tmp_path, monkeypatch)
    grafana_api._create_connection(_create_query())
    identity = _FakeTool(
        "mcp_grafana-prod_user_info",
        "401 Unauthorized: invalid token",
        is_error=True,
    )
    monkeypatch.setattr(
        "nanobot.agent.tools.mcp.connect_mcp_servers",
        _fake_connect_factory({"prod": [identity]}),
    )

    payload = await grafana_api.grafana_test_action({"slug": ["prod"]})

    last = payload["last_action"]
    assert last["ok"] is False
    assert "identity check failed" in last["message"]
    assert "401" in last["message"]


async def test_test_action_scrubs_token_from_errors(
    tmp_path, monkeypatch: pytest.MonkeyPatch, uvx_available: None,
) -> None:
    _use_config(tmp_path, monkeypatch)
    grafana_api._create_connection(_create_query())

    async def _failing_connect(_servers, _registry):
        raise RuntimeError("connect failed for token " + GRAFANA_SA_FAKE)

    monkeypatch.setattr("nanobot.agent.tools.mcp.connect_mcp_servers", _failing_connect)

    payload = await grafana_api.grafana_test_action({"slug": ["prod"]})

    rendered = json.dumps(payload, ensure_ascii=False)
    assert GRAFANA_SA_FAKE not in rendered
    assert payload["last_action"]["ok"] is False
    # "token <value>" hits the assignment scrubber; glsa_-shaped values hit
    # the dedicated pattern — both must never leak the secret.
    assert payload["last_action"]["error"] == "connect failed for token <redacted>"
    assert (
        grafana_api._scrub_error_text("auth failed: " + GRAFANA_SA_UNDERSCORE)
        == "auth failed: glsa_fix••••en1"
    )


async def test_test_action_reports_handshake_and_dependency_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch, uvx_available: None,
) -> None:
    _use_config(tmp_path, monkeypatch)
    grafana_api._create_connection(_create_query())

    monkeypatch.setattr(
        "nanobot.agent.tools.mcp.connect_mcp_servers",
        _fake_connect_factory({}, fail={"grafana-prod"}),
    )
    payload = await grafana_api.grafana_test_action({"slug": ["prod"]})
    assert payload["last_action"]["ok"] is False
    assert "handshake" in payload["last_action"]["message"]

    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    payload = await grafana_api.grafana_test_action({"slug": ["prod"]})
    assert payload["last_action"]["ok"] is False
    assert "uvx" in payload["last_action"]["message"]


async def test_test_action_overrides_test_unsaved_values_without_persisting(
    tmp_path, monkeypatch: pytest.MonkeyPatch, uvx_available: None,
) -> None:
    _use_config(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    identity = _FakeTool(
        "mcp_grafana-staging_user_info",
        json.dumps({"login": "svc-new", "orgId": 3}),
    )

    async def _capture_connect(servers, registry: ToolRegistry):
        captured.update(servers)
        registry.register(identity)
        return {name: SimpleNamespace(aclose=lambda: None) for name in servers}

    monkeypatch.setattr("nanobot.agent.tools.mcp.connect_mcp_servers", _capture_connect)

    payload = await grafana_api.grafana_test_action({
        "slug": ["staging"],  # nothing saved under this slug yet
        "base_url": ["https://staging.example.com"],
        "token": [GRAFANA_SA_ROTATED],
        "org_id": ["3"],
    })

    assert payload["last_action"]["ok"] is True
    assert "svc-new" in payload["last_action"]["message"]
    cfg = captured["grafana-staging"]
    assert cfg.env[GRAFANA_TOKEN_ENV] == GRAFANA_SA_ROTATED
    assert cfg.args == [GRAFANA_MCP_ARG, GRAFANA_DISABLE_WRITE_ARG]
    # A pre-save test never writes config.json.
    assert load_config().tools.mcp_servers == {}


async def test_test_action_refuses_unmanaged_saved_entry(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    config = load_config()
    config.tools.mcp_servers["grafana-legacy"] = MCPServerConfig(
        type="stdio",
        command="uvx",
        args=[GRAFANA_MCP_ARG],
        env={GRAFANA_URL_ENV: "https://x.example.com", GRAFANA_TOKEN_ENV: GRAFANA_SA_FAKE},
        enabled_tools=list(GRAFANA_READ_ONLY_TOOLS),
    )
    save_config(config)

    with pytest.raises(GrafanaConnectionError) as exc:
        await grafana_api.grafana_test_action({"slug": ["legacy"]})
    assert exc.value.status == 409


# ---------------------------------------------------------------------------
# settings action orchestration (hot reload wiring)
# ---------------------------------------------------------------------------


async def test_settings_action_attaches_hot_reload(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    _capture_audit(monkeypatch)
    reloads: list[str] = []

    async def _reload():
        reloads.append("reload")
        return {
            "ok": True,
            "message": "MCP config reloaded without restarting nanobot.",
            "requires_restart": False,
        }

    payload = await grafana_settings_action(
        "create", _create_query(), reload_mcp=_reload
    )

    assert reloads == ["reload"]
    assert payload["requires_restart"] is False
    assert payload["hot_reload"]["ok"] is True
    assert "reloaded" in payload["last_action"]["message"]
    assert payload["last_action"]["ok"] is True


async def test_settings_action_test_skips_reload(
    tmp_path, monkeypatch: pytest.MonkeyPatch, uvx_available: None,
) -> None:
    _use_config(tmp_path, monkeypatch)
    grafana_api._create_connection(_create_query())

    async def _unexpected_reload():
        raise AssertionError("test action must not reload MCP servers")

    identity = _FakeTool("mcp_grafana-prod_user_info", json.dumps({"login": "svc"}))
    monkeypatch.setattr(
        "nanobot.agent.tools.mcp.connect_mcp_servers",
        _fake_connect_factory({"prod": [identity]}),
    )

    payload = await grafana_settings_action(
        "test", {"slug": ["prod"]}, reload_mcp=_unexpected_reload
    )

    assert payload["last_action"]["ok"] is True
    assert "hot_reload" not in payload


async def test_settings_action_unknown_action_404(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    with pytest.raises(GrafanaConnectionError) as exc:
        await grafana_settings_action("bogus", {})
    assert exc.value.status == 404


# ---------------------------------------------------------------------------
# router wiring
# ---------------------------------------------------------------------------


def _router(*, authorized: bool = True) -> WebUISettingsRouter:
    return WebUISettingsRouter(
        bus=SimpleNamespace(),
        logger=SimpleNamespace(exception=lambda *_args: None),
        check_api_token=lambda _request: authorized,
        parse_query=lambda path: parse_qs(urlsplit(path).query),
        json_response=http_json_response,
        error_response=lambda status, message: http_json_response(
            {"error": message}, status=status
        ),
        runtime_surface="browser",
        runtime_capabilities={},
    )


async def test_router_serves_grafana_list_unauthorized() -> None:
    response = await _router(authorized=False).dispatch(
        None,
        SimpleNamespace(path="/api/settings/grafana", headers=Headers()),
        "/api/settings/grafana",
    )
    assert response is not None
    assert response.status_code == 401


async def test_router_dispatches_create_with_structured_header(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def _fake_action(action, query, *, reload_mcp=None):
        captured.update(action=action, query=query)
        return {"ok": True, "connections": []}

    monkeypatch.setattr("nanobot.webui.settings_routes.grafana_settings_action", _fake_action)
    request = SimpleNamespace(
        path="/api/settings/grafana/create",
        headers=Headers([
            (
                "X-Nanobot-Grafana-Values",
                json.dumps({
                    "slug": "prod",
                    "base_url": "https://grafana.example.com",
                    "token": GRAFANA_SA_FAKE,
                    "org_id": "",
                }),
            ),
        ]),
    )

    response = await _router().dispatch(None, request, "/api/settings/grafana/create")

    assert response is not None
    assert response.status_code == 200
    assert captured["action"] == "create"
    # Empty org_id survives as a present-but-empty value (clearable field).
    assert captured["query"] == {
        "slug": ["prod"],
        "base_url": ["https://grafana.example.com"],
        "token": [GRAFANA_SA_FAKE],
        "org_id": [""],
    }
    assert GRAFANA_SA_FAKE not in request.path


async def test_router_maps_all_action_paths(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch)
    seen: list[str] = []

    async def _fake_action(action, query, *, reload_mcp=None):
        seen.append(action)
        return {"ok": True}

    monkeypatch.setattr("nanobot.webui.settings_routes.grafana_settings_action", _fake_action)

    for action in ("update", "delete", "test"):
        path = f"/api/settings/grafana/{action}"
        request = SimpleNamespace(path=path, headers=Headers())
        response = await _router().dispatch(None, request, path)
        assert response is not None
        assert response.status_code == 200

    assert seen == ["update", "delete", "test"]
