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

管理员可以分页查看、创建、编辑、启用、停用和删除订阅。每项操作先修改 Cron，再持久化订阅状态；持久化失败时会尽力恢复原 Cron 状态并返回冲突，不把按钮成功当成任务已生效。

引导式管理界面（`report_subscription_guided_ui` 开启时）使用结构化表单配置报表类型、客户、模型、周期、频率、发送时间、时区和接收会话。Cron 表达式和 `report_params_json` 只在服务端编译与保存，用户界面不要求填写原始 JSON 或五段 Cron。客户选项来自 Cube 实时目录；目录展示上限与单次报表最多选择 20 个客户的执行上限分离，因此客户数量较多时仍可在管理台选择任意目录客户。服务端仍会重新校验真实 `tenantId`、模型目录、RBAC 和订阅策略。删除操作需要浏览器二次确认，历史运行记录不会删除。

旧 `/api/settings/reporting` 查询参数仍保留兼容；使用旧接口的自动化客户端可以提交受控参数，但 URL、Bearer、password、API key、secret 和任意 API 路径均被拒绝。新功能应优先使用引导式接口和同一 `ReportSubscriptionService`，避免产生无法编辑的遗留配置。

用户侧还可以通过结构化自然语言或引用 Feishu 报表卡片进入订阅确认流程。该入口不允许 LLM 生成 Cron、`tenantId` 或 Tool 参数：服务端将发送计划编译为 Cron，并根据实时 Cube catalog 和当前用户权限重新校验范围。最终订阅仍由同一个 Cron 与状态存储链路管理。

自然语言订阅会先识别完整客户列表，再与实时目录合并；例如“每天上午十点发送阳春面、豆汁、佛跳墙全部模型的多客户日报简报”必须保留三个客户，并编译为 `usage_customer_model_daily_brief`。直接引用一张同群报表卡片时，服务端从安全的 `message_id` 引用恢复客户和模型范围，历史日期不会固化到周期订阅；当前用户仍需重新通过权限校验。LLM 只提取发送计划和明确的名称修改，不能生成 Cron、ID、URL 或 API 参数。

结果卡片上的“订阅”按钮由 `show_subscription_button` 独立控制：关闭它只隐藏按钮，不会停用已有订阅，也不阻止自然语言或订阅中心入口。`subscription_mode=disabled` 才会阻止新订阅；`enabled=false` 会阻止新的报表运行和订阅执行。三个开关均由服务端策略和 feature flag 共同约束。

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
- `report_message_references`：Feishu `message_id` 到安全报表 scope 的短期映射，用于引用订阅；不保存业务数值、Prompt、Header、Token、Base URL 或原始响应。

审计摘要不记录 token、密码、URL、Header、原始 Cube 响应或完整业务结果。订阅输出也只返回受控参数字段。

## 失败处理与回滚

- `401/403`：管理 token 或报表授权不足，不重试。
- `409`：revision 冲突、Cron 不存在或订阅状态并发变化；刷新后核对 Cron 与数据库状态。
- 非法时区、Cron 或参数：修正输入后重试，不创建部分订阅。

回滚时关闭 `reportManagementV1` 并重启 Gateway。策略表和审计表保留但不参与执行，已有订阅、历史运行和 Cube 数据不删除。发布后观察管理操作失败率、revision 冲突、Cron 同步失败、订阅重复执行和未授权请求。

## 当前发布状态

- 已实现：模板策略、`show_subscription_button`、订阅列表/编辑/启停/删除、revision 并发保护、Cron 补偿、审计和 Feishu 引用范围持久化。
- 默认关闭：`report_management_v1`、`report_subscription_guided_ui`、`report_subscription_button_policy`、`cube_subscription_nlu_v3`。现有兼容路径保持可回滚。
- 已验证：引导式订阅服务、过期/非法范围拒绝、CAS 冲突、Cron 恢复和目标后端测试；WebUI 生产构建通过。
- 待验证：使用 staging read-only Cube 目录和 Feishu 测试账号完成真实三客户预览、确认及单周期投递。
