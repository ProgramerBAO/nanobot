# 公司级报表机器人

> 面向公司内部 AI 业务运营的产品文档
>
> 文档状态：内部讨论稿  |  当前基线：Magik Cube + Feishu  |  版本：0.1  |  日期：2026-08-26

## 1. 产品定位

公司需要的不是泛化聊天机器人，而是一套能够持续沉淀业务口径的报表产品。第一阶段围绕 AI 用量和模型运营数据建设，先把 Magik Cube 的查询、比较、分析和 Feishu 交付做稳定，再抽象为可接入多个数据平台的产品能力。

### 产品目标

- 让业务人员不需要记接口、字段和日期计算方式，就能获取日报、周报、月报和区间对比。
- 让相同业务问法得到相同口径、相同结构和可复核的数值，减少临时人工整理。
- 让新数据平台通过 Connector Plugin 接入，让新报表通过 Template Pack 管理。
- 把权限、审计、失败状态、订阅和版本纳入产品生命周期。

### 明确边界

固定报表链路默认 `0 LLM`。LLM 只用于未命中固定规则的新问法分类，不能计算指标、改变日期口径或绕过权限。

## 2. 业务问题与使用对象

### 当前问题

| 问题 | 具体表现 | 产品应对 |
| --- | --- | --- |
| 查询门槛高 | 需要知道数据平台、tenant、模型名称和接口参数 | 自然语言意图路由 + 客户/模型目录 |
| 口径不稳定 | 不同人使用不同时间范围、比较周期和聚合方式 | 固定日期规划器 + 版本化模板 |
| 结果太重 | 完整文本和矩阵包含大量明细，不利于快速浏览 | 默认简报，矩阵卡和完整报表作为进一步分析 |
| 重复劳动 | 日报、周报需要反复查询和整理 | 固定模板 + 定时订阅 + 幂等投递 |
| 扩展困难 | 新平台接入容易复制专属代码 | Connector、Template、Channel 三个扩展面 |

### 主要使用对象

| 角色 | 核心任务 | 首要诉求 |
| --- | --- | --- |
| 业务负责人 | 查看客户/业务线用量趋势和模型结构 | 少输入、结论先行、跨周期可比 |
| 研发/模型运营 | 定位模型用量变化、异常和新增/停用模型 | 模型维度、日级变化、数据质量标识 |
| SRE/平台管理员 | 维护连接、权限、模板、订阅和故障 | 可审计、可回滚、可观测 |
| 产品/数据同事 | 提出新报表需求并沉淀为模板 | 模板版本化、fixture 预览、明确验收口径 |

## 3. 产品形态与核心流程

所有入口最终进入同一套 Report Runner，保证导航卡、自然语言和定时订阅使用相同的查询与分析逻辑。

```text
Feishu 消息/卡片
    -> Intent Router
    -> RBAC
    -> Template Planner
    -> Connector Query
    -> Deterministic Analyzer
    -> ReportDocument
    -> Feishu 卡片或文本
```

### 用户入口

| 入口 | 行为 | LLM |
| --- | --- | --- |
| 明确自然语言 | 例如 `tencent_token_hub所有模型的周报`，直接构造完整 Intent 并执行 | 0 次 |
| 报表中心卡片 | 首次私聊或发送“帮助/菜单/报表中心”后选择功能 | 0 次 |
| 定时订阅 | Cron 到期后直接调用 Report Runner | 0 次 |
| 未固化新问法 | 只做结构化分类，槽位不完整时补充选择 | 最多 1 次 |
| 深度分析 | 先取一次确定性摘要，再让 LLM 解释原因和建议 | 最多 1 次分析 |

### 首次使用体验

1. 用户第一次进入 Feishu 私聊时，按 `user_id + channel + onboarding_version` 判断是否已经展示过报表中心。
2. 已授权用户看到可用功能；未授权用户只看到申请权限入口，不泄露 tenant、模型或数据源名称。
3. 用户可以选择日报、周报、月报、区间对比、最近报表和我的订阅。
4. 用户也可以跳过首页，直接发送明确的报表问法。
5. 卡片操作使用 opaque interaction nonce 绑定发起人和有效期，重复提交不会重复执行。

简报标题会显示已验证的客户展示名和模型范围，例如“佛跳墙 Kimi-K3模型日报简报”；多个模型统一显示“全部模型”。所有用量简报隐藏时间桶、桶数量和 interval；详细分析和健康诊断仍保留必要的趋势口径。结果卡不再提供订阅按钮，已有订阅继续执行，新建和修改订阅统一进入 WebUI `Report platform`。订阅保存实时 catalog 验证后的真实 `tenantId`。

订阅管理卡片按编号逐条展示计划，启停按钮位于对应订阅信息下方并携带相同编号。启停操作通过服务端 opaque action 或受控 WebUI 命令执行，完成后返回最新订阅状态；卡片不提供无确认的删除操作。

## 4. 固定报表产品定义

统一使用 `Asia/Shanghai` 时区。

| 模板 | 默认周期 | 核心内容 | 默认输出 |
| --- | --- | --- | --- |
| 日报简报 | 默认昨天；同比为上周同期，环比为前一日 | Token、请求数、单 Endpoint 平均 TPM、最高 Endpoint 峰值 TPM | 简报（默认） |
| 周报简报 | 上周 vs 上上周 | Token、请求数、平均 TPM、最高 Endpoint 峰值 TPM | 简报（默认） |
| 月报简报 | 上月 vs 前一自然月 | Token、请求数、平均 TPM、最高 Endpoint 峰值 TPM | 简报（默认） |
| 区间简报 | 自定义窗口 vs 前一等长窗口 | Token、请求数、平均 TPM、最高 Endpoint 峰值 TPM | 简报（默认） |
| 多客户多模型日报简报 | 昨日；同比 D-7、环比 D-1 | 按客户分组的模型 Token 百分比变化；全部模型模式过滤三个窗口均无用量的模型 | 独立简报，默认关闭 |
| 单机折算 TPM 峰值 | 日/周/区间 | 集群、卡型、峰值时间、折算 TPM、机器/GPU 数和质量 | 明细表，默认关闭 |
| 详细分析 | 继承对应周期和范围 | 模型/客户排行、分段趋势、Endpoint 明细和关键变化 | 矩阵卡 |
| 完整报表 | 继承对应周期 | 保留原有详细文本报告和数据质量说明 | 文本 |

多客户多模型简报的窗口、基准、时区、来源、口径、隐藏数量与质量原因默认折叠在“报表说明与数据质量”中，主卡只保留客户和模型变化。成功空数据不会造成 `partial`；真实查询故障不会被隐藏或转换成无用量。

### 日报卡片

- 查询日期为 `D` 时，固定展示前一日 `D-1` 和上周同期 `D-7`；显式日期优先，未提供日期才默认昨天。
- 默认简报将 `D-7` 标为“同比”、`D-1` 标为“环比”，按“同比、环比”顺序放在 KPI 内；独立对比周期区块不显示，基准日期保留在底部说明。
- 变化只展示 `↑/↓百分比`、`0.0%`、`新增`、`无变化`或`暂无可比基准`，不展示绝对增减值。
- 平均 TPM 来自 Cube Admin `analysis/endpoint-max-tpm/daily/query` 的 `avgTpm`。单日直接展示该值，多日只在同一 `tenant + model + endpoint` 序列内对有效日期取算术平均。
- 平均 TPM 不跨 Endpoint 或客户汇总；多序列概览显示“多 Endpoint/客户，不汇总”，并在独立 Endpoint TPM 明细续卡中逐序列展示。
- 峰值 TPM 来自同一接口的 `maxTpm`；多序列概览明确标为“最高 Endpoint 峰值 TPM”。
- 日报不显示与周期概览重复的“分段总量”；周报、月报和区间报表继续保留分段趋势。
- `avgTpm` 缺失或接口失败时显示“暂不可用”及数据质量，不以 `maxTpm` 或 0 代替。

### 周报卡片

周报是最适合优先推广的模板。卡片只保留一个模型 table，列固定为：

```text
模型 | 周期总量/占比 | 周期变化 | 分段变化
```

- 顶部展示本周总量、较上周变化和数据质量。
- 模型按 Token 降序。
- 所有模型模式隐藏两期均为 0 的模型，并明确显示隐藏数量。
- 前期为零、本期非零显示“新增”；本期为零、前期非零显示“停用”。
- 接口失败或缺失日期必须显示为不完整。
- 模型超过 8 个时在同一张卡内翻页，不拆成多条消息。

### 固定问法

| 用户问法 | 系统行为 |
| --- | --- |
| `tencent_token_hub所有模型的周报` | 直接生成指定客户、全部模型、上一完整周矩阵卡 |
| `tencent_token_hub周报` | 客户自动识别，只补充模型范围选择 |
| `我要周报` | 打开客户和模型范围选择卡 |
| `完整周报` | 返回原有详细文本报表 |
| `深度分析上周和上上周各模型用量` | 一次确定性取数，最多一次 LLM 解释，不重新计算指标 |

## 5. 产品架构

| 层 | 职责 | 公司业务价值 |
| --- | --- | --- |
| Capability Home | 首次引导、功能导航、示例和最近报表 | 降低培训成本 |
| Report Core | Intent、Query、Dataset、Document、Runner | 统一口径和入口行为 |
| Connector Plugin | 认证、目录发现、只读 API、字段映射和健康检查 | 接入 Magik、Grafana、ClickHouse 等平台 |
| Template Pack | 指标、槽位、时间规则、分析版本和布局 | 沉淀公司最佳实践 |
| Channel Adapter | Feishu、企微、钉钉的卡片和文本降级 | 适配多个工作入口 |
| Control Plane | 连接器、模板、RBAC、订阅、审计和发布管理 | 配置可治理、变更可回滚 |

### 领域对象

- `ReportIntent`：数据源、模板、周期、比较方式、tenant、模型范围、输出和深度标志。
- `ReportQuery`：指标、维度、过滤条件、时间窗口和质量要求。
- `ReportDataset`：标准化记录、来源和 `complete/partial/missing` 状态。
- `ReportDocument`：metric、table、trend、note、action 等渠道无关组件。
- `ReportRunContext`：身份、授权范围、时区、模板版本、幂等键和 trace ID。

## 6. 插件化设计

### Connector Plugin

每个数据平台实现独立 ConnectorPlugin，通过 `nanobot.report_connectors` entry point 注册。

- 声明认证方式、配置 Schema、SecretRef、API allowlist、只读能力、最大时间窗口和批量查询能力。
- 实现 configuration validation、health check、catalog discovery、query 和认证刷新。
- 将平台字段映射为 namespaced canonical metrics，例如 `ai.usage.tokens`、`ai.requests`、`ai.tpm`。
- 单个插件加载失败只记录 load error，不阻断 Gateway 或其他插件。

### Template Pack

模板优先采用受控 YAML/JSON；复杂计算才进入经过审核的 Python hook。模板不能直接访问凭据、HTTP client 或渠道 SDK。

生命周期：

1. `draft`：定义指标、槽位、样例数据和预期布局。
2. `canary`：仅对当前单用户或指定部门生效。
3. `publish`：版本固定，运行记录保存 template version。
4. `deprecated`：停止新建订阅，保留历史运行查询。
5. `rollback`：按 template version 切回，不需要远端数据迁移。

### Channel Adapter

`ReportDocument` 作为渠道无关的中间格式。Feishu 使用 Interactive Card；不支持 table、翻页或 action 的渠道降级为 Markdown/text，数值和质量标识不变。

## 7. 权限、安全与数据治理

当前本地测试版本保持 RBAC disabled，并沿用 Feishu 单用户 `allowFrom`。公司扩大范围前，必须先配置 grants，再打开 RBAC。

| 控制点 | 产品规则 | 验证方式 |
| --- | --- | --- |
| 身份 | 使用 `channel + user_id`；卡片 nonce 绑定发起人和 chat/thread | 伪造 user、跨 chat、过期 nonce 测试 |
| 授权 | 查询前检查 connector、template、tenant、model | RBAC deny 测试和审计日志 |
| 凭据 | 只保存 SecretRef；不进 Git、日志、报表或回调参数 | 日志扫描、配置导出扫描 |
| 数据质量 | `complete/partial/missing` 明确展示 | 故障 fixture 和部分失败测试 |
| 幂等 | `subscription_id + scheduled_at + template_version` | 重复 Cron 触发测试 |
| 审计 | 保存模板版本、参数摘要、耗时、状态和质量 | 运行记录追溯检查 |

## 8. 管理与运营

### 管理台首期

- 查看 connector/template/renderer catalog 和加载错误。
- 管理用户到 channel、connector、template、tenant、model 的 grants。
- 管理模板启用状态、是否可订阅和 `all_authorized/allowlist/disabled` 订阅受众。
- 分页查看订阅，并创建、修改计划、启用、停用和删除；变更同步 Cron。
- 使用 revision 防止模板策略并发覆盖，管理操作写入不含敏感字段的审计摘要。
- 导出不含凭据的声明式 catalog，进入 Git 审核流程。

第一版只允许持有 Gateway/WebUI 管理 token 的管理员操作，模板代码、计算公式和 API 路径只读。详细使用方法见 [`report-management.md`](report-management.md)。后续仍需补充连接测试、catalog 同步、模板 fixture preview 和完整生命周期管理。

### 关键 SLI

| 指标 | 目标/观察口径 | 异常动作 |
| --- | --- | --- |
| `report_run_duration` | 固定报表 P95 < 5s | 检查缓存、API 等待、分页和并发 |
| `card_callback_confirm` | 回调确认 P95 < 1s | 回调只校验和入队，不在回调线程取数 |
| `connector_error_total` | 按 connector、错误类型、HTTP 状态观察 | 降级为 partial/missing，禁止静默出零 |
| `unauthorized_total` | 观察越权尝试和权限缺口 | 核对 grant、身份和数据范围 |
| `duplicate_delivery_total` | 目标为 0 | 检查幂等键、Cron 重试和消息发送结果 |
| `schedule_lag` | 计划时间与实际触发时间差 | 检查 Cron、队列积压和远端延迟 |

## 9. 分阶段路线图

| 阶段 | 交付内容 | 退出条件 |
| --- | --- | --- |
| 阶段 1：当前版本 | Report Core、Magik reference connector、日报/周报/月报、Feishu Home、卡片交互、SQLite 状态 | 专项测试通过，单用户 canary 稳定，固定路径 0 LLM |
| 阶段 2：治理增强 | RBAC、模板版本发布、fixture preview、审计和订阅管理 | 越权前置拒绝，可回滚 |
| 阶段 3：第二数据源 | Grafana 或 ClickHouse，完成 canonical metric 对账 | 两个 connector contract test + 真实 canary |
| 阶段 4：第二渠道 | 企微或钉钉，复用 ReportDocument 和 ChannelRenderer | 两个渠道数值一致，降级稳定 |
| 阶段 5：平台化 | Plugin SDK、脚手架、模板晋升 CLI 和完整管理台 | 稳定运行后冻结 Plugin API v1 |

### 发布与回滚

1. 保持当前 Feishu 单用户 `allowFrom`，不扩大授权范围。
2. 验证首页、明确周报、完整报表、卡片选择和订阅五条链路。
3. 观察 callback error、429/5xx、card send failure、数据质量和 schedule lag。
4. 异常时按 feature flag、template version 或 plugin version 回滚，不执行远端数据迁移。

## 10. 当前状态与待决策事项

### 已实现

统一报告领域对象、插件 registry、Magik 兼容入口、日报/周报/月报简报与矩阵模板、Feishu 首页和通用卡片 action、SQLite 状态、订阅幂等、基础 RBAC、Web 管理首版和 CLI。普通周期问法和新订阅默认使用简报；详细分析、完整报表、旧订阅和历史模板继续保留。帮助中心同时公开 Cube 灵活只读查询示例。供应商质量报告已补充 WebUI/Feishu 结构化选择器、provider 多选、全部供应商无用量折叠和查询失败语义区分。

### 已验证

Cube 用量、Intent、Report Center、Connector、Platform 和 Feishu 目标集 `122 passed`；WebUI `49 files / 739 tests passed`；Ruff、`compileall`、`git diff --check` 和 production build 通过。本地 Gateway 已从仓库源码重启，health 与 WebUI 均返回 HTTP 200。真实 Cube `avgTpm` 数值和 Feishu 卡片仍需按下方指定问法完成人工验收。

### 当前限制

- Magik 查询仍通过兼容 Tool 执行，ReportRunner 已提供抽象但第二 connector 尚未接入。
- Cube 供应商质量报告已提供只读 Connector、Template、实时目录选择器和共享 `ReportDocument` 展示路径；生产报表 flags 仍默认关闭，需完成 staging contract 后再开放。全部供应商模式默认收起成功空数据，查询异常保持显式展示。
- PostgreSQL 尚未实测。
- 第二消息渠道尚未接入。
- Plugin API 暂不冻结为 v1。

### 需要公司确认

- 正式产品名称。
- 首批报表消费者和组织范围。
- tenant/model 授权边界。
- 订阅默认发送时间和数据保留期限。
- 第二数据源和第二消息渠道优先级。

### 暂不做

- 开放式任意 SQL/HTTP。
- 任意 Python 上传执行。
- 无审批的模板发布。
- 让 LLM 自由决定指标或业务口径。

## 附录：内部使用规范

- 优先使用固定问法：客户 + 报表类型 + 模型范围，例如“客户所有模型的周报”。
- 需要解释原因时明确说“深度分析”，系统会在确定性摘要基础上进行一次解释。
- 发现数据异常先看报表底部质量说明，不把 `missing` 或 `partial` 当成真实零值。
- 新报表先作为模板候选收集，经过指标定义、样例对账、权限确认和 canary 后再发布。
- 供应商质量报告的使用方法、指标来源和质量等级见 [`docs/cube-provider-quality.md`](cube-provider-quality.md)。
- 供应商质量报告从帮助中心进入后先选择供应商和周期；Feishu 与 WebUI 共用同一份结构化 `ReportDocument`，使用方式和数据质量语义保持一致。

> 本文件只针对公司内部报表机器人业务。新增数据源、报表模板或消息渠道时，应同步更新产品范围、权限模型、数据质量规则和验收指标。
