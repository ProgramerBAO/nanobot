# 技术债登记

## TD-20261010-001 · MCP shutdown reconnect 基线失败

- 位置：tests/agent/test_mcp_reconnect_crash.py::test_mcp_reconnect_during_shutdown_does_not_crash。
- 类型：测试/依赖契约；原因：本轮扩大回归发现 mcp1.30.0 下 5s reconnect_started.wait 超时，在未修改 fa51367 基线上同命令复现。
- 利息：影响 MCP 可靠性门禁可信度，不能据此声称全量通过；没有改变 timeout 或断言。
- 偿还触发：下一次 MCP 可靠性专项或升级依赖之前；成本：半天至一天，核对 transport idle/reconnect 协议及 fixture。
- owner：bot 管理者；deadline：试点部署前确认处置；证据由 Trail 本轮评审卡记录。

## TD-20261010-002 · Trail 真实飞书与模型验收缺口

- 位置：docs/TRAIL_INTEGRATION.md、独立部署 profile。
- 类型：测试/运维；原因：部署 SHA、profile、本人 open_id、模型配置及专用Token尚未提供；不访问/改动既有 bot 私密配置。
- 利息：SDK替身不能证明真实消息、模型建议、扫描周期与回滚行为，代码不能宣称生产可用。
- 偿还触发：单人试点启用前；成本：半天联调+一个实际扫描周期观察。
- owner：小沈/bot管理会话；deadline：试点启用前；验收：三类真实消息+追问+撤权+回滚健康检查，证据单独记录。

按金融债管理：允许为交付速度主动借债，但必须登记、计息、按计划偿还；
禁止无记录的隐性负债。每条含位置、类型、借债原因、风险与持续伤害（利息）、
偿还触发条件、预计偿还成本。

## TD-20261010-001 · Phase 2 真实模型与渠道验收尚未完成

**策略更新**：用户确认先关注功能，延后 3 秒性能目标。允许使用独立宽预算
评测，不再将慢响应本身列为功能阻塞；首轮宽预算 13/21、修正后完整轮 19/21，客户槽位遗漏和真实渠道
验收仍未收尾。旧短预算结果保留为历史证据，不解释为模型不支持工具调用。
昵称提示之后范围定向复测仍 0/3；下一步应复用已有实时目录原文实体补全，
而非持续追加例子或忽略实体遗漏。该实现尚未开始。

- **位置/类型**：`scripts/evaluate_report_intents.py`、统一意图路由；测试/发布。
- **原因与事实**：当前会话模型 `ep-glm-5.3-e8a127` 在 3 秒预算内，最新五条
  分类请求均超时；最小正文探针 4.66 秒、length、无正文。根因未确定，不据此
  声称模型不支持 tool calling。代码可以增量交付，但新路由必须保持默认 off。
- **风险/利息**：未完成真实分类正确率、Cube 只读与 Feishu 投递验收；提前开启
  会增加用户澄清失败和响应耗时，mock 不能代替真实模型性能与正确率。
- **偿还触发条件**：启用 fallback/primary 前，先核实 provider 响应及推理配置，
  同预算真实语料 smoke 达标，再进行测试账号查询和投递。
- **预计成本**：provider 定位及模型验证 0.5 天，渠道验收 0.5 天（估算非承诺）。
- **Owner/deadline**：项目维护者；启用新路由前。回滚为页面选择 off。

## TD-20261010-002 · 扩展回归的进程工具耗时门与报表 fixture warning

- **位置/类型**：`tests/tools/test_exec_session_tools.py:126`、
  `tests/tools/test_report_center.py::_tool`；测试。
- **发现**：Phase 2 扩展回归中早退进程用例 `<1.0s` 断言实测 1.058s 失败，
  单项复测 1.179s 仍失败；上述执行工具文件未修改，尚未做干净 HEAD 对照，
  不把它包装为已确认既有基线。report_center fixture 用 AsyncMock 包装含同步
  matcher 的工具，触发未 await warning（单项复测可见）。
- **风险/利息**：扩展套件不是全绿，固定机器耗时断言与错误 mock 类型削弱
  跨平台验证可信度。本次不放宽断言、不跳过用例、不混入无关工具修复。
- **偿还触发条件/成本**：独立测试治理时先复现并核实 Windows 进程启动路径，
  fixture 使用正确同步/异步边界；预计 0.5 天（估算）。
- **Owner/deadline**：项目维护者；下一次全量门禁前，保留原失败证据。

## TD-20261006-001 · 既有 pytest 失败与目录顺序依赖（4 项日志断言 + 2 项渠道基线）

- **位置**：`tests/agent/test_cursor_recovery.py`、`test_loop_save_turn.py`、
  `test_memory_store.py`、`test_runner_fallback.py`（顺序依赖）；
  `tests/channels/test_channel_plugins.py::test_optional_features_payload_lists_feishu_instances`、
  `tests/channels/test_channel_setup.py::test_channel_locales_cover_authoritative_setup_contracts`（基线失败）
- **类型**：测试
- **借债原因**：Grafana 三阶段验证期间发现（2026-10-06），非本弧引入——
  stash 干净 HEAD 复现同样失败，标准全序下 4 项顺序依赖不复现。
- **症状**：①以 `pytest tests/channels tests/webui tests/agent`（非字母序）
  运行时，4 个 agent 日志断言失败（channels 先跑疑似污染 loguru sink / 全局
  日志捕获状态）；②字母序全量恒定失败 2 项：feishu instances payload 断言
  与 channel locale 契约（`quoteGroupReplies` 等 feishu 新字段缺 locale 覆盖）。
- **风险与持续伤害**：**利息 = 验证可信度**——任何非标准顺序的定向运行可能
  出现与改动无关的红灯，浪费时间排查（本次实证花费一轮 stash 对照）；
  2 项基线失败持续出现在全量结果里，掩盖新问题。
- **偿还触发条件**：下次测试治理专项；或任何需要频繁定向跑 channels+agent
  组合的工作开始前。
- **预计偿还成本**：顺序依赖半天（定位共享日志状态、加隔离 fixture）；
  feishu locale 契约 1-2 小时（补 `quoteGroupReplies`/`topicIsolation`/
  `followBotThreads` 等新字段的 10 locale 文案与契约用例）；
  instances payload 断言 1 小时（核对测试期望与当前 payload 形状）。

## TD-20260919-001 · Mimosa 非凭据存量 findings（31 条）——已甄别，残留为登记基线

**状态更新（2026-09-19 晚，commit `3fb99b8`）**：31/31 已逐条甄别完毕，全部为误报或非安全用途；每处已落位带具体理由的行内 `mimosa-ignore` 注释（空格分隔格式）。**关键实测：deep audit / git-gate 口径不尊重行内 ignore**（空 commit 探针 + 4 种 policy exclusion 形状均复现全量 31 条），行内注释仅对编辑门禁/单文件扫描口径生效并充当行级文档。因此本条债务的"偿还"至此为止：**deep-gate 残留为已甄别、已注释的永久已知基线**，升级 `MIMOSA_GIT_GATE_MODE=graded/deny` 不可行（会永久阻断全部提交）——需上游提供 deep 口径豁免机制后再评估。门禁策略维持现状：编辑门禁 graded（写入时强制拦截，2026-09-19 当天实证拦截 5 次）+ git gate warn（报告增量）。

- **位置**：全仓（明细见下）
- **类型**：实现 / 安全工具误报与既有风险面
- **借债原因**：2026-09-19 占位凭据清零任务的非目标范围——凭据类
  （261 条）已全部清偿，这些非凭据 findings 属另一债务面，本次只登记不扩scope。
- **风险与持续伤害**：commit 门禁（warn 模式）每次提交仍会列出这 31 条
  high，掩盖新增问题；部分为误报（原子写的 tmp_path 被判路径穿越），部分
  值得逐条甄别（SSRF 报告的 config 驱动 URL）。**利息 = 门禁信噪比。**
- **偿还触发条件**：~~下次安全治理专项~~ **已完成甄别（见状态更新）**；
  彻底清零需 Mimosa 上游支持 deep 口径 ignore/allowlist。
- **预计偿还成本**：~~半天——逐条 triage~~ 已花费（2026-09-19）；
  剩余为上游依赖，无本仓库内可行动作。

### 明细（deep audit 2026-09-19，复扫确认仍为 31 条）

| 规则 | 数量 | 典型位置 | 初判 |
| --- | --- | --- | --- |
| open（路径穿越） | 14 | memory.py:464、session/manager.py:703、cron/service.py:405、utils/helpers.py:524、webui/{sidebar_state,token_usage,transcript,workspaces}.py、channels/{qq,matrix}/runtime.py、cli/commands.py:745、optional_features.py:267、triggers/local_store.py:400、utils/run_records.py:41 | 全部是"写 tmp_path 再 rename"的原子写模式，tmp_path 来自 `tempfile`/`Path(tmp)`，非用户可控——**疑似误报**；逐条加 ignore 或等 scanner 改进 |
| ssrf-dynamic-url | 8 | magik_cube.py:2256/2343/2381（`requests.get(day, 0)` 是 dict 操作，规则把 `.get(` 误判为 HTTP）、apps/cli/service.py:454/1012（config 驱动 URL + 固定 timeout/follow_redirects）、webui/version_check.py:37（PyPI 固定 URL）、tests/webui/test_gateway_webui_smoke.py:96、tests/tools/test_tool_validation.py:256 | 大半误报（dict.get、固定白名单 URL）；apps/cli 的 config URL 值得确认是否有 host 校验 |
| exec / 代码注入 | 3 | tests/agent/tools/test_self_tool.py:655（断言字符串含 "exec(error)"）、webui/src/tests/agent-activity-cluster.test.tsx:1398/1423（测试 fixture 构造 exec 日志行） | 测试数据，非执行——误报 |
| sql-dynamic-query | 2 | reporting/store.py:252/255（`PRAGMA table_info({table})` 与 `ALTER TABLE {table} ADD COLUMN {column}`，table/column 来自代码内常量迁移表，非外部输入） | 误报（f-string 但无外部数据流）；如需可改为常量拼接仍不引入绑定（DDL 无法参数化），加 ignore + 理由即可 |
| random.random / randint | 3 | channels/napcat/runtime.py:366（退避抖动）、channels/weixin/runtime.py:964（typing ticket TTL 抖动）、websocket tests（端口随机） | 非安全用途（非 token/secret），低危误报性质，Mimosa 标 low 本就不阻断 |
| Environment（XSS） | 1 | utils/prompt_templates.py:20（Jinja2 Environment 构造，模板为仓库内固定文件） | 误报（无不可信模板源）；如需可开 autoescape 并加 ignore 说明 |

### 关联工具机制备注（2026-09-19 调研实证）

- 无通用 baseline/ignore 文件；`mimosa backlog triage` 只分流不 dismiss。
- 代码内 `# mimosa-ignore` 注释：**仅对编辑门禁/单文件扫描口径生效**；
  格式上代码行任意形式可用，**嵌串匹配需裸 token 后跟空白/行尾**
  （`# mimosa-ignore: reason` 带冒号在嵌串上无效——探针 v5 实证）。
- **deep audit / git gate（L3）不尊重行内 ignore**（空 commit 探针实测：
  31 条注释全部落位后 gate 原样报 12 条展开 + 折叠总结）；
  `threatModel.exclusions` 四种形状（路径 glob/通配/规则/路径:规则）对
  deep audit findings 数量零影响（探针实测）。
- `mimosa policy init` 生成的 `.mimosa/security-policy.json`（gitignored）会把
  `command.forbidShell=true` 应用到全仓，令测试里的 exec mock 新增 44 条
  command-no-shell findings——2026-09-19 调研后已删除该文件还原基线；启用
  policy 前需先处置这批测试误报。
