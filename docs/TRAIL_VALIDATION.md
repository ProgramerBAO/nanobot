# 2026-10-10 Trail 交付证据

## 与实际本地最新 main 合并补验

用户确认实际路径 `D:\Code\bots\nanobot`。开始时本地/GitHub main均为 `07fbaed540c58901eaf18ae9fe737dc80cd0e83c`、工作区干净、manifest/installed均0.3.0。复用已有 `codex/trail-nanobot-integration`，未新建分支；合入最新main的合并提交 `e1f0532`。相对07fbaed，报表intent_router/cube/store/report_center/base/magik/reporting_api/pyproject文件无diff；loop只增加allowed_tools接线，registry只增加注册白名单。

解释器P=`D:\Code\bots\nanobot\.venv\Scripts\python.exe`，Python3.14.6；MCP1.29.0/httpx0.28.1/aiohttp3.14.3/lark-oapi1.7.3/pytest9.1.1/ruff0.16.3。本轮未升级实际项目依赖。下表cwd除注明外为 `D:\Code\Projects\nanobot`（合并验证克隆，实际解释器；已确认import源指向此克隆）。

| Command | Result / exit | Scope / Evidence |
|---|---|---|
| `P -m ruff check nanobot/ tests/test_trail_integration.py tests/test_trail_tool_boundary.py scripts/verify_trail_joint.py` | PASS / 0 | 全Python源码+本轮测试/脚本，All checks passed |
| `P -m compileall -q nanobot/integrations nanobot/agent/loop.py nanobot/agent/tools/registry.py tests/test_trail_tool_boundary.py` | PASS / 0 | 语法检查；未运行ruff format |
| `P -m pytest tests/test_trail_integration.py tests/test_trail_tool_boundary.py -q` | PASS / 0 | 57 pass、2 warnings；`.review/ai/nanobot-main-merge-focused.log`；含新增排除报表路由的注册边界回归 |
| `P -m pytest tests/test_trail_integration.py tests/test_trail_tool_boundary.py tests/tools tests/reporting tests/config tests/agent/test_magik_report_intent_route.py tests/agent/test_mcp_connection.py tests/agent/test_mcp_transient_retry.py tests/agent/test_mcp_reconnect_crash.py tests/agent/test_loop_tool_context.py tests/agent/test_loop_runner_integration.py tests/agent/test_skills_loader.py tests/webui/test_reporting_api.py -q` | FAIL / 1 | 1283 pass/28 skip/21 xfail/1 fail/3 warnings；203.91s；`.review/ai/nanobot-main-merge-tests.log`；进程工具耗时1.146s未达<1s断言；新增第4个boundary回归随后在57 suite验证 |
| `.venv\Scripts\python.exe -m pytest tests/tools/test_exec_session_tools.py::test_exec_session_yield_returns_when_process_finishes_early -q` | FAIL / 1 | cwd实际项目（仍是未合并07fbaed）；同一失败1.240s>1s，1failed；`.review/ai/nanobot-main-baseline-exec.log`；既有TD-20261010-002，不放宽断言 |
| `P -m pytest tests/agent/test_mcp_reconnect_crash.py -q -rs` | PASS / 0 | 2pass/6.59s，本地MCP1.29下通过；MCP1.30历史失败未声称修复 |
| `$env:NANOBOT_SKIP_WEBUI_BUILD='1'; uv build --wheel --python D:\Code\bots\nanobot\.venv\Scripts\python.exe --out-dir D:\Code\Projects\Trail\.review\ai\nanobot-merged-dist` | PASS / 0 | Python wheel打包；WebUI明确NOT RUN，未改前端源码；`.review/ai/nanobot-main-merge-build.log` |
| `go test -count=1 -tags=integration -run '^TestNanobotJointHTTP$' -v ./internal/webhook` | PASS / 0 | cwdTrail；trail_test双DSN，NANOBOT_REPO=验证克隆、NANOBOT_PYTHON=P、PYTHONPATH=验证克隆；1test，3事件6收件/3消息替身/5实时工具/撤权拒绝；`.review/ai/nanobot-main-merge-joint.log` |
| `git diff --check` | PASS / 0 | 合并差异与后续小幅文档/测试变更 |

P是上述实际解释器绝对路径简写，不是已安装CLI。日志位于Trail `.review/ai/`。联合联调初次FAIL/1：跨克隆用解释器直接执行脚本，editable安装优先指向未合并的实际仓库，子进程无法导入新增集成；显式PYTHONPATH指向验证源码后同一命令PASS；不改业务配置或依赖。迁入实际项目后需核对import源。

七视角作者自审（非独立专家）：Product默认工具兼容/Trail opt-in；Architecture保留既有报表路由；Development仅接线及边界回归；QA实际运行时/既有基线失败分层；Security无凭据/配置变更；SRE不重启、不启用消息链；Documentation历史证据保留、技术债003/004避让报表001/002。全仓pytest、真实飞书/模型/部署验收、WebUI构建及本轮coverage **NOT RUN**（实际venv没有pytest-cov）；不把历史80.08%当作本轮覆盖率。

运行中网关、用户profile、开关、workspace、收件箱均不修改；无生产发布。回退需在保护后续开发前提下评审revert集成提交，不reset/强推。新建分支前告知与跨项目保护规则同步AGENTS；忽略的本地交接文件追加本轮记录，不覆盖此前任务状态。

## 原始集成分支证据（历史运行时）

工作目录 `D:\Code\Projects\nanobot`；Python3.13.16，manifest0.3.0；依赖及部署缺口见TRAIL_INTEGRATION。所有以下执行为本轮本机证据，无真实飞书或模型调用。

| Command | Result / exit | Scope |
|---|---|---|
| `.venv\Scripts\ruff.exe check nanobot/integrations nanobot/config/schema.py nanobot/agent/tools/registry.py tests/test_trail_integration.py tests/test_trail_tool_boundary.py scripts/verify_trail_joint.py` | PASS / 0 | scoped lint，未运行ruff format |
| `.venv\Scripts\python.exe -m pytest tests/test_trail_integration.py tests/test_trail_tool_boundary.py -q --cov=nanobot.integrations.trail --cov-report=term-missing --cov-fail-under=75` | PASS / 0 | 56 pass/3依赖warnings；模块覆盖80.08%；非全仓coverage |
| `.venv\Scripts\python.exe -m pytest tests/test_trail_integration.py tests/test_trail_tool_boundary.py tests/tools/test_tool_registry.py tests/config tests/agent/test_skills_loader.py tests/agent/test_mcp_connection.py tests/agent/test_mcp_transient_retry.py tests/agent/test_loop_tool_context.py tests/agent/test_loop_runner_integration.py -q --cov=nanobot.integrations.trail --cov-report=term-missing --cov-fail-under=75` | PASS / 0 | 248 pass/1 skip/44 warnings；当轮覆盖81.96%；之后profile样例回归在最终56suite执行 |
| `.venv\Scripts\python.exe -m compileall -q nanobot/integrations scripts/verify_trail_joint.py` | PASS / 0 | syntax |
| `$env:NANOBOT_SKIP_WEBUI_BUILD='1'; uv build --wheel --out-dir D:\Code\Projects\Trail\.review\ai\nanobot-dist` | PASS / 0 | Python wheel；zipfile确认receiver/MCP/3skills存在；WebUI NOT RUN |
| `python -m pytest tests/agent/test_mcp_reconnect_crash.py::test_mcp_reconnect_during_shutdown_does_not_crash -q` | FAIL / 1 | 在原始fa51367基线+mcp1.30也失败，reconnect event5s超时；TD-20261010-003（合并时避让报表既有001编号）；不修改timeout或断言 |

跨仓库：在Trail根目录、trail_test DSN与NANOBOT_REPO/NANOBOT_PYTHON环境运行 `go test -count=1 -tags=integration -run '^TestNanobotJointHTTP$' -v ./internal/webhook`，PASS/0，1test。真实PG/GoHTTP/签名/Python接收/SQLite/stdio，3事件6投递3模板消息替身，5工具真实REST查询、撤权拒绝。此脚本不是独立对业务数据运行的工具。

证据日志保存在Trail `.review/ai/nanobot-focused-final.log`、`nanobot-final.log`、`nanobot-baseline-reconnect.log`、`joint.log`；完整双仓库账本在Trail `docs/reviews/2026-10-10-trail-nanobot-request.md`。

七视角作者自审：Product单人只读边界；Architecture独立适配器复用REST；Development预算与关闭；QA分层证据/基线失败；Security签名/SSRF/Token/工具白名单；SRE持久化与unknown/容量/指标；Documentation同步产品/契约/runbook/债务。**未获得独立专家会签。**

真实Feishu/model/prompt injection、定时扫描周期、部署切换及回滚：BLOCKED（部署身份信息缺失），不得声称首期全部验收或生产可用；全仓pytest/SCA/WebUI：NOT RUN。按TRAIL_INTEGRATION启用独立隔离profile前补齐。
