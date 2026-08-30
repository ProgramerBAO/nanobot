# Magik Cube 大客户日报

`magik_cube_daily_report` 是一个只读的内置工具，用于汇总 Magik Cube 管理平台的大客户运营数据，并生成适合发送到飞书的日报文本。

同一份连接配置还会启用 `magik_cube_admin_api`。它可以检索、说明并调用管理后台的
全部只读查询接口；完整的 206 个 Admin API 操作及只读/写入分类见
[`magik-cube-admin-api.md`](magik-cube-admin-api.md)。写操作只出现在目录中，不能通过该工具调用。

两个工具的职责不同：日期范围内的 Token、请求数和 TPM 报表使用
`magik_cube_daily_report`；租户、账号、endpoint、模型配置、账单、网关日志、集群等
管理对象查询使用 `magik_cube_admin_api`。例如“zhangyan 用户有哪些 endpoint”会直接
执行 `tenant_endpoints`，先查租户 ID，再查该租户的 endpoint，不会经过日报接口。

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
- 请求数：Token 接口返回的 `requestCount`；范围报告同时计算平均 Token/请求。
- 峰值 TPM：租户所有 Endpoint 在当天的最大 `maxTpm`。
- 配额变更：只展示能够映射到当前大客户 Endpoint/ModelConfig 的 TPM、RPM、并发 old/new 记录。
- Proxy 变更：当前快照相对上一份成功快照的净变化，包括 `maxTPM`、`maxRunningRequests`、`maxNewSessions`。
- 机器数：平台返回的 8 卡等效机器数。
- P/D：最近一段时间实际调用中出现过的不同 `prefillPodName` 和 `podName` 数量比，不代表完整部署拓扑。
- 告警：管理 API 暂无告警事件接口，所以第一版只显示未接入提示。

范围报表还固定计算 Token/请求数/平均 Token 每请求/TPM 的绝对变化和百分比变化、模型占比、日均、峰值日期、增长/下降排行、新增/停用。模型占比不足 1% 不进入趋势异常榜；当日 Token 高于周期中位数 50% 且模型占比至少 1% 时标记峰值异常。接口失败、分页截断或分片缺失会明确标为“数据不完整”，不会按零处理。
