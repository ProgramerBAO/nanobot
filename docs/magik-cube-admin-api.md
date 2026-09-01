# Magik Cube Admin API 接口目录

> 此文件由 `scripts/generate_magik_cube_api_catalog.py` 从 Magik Cube Admin OpenAPI
> 自动生成。它只总结接口，不会修改 `run/magik-cube`。

共 **206** 个操作，其中 **98** 个可由
`magik_cube_admin_api` 只读调用；其余写操作只展示在目录中，工具会在发出网络请求前阻止。

## Nanobot 查询方式

管理对象、配置和关系查询应使用 `magik_cube_admin_api`，不能用只统计用量的
`magik_cube_daily_report` 代替。对于“某租户/用户有哪些 endpoint”，工具提供
`tenant_endpoints` 复合动作：先通过 `UserAdminService_ListTenants` 将名称解析为
`tenant_id`，再分页调用 `InferenceAdminService_ListInferenceEndpoints`。例如：

```text
你看一下 zhangyan 用户有哪些 endpoint。
```

这类明确请求会在 command 阶段直接执行复合动作，不依赖 LLM 猜测接口。其他管理查询
按 `search → describe → call` 工作流执行；需要父对象 ID 时，先查父对象再把 ID 传给
下一个接口。Nanobot 始终加载的 `magik-cube-admin` 技能包含这套选择和联查规则。

帮助中心中的“Cube 灵活查询”对应这条链路，示例包括：

- `tencent_token_hub 有哪些 Endpoint`
- `Kimi-K3 配置在哪些集群`
- `查看某客户最近的账单`
- `查询某 Endpoint 的路由链`
- `查看某模型的价格和配置`

接口分页以返回的 `total` 为准，并受工具最大页数和记录数限制。多个实体命中时必须让用户选择；403 表示只读账号缺少对应 RBAC 权限，不得解释为无数据。只总结真实返回字段，写操作、任意 URL/API 路径和敏感配置不会进入调用或回答。

多轮追问中的“这个租户、该客户、上述用户”由 command 路由从最近一次结构化工具参数
或明确的会话结论中解析；指代词本身不会作为 API 查询参数发送。如果上下文不能唯一
确定实体，则回到普通 Agent 流程请求澄清。

只读判定规则：HTTP `GET`，或 RPC 操作名以 `Get`、`List`、`Query` 开头。
登录接口仅用于取得临时 Bearer Token，不计入 Admin API 操作数。

## 模块汇总

| 模块 | 全部 | 可调用只读 |
| --- | ---: | ---: |
| AnalysisAdminService | 24 | 23 |
| AuditAdminService | 4 | 2 |
| AuthzAdminService | 17 | 8 |
| BillingAdminService | 37 | 15 |
| ClusterAdminService | 15 | 6 |
| GatewayAdminService | 11 | 8 |
| InferenceAdminService | 22 | 9 |
| InviteCodeAdminService | 5 | 1 |
| ModelAdminService | 5 | 2 |
| PriceActivityAdminService | 12 | 4 |
| ProviderAdminService | 10 | 3 |
| RechargeActivityAdminService | 9 | 3 |
| RouterAdminService | 1 | 1 |
| ScaleAdminService | 7 | 2 |
| TaskAdminService | 21 | 8 |
| UserAdminService | 6 | 3 |

## AnalysisAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | POST | `/api/admin-manager/analysis/active-tenant-daily-usage/query` | `AnalysisAdminService_QueryActiveTenantDailyUsage` | — |
| 只读 | POST | `/api/admin-manager/analysis/channel-comparison/query` | `AnalysisAdminService_QueryChannelComparison` | 渠道对比分析 |
| 只读 | POST | `/api/admin-manager/analysis/daily-recharge-by-tenant/query` | `AnalysisAdminService_QueryDailyRechargeByTenant` | — |
| 只读 | POST | `/api/admin-manager/analysis/daily-recharge/query` | `AnalysisAdminService_QueryDailyRecharge` | — |
| 只读 | POST | `/api/admin-manager/analysis/endpoint-max-tpm/daily/query` | `AnalysisAdminService_QueryDailyEndpointMaxTpm` | 查询每日接入点 TPM 峰值/平均/利用率 |
| 只读 | POST | `/api/admin-manager/analysis/endpoint-tpm-trend/query` | `AnalysisAdminService_QueryEndpointTpmTrend` | — |
| 只读 | POST | `/api/admin-manager/analysis/machine-tpm-trend/query` | `AnalysisAdminService_QueryMachineTpmTrend` | — |
| 只读 | POST | `/api/admin-manager/analysis/machine-usage-summary/query` | `AnalysisAdminService_QueryMachineUsageSummary` | — |
| 只读 | POST | `/api/admin-manager/analysis/model-machine-usage/query` | `AnalysisAdminService_QueryModelMachineUsage` | — |
| 只读 | POST | `/api/admin-manager/analysis/model-performance/query` | `AnalysisAdminService_QueryModelPerformance` | — |
| 只读 | POST | `/api/admin-manager/analysis/model-token-trend/daily/query` | `AnalysisAdminService_QueryDailyModelTokenTrend` | — |
| 只读 | POST | `/api/admin-manager/analysis/model-token-utilization/daily/query` | `AnalysisAdminService_QueryDailyModelTokenUtilization` | 查询每日模型 token 容量利用率 TopN |
| 只读 | POST | `/api/admin-manager/analysis/performance-endpoints/query` | `AnalysisAdminService_QueryPerformanceEndpoints` | — |
| 只读 | POST | `/api/admin-manager/analysis/provider-daily-traffic/query` | `AnalysisAdminService_QueryProviderDailyTraffic` | — |
| 只读 | POST | `/api/admin-manager/analysis/provider-performance/query` | `AnalysisAdminService_QueryProviderPerformance` | — |
| 只读 | POST | `/api/admin-manager/analysis/recharged-token-tenant-count/query` | `AnalysisAdminService_QueryRechargedTokenTenantCount` | — |
| 只读 | POST | `/api/admin-manager/analysis/tenant-gpu-hours/list` | `AnalysisAdminService_ListTenantGpuHours` | — |
| 禁止调用（写） | POST | `/api/admin-manager/analysis/tenant-token-daily-stats/backfill` | `AnalysisAdminService_BackfillTenantTokenDailyStats` | — |
| 只读 | POST | `/api/admin-manager/analysis/token-overview/query` | `AnalysisAdminService_QueryTokenOverview` | — |
| 只读 | POST | `/api/admin-manager/analysis/token-trends/query` | `AnalysisAdminService_QueryTokenTrends` | — |
| 只读 | POST | `/api/admin-manager/analysis/token-utilization/daily/query` | `AnalysisAdminService_QueryDailyTokenUtilization` | 查询每日 token 容量利用率 |
| 只读 | POST | `/api/admin-manager/analysis/token-utilization/query` | `AnalysisAdminService_QueryTokenUtilization` | 查询接入点 token 容量利用率 |
| 只读 | POST | `/api/admin-manager/analysis/user-consumption-records/list` | `AnalysisAdminService_ListUserConsumptionRecords` | 用户消费记录 |
| 只读 | POST | `/api/admin-manager/analysis/user-model-usage-records/list` | `AnalysisAdminService_ListUserModelUsageRecords` | 用户模型使用记录详表 |

## AuditAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/audit/admin-authz-logs` | `AuditAdminService_ListAdminAuthzLogs` | 获取管理员鉴权审计日志列表 |
| 禁止调用（写） | POST | `/api/admin-manager/quota-changes/approve` | `AuditAdminService_ApproveQuotaChange` | Approve quota change |
| 只读 | POST | `/api/admin-manager/quota-changes/list` | `AuditAdminService_ListQuotaChanges` | List quota changes |
| 禁止调用（写） | POST | `/api/admin-manager/quota-changes/reject` | `AuditAdminService_RejectQuotaChange` | Reject quota change |

## AuthzAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/authz/menu-items` | `AuthzAdminService_ListMenuItems` | 获取菜单项列表 |
| 禁止调用（写） | POST | `/api/admin-manager/authz/menu-items` | `AuthzAdminService_CreateMenuItem` | 创建菜单项 |
| 禁止调用（写） | PUT | `/api/admin-manager/authz/menu-items` | `AuthzAdminService_UpdateMenuItem` | 更新菜单项 |
| 禁止调用（写） | POST | `/api/admin-manager/authz/menu-items/delete` | `AuthzAdminService_DeleteMenuItems` | 删除菜单项 |
| 只读 | GET | `/api/admin-manager/authz/menus` | `AuthzAdminService_GetUserMenus` | 获取当前用户菜单 |
| 只读 | GET | `/api/admin-manager/authz/permissions` | `AuthzAdminService_ListPermissions` | 获取权限定义列表 |
| 只读 | GET | `/api/admin-manager/authz/roles` | `AuthzAdminService_ListRoles` | 获取角色列表 |
| 禁止调用（写） | POST | `/api/admin-manager/authz/roles` | `AuthzAdminService_CreateRole` | 创建角色 |
| 禁止调用（写） | DELETE | `/api/admin-manager/authz/roles/{id}` | `AuthzAdminService_DeleteRole` | 删除角色 |
| 禁止调用（写） | PUT | `/api/admin-manager/authz/roles/{id}` | `AuthzAdminService_UpdateRole` | 更新角色 |
| 只读 | GET | `/api/admin-manager/authz/roles/{id}/detail` | `AuthzAdminService_GetRoleDetail` | 获取角色详情 |
| 只读 | GET | `/api/admin-manager/authz/roles/{role_id}/menus` | `AuthzAdminService_ListRoleMenus` | 获取角色菜单 |
| 禁止调用（写） | PUT | `/api/admin-manager/authz/roles/{role_id}/menus` | `AuthzAdminService_AssignRoleMenus` | 设置角色菜单 |
| 禁止调用（写） | PUT | `/api/admin-manager/authz/roles/{role_id}/permissions` | `AuthzAdminService_SetRolePermissions` | 设置角色权限 |
| 只读 | GET | `/api/admin-manager/authz/users/{user_id}/permissions` | `AuthzAdminService_ListUserPermissions` | 查询用户有效权限 |
| 只读 | GET | `/api/admin-manager/authz/users/{user_id}/roles` | `AuthzAdminService_ListUserRoles` | 查询用户角色 |
| 禁止调用（写） | PUT | `/api/admin-manager/authz/users/{user_id}/roles` | `AuthzAdminService_SetUserRoles` | 设置用户角色（全量替换） |

## BillingAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/billing/base-prices` | `BillingAdminService_ListBasePrices` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/base-prices` | `BillingAdminService_CreateBasePrice` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/base-prices/batch` | `BillingAdminService_BatchCreateBasePrices` | — |
| 禁止调用（写） | DELETE | `/api/admin-manager/billing/base-prices/{price_id}` | `BillingAdminService_DeleteBasePrice` | — |
| 禁止调用（写） | PUT | `/api/admin-manager/billing/base-prices/{price_id}` | `BillingAdminService_UpdateBasePrice` | — |
| 只读 | GET | `/api/admin-manager/billing/bills` | `BillingAdminService_ListBills` | — |
| 只读 | GET | `/api/admin-manager/billing/bills/details` | `BillingAdminService_ListBillDetails` | — |
| 只读 | GET | `/api/admin-manager/billing/cost-details` | `BillingAdminService_ListCostDetails` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/cost-details/backfill` | `BillingAdminService_BackfillCostDetails` | — |
| 只读 | GET | `/api/admin-manager/billing/custom-prices` | `BillingAdminService_ListCustomPrices` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/custom-prices` | `BillingAdminService_CreateCustomPrice` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/custom-prices/batch` | `BillingAdminService_BatchCreateCustomPrices` | — |
| 禁止调用（写） | DELETE | `/api/admin-manager/billing/custom-prices/{price_id}` | `BillingAdminService_DeleteCustomPrice` | — |
| 禁止调用（写） | PUT | `/api/admin-manager/billing/custom-prices/{price_id}` | `BillingAdminService_UpdateCustomPrice` | — |
| 只读 | GET | `/api/admin-manager/billing/gift-code-batches` | `BillingAdminService_ListGiftCodeBatches` | 查询代金券兑换码批次列表 |
| 禁止调用（写） | POST | `/api/admin-manager/billing/gift-code-batches` | `BillingAdminService_CreateGiftCodeBatch` | 创建代金券兑换码批次 |
| 禁止调用（写） | POST | `/api/admin-manager/billing/gift-code-batches/{batch_id}/disable` | `BillingAdminService_DisableGiftCodeBatch` | 禁用代金券兑换码批次 |
| 只读 | GET | `/api/admin-manager/billing/gift-codes` | `BillingAdminService_ListGiftCodes` | 查询代金券兑换码列表 |
| 禁止调用（写） | POST | `/api/admin-manager/billing/gift-codes/{code_id}/disable` | `BillingAdminService_DisableGiftCode` | 禁用代金券兑换码 |
| 只读 | GET | `/api/admin-manager/billing/gift-redemptions` | `BillingAdminService_ListGiftRedemptions` | 查询代金券兑换记录 |
| 只读 | GET | `/api/admin-manager/billing/gifts` | `BillingAdminService_ListGifts` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/gifts/grant` | `BillingAdminService_GrantGift` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/gifts/grant-batch` | `BillingAdminService_BatchGrantGiftsByInviteCode` | 按邀请码批量发放代金券 |
| 只读 | GET | `/api/admin-manager/billing/monthly_bills` | `BillingAdminService_ListMonthlyBills` | — |
| 只读 | GET | `/api/admin-manager/billing/provider-prices` | `BillingAdminService_ListProviderPrices` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/provider-prices` | `BillingAdminService_CreateProviderPrice` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/provider-prices/batch` | `BillingAdminService_BatchCreateProviderPrices` | — |
| 禁止调用（写） | DELETE | `/api/admin-manager/billing/provider-prices/{price_id}` | `BillingAdminService_DeleteProviderPrice` | — |
| 禁止调用（写） | PUT | `/api/admin-manager/billing/provider-prices/{price_id}` | `BillingAdminService_UpdateProviderPrice` | — |
| 只读 | GET | `/api/admin-manager/billing/skus` | `BillingAdminService_ListSkus` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/skus` | `BillingAdminService_CreateSku` | — |
| 禁止调用（写） | DELETE | `/api/admin-manager/billing/skus/{sku_id}` | `BillingAdminService_DeleteSku` | — |
| 只读 | GET | `/api/admin-manager/billing/wallets` | `BillingAdminService_ListWallets` | 获取钱包列表 |
| 禁止调用（写） | POST | `/api/admin-manager/billing/wallets/backend_recharge` | `BillingAdminService_BackendRechargeWallet` | — |
| 禁止调用（写） | POST | `/api/admin-manager/billing/wallets/business_recharge` | `BillingAdminService_BusinessRechargeWallet` | — |
| 只读 | GET | `/api/admin-manager/billing/wallets/monthly-overview` | `BillingAdminService_ListWalletMonthlyOverviews` | — |
| 只读 | GET | `/api/admin-manager/billing/wallets/wallet-transactions` | `BillingAdminService_ListWalletTransactions` | — |

## ClusterAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/clusters` | `ClusterAdminService_ListClusters` | 获取集群列表 |
| 只读 | GET | `/api/admin-manager/clusters/{cluster_name}/compute-nodes` | `ClusterAdminService_ListComputeNodes` | list compute nodes |
| 禁止调用（写） | POST | `/api/admin-manager/clusters/{cluster_name}/compute-nodes/cordon` | `ClusterAdminService_CordonComputeNode` | cordon compute node |
| 禁止调用（写） | POST | `/api/admin-manager/clusters/{cluster_name}/compute-nodes/delete` | `ClusterAdminService_DeleteComputeNode` | delete compute node |
| 禁止调用（写） | POST | `/api/admin-manager/clusters/{cluster_name}/compute-nodes/evict` | `ClusterAdminService_EvictComputeNode` | evict compute node |
| 禁止调用（写） | POST | `/api/admin-manager/clusters/{cluster_name}/compute-nodes/uncordon` | `ClusterAdminService_UncordonComputeNode` | uncordon compute node |
| 只读 | GET | `/api/admin-manager/clusters/{cluster_name}/compute-nodes/{node_name}` | `ClusterAdminService_GetComputeNode` | get compute node |
| 禁止调用（写） | POST | `/api/admin-manager/clusters/{cluster_name}/compute-nodes/{node_name}/assign` | `ClusterAdminService_AssignComputeNode` | assign compute node |
| 只读 | GET | `/api/admin-manager/clusters/{cluster_name}/engines` | `ClusterAdminService_ListEngines` | list engines |
| 禁止调用（写） | POST | `/api/admin-manager/clusters/{cluster_name}/engines/delete` | `ClusterAdminService_DeleteEngine` | delete engine |
| 禁止调用（写） | POST | `/api/admin-manager/clusters/{cluster_name}/engines/evict` | `ClusterAdminService_EvictEngine` | evict engine |
| 禁止调用（写） | POST | `/api/admin-manager/clusters/{cluster_name}/engines/exclude-gateway` | `ClusterAdminService_ExcludeEngineFromGateway` | exclude engine from gateway |
| 禁止调用（写） | POST | `/api/admin-manager/clusters/{cluster_name}/engines/include-gateway` | `ClusterAdminService_IncludeEngineToGateway` | include engine to gateway |
| 只读 | GET | `/api/admin-manager/clusters/{cluster_name}/engines/{pod_name}` | `ClusterAdminService_GetEngine` | get engine |
| 只读 | POST | `/api/admin-manager/lws/list` | `ClusterAdminService_ListClusterLWSs` | list lws |

## GatewayAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/gateway/conversations` | `GatewayAdminService_ListGatewayConversations` | 获取对话记录列表 |
| 只读 | GET | `/api/admin-manager/gateway/proxy-configs` | `GatewayAdminService_ListGatewayProxyConfigs` | 查询网关 proxy 配置列表 |
| 禁止调用（写） | POST | `/api/admin-manager/gateway/proxy-configs/apply` | `GatewayAdminService_ApplyGatewayProxyConfig` | 应用网关 proxy 配置 |
| 禁止调用（写） | POST | `/api/admin-manager/gateway/proxy-configs/batch-delete` | `GatewayAdminService_BatchDeleteGatewayProxyConfig` | 删除网关 proxy 配置 |
| 禁止调用（写） | POST | `/api/admin-manager/gateway/proxy-configs/update` | `GatewayAdminService_UpdateGatewayProxyConfig` | 更新网关 proxy 配置 |
| 只读 | GET | `/api/admin-manager/gateway/request/detail` | `GatewayAdminService_GetGatewayRequestDetail` | 获取完整requestbody |
| 只读 | GET | `/api/admin-manager/gateway/requests` | `GatewayAdminService_ListGatewayRequests` | 获取推理请求记录列表 |
| 只读 | GET | `/api/admin-manager/gateway/response/detail` | `GatewayAdminService_GetGatewayResponseDetail` | view gateway response detail |
| 只读 | GET | `/api/admin-manager/gateway/responses` | `GatewayAdminService_ListGatewayResponses` | 获取推理请求记录列表 |
| 只读 | GET | `/api/admin-manager/gateway/usage/log` | `GatewayAdminService_GatewayUsageLog` | 网关调用内容日志 |
| 只读 | GET | `/api/admin-manager/gateway/usages` | `GatewayAdminService_ListGatewayUsages` | 获取用量列表 |

## InferenceAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/inference/apikeys` | `InferenceAdminService_ListInferenceApiKeys` | List inference apikeys |
| 禁止调用（写） | POST | `/api/admin-manager/inference/apikeys` | `InferenceAdminService_CreateInferenceApiKey` | Create inference apikey |
| 禁止调用（写） | DELETE | `/api/admin-manager/inference/apikeys/{apikey_id}` | `InferenceAdminService_DeleteInferenceApiKey` | Delete inference apikey |
| 禁止调用（写） | PATCH | `/api/admin-manager/inference/apikeys/{apikey_id}` | `InferenceAdminService_UpdateInferenceApiKey` | Update inference apikey |
| 只读 | GET | `/api/admin-manager/inference/default-open-models` | `InferenceAdminService_ListInferenceDefaultOpenModels` | List inference default open models |
| 禁止调用（写） | POST | `/api/admin-manager/inference/default-open-models` | `InferenceAdminService_CreateInferenceDefaultOpenModel` | Create inference default open model |
| 禁止调用（写） | DELETE | `/api/admin-manager/inference/default-open-models/{id}` | `InferenceAdminService_DeleteInferenceDefaultOpenModel` | Delete inference default open model |
| 禁止调用（写） | PATCH | `/api/admin-manager/inference/default-open-models/{id}` | `InferenceAdminService_UpdateInferenceDefaultOpenModel` | Update inference default open model |
| 只读 | GET | `/api/admin-manager/inference/endpoints` | `InferenceAdminService_ListInferenceEndpoints` | List inference endpoints |
| 禁止调用（写） | POST | `/api/admin-manager/inference/endpoints` | `InferenceAdminService_CreateInferenceEndpoint` | Create inference endpoint |
| 禁止调用（写） | DELETE | `/api/admin-manager/inference/endpoints/{endpoint_id}` | `InferenceAdminService_DeleteInferenceEndpoint` | Delete inference endpoint |
| 只读 | GET | `/api/admin-manager/inference/endpoints/{endpoint_id}` | `InferenceAdminService_GetInferenceEndpoint` | Get inference endpoint |
| 禁止调用（写） | PATCH | `/api/admin-manager/inference/endpoints/{endpoint_id}` | `InferenceAdminService_UpdateInferenceEndpoint` | Update inference endpoint |
| 只读 | GET | `/api/admin-manager/inference/endpoints/{endpoint_id}/routing-chain` | `InferenceAdminService_GetEndpointRoutingChain` | 查询 Endpoint 网关路由链路树 |
| 只读 | GET | `/api/admin-manager/inference/metrics/table` | `InferenceAdminService_QueryInferenceTableData` | Query inference table metric |
| 只读 | GET | `/api/admin-manager/inference/metrics/timeseries` | `InferenceAdminService_QueryInferenceTimeseriesData` | Query inference timeseries metric |
| 只读 | GET | `/api/admin-manager/inference/model-configs` | `InferenceAdminService_ListInferenceModelConfigs` | List inference model configs |
| 禁止调用（写） | POST | `/api/admin-manager/inference/model-configs` | `InferenceAdminService_CreateInferenceModelConfig` | Create inference model config |
| 禁止调用（写） | DELETE | `/api/admin-manager/inference/model-configs/{model_config_id}` | `InferenceAdminService_DeleteInferenceModelConfig` | Delete inference model config |
| 只读 | GET | `/api/admin-manager/inference/model-configs/{model_config_id}` | `InferenceAdminService_GetInferenceModelConfig` | Get inference model config |
| 禁止调用（写） | PATCH | `/api/admin-manager/inference/model-configs/{model_config_id}` | `InferenceAdminService_UpdateInferenceModelConfig` | Update inference model config |
| 禁止调用（写） | POST | `/api/admin-manager/inference/resource-init` | `InferenceAdminService_InitInferenceResources` | Trigger inference resource init |

## InviteCodeAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 禁止调用（写） | POST | `/api/admin-manager/invite-codes/create` | `InviteCodeAdminService_CreateInviteCode` | 创建邀请码 |
| 禁止调用（写） | POST | `/api/admin-manager/invite-codes/disable` | `InviteCodeAdminService_DisableInviteCode` | 禁用邀请码 |
| 禁止调用（写） | POST | `/api/admin-manager/invite-codes/enable` | `InviteCodeAdminService_EnableInviteCode` | 启用邀请码 |
| 只读 | GET | `/api/admin-manager/invite-codes/list` | `InviteCodeAdminService_ListInviteCodes` | 邀请码列表 |
| 禁止调用（写） | POST | `/api/admin-manager/invite-codes/update` | `InviteCodeAdminService_UpdateInviteCode` | 更新邀请码 |

## ModelAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/models` | `ModelAdminService_ListModels` | List models |
| 禁止调用（写） | POST | `/api/admin-manager/models` | `ModelAdminService_CreateModel` | CreateModel creates a new model. |
| 只读 | POST | `/api/admin-manager/models/query-options/query` | `ModelAdminService_ListModelQueryOptions` | List model query options |
| 禁止调用（写） | DELETE | `/api/admin-manager/models/{model_id}` | `ModelAdminService_DeleteModel` | Delete model |
| 禁止调用（写） | PATCH | `/api/admin-manager/models/{model_id}` | `ModelAdminService_UpdateModel` | Update model |

## PriceActivityAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/price_activities` | `PriceActivityAdminService_ListPriceActivities` | 获取价格活动列表 |
| 禁止调用（写） | POST | `/api/admin-manager/price_activities` | `PriceActivityAdminService_CreatePriceActivity` | 创建价格活动 |
| 禁止调用（写） | PUT | `/api/admin-manager/price_activities` | `PriceActivityAdminService_UpdatePriceActivity` | 更新价格活动 |
| 禁止调用（写） | POST | `/api/admin-manager/price_activities/delete` | `PriceActivityAdminService_DeletePriceActivity` | 删除价格活动 |
| 只读 | POST | `/api/admin-manager/price_activities/detail` | `PriceActivityAdminService_GetPriceActivity` | 获取价格活动详情 |
| 只读 | GET | `/api/admin-manager/price_activities/enrollments` | `PriceActivityAdminService_ListPriceActivityEnrollments` | 查询价格活动报名列表 |
| 禁止调用（写） | POST | `/api/admin-manager/price_activities/enrollments` | `PriceActivityAdminService_EnrollPriceActivityTenants` | 批量报名价格活动 |
| 禁止调用（写） | POST | `/api/admin-manager/price_activities/enrollments/batch-delete` | `PriceActivityAdminService_UnenrollPriceActivityTenants` | 批量取消价格活动报名 |
| 禁止调用（写） | POST | `/api/admin-manager/price_activities/enrollments/delete` | `PriceActivityAdminService_UnenrollPriceActivityOne` | 取消单个价格活动报名 |
| 只读 | GET | `/api/admin-manager/price_activities/items` | `PriceActivityAdminService_ListPriceActivityItems` | 查询价格活动价目 |
| 禁止调用（写） | POST | `/api/admin-manager/price_activities/items` | `PriceActivityAdminService_UpsertPriceActivityItems` | 覆盖写入价格活动价目 |
| 禁止调用（写） | POST | `/api/admin-manager/price_activities/status` | `PriceActivityAdminService_UpdatePriceActivityStatus` | 更新价格活动状态 |

## ProviderAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 禁止调用（写） | POST | `/api/admin-manager/providers/create` | `ProviderAdminService_CreateProvider` | — |
| 禁止调用（写） | POST | `/api/admin-manager/providers/delete` | `ProviderAdminService_DeleteProvider` | — |
| 只读 | POST | `/api/admin-manager/providers/detail` | `ProviderAdminService_GetProviderDetail` | — |
| 禁止调用（写） | POST | `/api/admin-manager/providers/enable` | `ProviderAdminService_EnableProvider` | — |
| 只读 | POST | `/api/admin-manager/providers/get` | `ProviderAdminService_GetProvider` | — |
| 只读 | POST | `/api/admin-manager/providers/list` | `ProviderAdminService_ListProviders` | — |
| 禁止调用（写） | POST | `/api/admin-manager/providers/preview-config` | `ProviderAdminService_PreviewProviderConfig` | — |
| 禁止调用（写） | POST | `/api/admin-manager/providers/sync-config` | `ProviderAdminService_SyncProviderConfig` | — |
| 禁止调用（写） | POST | `/api/admin-manager/providers/test` | `ProviderAdminService_TestProvider` | — |
| 禁止调用（写） | POST | `/api/admin-manager/providers/update` | `ProviderAdminService_UpdateProvider` | — |

## RechargeActivityAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/recharge_activities` | `RechargeActivityAdminService_ListRechargeActivities` | 获取充值活动列表 |
| 禁止调用（写） | POST | `/api/admin-manager/recharge_activities` | `RechargeActivityAdminService_CreateRechargeActivity` | 创建充值活动 |
| 禁止调用（写） | PUT | `/api/admin-manager/recharge_activities` | `RechargeActivityAdminService_UpdateRechargeActivity` | 更新充值活动 |
| 禁止调用（写） | POST | `/api/admin-manager/recharge_activities/delete` | `RechargeActivityAdminService_DeleteRechargeActivity` | 删除充值活动 |
| 只读 | POST | `/api/admin-manager/recharge_activities/detail` | `RechargeActivityAdminService_GetRechargeActivity` | 获取充值活动详情 |
| 只读 | GET | `/api/admin-manager/recharge_activities/enrollments` | `RechargeActivityAdminService_ListEnrollments` | 查询报名列表 |
| 禁止调用（写） | POST | `/api/admin-manager/recharge_activities/enrollments` | `RechargeActivityAdminService_EnrollTenants` | 批量报名 |
| 禁止调用（写） | POST | `/api/admin-manager/recharge_activities/enrollments/batch-delete` | `RechargeActivityAdminService_UnenrollTenants` | 批量取消报名 |
| 禁止调用（写） | POST | `/api/admin-manager/recharge_activities/enrollments/delete` | `RechargeActivityAdminService_UnenrollOne` | 取消单个报名 |

## RouterAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/router` | `RouterAdminService_GetRouter` | 获取路由配置 |

## ScaleAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 禁止调用（写） | POST | `/api/admin-manager/lws/scale-schedules/create` | `ScaleAdminService_CreateScaleSchedule` | — |
| 禁止调用（写） | POST | `/api/admin-manager/lws/scale-schedules/delete` | `ScaleAdminService_DeleteScaleSchedule` | — |
| 只读 | POST | `/api/admin-manager/lws/scale-schedules/list` | `ScaleAdminService_ListScaleSchedules` | — |
| 禁止调用（写） | POST | `/api/admin-manager/lws/scale-schedules/update` | `ScaleAdminService_UpdateScaleSchedule` | — |
| 禁止调用（写） | POST | `/api/admin-manager/lws/scale/cancel` | `ScaleAdminService_CancelClusterLWSScale` | — |
| 禁止调用（写） | POST | `/api/admin-manager/lws/scale/create` | `ScaleAdminService_ScaleClusterLWS` | — |
| 只读 | POST | `/api/admin-manager/lws/scale/list` | `ScaleAdminService_ListClusterLWSScaleTasks` | — |

## TaskAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/jobs/logs` | `TaskAdminService_GetJobLogs` | — |
| 只读 | GET | `/api/admin-manager/task-datasets` | `TaskAdminService_ListTaskDatasets` | — |
| 禁止调用（写） | POST | `/api/admin-manager/task-datasets` | `TaskAdminService_CreateTaskDataset` | — |
| 禁止调用（写） | POST | `/api/admin-manager/task-datasets/delete` | `TaskAdminService_DeleteTaskDataset` | — |
| 禁止调用（写） | POST | `/api/admin-manager/task-datasets/{dataset_id}` | `TaskAdminService_UpdateTaskDataset` | — |
| 禁止调用（写） | POST | `/api/admin-manager/task-groups/add-tasks` | `TaskAdminService_AddTasksToGroup` | — |
| 禁止调用（写） | POST | `/api/admin-manager/task-groups/delete` | `TaskAdminService_DeleteTaskGroup` | — |
| 只读 | POST | `/api/admin-manager/task-groups/detail` | `TaskAdminService_GetTaskGroup` | — |
| 只读 | POST | `/api/admin-manager/task-groups/list` | `TaskAdminService_ListTaskGroups` | — |
| 只读 | POST | `/api/admin-manager/task-groups/report` | `TaskAdminService_GetTaskGroupReport` | — |
| 只读 | GET | `/api/admin-manager/task-templates` | `TaskAdminService_ListTaskTemplates` | — |
| 禁止调用（写） | POST | `/api/admin-manager/task-templates` | `TaskAdminService_CreateTaskTemplate` | — |
| 禁止调用（写） | POST | `/api/admin-manager/task-templates/delete` | `TaskAdminService_DeleteTaskTemplates` | — |
| 禁止调用（写） | POST | `/api/admin-manager/task-templates/update` | `TaskAdminService_UpdateTaskTemplate` | — |
| 只读 | GET | `/api/admin-manager/tasks` | `TaskAdminService_ListTasks` | — |
| 禁止调用（写） | POST | `/api/admin-manager/tasks` | `TaskAdminService_CreateTask` | — |
| 禁止调用（写） | POST | `/api/admin-manager/tasks/delete` | `TaskAdminService_DeleteTasks` | — |
| 只读 | GET | `/api/admin-manager/tasks/detail` | `TaskAdminService_GetTask` | — |
| 禁止调用（写） | POST | `/api/admin-manager/tasks/merge` | `TaskAdminService_MergeTasks` | — |
| 禁止调用（写） | POST | `/api/admin-manager/tasks/pause` | `TaskAdminService_PauseTask` | — |
| 禁止调用（写） | POST | `/api/admin-manager/tasks/resume` | `TaskAdminService_ResumeTask` | — |

## UserAdminService

| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |
| --- | --- | --- | --- | --- |
| 只读 | GET | `/api/admin-manager/accounts` | `UserAdminService_ListAccounts` | 获取账户列表 |
| 禁止调用（写） | POST | `/api/admin-manager/accounts` | `UserAdminService_CreateAccount` | 创建账户 |
| 禁止调用（写） | PUT | `/api/admin-manager/accounts/{user_id}` | `UserAdminService_UpdateAccount` | 更新账户信息 |
| 只读 | GET | `/api/admin-manager/tenant-tags/list` | `UserAdminService_ListTenantTags` | 获取租户标签列表 |
| 只读 | GET | `/api/admin-manager/tenants` | `UserAdminService_ListTenants` | 获取租户列表 |
| 禁止调用（写） | PATCH | `/api/admin-manager/tenants/{tenant_id}` | `UserAdminService_UpdateTenant` | 更新租户信息 |

## nanobot 使用方式

生产环境配置使用 `https://www.magikcloud.cn`（裸域会 301 跳转；工具为防止凭据
被重定向而不会跟随该跳转）：

```json
{
  "tools": {
    "magikCube": {
      "enable": true,
      "baseUrl": "https://www.magikcloud.cn",
      "apiPrefix": "/api/admin-manager",
      "account": "${MAGIK_CUBE_ACCOUNT}",
      "password": "${MAGIK_CUBE_PASSWORD}"
    }
  }
}
```

账号密码只应通过环境变量提供，不要写入仓库。工具登录后只在当前请求的内存中
保存临时 Token。

1. `action=search`：按模块、路径、Operation ID 或中文说明检索接口。
2. `action=describe`：查看路径参数、查询参数、请求体和响应 Schema。
3. `action=call`：按 Operation ID 调用；只能调用目录中标记为只读的操作。
