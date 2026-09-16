# 报表平台全量逻辑评审与收敛方案

评审日期：2026-09-16。代码锚点：`main@7e3e9ab`（下文所有 file:line 均以该提交为准，后续阶段落地后行号会漂移，以提交号为准）。

本文档是报表平台（Cube 报表编排 + 报表中心工具 + 订阅链路 + WebUI 管理面 + 遗留 Magik 兼容路径）的全量逻辑评审底稿，同时作为已批准收敛方案的阶段状态跟踪表。评审方式：三路并行代码探索（后端开关体系 / 订阅-NLU-Cron 链路 / WebUI 管理面）+ 关键结论行级复核。

## 1. 全景逻辑图（现状）

```text
聊天渠道（Feishu/WebSocket）
  │  入站消息
  ▼
AgentLoop._state_command（agent/loop.py:1502）
  │  ① 订阅候选门控：词法信号正则（agent/reporting/cube_subscription_intent.py:38-44）
  │     ⊕ 分类器资格（report_center.is_direct_intent_candidate，需 cube_subscription + NLU v2/v3）
  │  ② 引用卡片分支：classify_referenced_subscription（report_center.py:1110）
  │  ③ 直连 NL 分支：classify_direct_request → 确定性解析 or 一次 schema-forced LLM
  │  ④ 直达兜底：fixed_cube_reports_enabled=False 时回落 magik_cube_daily_report 旧工具
  ▼
ReportCenterTool（agent/tools/report_center.py，5356 行）
  │  match_direct_request（固定问法路由，不查 flag）
  │  → action 分发 execute()（:5052）
  │     ├── 手动报表 _run_*（家族 flag 门控 + RBAC + 模板策略）
  │     ├── subscription_preview（:3140，573 行）→ 确认卡
  │     └── subscribe（:4621）→ ReportSubscriptionService 或遗留内联建 cron
  ▼
编排层  ReportPluginRegistry（reporting/builtins.py:build_default_registry）
  │  × ReportRunner（reporting/runner.py，_authorize 查 RBAC + 模板策略）
  │  × 模板（cube.py / builtins.py / provider_quality.py）
  ▼
数据层  CubeConnector（reporting/cube.py）+ MagikCubeClient（agent/tools/magik_cube.py）
  │      ReportStateStore（reporting/store.py，SQLite/Postgres：
  │      订阅/策略/flag 覆盖/审计/引用/运行与投递记录）
  ▼
投递层  CronService → bound_runner → run_subscription（report_center.py:4968）
  │      → ReportRunner 或遗留 magik 执行 → OutboundMessage → ChannelManager（幂等投递记录）
  ▼
管理面  WebUI Report platform（4 页签）→ /api/settings/reporting*
        （nanobot/webui/reporting_api.py + settings_routes.py）
```

## 2. 开关与控制面现状（评审核心发现）

### 2.1 一个报表模板"能不能用"由 4 层 AND 决定

| 层 | 判定内容 | 位置 |
| --- | --- | --- |
| 注册层 | 构造期：连接器存在 + 构造级 config（2026-09-15 起 enable flag 不再门控注册） | builtins.py:1634-1730 |
| 执行层 | 家族运行时 flag（`_run_*` 入口）× 模板策略 enabled（runner `_authorize`，仅 `report_management_v1` 开启时执行） | report_center.py 各 `_run_*`；runner.py:60-63 |
| 可见层 | 家族 flag × `_template_enabled`（策略）× lifecycle ∈ {publish,canary} | capabilities.py:34-42,183 |
| 订阅层 | lifecycle × policy.enabled × subscription_mode × 订阅类 flag | subscriptions.py:289-334 |

### 2.2 一份订阅"会不会发"有 4 个互不知情的开关

cron job.enabled（cron/service.py:516）× 订阅行 enabled（report_center.py:4974）× 模板策略 enabled（:4976-4979 + runner.py:60-63）× 家族 flag（:4993-4998）。另有 subscription_mode=disabled（只挡新建）与 show_subscription_button（只藏按钮）两个辅助开关。没有任何一处能回答"这条订阅为什么没发"。

### 2.3 开关体系是三层叠加 + 双源默认

config 字段（`ReportCenterToolConfig`，report_center.py:251-341，19 个字段与 runtime flag 同名）→ store 覆盖表（`report_feature_flags`，store.py:223-228,513-570）→ 有效值 = 覆盖 > 默认（feature_flags.py:110-126；report_center.py:1534-1546 `_flag`；:510-526 `_EffectiveFlagConfigView` 三份等价实现）。

### 2.4 用户疑问的直接回答：报表类型页签"报表启用" vs 功能开关页

- 报表启用 = `report_template_policies.enabled`（CAS revision + 审计，reporting_api.py:821-848 → store.py:337-420）。
- 功能开关页 19 项 = `report_feature_flags` 覆盖（即时生效、无 CAS、审计）。
- **重叠区（成立）**：8 项"每模板家族 flag"与模板一一对应，两层都能关掉同一个报表且互不感知——可见层要求 flag 开 且 policy 开（capabilities.py:71-77），执行层 flag 在 `_run_*` 查、policy 在 runner 再查（report_center.py:1609 / runner.py:60-63）。
- **非重叠区**：功能开关还承载路由行为（NLU、引用订阅、订阅机制、简报默认路由、灵活查询帮助、管理 UI）；模板策略承载 subscription_mode / show_subscription_button 粒度。
- **加重因素**：`report_management_v1` 本身是功能开关页的一项，关闭时整个策略层停止执行（subscriptions.py:312-314；8 处 `template_policy_enforced` 构造点），但报表类型页签照常显示策略值。

## 3. 问题清单

### P0（语义混乱 / 正确性风险）

| # | 问题 | 证据 |
| --- | --- | --- |
| P0-1 | 双开关重叠 + `report_management_v1` 元开关语义（见 2.4） | capabilities.py:71-77；runner.py:60-63；subscriptions.py:312-314 |
| P0-2 | cost 家族脱离运行时开关：同一表达式 cost 读 config、其他家族读 `_flag()`；管理页对 cost 无效 | report_center.py:4996；注册/执行全 config 级（:1577-1582,304-307；builtins.py:1705-1710） |
| P0-3 | 订阅创建三入口三种 fingerprint，`(channel,user_id,fingerprint)` 唯一约束跨入口判重失效 | subscriptions.py:756-771 vs report_center.py:4787-4793 vs reporting_api.py:1023-1026；store.py:167 |
| P0-4 | 聊天卡订阅启停按钮不带 revision → 走无 CAS 遗留分支，与 CAS 分支并存 | capabilities.py:505-519 vs report_center.py:5326-5334 |
| P0-5 | 文档漂移：PRODUCT.md / report-management.md 写多个 flag"默认关闭"，代码已默认全开（已于 Phase 0 纠正） | 原 PRODUCT.md:30-34、report-management.md:7,72（2026-09-16 已修） |

### P1（重复实现 / 漂移风险）

| # | 问题 | 证据 |
| --- | --- | --- |
| P1-1 | 枚举多点复制：recurrence 5 处（前端 types.ts:988 缺 hourly）；report_type/模板映射 6+ 处；scope 白名单 4 处键集互有差异；"全部客户/模型"正则多处；hourly 窗口计算 3 处；tenant_models 重建 3 处；cron 反解析 3 处 | cube_subscription_intent.py:32,55-57；report_center.py:400-411,3185-3192,3260-3267；schedules.py:7-35；store.py:21-42；subscriptions.py:39-71；reporting_api.py:38-46；types.ts:988 |
| P1-2 | registry 构造 4 份参数不一致（Gateway/WebUI/Feishu onboarding/CLI），且 builtins 默认值 wecom/dingtalk=True 与 config 默认 False 相反 | report_center.py:550-569；reporting_api.py:292-329；runtime.py:5132-5152；cli/commands.py:2531-2551；builtins.py:1649-1650 |
| P1-3 | 校验重复：模型目录校验 5 层、租户目录 6 层、RBAC 3 层、模板策略 3+ 层（`_subscription_policy_denial` 与 `_check_template_policy` 逐条复刻） | report_center.py:2987-3040 vs subscriptions.py:289-334；`machine_tpm_peak` 默认不可订阅集合硬编码 5 处（subscriptions.py:315；report_center.py:3027；reporting_api.py:507,648,992） |
| P1-4 | WebUI 死面：REST 订阅路由零消费者（settings_routes.py:254-280）；`subscription_schedule` 死 action；legacy `subscription_create` 无前端调用（reporting_api.py:980-1098）；action 联合类型两处漂移（ReportsSettings.tsx:38-53 15 项 vs api.ts:644-661 16 项）+ 第三份 guidedActions（api.ts:666-673） | 同左 |
| P1-5 | 死配置/死枚举：`cube_health_connector`/`cube_health_template` 零读取；`cube_provider_quality_connector`/`_template` 仅 onboarding 文案读（runtime.py:5167-5168）；lifecycle canary/draft/deprecated/rollback 全员 publish；`cube_subscription_nlu_v3` 无独立行为（恒 `v2 or v3`） | report_center.py:275-276,311-312；registry.py:50-55；report_center.py:622,1122 |
| P1-6 | 管理面安全/审计缺口：`rbac`/`grant`/`revoke` 不写审计表；操作者恒 `webui_admin`；全部写 action 实际走 GET；管理页每请求重读磁盘 config 而 Gateway 用启动时 config（"默认值"展示可能失真） | reporting_api.py:807-820,64-75；api.ts:686-690；report_center.py:542 |
| P1-7 | 三注册表耦合（NLU 词表 ↔ tool schema ↔ preview 编译器），历史三起事故均源于此（"每个小时"门控漏配 / recurrence=hourly 被 schema 拒 / 引用跨租户校验），已固化回归测试 | test_cube_subscription_intent.py:93-136；test_report_center.py:354-387,390-400 |

### P2（体验 / 结构）

| # | 问题 | 证据 |
| --- | --- | --- |
| P2-1 | zh-CN 缺 `settings.nav.reports` 键（中文界面显示英文 "Reports"）；ReportsSettings 全页硬编码中文未接 i18n | common.json settings.nav；ReportsSettings.tsx |
| P2-2 | 两套 home 文档：report_center home 传 8 个开关 vs Feishu onboarding home 只传 2 个，同一用户看到不同能力集 | report_center.py:5094-5109 vs runtime.py:5159-5170 |
| P2-3 | Grafana `cost_summary`/`capacity_summary` 模板注册但无执行入口（目录可见的死模板）；health_sre(grafana 版) 仅最小部署注册且 metrics 不匹配 | business_templates.py:44,163-200 |
| P2-4 | 巨型文件/函数：report_center.py 5356 行（类占 90%）、`_subscription_preview` 573 行、feishu/runtime.py 5239 行、SettingsView.tsx 9821 行、`reporting_settings_action` 512 行 | 同左 |
| P2-5 | 遗留路径存活面广：magik 旧工具仍是选择器默认 resume 目标、full 模板执行、订阅兜底执行、WebUI 目录解析器；loop 直达兜底 | runtime.py:4565,4609,4629；report_center.py:1964-1976,5019-5021；loop.py:1618-1625 |

## 4. 目标架构（已批准）

1. **模板策略 = 每模板唯一运行时开关**：`report_template_policies` 管 enabled（执行+可见+新建订阅）与 subscription_mode、show_subscription_button；**始终执行**，`report_management_v1` 退化为管理页/引导表单可见性开关，不再门控策略执行。
2. **功能开关 = 纯行为/路由开关**（约 10 项）：`cube_subscription`、`cube_subscription_nlu_v2`（v3 删除）、`cube_report_reference_subscription`、`cube_usage_brief_default`、`cube_admin_skill_help`、`report_management_v1`、`report_subscription_guided_ui`、`report_subscription_button_policy`、cost 报告/订阅两项（从 config 级并入）。删除 8 项每模板家族 flag 与 3 项订阅类 flag（语义迁入 policy：enabled / subscription_mode）。
3. **订阅创建/启停单一入口**：全部走 `ReportSubscriptionService`（统一 fingerprint、CAS revision、审计）；聊天卡按钮带 revision。
4. **单一来源**：registry 构造 kwargs、recurrence/report_type/模板映射枚举、策略判定核心、hourly 窗口与 tenant_models 重建各收敛为一处；schema enum 由注册表生成。
5. **死面清除**：REST 订阅路由、legacy `subscription_create`、`subscription_schedule` 死 action、死配置字段、Grafana 死模板、`cube_subscription_nlu_v3`。

## 5. 阶段计划与状态

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 0 | 文档纠偏 + 本评审落盘（PRODUCT.md / report-management.md / WORK_CONTEXT.md） | ✅ 2026-09-16 完成 |
| Phase 1 | 无行为变化收敛：registry kwargs 单一来源、枚举单源、校验共享、WebUI 类型对齐、死配置删除；focused 套件全绿 | ✅ 2026-09-16 完成（157 passed 含 3 个新契约测试；channels 213 passed/2 既有失败；WebUI 746 全过、3 个并发超时单跑复核通过；eslint/tsc build 通过） |
| Phase 2 | 开关合并：2a 策略恒执行 → 2b 迁移脚本（幂等 expand-only）→ 2c cost 并入运行时体系 → 2d 读取点切换 + 注册表收缩 + WebUI 收缩；门控矩阵测试 | ✅ 2026-09-16 完成（2a `9a7112b`、2b `828ab7a`+`42a2040`、2c `de4ccd2`、2d `5d2c383`；focused 171 全绿含门控矩阵/迁移契约测试、channels 基线不变、WebUI 746 全过） |
| Phase 3 | 订阅链路收敛：创建/启停单入口、fingerprint 统一、聊天卡 revision、删除 REST/legacy/死 action、审计补齐、写 action 改 POST；等价性测试 | ✅ 2026-09-16 完成（3a `1881e79`、3b `deba21d`、3c-3e `4200bdf`；focused 177 全绿含 hourly service 化 + 跨入口判重 + 重复扫描测试、WebUI 746/eslint/tsc 全过） |
| Phase 4 | 管理面一致性：默认值与 Gateway 同源、构造级开关只读展示、home 统一、Grafana 死模板移除、i18n 补齐、文档终态重写 | ✅ 2026-09-16 完成（ReportsSettings 页面文案完整 i18n 抽取另行排期；Python 455 全绿含 websocket 路由树、WebUI 746/eslint/tsc 全过） |
| Phase 5 | 结构性拆分（独立排期）：report_center.py 分层拆模块、`_subscription_preview` 拆分、SettingsView.tsx 报表区块独立 | ◐ 2026-09-16 部分完成（`4760c00`：report_center 5,352 行 → 8 文件职责包，纯移动+3 处最小编辑，行为不变；验证全绿含真实网关注册与页面渲染。**遗留**：`_subscription_preview`（573 行）整体移入 subscription_flow 未内部分解；SettingsView 报表区块确认已独立（ReportsSettings.tsx），其完整 i18n 抽取与 preview 分解另行排期） |

每阶段独立 commit、独立可发布、可回滚。Phase 2 迁移 expand-only（写 policy 行、不删 flag 覆盖行），store 备份后执行，旧覆盖行在验证窗口后清理。

### Phase 1 落地记录（2026-09-16）

- 新单一来源：`default_registry_kwargs`（builtins.py，5 个构造点统一，wecom/dingtalk 默认值对齐 config=False）；schedules.py 新增 `SUBSCRIPTION_RECURRENCES`/`DAILY_MODES`/`PERIOD_TEMPLATES`/`BRIEF_PERIOD_TEMPLATES`/`SUBSCRIPTION_REPORT_TYPE_TABLE`/`SUBSCRIPTION_REPORT_TYPE_ENUM`/`RECURRENCE_SCHEDULE_PERIODS`；`evaluate_subscription_policy` + `DEFAULT_UNSUBSCRIBABLE_TEMPLATES`（subscriptions.py）；`EXPLICIT_ALL_TENANTS_RE`/`EXPLICIT_ALL_MODELS_RE`（cube_subscription_intent.py）；RC 内 `_previous_complete_hour`、`_normalized_subscription_scope` 两个 helper。
- 派生化：intent 模块校验集改 `get_args`；RC schema 的 recurrence/report_type/daily_mode 枚举、预览 safe_report_types、引用模板白名单全部改为引用共享表；subscriptions `_recurrence`/`_schedule_period` 改共享表。
- 删除：4 个死配置字段（`cube_health_connector`/`cube_health_template`/`cube_provider_quality_connector`/`cube_provider_quality_template`，onboarding 改读 registry 实态）；前端 15 项 action 联合类型改由 api.ts 导出的 `ReportingSettingsAction` 派生；types.ts recurrence 补 `hourly`（纯类型，编辑器选项仍硬编码不含 hourly）。
- 有意保留（非遗漏）：`authorization.py` 的 legacy 参数→模板映射（Phase 3 处理 legacy 路径时一并收敛）；magik 工具自有的更宽"各大/大客户"正则（legacy 语义不同）；`_subscription_preview` inherit 分支的 `reference_selection_models`（条件含 `item.get("models")` 非空检查，与 hourly 重建不同构，强行合并会改行为）。
- 新增契约测试：`tests/reporting/test_subscription_enum_contracts.py` 钉住 schedules 常量 = intent Literal = tool schema 枚举。

### Phase 2 落地记录（2026-09-16）

- **2a（`9a7112b`）**：模板策略始终执行——ReportRunner 移除 `template_policy_enforced` 参数恒查策略；`_subscription_policy_denial`、`_run_subscription`、`ReportSubscriptionService._check_template_policy`、capability catalog 全部移除 management 前置；`report_management_v1` 只剩管理页/引导表单可见性与 home 帮助文案职责。machine_tpm_peak 默认不可订阅不再依赖 management 开关（新增回归测试钉住）。
- **2b（`828ab7a`+`42a2040`）**：`nanobot reports policy migrate-flags [--dry-run]`（`nanobot/reporting/flag_migration.py`）幂等 expand-only 转换 store 覆盖与配置级非默认值；管理员既有 policy 行永远优先；旧 `report_feature_flags` 覆盖行保留观察窗口。附带修复既有缺陷：`ToolsConfig` 前向引用在特定导入顺序下未解析导致裸构造 AgentLoop 崩溃（schema 增加 `ensure_tool_config_resolved()` 惰性解析，loop 构造前调用——WORK_CONTEXT 记载的"被 deselect 的既有失败"根因）。
- **2c（`de4ccd2`）**：`cube_cost_report`/`cube_cost_subscription` 加入运行时注册表（成本报表组），RC 三处读取统一 `_flag()`；TokenAPI 连接注册保持 config 级。
- **2d（`5d2c383`）**：注册表 21→10（删 7 项每模板家族 + 3 项订阅类 + 无独立行为的 nlu_v3）；执行/预览/订阅/编译/可见层全部改读 `template_enabled`（capabilities 公共单源）；FSRUN onboarding 改 registry 派生；被退役 config 字段保留一个迁移窗口并记录警告；迁移 CLI 同时转换配置级非默认值。新增门控矩阵测试（enabled×subscription_mode×可见/创建）与迁移默认值契约测试。
- 行为变化声明：management 关闭时策略不再被绕过（此前依赖"关=放行"语义的部署需按上述语义评估）；每模板家族默认从 flag 默认值变为 policy 默认值（无行=启用），原有非默认状态由迁移 CLI 承接。

### Phase 3 落地记录（2026-09-16）

- **3a（`1881e79`）**：hourly 聊天确认改走 `ReportSubscriptionService`（compile_form 增加 hourly 模板分支：variant/report_template_id 标记 + brief 模板 + `5 * * * *` 调度）；`subscription_fingerprint` 单一身份（channel/chat_id/user_id/template_id/schedule/timezone/params），service 与剩余 legacy 内联路径共用，`UNIQUE(channel,user_id,fingerprint)` 首次具备跨入口判重能力；`authorize_magik_params` 将 hourly variant 映射到自身模板授权（原误落 usage_daily_brief）。
- **3b（`deba21d`）**：订阅卡启停/删除按钮携带 revision（action_id 追加 `:{revision}`，Feishu 解析器透传）；RC 与 WebUI 的无 CAS 内联分支删除——文本命令由服务端解析当前 revision 走同一 service 路径；WebUI 启停/删除缺 revision 返回 400（breaking）。
- **3c-3e（`4200bdf`）**：删除 REST 订阅路由（仅保留被前端实际消费的 options 路径）、legacy `subscription_create`、无调用方的 `subscription_schedule`、前端死联合成员与两个退役 helper；`rbac`/`grant`/`revoke` 补管理审计；`_run_subscription` 的 Cube-vs-legacy 判定成为唯一文档化决策点（注明删除条件）；新增 `nanobot reports policy scan-duplicates` 报告统一指纹前遗留的语义重复订阅（仅报告）。
- **追记（POST 化回退）**：3d 曾将 WebUI 报表 action 改为 POST，但嵌入式 WebUI 传输层（websockets 15.0.1 的 HTTP/1.1 解析器，http11.py 仅接受 GET）在路由前即断开非 GET 连接（浏览器 "Failed to fetch"；渠道 API 测试中 "not.toHaveBeenCalledWith method POST" 的既有断言本已编码了这一约束）——已回退为 GET 并在 api.ts 留下禁止改回 POST 的注释。教训已记入验证基线：涉及传输层行为的前端改造必须对运行中的网关做真实请求验证（GET/POST 各一次），单测 mock fetch 与路由层测试都不覆盖传输层。
- 测试：hourly service 化 e2e（模板标记、调度、跨入口 409 判重 + Cron 补偿）、WebUI 缺 revision 400、重复扫描分组语义。

### Phase 4 追记（2026-09-16，重启后故障修复）

`e11a34e` 的同源接线存在一个部署期缺陷：`GatewayHTTPHandler.config` 实为
WebSocket 渠道节（`WebSocketConfig`，无 `tools` 属性），被直接当作 startup 句柄
传入报表设置 API，导致重启后所有报表设置 action 报 500（"reporting settings
action failed"）——测试未覆盖真实 manager 接线，进程内探针用的又是根配置。
`1be841e` 修复：ChannelManager 经 `build_gateway_services`/`GatewayHTTPHandler`
显式传递已解析根配置；`_load_reporting_config` 对错误形状句柄回退为全新加载
并记录告警。两个回归测试钉住（services 接线保持根句柄、错误形状回退）；
真实机器配置进程内验证双路径通过；Gateway 已重启加载修复（双端口健康 200、
未鉴权设置端点探活 401 而非 500）。

## 6. 验证基线（每阶段通用）

- Python focused：`tests/reporting` + `tests/tools/test_report_center.py` + `tests/tools/test_cube_subscription_intent.py` + `tests/webui/test_reporting_api.py`（Phase 0 时基线 281 passed, 1 skipped）+ 各阶段新增回归；`ruff check nanobot/`、`compileall`、`git diff --check`。
- WebUI：vitest（基线 50 files / 746 tests）+ 生产构建（Windows 下用 PowerShell/npx）。
- 已知无关失败不纳入门禁：tests/channels 两个 feishu 既有失败（quoteGroupReplies locale 缺失、instances payload 断言，HEAD 上即失败，另行任务）。
- 涉及投递/页面的最终验收由本人在飞书/WebUI 执行（惯例）。

## 7. 非目标

不动 Cube 契约与数据口径（小时 TPM 双来源机器数语义等）；不动投递幂等/重试语义；不动 RBAC 权限模型本身；不激活 Grafana/WeCom/DingTalk；不重写 ReportDocument 渲染层；不做 cost/容量新模板；不处理 tests/channels 两个既有 feishu 失败。
