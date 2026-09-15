# Cube 多客户多模型小时 TPM 报表

## 状态

已实现代码、本地回归测试与本机只读 Cube 契约探针（2026-09-15 实证，见下）；功能由
`cube_customer_model_hourly_tpm` 控制（本机已开启），订阅播报由
`cube_customer_model_hourly_tpm_subscription` 独立控制（默认关闭）。

每小时播报的 Cron 为 `5 * * * *`（整点后 5 分钟执行）：探针实证进行中的小时返回 0
占位、聚合只在小时结束后发生，5 分钟缓冲用于避免把"尚未聚合"播报成 0。首次真实
投递后请观察最近小时是否为 0；若持续为 0，把 `nanobot/reporting/schedules.py` 中
`recent1h` 的分钟数调大即可。

## 实证接口契约（2026-09-15 只读探针）

`analysis/endpoint-max-tpm/daily/query`：

- 请求体携带 `time_level: "TIME_LEVEL_HOUR"`（蛇形；驼峰同样被接受）与 `noloading: true`；
  `accountType` 空串或 `ACCOUNT_TYPE_ENTERPRISE` 均不影响该路由结果。
- 返回逐小时点，覆盖范围为 `startDate 00:00` 至 `endDate 00:00`（含）。**同日
  startDate=endDate 只返回 00 点**，因此查询必须把 `endDate` 设为目标小时所在日的
  次日，再由本地过滤只保留目标完整小时。
- 小时点字段为 `date="YYYY-MM-DD HH"`、`maxTpm`、`avgTpm`、`utilizationRate`；没有
  timestamp、没有 machineCount。未流逝的小时返回 0 占位。
- 不带 `time_level` 或使用 `TIME_LEVEL_DAY` 时返回日级聚合点（`date="YYYY-MM-DD"`）。

`analysis/model-machine-usage/query`：**机器占用**来源。入参
`{"clusterName":"","model":"Kimi-K3","noloading":true}`，返回 `data.list[]`，每行含
`clusterName`、`machineCount`、`gpuProduct`（可能为 `unknown`）、`gpuCount`（字符串）。
按模型对 list 跨集群求和；该接口无时间维度，是发送时的当前配置快照。

`analysis/machine-tpm-trend/query`：**真实使用**来源。目标完整小时窗口内的逐小时点，
按 timestamp+cluster+gpuProduct 去重后跨集群求和，得到该小时实际在用的机器数；窗口
边界处 `cluster=""` 的 0 值行被小时过滤排除。

空闲 = 占用 − 真实使用，**≥ 1 台即在模型行标注（闲N）**（用户确认阈值），副标题汇
总显示 `N 机器空闲`（各模型正差值之和）；负差值（播报后缩容）只显示原始对不标注。
真实使用数缺失（该小时 trend 尚未聚合完）为信息性告警：显示 `占/-`、不计算空闲、
**不降级报表质量**；占用数缺失才降级 partial。

## 使用方式

- `佛跳墙 Kimi-K3 上一小时 TPM 峰值和均值`（指定模型）
- `查看阳春面、豆汁、佛跳墙全部模型上一小时 TPM`（全部模型，先做活跃模型发现）
- `上一小时 TPM`（进入客户/模型选择器）
- `每小时播报上一小时阳春面、豆汁、佛跳墙全部模型的TPM`（订阅，需确认卡）
- 引用小时 TPM 卡片回复 `每小时播报给我`（继承范围的小时订阅）
- 帮助菜单 `多客户多模型小时 TPM` 按钮与示例问法

报表展示"上一完整小时"（Asia/Shanghai）：按客户分组，每个模型行内联
`峰值 / 均值 / 机器 占/用（闲N）` 三个紧凑指标（万取整、亿两位小数、机器纯整数），
副标题为 `MM-DD HH:MM–HH:MM · N 客户 / M 模型 · K 机器空闲`（有时区与完整口径在
底部折叠区）；小时快照没有对比基准，卡片会显式说明。

## 数据口径

- TPM 峰值/均值来自 `endpoint-max-tpm/daily/query` 的 `maxTpm`/`avgTpm`，按客户与模型
  查询（每客户每模型一次请求，受限并发）；不自行推算、不回退其他小时。
- 客户模型关系来自活跃用量发现（`active-tenant-daily-usage` probe，目标小时所在日
  有用量的模型才进入报表）；指定模型则逐客户经实时目录校验。
- 机器占用来自 `model-machine-usage`（当前配置快照），真实使用来自
  `machine-tpm-trend`（上一完整小时时点值），均**按唯一模型各查询一次**（与租户
  无关）并跨集群求和、平台级；空闲 = 占用−使用 ≥ 1 标注（闲N），使用缺失显示
  `占/-` 且不降级质量，占用缺失降级 partial。同一模型在多个客户分组显示同一平台值。
- 多 Endpoint 模型：模型行峰值取各 Endpoint 最大值，均值显示 `多 Endpoint，不汇总`
  并在续卡提供 Endpoint 明细表；绝不跨 Endpoint 平均（与日简报同一硬规则）。
- 目标小时缺失或上游失败显示 `暂不可用` 并把报表标为 `partial`/`missing`，绝不渲染
  为 0；最近小时的 0 值也可能是上游聚合延迟，卡片说明中已注明。

## 订阅

- 话术：`每小时播报…TPM`（无需时刻，确定性解析）；带模型名的 hourly 请求走受控
  分类器提取；引用卡片订阅继承小时模板与范围。
- Cron：`5 * * * *`，每次执行重新做活跃模型发现、实时目录与 RBAC/模板策略校验；
  “全部模型”订阅的新模型自动进入后续播报。
- 订阅编译走 `report_center` 的 hourly intent 分支（此前 WIP 缺失，本次补齐并修复
  `_dynamic_magik_params` 对 recent1h 的 KeyError 与 `_subscription_setup` 的
  `hourly_requested` 未定义问题）。
- 现有代码路径已实现并通过单测，但**尚未执行真实小时订阅投递验收**；本机订阅 flag
  仍为关闭状态，验收前需开启并重启源码 Gateway。

## 未覆盖范围

不展示 `utilizationRate`（接口已返回，留作后续）；不支持“工作日每小时”等混合周期
（hourly 订阅仅支持每小时播报）；不提供客户独占 TPM、TPM 自动告警或 Cube 写操作；
Grafana、企业微信和钉钉不在本能力的真实执行链路中。
