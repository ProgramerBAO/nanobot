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
  Aliases resolve through `effective_tenant_mappings` (nanobot/agent/tools/
  magik_cube.py): a `report_settings` store override (key `tenant_mappings`,
  WebUI-managed since 2026-09-17) wins as a whole table over the config.json
  `tenantMappings` default and applies without a restart; every consumer
  (magik tools, Cube connector, report_center catalog) must read through that
  single source, never `config.tenant_mappings` directly. The store import in
  that helper stays function-level — a module-level import re-enters the
  partially initialized reporting package.
- A successful empty Cube response and a failed Cube query are different states.
  Only the former may be labeled as no business data; connection, auth, rate-limit,
  upstream, and tenant-resolution failures must remain explicit `missing`/`partial`.
- Cube deterministic report routing must treat an explicit `YYYY-MM-DD` date as
  authoritative, including Chinese suffixes such as `2026-08-29日`; only an omitted
  date may default to yesterday. Model parsing must preserve common names such as
  `Kimi-K3` and `vLLM` before applying exact catalog/model validation.
- Cube usage daily reports must retain two named baselines: the previous day and the
  same weekday one week earlier. User-visible change text is percentage-only; raw
  absolute deltas may remain internal but must not be rendered.
- Cube usage reports source average TPM only from `avgTpm`. Average TPM may be
  averaged across valid dates within one tenant/model/Endpoint series, but must never
  be aggregated across Endpoint or customer boundaries; missing `avgTpm` stays
  unavailable and must not fall back to `maxTpm` or zero.
- Cube hourly TPM reports must query `analysis/endpoint-max-tpm/daily/query` with
  `time_level=TIME_LEVEL_HOUR` and an `endDate` one day after the target hour's date
  (a same-day range returns only the midnight point), then filter locally to the
  target complete hour; not-yet-elapsed hours return zero placeholders and must stay
  out of the report. Machines are dual-source per unique model: allocation from
  `analysis/model-machine-usage/query` (current snapshot, quality-relevant) and
  actual usage from `analysis/machine-tpm-trend/query` (target-hour point-in-time,
  informational — a missing usage value must not downgrade quality). The data
  section renders as a single six-column table (客户 | 模型 | 峰值 | 均值 |
  机器占用 | 机器真实使用，user-confirmed 2026-09-16). Idle =
  allocation minus usage, flagged as （闲N） inside the 机器真实使用 column from one
  machine difference and summed into the subtitle as `N 机器空闲`; both values are
  platform-level and must never be attributed to a customer. A missing target-hour
  point stays 暂不可用 with `partial` quality and must never be rendered as zero.
  The card appends a seven-column cluster inventory table (集群 | 机器总数 | 空闲 |
  生产 | 测试 | 开发 | 备用, user-confirmed 2026-09-17 order with idle right
  after the total) from `analysis/machine-usage-summary/query` (POST
  `{"noloading":true}`, send-time platform snapshot; `occupiedMachineCount` carries
  the TEST machine total; production = total − the other categories and renders `—`
  when parts are missing or exceed the total). The inventory table is
  informational: a failed or empty summary omits the table without downgrading
  quality, and cluster-level idle never converts to or from the model-level （闲N）
  semantics. Feishu splits the tables across cards (one table per card,
  page-marked subtitles).
- Hourly subscriptions run at five minutes past the hour; an explicit hour list
  (e.g. "每天 9 点、10 点播报上一小时 TPM") compiles to `5 9,10 * * *` while the
  reported window always comes from the clock at execution time, never from the
  cron expression. Hours are 0-23 lists valid only on the hourly TPM report
  (recent1h ↔ hourly pinned both ways in compile_form; workdays/weekly/monthly ×
  hour lists stay unsupported). Hour-list helpers live in the dependency-free
  `nanobot/utils/schedule_hours.py` — the agent-layer intent parser must not
  import the reporting package (connector-construction import cycle). The
  subscription delivery vocabulary includes the bare verb 报 (2026-09-17):
  vocabulary misses drop a message out of the deterministic subscription chain
  into the interactive report flow, so any new delivery verb must be added to
  BOTH gates (the signal regex's delivery group and has_delivery in
  cube_subscription_intent.py) with a regression pinning the live phrasing.
- All-model discovery (hourly TPM and multi-scope briefs, manual and
  scheduled): a tenant with no active models in the window is successful empty
  data, not a failure (user-confirmed 2026-09-17). Partially idle runs keep
  every requested customer visible — the report renders for active tenants
  plus an explicit 本期无用量客户 note; a fully idle window delivers a
  no-usage reminder card (quality=complete, no error delivery record, no
  transient retry, one ok run-history row). Catalog query failures still fail
  closed with their original permission/upstream wording.
- Delivery groups (2026-09-16): a broadcast fanning out to multiple chats is N
  subscription rows sharing the fingerprint identity minus the chat target
  (`subscription_group_key`, derived at read time — no group column). Each row
  keeps its own Cron job, fingerprint, and delivery idempotency key; group
  create skips per-target duplicates (≤20 targets), and a group update diffs
  the target list (stays synced first, then adds, then deletes — per-row CAS,
  non-atomic). Cross-chat duplicate creation is allowed by design (the
  fingerprint includes chat_id; reports of it being blocked predate the
  phase-3 unified fingerprint).
- Report feature flags default ON for the usage/health/provider/management
  families. Enable/disable flags no longer gate template registration (all Cube
  templates register whenever the connector exists); execution and visibility read
  the effective flag per request: a `report_feature_flags` store override wins over
  the configured default, so the WebUI "功能开关" page toggles take effect without a
  restart and every change is audited. Construction-level semantics (semantics v2
  family, TTFT detail, thresholds, provider include_details) and extension
  connectors (Grafana/WeCom/DingTalk/cost TokenAPI) stay config-level. New user-
  facing configuration must ship with a page-level control in the Report platform
  settings; do not add config.json-only switches for report features.

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
