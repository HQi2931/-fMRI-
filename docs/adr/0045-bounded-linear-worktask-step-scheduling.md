# ADR 0045：WorkTask 有界线性步骤调度

- 状态：Proposed
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-15
- 决策日期：2026-10-15
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[Agent 通用自动化运行架构方案](../architecture/agent-automation-runtime.md)、[ADR 0036：WorkTask 状态边界](0036-persistent-work-task-boundary.md)、[ADR 0043：步骤生命周期转换](0043-shared-worktask-step-lifecycle.md)、[ADR 0044：步骤 readiness 检查器](0044-worktask-step-readiness-handlers.md)

## 背景

WorkTask 已能保存多个带 `sequence` 的 TaskStep，但 Run 绑定、readiness 检查和最终收口仍要求恰好一个显式步骤。直接移除这个限制并不安全：WorkTask 根引用当前只指向一个 Run，完成转换会把整个任务和唯一步骤一起置为完成，事件领取也按根 Run 建索引。若多个步骤同时就绪、后续步骤引用错 Run，或前一步完成就关闭整个目标，系统会把不完整流程误报为成功。

本项目需要从单步骤任务逐步走向可恢复的复合目标，同时保留本地 SQLite、双 Agent、确定性应用编排和现有审批/执行门禁。首个扩展应解决“前一步完成后，系统能否可靠识别唯一下一步”这一基础问题，而不是引入任意工作流 DSL 或把科学判断交给调度器。

## 提议决策

先支持**有界线性步骤序列**，不支持一般 DAG、并行 fanout 或运行时自由生成步骤。`TaskStep.sequence` 定义唯一总顺序；序列必须从 0 连续递增，步骤 ID 与序号唯一。步骤间依赖隐含为“前一项成功后，后一项才可就绪”，首版不增加可选依赖边或条件分支。

调度器按以下不变量运行：

1. 新任务中只有序号最小的步骤可以是 `READY`；后续步骤为 `PENDING`。任一时刻最多有一个非终态步骤处于 `READY`、`RUNNING` 或 `WAITING`。
2. 步骤只有在类型专属 readiness handler 核验权威 Run、冻结输入和目标证据后才能变为 `SUCCEEDED`。其成功只完成该步骤；只有所有必需步骤均成功后，WorkTask 才能完成。
3. 当前步骤成功且存在后继时，纯调度转换只把唯一直接后继置为可准备状态；随后由该步骤类型的应用用例决定准备草稿、等待用户、等待运行或阻断。协调器不直接调用模型、DPABI、文件系统或统计服务。
4. `FAILED`、`BLOCKED`、`SKIPPED`、未知步骤类型、序号缺口、多活跃步骤、前驱未成功、缺失/冲突的权威引用或过期版本都不得让后继自动就绪。未知和不稳定证据返回 `indeterminate`，保持当前持久生命周期状态。
5. 用户审批、QC 决定和运行授权仍绑定单个步骤的 Plan/输入版本与明确操作。收到“继续”、重复事件或旧授权不能隐式批准后续步骤，也不能创建新的 Run。

现有 `preprocessing_run` 与 `statistical_run` 继续使用各自的只读检查器和类型专属 finalizer。线性调度器只选择当前步骤并验证顺序，不统一或放宽两类步骤的科学证据合同。所有步骤通过后，才允许执行 WorkTask 级目标产物检查和目标收口；零显著统计结果仍是有效成功结果。

## 引用与持久化边界

- 每个 TaskStep 的 `authority_refs` 是该步骤 Plan、Run、输入版本及已核验 Artifact 的权威关联。单步骤旧任务继续保持 Task 根 Run/Artifact 引用与步骤引用一致。
- 多步骤任务必须按 `TaskStep` 精确解析 Run 与事件，不再假设 Task 根 `workflow_run_id` 能代表全部历史/当前步骤。根引用在兼容窗口内只作为当前活动步骤的投影。在线性且最多一个等待步骤的约束下，可从 `step_json` 解析当前 Run 并复用每任务一个目标的现有 SQLite 索引，无需单独 `step_id` 列；若后续要求 claim 固定 `(task, step, run)` 快照、审计历史步骤映射或支持多个并行目标，再增加持久步骤映射。
- 每步的 Artifact 集与 lineage 保存在该步骤；Task 级 Artifact 引用只表达目标级、已核验输出。任何 finalizer/outbox 仍绑定产生它的精确步骤与 Artifact 集，不能用聚合引用替代类型校验。
- 步骤完成、后继变为可准备状态、活动 Run 投影更新及事件回执必须在同一 SQLite Unit of Work 中按 Task version/CAS 与 Worker fencing token 原子提交。步骤级 Run 查询索引或等价持久映射若无法无迁移实现，须提供向后兼容迁移，并验证历史单步骤任务。
- 空 `steps` 的历史任务继续走既有只读兼容路径；它们不会被迁移为成功的显式步骤，也不会自动扩展成多步骤。

## 下一步骤准备与授权

`READY` 只表示前驱依赖满足，不表示已有可执行 Plan，更不是执行授权。调度器将这个确定的 `step_id/kind/input_version` 交给注册的应用层 prepare handler。handler 可读取事实、生成未批准草稿或运行确定性预检；若要执行 Run，仍须先形成不可变 Plan、通过现有审查并取得绑定该步骤的用户确认。模型调用在事务外进行，提交草稿前重核 Task/Step 版本、claim fencing 和输入 fingerprint。未注册 handler、科学决定缺失或能力合同不满足时，明确等待用户或阻断，不得降级成任意 MATLAB/Shell。

线性顺序本身不决定哪些 fMRI 操作需要发生。步骤集合必须来自已审核的用户目标和能力合同；各指标 Skill 决定处理顺序、统计设计决定输入组合。首版不根据自然语言自由展开多指标/多组工作流，也不自动纳入、排除样本或改变统计方法。

## 分阶段落地

1. 新增纯线性调度解析器和状态转换，覆盖单步骤兼容、双步骤前驱门控、重复/乱序事件、多个活跃步骤与非法序号拒绝。
2. 首个事件路由切片已复用现有目标索引：严格解析唯一等待步骤，校验事件 Run 和标准 stream，并幂等消费事件身份；消费事件不会改变科研或步骤状态。仍需扩展步骤级 Run binding、fenced prepare/claim 身份及迟到事件防护，确保已成功的前一步不会被后续 Run 绑定覆盖或重新打开。
3. 注册有界 `prepare_step` 应用端口，完成事务外提案生成及 fenced 提交；复用现有审批、确认和类型专属 finalizer。
4. 将复合目标 proposal/result outbox、目标级 Artifact 完成合同、共享任务预算接入序列推进；每项单独验证幂等和重启恢复。

每一步均保持旧单步骤任务行为不变。第一阶段不是宣称预处理到统计可以无确认自动执行；它只建立安全、可观测、可恢复的后继步骤选择基础。

## 不在本提议范围

本提议不支持任意 DAG、并行步骤、多目标 fanout、自动审批 Plan/QC、自动创建或重试 Run、自由 MATLAB/Shell、运行时生成新能力、静默样本纳排或自动改变科学方法。FC、多 session/run、重复测量及复杂统计设计仍需各自贯通输入身份、设计、执行、结果和 Artifact lineage 合同后才能进入可调度范围。

## 验收场景

- 单步骤预处理与统计任务行为保持兼容，既有历史空步骤任务只读恢复不变。
- 两步骤任务中，第二步在第一步成功且证据完整前保持 `PENDING`；第一步成功后只唯一第二步变为可准备。
- 第一阶段完成后任务不能提前标记 `COMPLETED`；最后一步未成功、QC 未决定、结果未登记或目标产物不完整均不能完成目标。
- 重复、乱序和旧 Run 事件不会重复推进、重开成功步骤或影响当前步骤；Task/Step 版本冲突和失效 fencing token 无法提交。
- 缺失/重复/非连续序号、多个 active step、未知 kind、错项目或旧输入版本全部 fail closed，并返回稳定、可解释的停止原因。
- 后继步骤所需用户批准仍绑定本步 Plan 与输入 fingerprint；复用上一步确认不能启动下一个 Run。

## 后果

这项提议将 `TaskStep.sequence` 从展示顺序提升为明确、可验证的线性依赖合同，并给多步任务恢复提供渐进基础。代价是事件解析、完成收口和查询索引必须由 Task 级 Run 语义升级为 Step 级身份；在相关代码与迁移完成前，ADR 0043/0044 的单步骤 fail-closed 规则继续生效。
