# Security Boundaries

The agent operates with significant power (file system, shell, web). The following guards must not be bypassed when modifying related code.

## Workspace Restriction

Filesystem tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `apply_patch`) resolve paths through the workspace path resolver (`agent/tools/filesystem.py` / `agent/tools/path_utils.py`), which enforces that the resolved path must lie under the active workspace when workspace restriction is enabled. The media upload directory is always an internal extra read root while restricted.

Additional filesystem roots must be capability-specific. `extra_allowed_dirs` is a legacy read-only alias. Use `extra_read_allowed_dirs` for read-only roots, `extra_write_allowed_dirs` only when a write-capable tool is intentionally allowed to modify an extra directory, and exact file allowlists when a tool may modify only specific files.

Shell execution (`ExecTool`, `agent/tools/shell.py`) also respects `restrict_to_workspace` as an application-level guard: if enabled and `working_dir` is outside the workspace, the command is rejected before execution, and command text is checked for obvious workspace escapes. This is not process-level isolation; use an exec sandbox backend for that.

**Rule**: Any new path-handling logic must go through the workspace path resolver or perform an equivalent containment check with explicit read/write capability semantics.

## SSRF Protection

All outbound HTTP requests from agent tools must pass through `validate_url_target` (`security/network.py`). By default it blocks loopback, RFC1918 private addresses, CGNAT ranges, link-local ranges, and cloud metadata endpoints (including `169.254.169.254`).

The only escape hatch is `configure_ssrf_whitelist(cidrs)`, which reads from `config.tools.ssrf_whitelist` at load time.

HTTP/SSE MCP transports are part of this boundary: validate configured MCP URLs before probing or constructing clients, and validate each outgoing HTTP request before redirects are followed. Local/private HTTP MCP endpoints are allowed only through the explicit SSRF whitelist. Stdio MCP servers are not part of the HTTP SSRF path.

**Rule**: Do not add direct `httpx.get` / `requests.get` calls in tools. Route through the existing web fetch utilities or replicate the `validate_url_target` check.

## Shell Sandbox

`tools/sandbox.py` provides optional command wrapping. The only backend currently shipped is `bwrap` (bubblewrap), intended for containerized deployments. On Windows and bare-metal Linux without `bwrap`, commands run in the native shell with workspace restriction as an application-level guard only.

**Rule**: If adding a new sandbox backend, implement `_wrap_<name>(command, workspace, cwd) -> str` and register it in `_BACKENDS`.

## MCP Confirmation Gate

MCP server configs may list tools in `confirmTools` (`agent/tools/mcp_confirm.py`); those tools are wrapped at registration behind an interactive human-confirmation gate. The gate issues a chat confirmation card carrying a server-side single-use nonce (600s TTL, SHA-256 params fingerprint, chat-bound) and executes only through the channel-callback direct-tool resume after the nonce resolves. Both card issuance and execution are audited with scrubbed parameters.

Card actions run through channel-owned state on every surface: Feishu resolves its interaction registry; the WebUI websocket runtime rewrites direct-tool actions to opaque single-use tokens at the outbound agent_ui choke point and dispatches them via the `card_action` event.

**Rules**:
- Never trust client-supplied `tool_name`/`params` in a card callback on any channel; resolve them from server-side state only.
- Never let the confirmation nonce appear in model-visible text (tool results, card markdown, logs) — only inside the card action value held server-side.
- A gated tool must set `trusted_direct` only with server-side validation of every state-changing action (the base `Tool.trusted_direct` contract).
- The Grafana write-mode catalogs (read ∪ write, `--disable-write` dropped) rely on this gate plus the registration allowlist as the whole write boundary; do not add a write tool to those catalogs without the gate.
