This file provides guidance to AI coding agents working with this repository.

## Project Overview

nanobot is a lightweight, open-source AI agent framework written in Python with a React/TypeScript WebUI. It centers around a small agent loop that receives messages from chat channels, invokes an LLM provider, executes tools, and manages session memory.

## Development Commands

```bash
# Python: run single test / lint
pytest tests/test_openai_api.py::test_function -v
ruff check nanobot/

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev      # or NANOBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# Gateway
nanobot gateway
```

## High-Level Architecture

### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages from external platforms and publish `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) handles the actual LLM conversation loop: send messages to the provider, receive tool calls, execute tools, and stream responses.
4. Responses are published as `OutboundMessage` events back to the appropriate channel.

### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): The core processing engine. `AgentLoop` manages session keys, hooks, and context building. `AgentRunner` executes the multi-turn LLM conversation with tool execution.
- **LLM Providers** (`nanobot/providers/`): Provider implementations (Anthropic, OpenAI-compatible, OpenAI Responses API, Azure, Bedrock, GitHub Copilot, OpenAI Codex, etc.) built on a common base (`base.py`). Includes image generation (`image_generation.py`) and audio transcription (`transcription.py`). `factory.py` and `registry.py` handle instantiation and model discovery.
- **Channels** (`nanobot/channels/`): Platform integrations (Telegram, Discord, Slack, Feishu, Matrix, WhatsApp, QQ, WeChat, WeCom, DingTalk, Email, MoChat, MS Teams, WebSocket, Mattermost). `manager.py` discovers and coordinates them. Channels are self-contained packages auto-discovered via `pkgutil` scanning.
- **Tools** (`nanobot/agent/tools/`): Agent capabilities exposed to the LLM: filesystem (read/write/edit/list), shell execution (with sandbox backends), web search/fetch, MCP servers, cron, notebook editing, subagent spawning, long-running tasks / sustained goals (`long_task.py`), image generation, and self-modification. Tools are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Memory** (`nanobot/agent/memory.py`): Session history persistence with Dream two-phase memory consolidation. Uses atomic writes with fsync for durability.
- **Session Management** (`nanobot/session/`): Per-session history, context compaction, TTL-based auto-compaction (`manager.py`), and sustained goal state tracking (`goal_state.py`).
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic-based configuration loaded from `~/.nanobot/config.json`. Supports camelCase aliases for JSON compatibility.
- **WebUI** (`webui/`): Vite-based React SPA that talks to the gateway over a WebSocket multiplex protocol. The dev server proxies `/api`, `/webui`, `/auth`, and WebSocket traffic to the gateway.
- **API Server** (`nanobot/api/server.py`): OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) for programmatic access.
- **Command Router** (`nanobot/command/`): Slash command routing and built-in command handlers.
- **Heartbeat** (`nanobot/templates/HEARTBEAT.md`): Periodic task list checked via `cron` jobs (legacy dedicated service removed).
- **Pairing** (`nanobot/pairing/`): DM sender approval store with persistent pairing codes per channel.
- **Skills** (`nanobot/skills/`): Built-in skill definitions (cron, github, image-generation, etc.) loaded into agent context.
- **Security** (`nanobot/security/`): PTH file guard and other security measures activated at CLI entry.

### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Contribution Flow

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for contribution flow and PR guidelines.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

## Feishu Card UX

- All structured report push cards must use the shared `ReportDocument` renderer;
  do not introduce one-off report layouts for individual templates.
- Cards must present status and data quality first, then a compact KPI summary,
  followed by titled detail sections and actionable controls.
- Use restrained status colors, stable metric ordering, readable spacing, and
  explicit empty/partial/missing states. Never turn failed or missing data into 0.
- Keep each card within Feishu element limits, including one table per card;
  split continuation cards with a repeated title and page marker when necessary.
- Card actions must remain server-validated opaque callbacks and must not expose
  credentials, raw API responses, SQL, PromQL, or unrestricted request parameters.
- Every new renderer or fallback must preserve the same semantic order and data
  quality warnings as the Feishu card.
- Every structured report must expose its current window, comparison baseline,
  logical data source, unit, aggregation, and a short reading guide. Do not label
  a time-bucket peak as an average or hide missing baselines.
- Health reports use request-level TTFT percentiles when available. Trend
  `FIRST_TOKEN_DELAY` values must remain explicitly labeled as time-series data;
  missing detail or insufficient samples must be visible and cannot produce a
  normal health status.
- Cube customer selectors must submit the exact `tenantId` returned by the live
  catalog. Configured aliases are display and matching aids only after their target
  ID is present in that catalog; aliases must never create synthetic customers.
- A successful empty Cube response and a failed Cube query are different states.
  Only the former may be labeled as no business data; connection, auth, rate-limit,
  upstream, and tenant-resolution failures must remain explicit `missing`/`partial`.
- Cube deterministic report routing must treat an explicit `YYYY-MM-DD` date as
  authoritative, including Chinese suffixes such as `2026-08-29日`; only an omitted
  date may default to yesterday. Model parsing must preserve common names such as
  `Kimi-K3` and `vLLM` before applying exact catalog/model validation.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- WebUI dev proxy config: `webui/vite.config.ts`
- Tests mirror the `nanobot/` package structure.

## Cross-device Handoff

- At the start of work, read `docs/WORK_CONTEXT.md` and `docs/CODEX_SESSIONS.md` when
  they exist, then run `git status --short --branch`.
- Keep durable project rules in this file or `.agent/`; keep current task state in
  `docs/WORK_CONTEXT.md` and historical Codex session summaries in
  `docs/CODEX_SESSIONS.md`.
- Before handoff, update the context document with the goal, changed files,
  verification, next action, and known risks.
- Review generated files before staging. Never commit tokens, passwords, `.env` files,
  `C:\Users\38658\.nanobot\config.json`, or other machine-local secrets.
- Use `git pull --ff-only` when synchronizing another device. Do not force-push without
  explicit approval.
