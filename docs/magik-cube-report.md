# Magik Cube 大客户日报

`magik_cube_daily_report` 是一个只读的内置工具，用于汇总 Magik Cube 管理平台的大客户运营数据，并生成适合发送到飞书的日报文本。

同一份连接配置还会启用 `magik_cube_admin_api`。它可以检索、说明并调用管理后台的
全部只读查询接口；完整的 206 个 Admin API 操作及只读/写入分类见
[`magik-cube-admin-api.md`](magik-cube-admin-api.md)。写操作只出现在目录中，不能通过该工具调用。

两条链路的职责不同：日期范围内的 Token、请求数和 TPM 报表优先使用
`report_center` 的确定性模板，`magik_cube_daily_report` 保留为兼容入口；租户、账号、endpoint、模型配置、账单、网关日志、集群等
管理对象查询使用 `magik_cube_admin_api`。例如“zhangyan 用户有哪些 endpoint”会直接
执行 `tenant_endpoints`，先查租户 ID，再查该租户的 endpoint，不会经过日报接口。

## 简报、详细分析与完整报表

普通的 `日报`、`周报`、`月报` 和区间问法默认返回简报。简报只展示当前周期、时区、Token、请求数、平均 TPM、最高 Endpoint 峰值 TPM、数据质量和简短口径说明，不展示独立对比区块、分段、排行、关键变化或详细样本表。

```text
日报                         -> 日报简报
详细日报                     -> 原矩阵报表
完整日报                     -> 原完整文本报表
tencent_token_hub Kimi-K3 2026-08-31日的日报 -> 指定范围日报简报
```

日报简报中的 `同比` 指上周同期 `D-7`，`环比` 指前一日 `D-1`，固定按“同比、环比”顺序显示。周报、月报和区间简报只显示前一等长周期的环比。基准日期不会作为独立区块展示，但会保留在卡片底部说明、运行记录和计算上下文中。

简报标题会根据已确认的报表范围生成：单客户单模型显示为“客户别名 模型名模型日报简报”，多模型显示为“客户别名 全部模型日报简报”，全部客户显示为“全部客户 …”。客户别名只用于展示，查询仍使用 Cube catalog 返回的真实 `tenantId`。

所有用量简报均不展示“时间桶”、桶数量或 interval。查询内部仍保留采样粒度，详细分析和健康诊断保留必要的趋势聚合说明；时间序列峰值不能理解为窗口平均值。

用量简报结果不再显示订阅按钮。已有订阅和 Cron 执行链路保持不变；新建、修改、启停和删除订阅统一在 WebUI `Report platform` 中完成。订阅参数仍保存 Cube catalog 验证后的真实 `tenantId`，历史查询日期不会固化到周期订阅中。

“我的订阅”会把每条计划渲染为独立信息区块，并显示编号、报表名称、启停状态、发送计划、统计范围、数据周期和口径版本。“停用订阅 1 / 启用订阅 2”等按钮紧邻对应区块；操作完成后返回刷新后的卡片。当前不展示无确认的删除按钮，避免误删定时计划。

简报的“进一步分析”操作保留当前客户、模型和日期范围，并在服务端重新校验权限后生成矩阵报表。已有订阅不自动迁移；新建订阅默认使用简报。
区间和近 7 天使用独立 `usage_custom_matrix` 作为详细分析模板，避免把自定义窗口错误标成周报。

### 多客户多模型日报简报

发送 `多客户多模型日报简报` 后选择客户和模型。报告按客户分组，每个模型只展示 Token `同比`（D-7）和`环比`（D-1）。客户名来自实时 catalog 或已验证别名，查询始终使用真实 `tenantId`。“全部模型”会先通过 Cube 实时 `inference/model-configs` 目录展开为该客户的明确模型列表，再逐个查询；目录为空或读取失败时返回明确错误，不生成 `0 个模型` 的空壳报表。三个命名窗口（当前日、前一日、上周同期）均无 Token 用量的目录模型会自动隐藏；任一基准期有量、用户明确点选或存在查询失败时不隐藏。标题模型数量按实际展示模型计算。手工多选最多 20 个模型；“全部模型”允许使用实时目录中的完整模型集合，但仍受最多 20 个客户和 200 个客户模型组合的总上限保护。单客户失败时报告标记 `partial`，失败项不会被当作无用量折叠。该能力由 `cube_multi_scope_brief` 控制，默认关闭。

### 单机折算 TPM 峰值

发送 `Kimi-K3 单机 TPM 峰值` 可查询指定模型。数据来自 `analysis/machine-tpm-trend/query`，请求使用 `TIME_LEVEL_HOUR`，按 `model + cluster + gpuProduct` 取窗口最大 `tpmPerMachine`。报告展示峰值北京时间、机器数、GPU 数、总 Token 和有效样本数。接口不返回 `machineId`，因此该指标只代表按机器数量折算的集群/卡型峰值，不能定位到某台物理机器。`machineCount` 和 `gpuCount` 保留小数；公式偏差只产生质量 warning，不覆盖上游值。该能力由 `cube_machine_tpm_report` 控制，默认关闭。

连续追问会从近期会话的结构化工具参数和明确业务结论中继承租户，例如在确认
“endpoint 由 `magik_test` 租户的 API key 调用”后询问“这个租户这两天用了多少 M
Token”，会查询 `magik_test`，而不是把“这个”当租户名。“这两天”按上海时区解释为
昨天至今天；今天的数据会标为截至查询时刻的未完结数据。M Token 按 1,000,000 Token
换算，并同时保留精确 Token 数。

## 配置

在 `~/.nanobot/config.json` 中加入：

```json
{
  "agents": {
    "defaults": {
      "timezone": "Asia/Shanghai"
    }
  },
  "tools": {
    "magikCube": {
      "enable": true,
      "baseUrl": "https://www.magikcloud.cn",
      "apiPrefix": "/api/admin-manager",
      "account": "${MAGIK_CUBE_ACCOUNT}",
      "password": "${MAGIK_CUBE_PASSWORD}",
      "maxConcurrency": 8,
      "maxRangeDaysPerRequest": 90,
      "maxQueryDays": 366,
      "cacheTtlSeconds": 300,
      "trendMinShare": 0.01,
      "spikeMedianMultiplier": 1.5,
      "tenantMappings": {
        "豆汁": "tenant-baka99jxwy88n",
        "佛跳墙": "tenant-baowjhsicyf65",
        "阳春面": "tenant-baxjd983oy8q1"
      },
      "clusterNames": ["prod-cluster"]
    }
  }
}
```

启动 gateway 前设置凭据环境变量：

```powershell
$env:MAGIK_CUBE_ACCOUNT = 'your-account'
$env:MAGIK_CUBE_PASSWORD = 'your-password'
python -m nanobot gateway
```

如果直接访问 Admin 服务而非前端网关，将 `apiPrefix` 改为 `/api/v1`。
生产前端的裸域 `https://magikcloud.cn` 会重定向到 `https://www.magikcloud.cn`；工具不会
带着登录凭据跟随重定向，因此生产配置必须直接使用带 `www` 的地址。

每次生成日报时，工具先调用 `/token-api/v1/accounts/login/with-password` 获取临时 Access Token，Token 只保存在本次运行的内存中。也可以不配置账号密码，改用 `accessToken` 提供现成 Token；账号密码存在时优先自动登录。

`tenantMappings` 是业务名称到租户 ID 的精确映射。临时用量查询会优先使用该映射；未配置映射时会匹配平台返回的租户名称和标签。飞书也可以直接使用 `tencent_token_hub` 这类 tenant slug。

登录后只调用代码内固定白名单中的查询接口。任何未列入白名单的方法或路径都会在发出网络请求前被拒绝；登录路径同样固定，不能通过配置改成其他接口。Proxy 当前配置会保存到 nanobot 实例数据目录下的 `magik_cube/proxy_snapshot.json`，用于下一次运行时计算净变化。该快照仅写入 Bot 本机，不会修改 Magik Cube 平台。

标准日报、周报、月报、区间对比和模型分析在 command 阶段直接执行 Tool 并返回飞书，不进入 Agent BUILD，不调用 LLM。显式包含“深度分析、原因解释、业务建议”的请求才进入 Agent；每个用户回合最多调用一次范围 Tool，LLM 只能解释确定性摘要，不能重新计算数值。

租户、账号、模型、Endpoint、账单、集群、日志和配置等灵活管理问题由 `magik-cube-admin` Skill 路由到 `magik_cube_admin_api`。陌生问题严格执行 `search -> describe -> call`；多步问题先查询父实体真实 ID。分页遵循接口 `total` 并受工具上限约束，403 显示为 RBAC 权限不足，写接口和用户提供的 URL/API 路径不会执行。

范围参数如下：

- `start_date/end_date`：主周期闭区间，与 `report_date` 互斥。
- `compare_start_date/compare_end_date`：自定义对比周期，必须成对出现。
- `comparison`：`none|previous_period|previous_week|previous_month`。
- `breakdown`：`summary|model`；`model` 会展示租户的全部模型。
- `include_tpm`：默认 `true`。

单请求最多 90 天，主周期与对比周期合计最多 366 天。相邻周期合并请求；超过 90 天自动分片，分片最多 2 路并发。所有业务 API 共用 `Semaphore(8)`，租户和模型清单缓存 300 秒，用量数据不缓存。

飞书 fast path 示例：

> 给我 tencent_token_hub 各个模型，一周的使用情况分析

“近 7 天/一周”表示截至昨天的 7 个完整自然日；“上周”表示上一完整周一至周日；“上个月”表示上一完整自然月。日期统一使用 `Asia/Shanghai`。

生产部署还应禁用通用 `exec`、文件和 CLI Apps 工具，并启用 `tools.restrictToWorkspace`，防止 Agent 绕过专用工具使用管理员凭据。最终的强安全边界仍应由 Magik Cube 服务端提供只读 RBAC 账号。

## 飞书定时日报

先在目标飞书群聊或话题中让机器人执行一次：

> 生成昨天的 Magik Cube 大客户日报。

确认格式和数据后，在同一个群聊或话题中创建定时任务：

> 每天北京时间 10:00 调用 magik_cube_daily_report 生成昨天的大客户日报，并把工具返回内容原样发送到当前飞书话题。不要自行改写数值。若工具失败，发送失败原因。

对应 Cron 表达式为 `0 10 * * *`，时区为 `Asia/Shanghai`。任务会继承创建它的飞书会话和话题路由。

## 当前统计口径

- 大客户：`GET /tenants` 返回的 `isKeyAccount=true` 租户。
- Token：报表日全天 `totalTokens`，分别与前一天、七天前比较。
- 请求数：Token 接口返回的 `requestCount`，在窗口内求和。
- 平均 TPM：`analysis/endpoint-max-tpm/daily/query` 返回的 `avgTpm`。单日直接展示；多日只对同一 `tenant + model + endpoint` 序列的有效日期取算术平均，不跨 Endpoint 或客户汇总。
- 峰值 TPM：同一接口返回的 `maxTpm`。单序列取窗口最大值；多序列概览展示“最高 Endpoint 峰值 TPM”，明细保留各 Endpoint 的峰值。
- 配额变更：只展示能够映射到当前大客户 Endpoint/ModelConfig 的 TPM、RPM、并发 old/new 记录。
- Proxy 变更：当前快照相对上一份成功快照的净变化，包括 `maxTPM`、`maxRunningRequests`、`maxNewSessions`。
- 机器数：平台返回的 8 卡等效机器数。
- P/D：最近一段时间实际调用中出现过的不同 `prefillPodName` 和 `podName` 数量比，不代表完整部署拓扑。
- 告警：管理 API 暂无告警事件接口，所以第一版只显示未接入提示。

日报固定比较查询日与前一日、上周同期，并省略与概览重复的“分段总量”；周报、月报和区间报表继续保留分段趋势。所有用户可见变化只展示百分比：上升 `↑x.x%`、下降 `↓x.x%`、相等 `0.0%`、零基准增长`新增`、两期均零`无变化`，基准缺失显示`暂无可比基准`。底层保留原始当前值、基准值和差值用于计算与审计，但卡片、Markdown、WebUI 和文本 fallback 不展示绝对增减值。

在默认简报中，日报变化标签进一步标准化为 `同比=D-7`、`环比=D-1`。详细矩阵报表继续显示基准名称和日期，便于进一步分析。

范围报表还固定计算模型占比、日均、峰值日期、增长/下降排行和新增/停用。模型占比不足 1% 不进入趋势异常榜；当日 Token 高于周期中位数 50% 且模型占比至少 1% 时标记峰值异常。`avgTpm` 缺失、接口失败、分页截断或分片缺失会明确标为“暂不可用”或“数据不完整”，不会使用 `maxTpm` 或 0 替代。
