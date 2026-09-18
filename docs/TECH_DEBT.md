# 技术债登记

按金融债管理：允许为交付速度主动借债，但必须登记、计息、按计划偿还；
禁止无记录的隐性负债。每条含位置、类型、借债原因、风险与持续伤害（利息）、
偿还触发条件、预计偿还成本。

## TD-20260919-001 · Mimosa 非凭据存量 findings（31 条）

- **位置**：全仓（明细见下）
- **类型**：实现 / 安全工具误报与既有风险面
- **借债原因**：2026-09-19 占位凭据清零任务的非目标范围——凭据类
  （261 条）已全部清偿，这些非凭据 findings 属另一债务面，本次只登记不扩scope。
- **风险与持续伤害**：commit 门禁（warn 模式）每次提交仍会列出这 31 条
  high，掩盖新增问题；部分为误报（原子写的 tmp_path 被判路径穿越），部分
  值得逐条甄别（SSRF 报告的 config 驱动 URL）。**利息 = 门禁信噪比。**
- **偿还触发条件**：下次安全治理专项，或 MIMOSA_GIT_GATE_MODE 计划升级为
  graded/deny 前（必须先清零或逐条 triage）。
- **预计偿还成本**：半天——逐条 triage（误报加 `# mimosa-ignore` + 理由，
  真问题逐个修复），或引入 policy exclusions。

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
- 代码内 `# mimosa-ignore` 注释：代码行有效；**字符串内匹配需裸 token 后跟空白/行尾**（`# mimosa-ignore: reason` 带冒号在嵌串匹配上无效——探针 v5 实证）。
- `mimosa policy init` 生成的 `.mimosa/security-policy.json`（gitignored）会把
  `command.forbidShell=true` 应用到全仓，令测试里的 exec mock 新增 44 条
  command-no-shell findings——本次调研后已删除该文件还原基线；后续启用
  policy 前需先处置这批测试误报。
