# 统计设计的取值来源与装配

- 状态：Implemented
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-09-23
- 关联：[系统设计](system-design.md)、[API v1 契约](../api/api-v1.md)

本文记录对话式统计设计如何把「方法选择」与「数据来源」分开，以及为什么这样分。实现位于
`neuroagent/application/statistics_draft.py`，科学校验仍然完全由
`neuroagent/domain/fmri/statistics.py` 的 `StatisticalDesignRevision` 负责。

## 为什么需要这一层

预处理方案的对话式草稿只需要收集**参数**（TR、时间点、是否滤波……），这些值由用户或方案决定。
统计设计不同：它还必须绑定**数据**——受试者顺序、每个受试者的指标产物、掩膜产物、协变量取值。
这些值不能来自模型的自由文本。

因此本模块只做领域层做不到的一件事：**决定每个值从哪来**。领域层已有的校验（显式对齐、配对顺序、
分组数、规范对比向量、设计矩阵满秩、残差自由度、协变量中心化与检验类型的匹配）一律不重复实现。

## 职责划分

| 来源 | 内容 |
| --- | --- |
| 模型或用户（`StatisticsChoices`） | 检验类型、分组列名、分组顺序、条件列名与顺序、协变量列名与中心化、尾部、基线、缺失值策略 |
| 服务端（`DesignSources`） | 受试者顺序、逐受试者指标产物 ID、掩膜产物 ID、分组/条件标签取值、协变量取值 |

关键性质：**模型只能引用服务端已经持有的列，不能创造取值。** 引用一个未登记的人口学列会得到
`covariate_column_missing`，而不是一个凭空构造的协变量向量。

## 装配器的阻断项

装配器不抛异常，而是返回阻断项，因为对话流程必须能向用户解释要改什么。

- `no_subjects` / `duplicate_subject`：空清单或同一 (受试者, 条件) 行重复
- `grouping_column_required` / `group_order_unsupported` / `group_label_missing`：两独立样本设计缺少分组信息
- `condition_column_required` / `condition_order_unsupported` / `condition_coverage_incomplete`：配对设计缺条件或覆盖不全
- `baseline_required`：单样本 t 检验缺少基线
- `covariate_column_missing` / `covariate_value_missing`：协变量列不存在或存在缺失取值
- `test_not_assembled`：相关/回归等尚未由本装配器支持
- `design_rejected_by_domain`：领域层拒绝，消息原样保留（例如「设计矩阵必须满列秩」）

## 来源解析规则

`collect_design_sources()` 从运行的已登记产物与 QC 决定解析出 `DesignSources`：

- **受试者顺序来自 QC 决定，不来自文件系统。** 纳入清单的顺序就是冻结顺序；产物列表的排列顺序、
  路径名或创建时间都不参与排序。这是 AGENTS.md §6「不得依赖文件系统隐式排序」的直接落实。
- 每个受试者必须**恰好一个**指定类型的指标产物：缺失得到 `missing_metric_artifact`，多个得到
  `ambiguous_metric_artifact`（不擅自挑一个）。
- 掩膜产物必须**恰好一个**：缺失或歧义都阻断。
- 指定分组列时，每个纳入受试者都必须有标签，否则得到 `label_value_missing`。

## 多轮草稿

`StatisticsDraft` 承载逐轮累积的方法选择，与预处理草稿同构：

- **服务端独占字段不可被模型注入。** `merge_statistics_draft()` 会剥离 `source_run_id`、
  `qc_review_revision_id`、`qc_review_hash`、`input_manifest_hash`、`confirmed_hash` 等字段，
  因此模型无法自行把设计绑定到某个运行、某个 QC 决定或某份清单，也无法预先盖好确认哈希。
- 未知字段被忽略而不是存入，避免草稿变成模型的任意键值袋。
- 校正参数必须与校正方法同时给出（`校正参数必须与校正方法一起提供`）。
- `statistics_draft_hash()` 不包含 `confirmed_hash`，所以「确认」本身不会改变被确认的内容；
  任何真实选择变化都会改变哈希，使旧确认失效。
- `render_statistics_draft_review()` 明确写出「受试者顺序、逐受试者产物与掩膜由服务端在确认时
  从已批准的 QC 决定冻结登记」，让用户在审阅时就知道哪些不是模型选的。

## 尚未接入对话

草稿、装配器与来源解析器均已完成并有测试覆盖。尚未完成的是流程接线：`确认统计设计 <hash>`
确认口令、Work 侧 Tier B 动作、以及从服务端抓取产物/QC/人口学并调用 `create_statistical_design`
的服务方法。在这些完成之前，**统计设计仍只能通过 REST/UI 创建**，本模块不构成用户可达功能。
