# Report platform 管理指南

## 产品定位

`Report platform` 是 Nanobot WebUI 内的报表控制面，供持有 Gateway/WebUI 管理 token 的管理员管理模板策略、订阅计划和订阅授权。它不允许编辑模板代码、指标公式、Cube API 路径或凭据，也不替代 Feishu 用户侧报表查询。

功能由 `tools.reporting.reportManagementV1` 控制，默认关闭。关闭时页面只读，既有 `/api/settings/reporting` 查询保持兼容，已有订阅和历史运行不受影响。

## 使用入口

1. 启动 Gateway 并打开 WebUI。
2. 进入 `Settings -> Report platform`。
3. 使用三个标签页：`报表类型`、`订阅管理`、`权限管理`。

## 报表类型

每个模板展示 ID、版本、支持周期和只读说明。管理员可以修改：

- `enabled`：关闭后阻止新的手动运行和订阅执行，历史记录和订阅配置保留。
- `subscription_mode=all_authorized`：通过现有报表授权的用户可新建订阅。
- `subscription_mode=allowlist`：除报表授权外，还需要 `subscription_template:<template_id>` grant。
- `subscription_mode=disabled`：阻止新建订阅，不自动停用已有订阅。

保存时必须提交当前 `revision`。其他管理员已先修改时返回 `409`，页面刷新后才能基于最新版本重试，避免静默覆盖。

## 订阅管理

管理员可以分页查看、创建、修改 Cron/时区、启用、停用和删除订阅。每项操作先修改 Cron，再持久化订阅状态；持久化失败时会尽力恢复原 Cron 状态并返回冲突，不把按钮成功当成任务已生效。

新建订阅需要填写 channel、chat ID、user ID、五段 Cron、IANA 时区和受控报表参数。参数只接受服务端 allowlist 字段，URL、Bearer、password、API key、secret 和任意 API 路径均被拒绝。删除操作需要浏览器二次确认，历史运行记录不会删除。

## 权限管理

`Report RBAC` 打开后，运行路径继续校验 Connector、Template、tenant、model 等 grant。模板订阅白名单使用：

```text
resource_type: subscription_template
resource_id: <template_id>
```

`allowlist` 只增加订阅资格，不扩大用户的数据读取范围；执行订阅时仍重新校验原报表 scope。

## 持久化与审计

SQLite 和 PostgreSQL 启动时增量创建：

- `report_template_policies`：模板开关、订阅策略、revision、更新时间和操作者。
- `report_admin_audit`：动作、目标、变更前后摘要和时间。

审计摘要不记录 token、密码、URL、Header、原始 Cube 响应或完整业务结果。订阅输出也只返回受控参数字段。

## 失败处理与回滚

- `401/403`：管理 token 或报表授权不足，不重试。
- `409`：revision 冲突、Cron 不存在或订阅状态并发变化；刷新后核对 Cron 与数据库状态。
- 非法时区、Cron 或参数：修正输入后重试，不创建部分订阅。

回滚时关闭 `reportManagementV1` 并重启 Gateway。策略表和审计表保留但不参与执行，已有订阅、历史运行和 Cube 数据不删除。发布后观察管理操作失败率、revision 冲突、Cron 同步失败、订阅重复执行和未授权请求。
