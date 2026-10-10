# Trail 单人只读集成

2026-10-10：外部通知接收器 + stdio MCP + 三个 skills。代码实现，真实飞书/模型/定时扫描周期/部署回滚验收未完成，禁止默认启用现有多用户 bot。Trail 共用契约位于其 `docs/integrations/trail-nanobot.md`；本端仅通过 REST 获取事实。

## 启用条件

使用独立 nanobot profile、独立 workspace、独立 SQLite state_db、本人私聊。显式绑定 Trail 普通服务账号 ID、可见项目 ID 和本人飞书 open_id。专用 API Token 只有 issues:read；拒绝管理员、额外 scope、越权项目。团队请求不能共享此账号。不要替换正在服务团队的 profile。

开发基线 `fa513674910736419366e226710434f33b380e6d`，manifest 0.3.0 / Python >=3.11；本轮虚拟环境 Python 3.13.16，httpx 0.28.1、mcp 1.30.0、aiohttp 3.14.4、lark-oapi 1.5.5、pydantic 2.14.0。依赖均已在 manifest 中，无新增依赖；无锁文件，实际部署版本仍需 bot 管理者核验，不把开发环境视为部署事实。

## 配置

接收器与 MCP 仅从受控环境读取以下真实字段，非法值启动失败且不输出输入。secret 不进入模型上下文、结果、Git 或日志。

| 环境变量 | 契约 |
|---|---|
| TRAIL_BASE_URL | http(s) origin，无路径/认证/query/fragment |
| TRAIL_API_TOKEN | 专用 issues:read Token，SecretStr |
| TRAIL_SERVICE_USER_ID | 普通账号正整数 ID |
| TRAIL_PROJECT_IDS | JSON 数组，1..64 个唯一正整数；公开项目也需列入 |
| TRAIL_CSI_PROJECT_ID | 可选；必须在允许项目中，报表还验证 identifier=customer-issues |
| TRAIL_ALLOW_LOOPBACK | 默认 false；同机测试明确 true |
| TRAIL_ALLOWED_CIDRS | 默认 []；仅精确 /32 或 /128，不能放宽网段 |
| TRAIL_WEBHOOK_SECRET | 接收器必需，至少 16 字符，来自 Trail 创建响应 |
| TRAIL_FEISHU_OPEN_ID | 接收器必需，显式本人 ou_ ID |
| TRAIL_FEISHU_INSTANCE_ID | 默认 default；选择现有 Feishu 实例 |
| TRAIL_STATE_DB | 接收器必需；SQLite 绝对路径，一进程独占 |

profile 片段见 `docs/examples/trail-profile.fragment.json`。这是待合并的片段，不含模型/provider 配置，不能作为完整启动配置。操作员需设置示例中其他占位变量，并复用已核验的模型配置；不要写入凭据明文。loader 支持 `${VAR}`，接收器会解析 profile 的环境引用。

必须设置 tools.allowedTools 为五个 exact 包装名。仅设置 mcpServers.enabledTools 不能阻止其他内置/插件工具；allowedTools 在注册边界拒绝 shell、文件、其他 MCP 与插件，包括重连。默认 null 保持现有 profile 行为，[] 拒绝所有工具；更改须重启。不可将只读 profile 与其他用户共用。

示例命令（PowerShell，仓库根目录；先由受控环境注入上述变量）：

```powershell
.venv\Scripts\python.exe -m nanobot.integrations.trail.receiver --config "$env:TRAIL_PROFILE_PATH" --port 8091
```

TRAIL_PROFILE_PATH 是操作员用于命令的独立 profile 路径，不是集成配置字段。接收器仅监听 127.0.0.1，不启动 gateway，不改 profile，不要求模型。MCP 由现有 gateway 按 profile 启动独立 stdio 子进程：`python -m nanobot.integrations.trail.mcp_server`。

## 通知状态与可靠性

签名协议：HMAC-SHA256 原始 `POST\n/path\n<unix-seconds>\n<body>`；sha256= 头，偏差≤300s，事件年龄≤24h，请求≤32KiB。接收器持久化 FULL/WAL 后返回202，重复event_id+recipient同样202。SQLite上限10000行，终态保留7天，unknown保留待核对；磁盘/容量失败503，将投递责任保留给 Trail。

状态：received → checking → sending → sent；不再符合条件/撤权→cancelled；明确失败→failed；发送结果不明/发送期间停机→unknown。重启将 sending 变 unknown，checking 变 received。飞书 UUID 使用 event_id+recipient 的稳定哈希，不能只依赖远端 UUID 声称 exactly-once。

发送前调用实际 REST 读取最新工单与 reminder_status（含 subscription_id），订阅停用、授权撤销、闭单/交付、提醒过期均取消。模板展示编号、标题、原因、时间与 Trail链接，不调用模型。

SDK连接/读取timeout10s，发送整体20s；REST请求整体8s、connect2s、read5s、并发2、120req/min；工具完整12s。显式 HTTP429 与可重试读取失败最多3次，等待60/120秒±20% jitter；未知发送结果不自动重试。Trail webhook 自有重试，不叠加外层重试。事件超过24h停止发送。

## MCP 与 skills

五个工具：trail_projects、trail_search_issues、trail_issue、trail_weekly_report、trail_say_do。固定GET，无任意URL/SQL/Shell；项目双重 allowlist 与服务端授权；每次项目读取先调用新鲜授权检查，避免旧政策缓存。256KiB响应上限、16000字符结果上限、分页最多50，partial 显式标记，统计取服务端值。

工单正文/评论/附件不返回。标题等文本标记不可信，skills 禁止作为指令执行。三个 skills：trail-followup（跟进建议）、trail-risk-check（风险核查）、trail-report-reading（周报/Say-do）。输出含查询时间、范围、链接，区分事实/建议；离线和403不能解读为无风险。模型失败不影响通知模板。读取失败只返回安全错误码。

## 可观测与故障处理

GET /healthz：队列计数、最老待处理年龄、低基数接收结果；worker故障、unknown、积压≥300s 返回503。GET /metrics：trail_inbox_jobs{status}、trail_inbox_oldest_pending_seconds、trail_receiver_requests_total{outcome}；无用户/工单label。指标仅本机接收器，监控外部采集由部署者配置；没有自动宣称告警规则已上线。

建议停止扩展：验签失败持续出现、unknown>0、failed增长、队列年龄≥300s、Trail死信增长。检查 Trace/request 与 event_id 关联的结构化日志、Trail delivery_attempts；不要开启原始请求体或Token日志。当前 query 日志含outcome/duration，未引入全局模型/指标系统改造。

unknown 人工核对：先停用 Trail 订阅与接收器，备份 inbox.db 及 WAL/SHM；通过飞书消息记录核验 event_id对应UUID是否成功，记录关联message_id。无法证明未发送时保持unknown，**不能直接改成received补发**。证明成功后由受控维护流程登记sent/message_id；无法核验则保持unknown或登记failed人工后续跟进。当前无管理写API，不能把202视为消息成功。

回滚顺序：停用新订阅/MCP→停止专用接收器/profile→核对在途sending/unknown→恢复旧发送方→必要时回退二进制。保留SQLite、Trail0011 expand字段与审计，不执行数据库down。停用订阅仍保留项目/事件路由占位，删除旧订阅后才能更换身份范围。真实部署回滚演练待验收。

## 验证入口

```powershell
.venv\Scripts\ruff.exe check nanobot/integrations nanobot/config/schema.py nanobot/agent/tools/registry.py tests/test_trail_integration.py tests/test_trail_tool_boundary.py scripts/verify_trail_joint.py
.venv\Scripts\python.exe -m pytest tests/test_trail_integration.py tests/test_trail_tool_boundary.py -q --cov=nanobot.integrations.trail --cov-report=term-missing --cov-fail-under=75
```

扩大回归覆盖现有 registry/config/skills/MCP连接/调用/loop。既有 `tests/agent/test_mcp_reconnect_crash.py::test_mcp_reconnect_during_shutdown_does_not_crash` 在原始基线+mcp1.30也失败（5s reconnect event wait 超时）；不可扩大timeout或跳过断言伪造绿色。参见 TECH_DEBT。本轮跨仓库脚本 `scripts/verify_trail_joint.py` 只由 Trail TestNanobotJointHTTP 启动，不手动填业务账号。真实数据库/签名/stdio与飞书替身分别报告。

Python wheel验证使用已有 NANOBOT_SKIP_WEBUI_BUILD=1 跳过未改动的 WebUI；只证明Python插件及skills打包，不代表WebUI构建通过。

## 尚未验收

真实私聊三提醒、真实模型追问/注入、至少一次定时扫描、实际部署版本/profile/MCP配置、实际飞书UUID行为、回滚演练。owner：小沈/bot管理会话；deadline：试点启用前。没有主动读取现有私密bot配置或联系其他会话。
