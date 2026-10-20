# ADR 0038：逻辑科研方案与物理执行单元

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 决策日期：2026-10-12
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[Skill 层架构](../architecture/fmri-skill-layer.md)、[DPABI 单次预处理 ADR](0021-single-pass-dpabi-preprocessing.md)

## 背景

Skill 的偏序图表达科研步骤和产物依赖，但 DPABI 包装器可能把多个逻辑步骤合并进一次 `DPARSFA_run`。把每个逻辑节点都呈现为可暂停、可重试的检查点，会错误描述引擎能力，也可能让恢复过程重复或跳过科学处理。

## 决策

将逻辑方案节点与物理 `ExecutionUnit` 分开建模。每个执行单元需声明稳定 ID、注册的执行策略、冻结输入及其身份/哈希、预期产物合同、执行前后 gate、资源/超时策略、重试边界和验证成熟度。

对于当前 DPABI 预处理，所有已批准步骤仍编译为一个受控作业中的一次 `DPARSFA_run` 调用。逻辑上的 ALFF/fALFF/ReHo 中间检查点用于确定合法输入依赖和执行后的产物检查；除非实际实现了可靠落盘、证据校验和恢复测试，否则不得宣称这些节点能够在 DPABI 调用内部暂停或断点续跑。

仅由具有完整 lineage、内容哈希、软件/模板版本、空间/网格/mask 及适用样本集绑定的已核验产物，才可作为新的执行单元输入。复用来源不能因文件名相似或上次运行曾失败而推断成立。执行失败后的 attempt 重试仍归 Worker；跨执行单元的科研方案调整须创建新草稿和新的审批绑定。

## 后果

规划界面可以展示科研逻辑步骤，同时准确说明它们映射到多少个真实 DPABI 调用和检查边界。未来分段执行、批量 checkpoint 或多 session 作业需要各自的执行器证据与故障验证；仅增加 Skill DAG 节点不能使其可执行。

## 实施补充（2026-10-20）

预处理编译器对冻结 `SkillPlan.steps` 使用穷尽的 capability 边界分类：`fmri.dpabi.preprocess`、`fmri.dpabi.alff_falff`、`fmri.dpabi.prepare_reho` 和 `fmri.dpabi.reho` 是由一次 `DPARSFA_run` 执行的计算步骤；`fmri.artifact.verify_metadata` 是该物理单元调用后的验证 gate，随同单元声明，不视为忽略步骤。manifest 核验、输入 staging、只读数据检查、BIDS 转换候选计划、指标 QC 与统计前人工 QC 明确位于物理调用之外。当前统计计算能力属于不同执行合同，不可混入预处理作业。

派发前的预处理能力覆盖校验拒绝未分类 capability，以及需要其他执行单元的已注册能力；增加任何新能力时，必须先明确其物理映射或外部 gate/编排边界。空 `SkillPlan.steps` 仅为旧低层编译夹具保留兼容路径；有逻辑步骤的计划必须至少含一个映射到 DPARSFA 单元的能力。该校验不增加调用、checkpoint 或可暂停点。
