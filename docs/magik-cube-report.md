# Magik Cube 大客户日报

`magik_cube_daily_report` 是一个只读的内置工具，用于汇总 Magik Cube 管理平台的大客户运营数据，并生成适合发送到飞书的日报文本。

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
      "baseUrl": "https://your-magik-cube-host",
      "apiPrefix": "/api/admin-manager",
      "account": "${MAGIK_CUBE_ACCOUNT}",
      "password": "${MAGIK_CUBE_PASSWORD}",
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

```bash
export MAGIK_CUBE_ACCOUNT='your-account'
export MAGIK_CUBE_PASSWORD='your-password'
nanobot gateway
```

如果直接访问 Admin 服务而非前端网关，将 `apiPrefix` 改为 `/api/v1`。

每次生成日报时，工具先调用 `/token-api/v1/accounts/login/with-password` 获取临时 Access Token，Token 只保存在本次运行的内存中。也可以不配置账号密码，改用 `accessToken` 提供现成 Token；账号密码存在时优先自动登录。

`tenantMappings` 是业务名称到租户 ID 的精确映射。临时用量查询会优先使用该映射，不再通过租户标签模糊匹配；未配置映射的名称仅与平台返回的租户名称匹配。

登录后只调用代码内固定白名单中的查询接口。任何未列入白名单的方法或路径都会在发出网络请求前被拒绝；登录路径同样固定，不能通过配置改成其他接口。Proxy 当前配置会保存到 nanobot 实例数据目录下的 `magik_cube/proxy_snapshot.json`，用于下一次运行时计算净变化。该快照仅写入 Bot 本机，不会修改 Magik Cube 平台。

为避免模型把宽泛追问误解为无限历史扫描，`magik_cube_daily_report` 每个用户回合最多执行 3 次。达到上限后 Runner 会禁用工具并要求模型根据已经返回的数据立即总结。生产实例还应设置合理的 `agents.defaults.maxToolIterations`（例如 `20`）作为全局兜底。

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
- 峰值 TPM：租户所有 Endpoint 在当天的最大 `maxTpm`。
- 配额变更：只展示能够映射到当前大客户 Endpoint/ModelConfig 的 TPM、RPM、并发 old/new 记录。
- Proxy 变更：当前快照相对上一份成功快照的净变化，包括 `maxTPM`、`maxRunningRequests`、`maxNewSessions`。
- 机器数：平台返回的 8 卡等效机器数。
- P/D：最近一段时间实际调用中出现过的不同 `prefillPodName` 和 `podName` 数量比，不代表完整部署拓扑。
- 告警：管理 API 暂无告警事件接口，所以第一版只显示未接入提示。
