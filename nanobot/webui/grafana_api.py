"""Grafana connection management for the WebUI settings surface.

Each Grafana platform is stored as one managed MCP server entry in config.json
(``tools.mcpServers["grafana-<slug>"]``): a stdio ``uvx mcp-grafana@<pinned>``
child process started with ``--disable-write`` and a curated read-only tool
allowlist.  Connection lifecycle (spawn, registration, hot reload, shutdown) is
delegated entirely to the shared MCP pipeline in
:mod:`nanobot.agent.tools.mcp`; this module only owns the Grafana facade
invariants:

- the server entry always matches the managed template (pinned package,
  ``--disable-write``), so writes are disabled inside the child process;
- ``enabled_tools`` is always a subset of the curated read-only allowlist;
- tokens never leave this module unredacted (API responses return a hint such
  as ``glsa_ab••••3f2d``, audit summaries and error messages are scrubbed).

Phase 1 is read-only by design.  Write operations, per-connection Editor
tokens, and chat-side RBAC are deliberate non-goals; the reporting-layer
connector (:mod:`nanobot.reporting.grafana`) stays a separate, pre-approved
PromQL surface and is intentionally untouched by this facade.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import urllib.parse
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Literal

from loguru import logger

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import (
    load_config,
    resolve_config_env_vars,
    resolve_env_refs,
    save_config,
)
from nanobot.config.schema import MCPServerConfig
from nanobot.reporting import get_report_state_store
from nanobot.webui.http_utils import query_first
from nanobot.webui.mcp_presets_api import attach_mcp_hot_reload_result

QueryParams = dict[str, list[str]]

# Managed-entry contract. ``grafana-<slug>`` entries are owned by the Grafana
# settings page and hidden from the generic MCP management list (single entry
# point), so nobody can bypass these invariants through the custom-server form.
GRAFANA_SERVER_PREFIX = "grafana-"
GRAFANA_MCP_COMMAND = "uvx"
GRAFANA_MCP_PACKAGE = "mcp-grafana"
# Pinned deliberately: bumping this requires re-verifying the read-only tool
# list below against the new release (tool names are stable but not contractual).
GRAFANA_MCP_VERSION = "1.6.2"
GRAFANA_MCP_ARG = f"{GRAFANA_MCP_PACKAGE}@{GRAFANA_MCP_VERSION}"
GRAFANA_DISABLE_WRITE_ARG = "--disable-write"

# Environment consumed by the mcp-grafana child process (official names).
GRAFANA_URL_ENV = "GRAFANA_URL"
GRAFANA_TOKEN_ENV = "GRAFANA_SERVICE_ACCOUNT_TOKEN"
GRAFANA_ORG_ID_ENV = "GRAFANA_ORG_ID"
_GRAFANA_KNOWN_ENV_KEYS = frozenset({GRAFANA_URL_ENV, GRAFANA_TOKEN_ENV, GRAFANA_ORG_ID_ENV})

# Curated tool surface (verified against the pinned release). Read tools
# below survive --disable-write inside the child process; the write list is
# the phase-2 management surface. The --disable-write flag removes every
# write tool on the child-process side, and the nanobot-side enabled_tools
# allowlist is the second gate — a write tool can never be registered unless
# the facade explicitly opts the connection into write mode.
GRAFANA_READ_ONLY_TOOLS: tuple[str, ...] = (
    "user_info",
    "search_dashboards",
    "get_dashboard_by_uid",
    "get_dashboard_summary",
    "get_dashboard_panel_queries",
    "list_datasources",
    "get_datasource",
    "query_prometheus",
    "list_prometheus_label_names",
    "list_prometheus_label_values",
    "generate_deeplink",
    # Phase-3 additions (all read-only under --disable-write at 1.6.2):
    "query_prometheus_histogram",
    "list_prometheus_metric_names",
    "list_prometheus_metric_metadata",
    "check_datasources_health",
    "get_annotations",
    "get_annotation_tags",
    "list_dashboard_versions",
    "get_dashboard_property",
    "search_folders",
    "get_folder",
    "get_doc",
    "search_docs",
    # Panel PNG rendering; the image content block flows through the existing
    # MCP image-artifact pipeline into chat images. Requires the Grafana
    # image renderer (plugin or Cloud) on the platform side.
    "get_panel_image",
    # Loki (LogQL queries cannot write; the child caps log volume by default).
    "query_loki_logs",
    "list_loki_label_names",
    "list_loki_label_values",
    "query_loki_stats",
    "query_loki_patterns",
    "analyze_loki_labels",
)

# Curated write surface (phase 2). At mcp-grafana 1.6.2 the --disable-write
# flag gates whole tool categories and --enable-write-tools CANNOT restore
# them (verified in the v1.6.2 source: only sift/raw-SQL route through the
# per-name override), so a write-mode connection necessarily drops
# --disable-write. The write boundary is therefore carried entirely by the
# nanobot side: enabled_tools is pinned to exactly read ∪ chosen-write, and
# every chosen write tool is wrapped in the mandatory chat confirmation
# gate (confirm_tools) with per-operation audit. Incidents, OnCall, Sift and
# raw-SQL tools are deliberately excluded from the curated surface.
GRAFANA_WRITE_TOOLS: tuple[str, ...] = (
    "update_dashboard",
    "create_folder",
    "alerting_manage_rules",
    "alerting_manage_silences",
    "alerting_manage_routing",
    "create_annotation",
    "update_annotation",
    "delete_annotation",
    "create_snapshot",
    "delete_snapshot",
)
GRAFANA_IDENTITY_TOOL = "user_info"

GRAFANA_TOOL_TIMEOUT = 30  # per-call timeout for tools of a managed connection
GRAFANA_TEST_TIMEOUT = 30  # whole test budget; a cold uvx cache downloads the package first

_GRAFANA_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
# Error-text scrubbers: mcp-grafana failures must never echo a token back.
_GLSA_SCRUB_RE = re.compile(r"(glsa_[A-Za-z0-9_\-]{3})[A-Za-z0-9_\-]+([A-Za-z0-9_\-]{3})")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"((?:token|secret|password|authorization|bearer)(?:[=:\s]+))[^\s,;&'\"]+",
    re.IGNORECASE,
)
_MAX_URL_LENGTH = 512
_MAX_TOKEN_LENGTH = 512
_MAX_ORG_ID_LENGTH = 32

GrafanaReload = Callable[[], Awaitable[dict[str, Any]]]


class GrafanaConnectionError(Exception):
    """WebUI-facing Grafana connection error."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _server_name(slug: str) -> str:
    return f"{GRAFANA_SERVER_PREFIX}{slug}"


def _validated_slug(raw: str | None) -> str:
    slug = (raw or "").strip().lower()
    if not slug or _GRAFANA_SLUG_RE.match(slug) is None:
        raise GrafanaConnectionError(
            "Connection ID must start with a letter or digit and use only "
            "lowercase letters, digits, '-' and '_' (max 64 chars)"
        )
    return slug


def _validated_base_url(raw: str | None) -> str:
    url = (raw or "").strip()
    if not url:
        raise GrafanaConnectionError("Grafana URL is required")
    if len(url) > _MAX_URL_LENGTH or any(ord(char) < 32 for char in url):
        raise GrafanaConnectionError("Grafana URL is not a valid URL")
    # Mirror the reporting connector's rule (grafana.py): absolute http(s),
    # host present, no userinfo. Internal hosts are allowed by design — the
    # URL is the user's own platform and requests are made by the child
    # process, not by nanobot's HTTP stack.
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username:
        raise GrafanaConnectionError(
            "Grafana URL must be an absolute http(s) URL without userinfo"
        )
    return url.rstrip("/")


def _validated_token(raw: str | None) -> str:
    token = (raw or "").strip()
    if not token:
        raise GrafanaConnectionError("Grafana service account token is required")
    if len(token) > _MAX_TOKEN_LENGTH:
        raise GrafanaConnectionError("Grafana token is too long")
    if any(char.isspace() for char in token):
        raise GrafanaConnectionError("Grafana token must not contain whitespace")
    if not _ENV_REF_RE.match(token) and token.startswith("$"):
        raise GrafanaConnectionError(
            "Grafana token must be a literal value or a ${VAR} environment reference"
        )
    return token


def _validated_org_id(raw: str | None) -> str:
    org_id = (raw or "").strip()
    if not org_id:
        return ""
    if len(org_id) > _MAX_ORG_ID_LENGTH:
        raise GrafanaConnectionError("Grafana org ID is too long")
    # GRAFANA_ORG_ID is a numeric organization ID in mcp-grafana; ${VAR} is
    # accepted so deployments can keep the value out of config.json.
    if not org_id.isdigit() and not _ENV_REF_RE.match(org_id):
        raise GrafanaConnectionError("Grafana org ID must be numeric (or a ${VAR} reference)")
    return org_id


def _optional_bool(query: QueryParams, key: str) -> bool | None:
    raw = query_first(query, key)
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise GrafanaConnectionError(f"'{key}' must be true or false")


def _scrub_error_text(text: str) -> str:
    scrubbed = _GLSA_SCRUB_RE.sub(r"\1••••\2", text.strip())
    scrubbed = _SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", scrubbed)
    return scrubbed[:400] if scrubbed else "Connection failed."


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _package_from_args(cfg: MCPServerConfig) -> str:
    for arg in cfg.args:
        if arg.startswith(f"{GRAFANA_MCP_PACKAGE}@"):
            return arg
    return ""


def _managed_mode(name: str, cfg: MCPServerConfig) -> Literal["read", "write"] | None:
    """Classify an entry against the facade's managed templates.

    Returns "read" (phase-1 read-only shape), "write" (phase-2 write-mode
    shape), or None for entries the facade refuses to manage.  The package
    version may drift from the current pin (older connections stay managed
    and are normalized on the next update); everything else must match
    exactly.  Extra env keys are tolerated (and preserved on update) because
    the Windows subprocess env is whitelisted, so entries like an HTTP proxy
    for the child process are legitimate.

    Write mode has no --disable-write backstop inside the child (see
    GRAFANA_WRITE_TOOLS), so the managed check is strict there: enabled_tools
    must stay within read ∪ curated-write, and every enabled write tool must
    carry the confirmation gate.
    """

    if not name.startswith(GRAFANA_SERVER_PREFIX):
        return None
    if cfg.type not in (None, "stdio") or cfg.command != GRAFANA_MCP_COMMAND:
        return None
    if not _package_from_args(cfg):
        return None
    if not cfg.env.get(GRAFANA_URL_ENV) or not cfg.env.get(GRAFANA_TOKEN_ENV):
        return None
    tools = set(cfg.enabled_tools)
    allowed = set(GRAFANA_READ_ONLY_TOOLS) | set(GRAFANA_WRITE_TOOLS)
    if not tools or "*" in tools or not tools <= allowed:
        return None
    write_enabled = tools & set(GRAFANA_WRITE_TOOLS)
    confirms = set(getattr(cfg, "confirm_tools", None) or [])
    if write_enabled:
        if len(cfg.args) != 1:
            return None
        if confirms != write_enabled:
            return None
        return "write"
    if len(cfg.args) != 2 or cfg.args[1] != GRAFANA_DISABLE_WRITE_ARG:
        return None
    return "read"


def _is_managed_server(name: str, cfg: MCPServerConfig) -> bool:
    return _managed_mode(name, cfg) is not None


def _token_hint(token: str) -> dict[str, Any]:
    """Redacted token description; never returns the raw value."""

    if not token:
        return {"hint": None, "source": "none", "env_available": None}
    if _ENV_REF_RE.match(token):
        return {
            "hint": token,  # shows the variable name only
            "source": "env",
            "env_available": None,
        }
    if len(token) < 16:
        return {"hint": "••••", "source": "value", "env_available": None}
    return {
        "hint": f"{token[:7]}••••{token[-4:]}",
        "source": "value",
        "env_available": None,
    }


def _connection_row(name: str, cfg: MCPServerConfig) -> dict[str, Any]:
    slug = name[len(GRAFANA_SERVER_PREFIX):]
    token = cfg.env.get(GRAFANA_TOKEN_ENV, "")
    hint = _token_hint(token)
    if hint["source"] == "env":
        resolved = resolve_env_refs(token)
        hint["env_available"] = bool(resolved) and resolved != token
    tools = list(cfg.enabled_tools)
    mode = _managed_mode(name, cfg)
    write_tools = sorted(set(tools) & set(GRAFANA_WRITE_TOOLS))
    return {
        "slug": slug,
        "server_name": name,
        "base_url": cfg.env.get(GRAFANA_URL_ENV, ""),
        "org_id": cfg.env.get(GRAFANA_ORG_ID_ENV, ""),
        "enabled": bool(getattr(cfg, "enabled", True)),
        "managed": mode is not None,
        "mode": mode,
        "write_tools": write_tools,
        "write_enabled": bool(write_tools) and mode == "write",
        "package": _package_from_args(cfg),
        "token_hint": hint["hint"],
        "token_source": hint["source"],
        "token_env_available": hint["env_available"],
        "token_configured": bool(token),
        "tools": tools,
        "tool_count": len(tools),
    }


def grafana_connections_payload(*, last_action: dict[str, Any] | None = None) -> dict[str, Any]:
    config = load_config()
    rows = [
        _connection_row(name, cfg)
        for name, cfg in sorted(config.tools.mcp_servers.items())
        if name.startswith(GRAFANA_SERVER_PREFIX)
    ]
    payload: dict[str, Any] = {
        "connections": rows,
        "read_only_tools": list(GRAFANA_READ_ONLY_TOOLS),
        "write_tools_catalog": list(GRAFANA_WRITE_TOOLS),
        "package": GRAFANA_MCP_ARG,
        "read_only": True,  # phase-level statement: the default mode stays read-only
        "uvx_available": shutil.which(GRAFANA_MCP_COMMAND) is not None,
    }
    if last_action is not None:
        payload["last_action"] = last_action
    return payload


def _validated_write_tools(raw: Any) -> list[str]:
    """Parse and bound the write-tool selection (empty = read-only mode)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            # The structured header JSON-encodes arrays before they land in
            # the query; parse that back instead of comma-splitting.
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise GrafanaConnectionError(
                    "write_tools must be a JSON array of tool names"
                ) from exc
        else:
            raw = [item.strip() for item in text.split(",") if item.strip()]
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise GrafanaConnectionError("write_tools must be a JSON array of tool names")
    selected = {item.strip() for item in raw if item.strip()}
    unknown = sorted(selected - set(GRAFANA_WRITE_TOOLS))
    if unknown:
        raise GrafanaConnectionError(
            "unknown Grafana write tools: "
            + ", ".join(unknown)
            + f"; allowed: {', '.join(GRAFANA_WRITE_TOOLS)}"
        )
    return sorted(selected)


def _managed_server_config(
    *,
    base_url: str,
    token: str,
    org_id: str,
    enabled: bool,
    write_tools: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> MCPServerConfig:
    env: dict[str, str] = {GRAFANA_URL_ENV: base_url, GRAFANA_TOKEN_ENV: token}
    if org_id:
        env[GRAFANA_ORG_ID_ENV] = org_id
    for key, value in (extra_env or {}).items():
        if key not in _GRAFANA_KNOWN_ENV_KEYS:
            env[key] = value
    writes = sorted(set(write_tools or []))
    if writes:
        # Write mode (phase 2): --disable-write must be dropped because it
        # removes whole tool categories at 1.6.2 (see GRAFANA_WRITE_TOOLS).
        # The nanobot-side allowlist + mandatory confirmation gate carry the
        # write boundary instead.
        args = [GRAFANA_MCP_ARG]
        enabled_tools = list(GRAFANA_READ_ONLY_TOOLS) + writes
        confirm_tools = list(writes)
    else:
        args = [GRAFANA_MCP_ARG, GRAFANA_DISABLE_WRITE_ARG]
        enabled_tools = list(GRAFANA_READ_ONLY_TOOLS)
        confirm_tools = []
    return MCPServerConfig(
        type="stdio",
        command=GRAFANA_MCP_COMMAND,
        args=args,
        env=env,
        tool_timeout=GRAFANA_TOOL_TIMEOUT,
        enabled_tools=enabled_tools,
        confirm_tools=confirm_tools,
        enabled=enabled,
    )


def _audit_summary(cfg: MCPServerConfig | None) -> dict[str, Any]:
    """Bounded audit view of a managed entry — never includes the token."""

    if cfg is None:
        return {}
    return {
        "base_url": cfg.env.get(GRAFANA_URL_ENV, ""),
        "org_id": cfg.env.get(GRAFANA_ORG_ID_ENV, ""),
        "enabled": bool(getattr(cfg, "enabled", True)),
        "package": _package_from_args(cfg),
        "tool_count": len(cfg.enabled_tools),
        "write_tools": sorted(set(cfg.enabled_tools) & set(GRAFANA_WRITE_TOOLS)),
    }


def _record_audit(
    action: str,
    target_id: str,
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    """Best-effort audit row in the report settings store.

    Grafana connection CRUD must not fail because the report state backend is
    unavailable (different subsystem), so audit failures are logged, not
    raised.  Summaries are pre-scrubbed by the caller via _audit_summary.
    """

    try:
        config = load_config()
        store = get_report_state_store(
            config.tools.reporting.state_backend,
            config.tools.reporting.postgres_dsn_env,
        )
        store.record_admin_audit(
            action=action,
            target_type="grafana_connection",
            target_id=target_id,
            before_summary=before,
            after_summary=after,
            updated_by="webui_admin",
        )
    except Exception:
        logger.warning("Grafana connection audit '{}' could not be recorded", action)


def _existing_server(config: Any, name: str, *, missing_status: int = 404) -> MCPServerConfig:
    cfg = config.tools.mcp_servers.get(name)
    if cfg is None:
        raise GrafanaConnectionError("unknown Grafana connection", status=missing_status)
    return cfg


def _require_managed(name: str, cfg: MCPServerConfig) -> None:
    if not _is_managed_server(name, cfg):
        raise GrafanaConnectionError(
            "this grafana-* entry was not created by the Grafana page "
            "(hand-edited template); delete and re-create it here",
            status=409,
        )


def _create_connection(query: QueryParams) -> dict[str, Any]:
    slug = _validated_slug(query_first(query, "slug"))
    base_url = _validated_base_url(query_first(query, "base_url"))
    token = _validated_token(query_first(query, "token"))
    org_id = _validated_org_id(query_first(query, "org_id"))
    write_tools = _validated_write_tools(query_first(query, "write_tools"))
    enabled = _optional_bool(query, "enabled")
    name = _server_name(slug)

    config = load_config()
    if name in config.tools.mcp_servers:
        raise GrafanaConnectionError(f"Grafana connection '{slug}' already exists", status=409)
    cfg = _managed_server_config(
        base_url=base_url,
        token=token,
        org_id=org_id,
        write_tools=write_tools,
        enabled=enabled if enabled is not None else True,
    )
    config.tools.mcp_servers[name] = cfg
    save_config(config)
    _record_audit(
        "grafana_connection_create",
        slug,
        before={},
        after=_audit_summary(cfg),
    )
    payload = grafana_connections_payload(last_action={
        "ok": True,
        "action": "create",
        "slug": slug,
        "message": f"Added Grafana connection '{slug}'.",
    })
    payload["requires_restart"] = True
    return payload


def _update_connection(query: QueryParams) -> dict[str, Any]:
    slug = _validated_slug(query_first(query, "slug"))
    name = _server_name(slug)

    config = load_config()
    cfg = _existing_server(config, name)
    _require_managed(name, cfg)

    # Field semantics: base_url/token are "absent or empty = keep" (the
    # editor omits the token unless the user typed a new one); org_id is
    # "present (even empty) = overwrite" so the field can be cleared;
    # write_tools likewise overwrites when present (an empty array returns
    # the connection to read-only mode); enabled is absent = keep.
    base_url_raw = (query_first(query, "base_url") or "").strip()
    base_url = _validated_base_url(base_url_raw) if base_url_raw else cfg.env.get(GRAFANA_URL_ENV, "")

    token_raw = (query_first(query, "token") or "").strip()
    if token_raw:
        token = _validated_token(token_raw)
    else:
        token = cfg.env.get(GRAFANA_TOKEN_ENV, "")
        if not token:
            raise GrafanaConnectionError(
                "this connection has no stored token; provide one to save it"
            )

    if "org_id" in query:
        org_id = _validated_org_id(query_first(query, "org_id"))
    else:
        org_id = cfg.env.get(GRAFANA_ORG_ID_ENV, "")

    if "write_tools" in query:
        write_tools = _validated_write_tools(query_first(query, "write_tools"))
    else:
        write_tools = sorted(
            set(cfg.enabled_tools) & set(GRAFANA_WRITE_TOOLS)
        )

    enabled = _optional_bool(query, "enabled")
    extra_env = {k: v for k, v in cfg.env.items() if k not in _GRAFANA_KNOWN_ENV_KEYS}
    new_cfg = _managed_server_config(
        base_url=base_url,
        token=token,
        org_id=org_id,
        write_tools=write_tools,
        enabled=enabled if enabled is not None else bool(getattr(cfg, "enabled", True)),
        extra_env=extra_env,
    )
    config.tools.mcp_servers[name] = new_cfg
    save_config(config)
    _record_audit(
        "grafana_connection_update",
        slug,
        before=_audit_summary(cfg),
        after=_audit_summary(new_cfg),
    )
    payload = grafana_connections_payload(last_action={
        "ok": True,
        "action": "update",
        "slug": slug,
        "message": f"Updated Grafana connection '{slug}'.",
    })
    payload["requires_restart"] = True
    return payload


def _delete_connection(query: QueryParams) -> dict[str, Any]:
    slug = _validated_slug(query_first(query, "slug"))
    name = _server_name(slug)

    config = load_config()
    cfg = _existing_server(config, name)
    before = _audit_summary(cfg)
    del config.tools.mcp_servers[name]
    save_config(config)
    _record_audit(
        "grafana_connection_delete",
        slug,
        before=before,
        after={"deleted": True},
    )
    payload = grafana_connections_payload(last_action={
        "ok": True,
        "action": "delete",
        "slug": slug,
        "message": f"Removed Grafana connection '{slug}'.",
    })
    payload["requires_restart"] = True
    return payload


def _identity_from_tool_output(text: str) -> str:
    """Best-effort identity line from the user_info tool output."""

    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        login = str(parsed.get("login") or "").strip()
        org = str(parsed.get("orgId") or "").strip()
        if login and org:
            return f" as {login} (org {org})"
        if login:
            return f" as {login}"
    return f": {text.strip()[:120]}" if text.strip() else ""


def _identity_tool_name(registry: ToolRegistry) -> str | None:
    # The sanitized wrapper name embeds the server name with unknown separator
    # handling, so match on the stable tool suffix instead.
    for tool_name in registry.tool_names:
        if tool_name.endswith(f"_{GRAFANA_IDENTITY_TOOL}"):
            return tool_name
    return None


async def _close_mcp_stacks(stacks: dict[str, Any]) -> None:
    for stack in stacks.values():
        with suppress(Exception):
            await stack.aclose()


async def grafana_test_action(query: QueryParams) -> dict[str, Any]:
    """Spawn the managed server and call user_info end-to-end.

    The connection, handshake, and identity probe all run inside the
    mcp-grafana child process, so this validates the URL and token against the
    real Grafana API without nanobot making any HTTP request itself.  With
    field overrides present (editor pre-save test) the probe uses the submitted
    values; without them it tests the saved entry, which must be managed.
    """

    from nanobot.agent.tools.mcp import (
        connect_mcp_servers,  # noqa: PLC0415 — lazy like the MCP presets test action
    )

    slug = _validated_slug(query_first(query, "slug"))
    name = _server_name(slug)

    try:
        config = resolve_config_env_vars(load_config())
    except ValueError as exc:
        return grafana_connections_payload(last_action={
            "ok": False,
            "message": _scrub_error_text(str(exc)),
            "error": _scrub_error_text(str(exc)),
            "slug": slug,
            "tool_count": 0,
            "checked_at": _checked_at(),
        })

    saved = config.tools.mcp_servers.get(name)
    base_url_override = (query_first(query, "base_url") or "").strip()
    token_override = (query_first(query, "token") or "").strip()
    has_overrides = bool(base_url_override or token_override or "org_id" in query)

    if has_overrides:
        # Editor pre-save test: build the managed template from submitted
        # values, falling back to the saved entry for fields not overridden.
        # Unmanaged or missing saved entries contribute nothing — their values
        # were never validated by this facade.
        saved_env = (
            saved.env if saved is not None and _is_managed_server(name, saved) else {}
        )
        base_url = _validated_base_url(base_url_override or saved_env.get(GRAFANA_URL_ENV, ""))
        if token_override:
            token = _validated_token(token_override)
        else:
            token = saved_env.get(GRAFANA_TOKEN_ENV, "")
            if not token:
                raise GrafanaConnectionError(
                    "a token is required to test (none stored for this connection)"
                )
        if "org_id" in query:
            org_id = _validated_org_id(query_first(query, "org_id"))
        else:
            org_id = saved_env.get(GRAFANA_ORG_ID_ENV, "")
        # The saved write-mode selection only affects spawn args (no
        # --disable-write); connectivity is identical either way, so the
        # probe always uses the saved mode to mirror the eventual save.
        saved_write_tools = (
            sorted(set(saved.enabled_tools) & set(GRAFANA_WRITE_TOOLS))
            if saved is not None and _is_managed_server(name, saved)
            else []
        )
        cfg = _managed_server_config(
            base_url=base_url,
            token=token,
            org_id=org_id,
            write_tools=saved_write_tools,
            enabled=True,
        )
    else:
        if saved is None:
            raise GrafanaConnectionError("unknown Grafana connection", status=404)
        _require_managed(name, saved)
        cfg = saved

    if not shutil.which(GRAFANA_MCP_COMMAND):
        return grafana_connections_payload(last_action={
            "ok": False,
            "message": (
                "Test requires 'uvx' on the nanobot PATH "
                "(https://docs.astral.sh/uv/)."
            ),
            "error": "missing dependency: uvx",
            "slug": slug,
            "tool_count": 0,
            "checked_at": _checked_at(),
        })

    registry = ToolRegistry()
    stacks: dict[str, Any] = {}
    try:
        stacks = await asyncio.wait_for(
            connect_mcp_servers({name: cfg}, registry),
            timeout=GRAFANA_TEST_TIMEOUT,
        )
        tool_count = len(registry.tool_names)
        if name not in stacks:
            last_action = {
                "ok": False,
                "message": f"'{slug}' did not complete an MCP handshake.",
                "error": "MCP handshake failed",
                "slug": slug,
                "tool_count": 0,
                "checked_at": _checked_at(),
            }
        else:
            identity_tool_name = _identity_tool_name(registry)
            if identity_tool_name is None:
                last_action = {
                    "ok": True,
                    "message": (
                        f"'{slug}' connected with {tool_count} tools "
                        "(identity check unavailable)."
                    ),
                    "slug": slug,
                    "tool_count": tool_count,
                    "checked_at": _checked_at(),
                }
            else:
                identity_tool = registry.get(identity_tool_name)
                output = await identity_tool.execute()
                if getattr(output, "is_error", False):
                    last_action = {
                        "ok": False,
                        "message": (
                            f"'{slug}' connected, but the identity check failed: "
                            f"{_scrub_error_text(str(output))}"
                        ),
                        "error": _scrub_error_text(str(output)),
                        "slug": slug,
                        "tool_count": tool_count,
                        "checked_at": _checked_at(),
                    }
                else:
                    last_action = {
                        "ok": True,
                        "message": f"'{slug}' connected{_identity_from_tool_output(str(output))}.",
                        "slug": slug,
                        "tool_count": tool_count,
                        "checked_at": _checked_at(),
                    }
    except asyncio.TimeoutError:
        last_action = {
            "ok": False,
            "message": (
                f"'{slug}' test timed out after {GRAFANA_TEST_TIMEOUT}s — "
                "the first run downloads the package; try again."
            ),
            "error": "timeout",
            "slug": slug,
            "tool_count": 0,
            "checked_at": _checked_at(),
        }
    except Exception as exc:
        error = _scrub_error_text(str(exc))
        last_action = {
            "ok": False,
            "message": f"'{slug}' could not connect.",
            "error": error,
            "slug": slug,
            "tool_count": 0,
            "checked_at": _checked_at(),
        }
    finally:
        await _close_mcp_stacks(stacks)

    return grafana_connections_payload(last_action=last_action)


_SYNC_ACTIONS = {
    "create": _create_connection,
    "update": _update_connection,
    "delete": _delete_connection,
}


async def grafana_settings_action(
    action: str | None,
    query: QueryParams,
    *,
    reload_mcp: GrafanaReload | None = None,
) -> dict[str, Any]:
    """Run a WebUI Grafana connection action and hot-reload when config changes.

    ``create``/``update``/``delete`` persist to config.json and then reconcile
    the live agent through the shared MCP reload pipeline (no restart);
    ``test`` never writes config and therefore never reloads.
    """

    if action is None:
        return await asyncio.to_thread(grafana_connections_payload)
    if action == "test":
        return await grafana_test_action(query)
    sync_action = _SYNC_ACTIONS.get(action)
    if sync_action is None:
        raise GrafanaConnectionError(f"unknown Grafana action '{action}'", status=404)
    payload = await asyncio.to_thread(sync_action, query)
    if reload_mcp is not None:
        payload = attach_mcp_hot_reload_result(payload, await reload_mcp())
    return payload
