# 计划 0001：fMRI Skill 层 MVP

- 状态：已实现（计划编译与 Mock 闭环范围）
- 创建日期：2026-08-06
- 完成日期：2026-08-07
- 架构依据：[静息态 fMRI Skill 层架构](../architecture/fmri-skill-layer.md)

## 目标

交付一个不运行真实 MATLAB 的最小闭环：给定 DatasetProfile 和 ALFF/fALFF、ReHo 请求，系统能够选择经过审核的 SkillSpec、报告冲突和缺失参数、生成带类型化检查点的不可变 SkillPlan，并实例化 Mock Workflow。

该范围已经实现并纳入候选基线。这里的“完成”仅指声明式 Skill、计划校验/编译、审批与 Mock 执行合同，不表示真实 DPABI 指标计算已经接入公共 Worker，也不表示科学结果已经通过真实数据验证。

## 范围

- SkillSpec、ArtifactContract、SkillRequest、SkillResolution、ValidationIssue 和 SkillPlan schema。
- 只读 Loader、Registry、Resolver、Validator 与 Compiler。
- 数据检查、DPABI 输入准备、通用预处理、ALFF/fALFF、ReHo 和 combined 六个内置 Skill package。
- capability 到 Mock Tool 的绑定和 WorkflowFactory。
- 计划预览、参数来源、QC gate 和审批失效规则。

本计划不包含真实 DPABI 执行、通用插件系统、RAG、Memory、多 Agent 运行时或生产级多用户数据库。SQLite 仅用于本地应用元数据和任务队列。

## 里程碑

1. 冻结模型与 schema，并记录兼容性和版本规则。
2. 完成 Registry、Resolver 与结构/兼容性校验。
3. 完成 DAG 编译、公共节点合并和冲突解释。
4. 增加科学负向用例与审批失效测试。
5. 接入 Mock Workflow/Tool，形成端到端计划演示。
6. 在全量 MVP 路线图中继续实现 DPABI V8.2 adapter，并将用户批准的小数据验证作为独立发布条件。

## 必须通过的验收用例

- 标准 fALFF 输入若已带通滤波，计划被阻断并指出输入谱系冲突。
- ReHo 计算前存在空间平滑，计划被阻断。
- ReHo 专用平滑与全局结果平滑同时命中时，计划阻断或要求消歧。
- 同一 DPARSFA Workflow 中，ALFF/fALFF 可以消费滤波前 Artifact，ReHo 可以消费滤波后 Artifact，且两个检查点的谱系不会混淆。
- ALFF/fALFF 与 ReHo 同时请求时，仅在 Artifact 谱系一致处合并公共节点；协议不兼容时才拆分作业。
- 仅请求 ALFF 时，DPABI 同时生成的 fALFF 被注册为非主终点的伴生产物，不会被误用于统计。
- `SmoothReHo=1` 且掩膜为空时，在引用缺失的 `mReHo/zReHo` 前被阻断。
- 频段超出 Nyquist、ReHo 邻域不是 7/19/27、缺失 TR 或掩膜不匹配时返回结构化问题。
- 相同输入与参数生成稳定 plan hash。
- 输入清单、参数、Skill 或 Tool 版本变化后旧审批失效。
- Agent 无法绕过阻断问题或直接推进 Workflow 状态。
- ReHo 计算前禁止空间平滑的硬规则具有方法学审核记录和证据等级，而不是无来源布尔值。

## 风险

- 将 DPABI 可运行性误当成科学有效性：保留软件、技术、方法学三层验证结果。
- Artifact 谱系字段不足导致错误合并：先覆盖空间、滤波、平滑、scrubbing、掩膜、频段和缩放。
- 通用化过早：只服务首期 ALFF/fALFF 与 ReHo 用例，再从真实重复需求中抽象。
- 默认值来源不清：所有科学值都携带 provenance，缺失时进入待确认状态。

## 每个研究项目仍需确认

- 主终点图类型、频段、ReHo 邻域和计算空间。
- 头动、去噪、GSR、scrubbing 和排除方案。
- 平滑时点、FWHM、标准化路径和目标体素尺寸。
- 进入组统计前的 QC 阈值和冻结规则。

这些项目级科学选择不是 Skill 框架代码的隐藏默认值。缺失时计划必须保持待确认或阻断状态；实现完成不等于替研究者选择参数。
