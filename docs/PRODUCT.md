# Nanobot Cube 报表产品

## 产品定位

Nanobot 是面向内部 SRE、运营和管理人员的只读 Cube 报表与管理查询入口。固定报表使用确定性 `ReportRunner`，灵活管理问题使用 `magik-cube-admin` Skill。Grafana、企业微信和钉钉当前只保留扩展能力，不属于已启用产品范围。

## 使用场景

- `日报`、`周报`、`月报`：默认生成只含核心 KPI 的简报。
- `详细日报`、`详细周报`：生成保留排行、分段和下钻的矩阵报表。
- `完整日报`：生成兼容的完整文本报表。
- `tencent_token_hub 有哪些 Endpoint`：通过只读 Admin API 查询真实管理对象。
- 新建订阅默认保存简报模板；已有订阅和历史报表继续使用原 `template_id`。

## 已实现功能

| 能力 | 当前行为 | 权限与开关 |
| --- | --- | --- |
| 用量简报 | 日报、周报、月报、区间报表；Token、请求数、平均 TPM、最高 Endpoint 峰值 TPM | `cube_usage_brief_template` |
| 简报默认路由 | 普通周期问法进入简报；详细和完整问法保留旧模板 | `cube_usage_brief_default` |
| Cube 灵活查询 | 租户、模型、Endpoint、账单、集群、日志和配置的只读查询 | `cube_admin_skill_help`，且 Admin Tool 已启用 |
| 进一步分析 | 简报卡片携带原客户、模型和时间窗口进入矩阵报表 | 服务端重新执行权限校验 |

区间和近 7 天的进一步分析使用独立 `usage_custom_matrix`，标题与基准均按“区间 / 前一等长周期”展示，不借用周报名称。

## 统计与交互契约

日报查询日期为 `D` 时，环比固定使用 `D-1`，同比固定使用 `D-7`。KPI 按“同比、环比”顺序展示，变化只显示百分比；独立对比周期区块不在简报中渲染，但基准日期保留在底部说明、运行记录和 `ReportContext`。

平均 TPM 只使用 Cube `analysis/endpoint-max-tpm/daily/query` 的 `avgTpm`，不得跨客户或 Endpoint 聚合。接口失败、缺失和成功空数据必须保持不同状态，不得转换为 0。

灵活管理查询先 `search`，再 `describe`，最后 `call`。多步查询先取得父实体真实 ID；写接口、用户提供的 URL/API 路径和敏感字段始终阻断。

## 验收

- `tencent_token_hub Kimi-K3 2026-08-31日的日报` 返回简报，并显示同比、环比。
- `详细日报` 和“进一步分析”进入矩阵报表；`完整日报` 保留完整文本输出。
- 简报不包含独立对比区块、分段、排行、关键变化或详细样本表。
- `tencent_token_hub 有哪些 Endpoint` 使用真实只读 Admin API 结果回答。
- Feishu、WebUI、Markdown 和文本 fallback 保持相同统计语义。

## 非功能与运维

所有 Cube 调用必须只读、有 timeout、受控重试和输出脱敏。发布时先注册模板，再对白名单开启默认路由；观察报表成功率、`partial/missing` 比例、执行 P95 和投递失败率。

回滚只需关闭 `cube_usage_brief_default`，普通报表会恢复矩阵模板。关闭 `cube_usage_brief_template` 可停止注册简报；不涉及数据库迁移、订阅迁移或 Cube 远端写入。

## 发布记录与待办

- `2026-09-01`：新增默认用量简报、同比/环比命名、进一步分析入口和 Cube 灵活查询帮助。
- 已实现：代码、测试和文档随本变更交付。
- 待验证：使用 staging read-only Cube 响应和 Feishu 测试群完成用户可见 smoke test。
