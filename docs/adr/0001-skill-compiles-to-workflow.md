# ADR 0001：运行时 Skill 编译为受控 Workflow

- 状态：已接受
- 日期：2026-08-06

## 背景

静息态 fMRI 的 ALFF/fALFF、ReHo 及后续指标对滤波、平滑、计算空间和 QC 的顺序要求不同。若只依赖 Agent 提示词或让 Tool 自由串联，流程难以校验、审批和复现；若 Skill 自己执行，又会与 Workflow、Tool 和 Executor 形成重复边界。

## 决定

将运行时 Skill 定义为版本化、声明式科研协议：

- SkillSpec 声明适用条件、参数、Artifact 合同、步骤偏序、能力、QC、证据和兼容版本。
- SkillResolver、SkillValidator 和 SkillCompiler 把一个或多个 SkillSpec 编译为不可变 SkillPlan。
- SkillPlan 不包含可变生命周期状态；PlanRevision 和追加式 ApprovalRecord 管理审批前状态与批准证据。
- SkillPlan 经人工审批后实例化 WorkflowInstance。
- Workflow 是运行状态的唯一真源；Tool 和 Execution 完成确定性执行。
- 多指标 DAG 可以编译为含类型化中间检查点的一次线性工作流，也可以在输入协议不兼容时编译为显式分支。
- Skill 不携带任意 Shell 或 MATLAB 文本，不直接执行 Tool，不直接推进 Workflow 状态。
- 首期 fMRI Skill 作为内置能力提供，通用插件机制不是前置依赖。

## 后果

正面影响：

- ALFF/fALFF 与 ReHo 的不同顺序成为可测试约束。
- 审批可绑定输入、参数、Skill、Tool 和环境哈希。
- 多指标能在类型和谱系兼容时复用公共节点。
- Agent 的不确定性与 MATLAB 的确定性执行分离。

代价：

- 需要维护 Artifact 谱系、Schema、编译器和版本迁移规则。
- 新增科研协议必须同时完成方法学审核、适配器映射和负向测试。
- 协议变化需要新版本和重新审批，不能临时绕过约束。

## 未采用方案

- 提示词型 Skill：科学规则不可稳定校验，版本和审批边界不清楚。
- Skill 直接执行：会绕过 Workflow/Tool/Executor，并产生双重状态。
- 每个指标复制完整流水线：重复公共步骤，容易出现参数漂移。
- 任意步骤编排器：组合空间过大，难以保证科学有效性。
