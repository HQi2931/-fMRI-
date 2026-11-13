# ADR 0050：analysis-only 外部来源目标的结果续接

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-18
- 决策日期：2026-10-18
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[ADR 0042：真实统计结果解释续接](0042-statistical-result-interpretation-outbox.md)、[ADR 0048：复合目标共享模型调用预算](0048-composite-goal-model-call-budget.md)、[ADR 0049：已有外部指标的独立统计目标](0049-analysis-only-external-import-goals.md)

## 背景

ADR 0049 增加了没有预处理任务的 `analysis_only_external_import` CompositeGoal。统计方案与 Run 继续使用现有授权链路，但该目标最初没有进入统一进度投影，也没有在统计结果登记后绑定结果解释 outbox。若为了接入通用 outbox 而创建虚构的 preprocessing task，或把目标的状态复制到 root，就会模糊 WorkTask、统计结果和 outbox 各自的权威边界。

## 决策

analysis-only root 继续以外部来源身份为根，`preprocessing_task_id` 保持为空，也不关联 proposal-only 分析 outbox。统计 WorkTask 完成器只有在核验其唯一、真实且非占位的 `StatisticalResult`、冻结结果 bundle 和 Artifact lineage 后，才在同一事务创建结果解释 outbox，并把 root 的 `analysis_work_task_id` 与 `result_interpretation_outbox_task_id` 关联到同一个 WorkTask。迁移 0034 放宽来源种类约束只允许该精确绑定；若降级会丢失现有 analysis-only 结果 outbox 关联，则拒绝降级。

创建及消费结果解释 outbox 时必须重核当前外部导入仍处于已纳入状态，且其项目、版本、source-tree hash、inclusion hash、manifest hash 与 root revision 一致。批准的 Plan 必须属于该项目和冻结 manifest，内容 hash 有效，design 指向同一外部来源，并与当前分析 proposal/草稿身份一致。当前任务的统计步骤、active attempt、Run 和唯一结果必须与 outbox 的 Task、Plan、result ID、design revision、bundle hash 与 version 完全相符。模型调用后提交前再次核验同一权威链；变化或 fencing 失效时不得提交解释。最终完成投影还必须找到与 intent、会话、Task、result ID/version 一致的 assistant continuation。

复用 ADR 0042 的结果解释 outbox、消费者和可审查 assistant 草稿合同。analysis-only root 已有的累计模型预算同时约束结果解释调用；续接阶段不创建或重置预算。进度 API 按会话列出 analysis-only 与 `preprocess_then_analyze` 两种目标，返回目标种类、可空的预处理任务引用以及由 child/outbox/消息证据即时计算的状态。Work 页面仅为 analysis-only 目标显示“统计分析”和“结果解释”两个阶段。

缺少分析任务或统计任务/结果解释 outbox 要求科研决定时，进度等待用户；来源 revision 变化、统计任务终止阻断或解释 outbox 阻断时显示阻断；Task、Plan、attempt、结果和 continuation 引用互相矛盾或证据不足时显示待核验状态，不能标为完成。该进度和续接均不批准 Plan、不启动 Run，也不改变检验、纳排、协变量、校正或其他科学决定。

## 范围与后果

- 外部来源仍须由用户在 Work 中明确选择，当前支持范围受已登记外部导入和统计设计装配器约束。
- 结果阶段不伪造预处理引用，不把 outbox 生命周期复制进 CompositeGoal root；WorkTask、StatisticalResult、outbox 和 assistant 消息继续作为各自事实来源。
- 现有 `preprocess_then_analyze` 与历史 standalone 结果解释路径继续使用各自现有绑定合同。
- 当前实现不等于通用 dispatcher 或完整计划 0014；多来源、多 Run/session、自动多目标拆分、FC 和任意 DAG 仍在范围之外。
- 定向回归 `test_analysis_only_result_continuation.py` 13 项、`AgentPage.test.tsx` 64 项通过；Ruff、307 个 Python 源文件 Mypy、前端 TypeScript/ESLint、89 份 Markdown 文档校验及差异空白检查通过。未运行全量后端/前端测试或真实 Provider、MATLAB/DPABI。
