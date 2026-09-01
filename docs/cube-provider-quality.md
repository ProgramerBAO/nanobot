# Cube 供应商质量报告

## 1. 功能定位

供应商质量报告是 Nanobot 面向 SRE、模型运营和平台管理员的只读分析功能。它从 Magik Cube 的供应商目录、在线流量与性能、主动探测、离线测试和价格快照读取数据，按供应商、模型和 Endpoint 展示可追溯的质量信息。

本功能不执行供应商启停、流量切换、配置修改、价格修改或自动告警。Grafana、企业微信和钉钉在当前阶段也不会被真实调用。

## 2. 使用方法

直接在 Feishu 私聊发送以下固定问法：

- `供应商质量报告`
- `查看供应商 ppio 的质量`
- `过去 15 分钟各供应商性能`
- `昨天各供应商性能`
- `Kimi-K3 各供应商性能对比`
- `查看供应商 ppio 的详细情况`

供应商、模型和 Endpoint 必须使用 Cube 实时目录中的标识。供应商名称或配置别名只能辅助匹配，不能创建本地虚拟供应商。没有精确匹配时，机器人会提示重新选择或标识不唯一。

直接发送“供应商质量报告”或从帮助中心进入时，会先打开供应商选择器。选择器支持全部供应商、单个供应商和多个供应商，也可以选择近 15 分钟、昨日或上一完整周。点击“生成报告”后才发起查询；选择器中的展示名称只是标签，服务端仍使用实时目录返回的 provider 标识。

全部供应商模式会把有用量供应商和查询异常供应商放在主视图，把查询成功但没有业务用量的供应商放入默认收起区域。查询失败、权限失败和数据不足不会被收起。指定供应商模式下，即使该供应商没有用量，也会直接展示“暂无用量”。

当前支持的周期是近 15 分钟、昨天、上一完整周和自定义区间。未指定周期时使用近 15 分钟。比较基准默认是前一等长窗口：近 15 分钟对比前一个 15 分钟，昨天对比前天，上一完整周对比上上一完整周。

## 3. 报告怎么读

卡片顺序固定为：质量状态和数据质量、统计窗口与基准、核心指标、供应商排行、模型和 Endpoint 下钻、探测与测试信息、成本辅助信息、失败和缺失说明。WebUI 会把同一份 `ReportDocument` 渲染为可折叠表格；Feishu 使用结构化卡片，无法原生收起的无用量区以明确的收起提示展示。

供应商排行不是一个无法解释的总分。每一行保留供应商、模型、Endpoint、状态、错误率 P99、E2E 延迟 P99、吞吐、TPM、请求数、探测状态和较基准变化。表格标题会写明排序依据。

报告底部的小字说明应结合以下问题阅读：当前窗口是哪一段时间？基准窗口是否有数据？数据来自哪个 Cube 接口和字段？指标是窗口总和、时间桶值、P99、快照平均值还是配置值？数据质量是 `complete`、`partial` 还是 `missing`？

错误率、延迟和 TTFT 越低越好；吞吐、TPM 和流量占比描述承载情况，不能单独证明供应商发生故障。请求样本少于 20 时显示“样本不足”，不能据此把供应商判断为正常或异常。

## 4. 指标、来源和口径

| 信息 | 逻辑来源 | 单位 | 口径 |
| --- | --- | --- | --- |
| 供应商目录 | `Cube Admin / providers/list` | 标识/配置快照 | 实时目录，provider、model、Endpoint 分开保存 |
| 在线吞吐、E2E 延迟、错误率、TPM | `Cube Admin / analysis/provider-performance/query` | tokens/s、ms、ratio、tokens/minute | P50/P99 时间序列；不把单桶峰值称为窗口平均 |
| Token、TPM、流量占比 | `Cube Admin / analysis/provider-daily-traffic/query` | tokens、tokens/minute、ratio | Token 可按窗口汇总；TPM 和占比保留时间桶语义 |
| 请求数、实际 TPM、平均延迟、平均 TTFT | `Cube Admin / providers/detail` realtime | requests、tokens/minute、ms | 详情接口当前快照，不代表完整历史窗口 |
| 主动探测 | `Cube Admin / providers/detail` | 状态/时间 | `lastProbeAt`、`lastProbeStatus` 独立展示 |
| 离线测试 | `Cube Admin / providers/detail` tests | score | 只展示原始结果；仅同测试类型、数据集和可比版本才允许比较 |
| 输入/输出单价 | `Cube Admin / providers/list` 配置快照 | currency/token | 成本辅助信息，不等同于质量 |

当前供应商 TTFT 主要使用详情中的平均快照和性能趋势数据。请求级 `gateway/usages.ttft` 只有在能够可靠按 provider 过滤并完成 staging contract 验证后，才会增加 P50/P95/P99；不能把单个时间桶或 `avgTtftMs` 误称为窗口 P95。

## 5. 质量等级

- `数据不足`：核心指标缺失、接口失败或有效请求样本少于 20。
- `异常`：错误率或延迟达到异常阈值，或最近主动探测失败。
- `关注`：指标达到关注阈值，或相较基准明显恶化。
- `正常`：数据完整、样本充分且未触发异常条件。

默认阈值为错误率 2%/5%、E2E 延迟 1000/3000ms。实际发布时可以通过 reporting 配置调整，但必须是非负数值并经过 staging 验证。

在线请求质量、主动探测、离线测试和价格是四类不同证据：探测失败不等同于线上错误率升高；测试分不能替代线上质量；价格低也不能推出质量高。核心数据缺失时不会转换为 0，也不会显示为正常。

## 6. 常见失败

| 现象 | 含义和处理 |
| --- | --- |
| 客户或供应商标识已失效 | 目录已变化，重新发送问法或重新选择 |
| `暂无数据` | 查询成功但该维度没有业务数值，查看窗口和过滤条件 |
| `partial` | 部分接口或可选指标失败，报告只使用已取得的数据 |
| `missing` / `数据不足` | 核心数据不可用、样本不足或目录未匹配，不能据此判断供应商正常 |
| Cube 暂时无法连接 | 检查 Gateway 到 Cube 的网络、DNS、TLS 和 read-only 凭据 |
| 没有权限 | 检查 Feishu 身份、`cube_provider_quality` Connector grant 和 `provider_quality` Template grant |

报告会显示安全的失败原因、数据质量和逻辑来源，不展示 Base URL、API Key、Bearer Token、完整配置、请求 Header 或原始响应体。

供应商单项状态包括：`active`（当前窗口有成功用量）、`no_usage`（查询成功但没有业务用量）、`partial`（部分指标失败）和 `unavailable`（查询失败、权限失败或目录信息不足）。`no_usage` 不等于接口失败，也不会被转换为 0。

## 7. 发布边界

供应商质量能力通过以下 feature flag 分阶段启用：

```text
cube_provider_quality_connector
cube_provider_quality_template
cube_provider_quality_report
cube_provider_quality_detail
cube_provider_quality_subscription
cube_provider_quality_selector
cube_provider_quality_empty_collapse
```

默认全部关闭。启用顺序为脱敏 fixture、contract tests、staging read-only、内部白名单手动查询、Feishu 卡片验证，再考虑日报/周报订阅。近 15 分钟只支持手动查询，不创建高频定时任务。

本阶段不支持自动切换供应商、生产配置变更、自动告警、任意 SQL/HTTP/PromQL、任意跨源 Join 或真实 Grafana/企微/钉钉投递。回滚只需关闭供应商质量 flags，历史运行记录和 Cube 既有用量/健康报表不受影响。
