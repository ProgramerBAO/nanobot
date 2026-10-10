# 2026-10-10 Trail 交付证据

工作目录 `D:\Code\Projects\nanobot`；Python3.13.16，manifest0.3.0；依赖及部署缺口见TRAIL_INTEGRATION。所有以下执行为本轮本机证据，无真实飞书或模型调用。

| Command | Result / exit | Scope |
|---|---|---|
| `.venv\Scripts\ruff.exe check nanobot/integrations nanobot/config/schema.py nanobot/agent/tools/registry.py tests/test_trail_integration.py tests/test_trail_tool_boundary.py scripts/verify_trail_joint.py` | PASS / 0 | scoped lint，未运行ruff format |
| `.venv\Scripts\python.exe -m pytest tests/test_trail_integration.py tests/test_trail_tool_boundary.py -q --cov=nanobot.integrations.trail --cov-report=term-missing --cov-fail-under=75` | PASS / 0 | 56 pass/3依赖warnings；模块覆盖80.08%；非全仓coverage |
| `.venv\Scripts\python.exe -m pytest tests/test_trail_integration.py tests/test_trail_tool_boundary.py tests/tools/test_tool_registry.py tests/config tests/agent/test_skills_loader.py tests/agent/test_mcp_connection.py tests/agent/test_mcp_transient_retry.py tests/agent/test_loop_tool_context.py tests/agent/test_loop_runner_integration.py -q --cov=nanobot.integrations.trail --cov-report=term-missing --cov-fail-under=75` | PASS / 0 | 248 pass/1 skip/44 warnings；当轮覆盖81.96%；之后profile样例回归在最终56suite执行 |
| `.venv\Scripts\python.exe -m compileall -q nanobot/integrations scripts/verify_trail_joint.py` | PASS / 0 | syntax |
| `$env:NANOBOT_SKIP_WEBUI_BUILD='1'; uv build --wheel --out-dir D:\Code\Projects\Trail\.review\ai\nanobot-dist` | PASS / 0 | Python wheel；zipfile确认receiver/MCP/3skills存在；WebUI NOT RUN |
| `python -m pytest tests/agent/test_mcp_reconnect_crash.py::test_mcp_reconnect_during_shutdown_does_not_crash -q` | FAIL / 1 | 在原始fa51367基线+mcp1.30也失败，reconnect event5s超时；TD-20261010-001；不修改timeout或断言 |

跨仓库：在Trail根目录、trail_test DSN与NANOBOT_REPO/NANOBOT_PYTHON环境运行 `go test -count=1 -tags=integration -run '^TestNanobotJointHTTP$' -v ./internal/webhook`，PASS/0，1test。真实PG/GoHTTP/签名/Python接收/SQLite/stdio，3事件6投递3模板消息替身，5工具真实REST查询、撤权拒绝。此脚本不是独立对业务数据运行的工具。

证据日志保存在Trail `.review/ai/nanobot-focused-final.log`、`nanobot-final.log`、`nanobot-baseline-reconnect.log`、`joint.log`；完整双仓库账本在Trail `docs/reviews/2026-10-10-trail-nanobot-request.md`。

七视角作者自审：Product单人只读边界；Architecture独立适配器复用REST；Development预算与关闭；QA分层证据/基线失败；Security签名/SSRF/Token/工具白名单；SRE持久化与unknown/容量/指标；Documentation同步产品/契约/runbook/债务。**未获得独立专家会签。**

真实Feishu/model/prompt injection、定时扫描周期、部署切换及回滚：BLOCKED（部署身份信息缺失），不得声称首期全部验收或生产可用；全仓pytest/SCA/WebUI：NOT RUN。按TRAIL_INTEGRATION启用独立隔离profile前补齐。
