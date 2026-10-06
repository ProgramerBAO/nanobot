# 2026-10-01 Grafana 三阶段全量评审 findings（降级模式）

> 评审协议：adversarial-reviewer skill。`.review/config.md` 不存在 → 按协议降级为
> 只读评审（未运行动态验证命令；运行级证据来自作者会话同日已执行的全量测试，
> 并按"测试全绿≠闭合"纪律对待）。Rxx 编号无基线，本批从 R01 起。

## 请求卡（从会话构造）

- **评审类型**：首次全量（增量于 origin/main 已推的阶段一，含本地阶段二/三两个 commit）
- **目标提交**：`cd9da0f` + `55eab03`（阶段一，已推）+ `d9737ff` + `aa21c88`（阶段二三，本地）
- **目标行为**：多平台 Grafana 连接管理（只读默认 + 写模式确认门 + 30 读/10 写工具面）
- **必须保护的不变量**：
  1. token 永不泄露（API 响应 / 审计 / 错误 / 卡片 / 会话历史）
  2. 写操作必须经人工确认，模型不可伪造；nonce 单次 + 参数指纹 + chat 绑定 + TTL
  3. facade 受管形状精确（读形态带 `--disable-write`；写形态无旗标 + enabled_tools ⊆ 目录 + confirm == 所选写集）
  4. 设置面 GET-only；敏感字段走结构化头不进 URL
  5. 热重载与 disabled 语义不破坏其它 MCP server
  6. 通用机制不含 Grafana 专属逻辑；agent 侧导入不触 reporting 环
  7. 默认形态（全不勾写）与阶段一行为零差异
- **权威契约**：AGENTS.md Grafana 条目、docs/PRODUCT.md 发布记录、用户批准的方案、
  mcp-grafana v1.6.2 源码事实（fetch 摘要，见待验证 V2）
- **验证证据**：作者会话同日全量（pytest 5663 通过/2 既有基线失败、vitest 759/759、
  build 过）；真实连接验收 NOT RUN（无 uvx/token）

---

## 1. 结论

**存在 1 项应修订（P1），无阻断项（P0）。** 确认门的核心安全属性
（nonce 不可伪造 / 单次 / 参数与会话绑定 / 双路径投递 / 属主校验）经静态对抗
核查全部成立；P1 是可用性级缺陷（写工具在 MCP 会话终止后失去自愈），
不影响安全边界。

## 2. Findings

### R01 · P1 · 确认门工具在 MCP 会话终止后失去自愈（双缺口）[静态确认]

- **位置**：
  - `nanobot/agent/tools/mcp.py:1377`（`_attach_reconnect_handlers`：`isinstance(tool, _MCPWrapperBase)` 过滤——注册表中的 `ConfirmGateWrapper` 不是 `_MCPWrapperBase` 子类，其内部的 base `MCPToolWrapper` 从未获得 `set_reconnect_handler`）
  - `nanobot/agent/tools/mcp.py:388`（`_refresh_session_after_termination`：`refreshed_session = getattr(refreshed_tool, "_session", None)`——重连回调返回注册表中的**门**，门没有 `_session` 属性 → 即使修复上一处，会话提取仍得 None）
- **契约依据**：阶段二不变量"每次写操作强制确认 + 执行可用"；`_refresh_terminated_server`（mcp.py:1381-1421）经 `connect_mcp_servers` 重注册时**会**重新套门（wrap_if_confirmed 在注册环内），因此重连后注册表是正确的——坏的只是触发重连的句柄与会话回填。
- **触发条件与最小交错**：mcp-grafana 子进程会话终止（idle 超时是真实常态——仓库自身有 `test_mcp_reconnect_after_session_timeout` 佐证）→ 此后对门控写工具的下一次调用：base 的 `_reconnect is None` → 不走 `_refresh_terminated_server` → 直接返回 `(MCP tool call failed: McpError)`。若该失败发生在**已确认的恢复执行**上，nonce 已在 `resolve()` 中被消费——用户必须重新触发整个确认流程。读工具（裸 wrapper，有句柄）同场景自动重连恢复。
- **影响**：写/读工具自愈不对称；确认后执行在会话已死时失败且 nonce 白耗。安全无损（未执行就失败），但可用性与"确认即执行"的可信度受损。
- **最小修复方向**：① `_attach_reconnect_handlers` 对非 `_MCPWrapperBase` 工具取 `getattr(tool, "_base", None)`，是则给 base 挂句柄；② `mcp.py:388` 的会话提取先解包（非 `_MCPWrapperBase` 时取 `getattr(refreshed_tool, "_base", None)` 再取 `_session`）。
- **回归验证**：假 base 连续两次抛 session-terminated 后成功——经门确认路径断言自愈且 nonce 只消费一次；以及断言 `_attach_reconnect_handlers` 后 `gate._base._reconnect` 非空。
- **作者侧最短复现**：单测内构造 `ConfirmGateWrapper(fake_base)`，给 fake base 预置"首次抛 Session terminated、重连后成功"行为，注册到 ToolRegistry 并走 `_attach_reconnect_handlers` 同形逻辑——当前会直接收到失败结果而非恢复。

### R02 · P2 · 重复确认卡（重试/无效 nonce/TTL 边缘）[静态确认]

- **位置**：`nanobot/agent/tools/mcp_confirm.py` `_invoke`（`_deliver_card` 每次投递新卡）。
- **触发**：模型同一轮反复调用同参数（pending 复用同一 nonce 但卡片重投）；用户在 600s 边缘点击（渠道放行、store 已过期）→ 恢复轮重发一张新卡；无效/已消费 nonce 同样落到发卡分支。
- **影响**：UX 噪音（多张相同卡），无安全后果（nonce 单次、参数绑定、渠道一次性消费按 interaction 独立）。
- **修复方向**：pending 未过期时不再重复投卡，工具结果改为"确认卡已发送（10 分钟内有效），请等待用户点击"；无效 nonce 分支可显式提示原因。
- **回归验证**：同一 pending 两次调用断言 `deliver_outbound` 只被调用一次。

### R03 · P2 · pending 与交互状态的参数无大小上限 [静态确认]

- **位置**：`mcp_confirm.py` `_PendingConfirmation.params`（全量保留以供执行回放）与飞书渠道交互 `option_values`（存完整 card action params）。
- **触发**：`update_dashboard` 的 `dashboard` 参数可达数百 KB；store 上限 128 条 pending，理论上 128 × 大负载的内存放大（模型上下文本身限制了实际上限，概率低但路径真实；`max_tool_result_chars` 只限输出不限输入，无上游兜底）。
- **影响**：网关内存放大；无越权。
- **修复方向**：`issue()` 前对序列化参数设上限（如 256KB），超限拒绝发卡并明确报错。
- **回归验证**：超大参数调用断言不发卡、返回明确错误。

### R04 · P2 · 投递失败回退时文案不诚实 [静态确认]

- **位置**：`mcp_confirm.py` `_invoke` 末段——`_deliver_card` 返回 False（无 message tool / 无回调，如 CLI 或部分测试形态）时回退为元数据 ToolResult，但文本仍断言"已发送确认卡片"。
- **影响**：无卡环境下用户/模型被误导以为卡已发出；写操作永远不会执行（安全无损）。
- **修复方向**：按投递结果动态生成文案（"该渠道无法投递确认卡，请在支持的渠道中发起"）。
- **回归验证**：无 message tool 的 registry 下断言文案不含"已发送"。

### R05 · P2 · 短 token 的 hint 泄露比例偏高 [静态确认]

- **位置**：`nanobot/webui/grafana_api.py` `_token_hint`——16–23 字符 token 显示 7+4=11 字符（最高约 69%）。真实 `glsa_` token 很长（约 11%），实际风险极低，但通用形态偏松。
- **修复方向**：`len(token) < 24` 时全掩码。
- **回归验证**：17 字符 token 断言 hint 为 `••••`。

## 3. 待验证项

- **V1 · WebUI 侧确认按钮的动作通路**：websocket 渠道的 DIRECT_TOOL 注入是
  `report_center` 硬编码（`nanobot/channels/websocket/runtime.py` ~790，
  `report_action_validated` 仅在该处设置），未找到通用 report_document 动作
  处理器——确认卡在 WebUI 聊天可渲染（websocket 渲染 agent_ui）但按钮可能无动作。
  影响：写确认实际仅飞书可用（用户主面是飞书，影响有限）。**核验**：真实 WebUI
  聊天触发一次写操作并点击确认按钮（或 grep websocket 渠道对 agent_ui action
  事件的分发）。
- **V2 · `--enable-write-tools` 无法恢复写类目的 1.6.2 结论**：来自 main.go 源码
  的 fetch 摘要，未在本机用真实 1.6.2 二进制验证（uvx 未装）。该前提若不成立
  （旗标实际可按名恢复），写模式去掉 `--disable-write` 属过度让步——方向是
  安全无损的（不影响正确性，只影响纵深）。**核验**：装 uv 后
  `uvx mcp-grafana@1.6.2 --disable-write --enable-write-tools=update_dashboard`
  观察 `update_dashboard` 是否注册。

## 4. 覆盖与盲区

**已核查（静态对抗）**：门的全攻击面（伪造/重放/过期/参数劫持/跨 chat/
并发 resolve 的锁序/指纹跨路径一致性——runner 与 registry 共用 `prepare_call`
已核实）；nonce 只存在于卡 action value 且飞书回调参数取自渠道侧状态（不回传
客户端参数）；`sender_open_id` 在飞书入站元数据中存在（确认卡可点击的前提）；
`exclusive` 语义由 runner `_partition_tool_batches` 兑现（非并发安全工具独占
批次）；LLM 路径投递不污染 agent 自身回复（门构造独立 metadata dict）；
`deliver_outbound` 不设 `_sent_in_turn`（agent 最终文本仍外发，行为正确）；
facade 写模式矩阵（受管分类、CSV/JSON 双形、双向切换、toggle 保写集、测试
动作写形态、审计含 write_tools）；schema 降级兼容（Base `extra="allow"`，
回滚到旧代码读新 config 不炸）；mcp_presets 过滤、reload/disabled 语义（有
测试）；前端状态机与提交语义（editor 保留语义、失败不关编辑器、toggle 只发
enabled、checkbox 状态、canSave 条件）；i18n 形状对齐（13/13 测试）。

**盲区/边界**：真实飞书端到端（确认卡点击→执行→回传→审计两行）未执行
（无 uvx/token，NOT RUN）；面板 PNG 渲染依赖平台 image renderer 未实测；
2 个既有 pytest 基线失败（feishu instances payload、channel locale 契约）
非本弧引入（阶段一已 stash 干净 HEAD 实证），未计入 findings；Mimosa git
门禁 warn 报告的 12 条 high 为已登记 TD-20260919-001 存量，非本弧。

## 5. 验证声明

本轮为**静态评审**（只读降级模式，未运行任何命令；行号与代码证据来自当前
工作树 = d9737ff + aa21c88）。运行级证据（pytest 5663 过/2 既有、vitest
759/759、build 过）来自作者会话同日执行，本轮按证据纪律引用但未重跑。
所有 P1/P2 的"实际行为"均有 file:line 或精确符号支撑；两项待验证均已给出
最短核验路径。

## 6. 处置记录（2026-10-06 修复批次，用户指令"修复全部"）

| ID | 处置 | 修复要点 | 证据 |
| --- | --- | --- | --- |
| R01 | **已修复** | `_attach_reconnect_handlers` 对非 `_MCPWrapperBase` 工具解包 `_base` 挂句柄；`_refresh_session_after_termination` 会话提取先解包门再取 `_session` | 新回归 ×2（`test_reconnect_handler_reaches_gated_base`、`test_refresh_session_extracts_session_from_gated_tool`）随套件通过；受影响面 324 passed |
| R02 | **已修复** | `issue()` 返回 `(pending, created)`；未新建时返回"确认卡已发送且仍在有效期"纯文本，不重投卡、不重复审计 | `test_identical_retry_reuses_pending_without_duplicate_card`（重写）通过 |
| R03 | **已修复** | 发卡前对参数 JSON 序列化校验 + 256KB 上限，超限/不可序列化拒绝且不入 pending | `test_oversized_params_are_rejected_without_issuing_a_card`、`test_non_serializable_params_are_rejected` 通过 |
| R04 | **已修复** | LLM 路径投递失败分支文案改为"无法投递确认卡片…请在支持卡片的渠道发起"，文档仍随元数据尽力携带 | `test_no_delivery_environment_reports_honestly` 通过 |
| R05 | **已修复** | `_token_hint` 阈值 16→24（短值全掩码）；凭据模块补 `GRAFANA_SA_LONG`；hint 用例改用长值并新增短值掩码用例 | `test_payload_redacts_tokens_and_marks_unmanaged_entries`（改）+ `test_token_hint_masks_short_values_fully`（新）通过 |

**修复批次验证**（工作目录 D:\Code\bots\nanobot）：
- `pytest tests/agent/tools/test_mcp_confirm.py tests/webui/test_grafana_api.py tests/agent/test_mcp_connection.py tests/tools/test_mcp_tool.py tests/webui -q` → exit 0，**324 passed**
- `ruff check nanobot/ tests/` → All checks passed；`compileall -q nanobot tests` → OK
- `pytest -q` 全量 → **5668 passed / 45 skipped / 3 failed**：其中 2 个为已实证既有基线（feishu instances payload、channel locale 契约，非本弧）；`test_mcp_reconnect_after_session_timeout` 为已记录满载抖动——**修复后隔离复跑 2/2 通过、与门套件交叉 23/23 通过**，非 R01 回归
- 前端零改动（git status 确认无 webui 源文件变更；R05 的 hint 为服务端计算）

**V1 / V2 处置（2026-10-06 第二批，同日随"继续"完成）：**

- **V1 已实现闭环（原为待验证 → 查证为确认缺口 → 实现）**：查证结论——WebUI 的
  `ReportActions` 只认 `command` 字段与两个硬编码 id，websocket 渠道的直呼注入
  仅 `report_center` 硬编码——确认卡按钮（及订阅确认卡按钮）在 WebUI 是死按钮。
  实现（加法式，双端）：websocket runtime 在出站 agent_ui 唯一 choke point
  （原 :948）把带 `tool_name`+`params` 的 actions 改写为**不透明单次 token**
  （服务端 `_WebCardInteraction` 状态，600s TTL、256 上限、chat 绑定、单次消费），
  参数永不作为可执行输入上线；新增 `card_action` 入站事件 → 解析 token → 复用
  `report_action_validated` 直呼通道重入（镜像既有 `_dispatch_report_action`
  范式）。前端：`sendCardAction` + `webui_token` 按钮分支（danger 样式）经
  ThreadShell→Viewport→Messages→Bubble→ReportDocumentView 全链穿线。副作用收益：
  WebUI 里订阅确认卡的死按钮同时被激活（与飞书语义一致，直接经 report_center
  的 trusted_direct + RBAC）。测试：后端 6 用例（改写剥离参数/透传非目标
  blob/单次+chat 绑定/直呼重入/重放与畸形拒绝/跨 chat 拒绝）+ 前端 1 用例
  （点击只回传 token）。
- **V2 静态加固完成（后补：二进制实证）**：v1.6.2 `tools/alerting.go` 直接确认
  `AddAlertingTools(mcp, enableWriteTools bool)` 类目布尔门（rules/silences 按
  布尔注册读版或读写版）——与 main.go toolEntries 的证据互相印证，结论从
  "fetch 摘要" 升级为**双重源码确认**。**2026-10-06 装 uv 后以真实二进制
  `uvx mcp-grafana@1.6.2 --help`（exit 0）三重实证**：`--enable-write-tools`
  自述"仅恢复写行为受控可独立放行的工具（例举 sift 两件），对整类目被禁用
  的工具无效"——写模式去掉 `--disable-write` 的架构决策闭合。新精确化注记：
  `ManageRouting` 在 1.6.2 **无条件注册**（routing 写操作不受旗标移除）——
  nanobot 白名单在只读模式从不注册该工具（无暴露），写模式下照常受确认门；
  已同步 grafana_api 注释。
