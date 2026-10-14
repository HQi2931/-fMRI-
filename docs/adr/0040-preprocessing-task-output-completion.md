# ADR 0040：预处理任务以冻结目标产物完成

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 决策日期：2026-10-14
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[ADR 0036：持久 Work 任务与运行状态边界](0036-persistent-work-task-boundary.md)、[ADR 0039：WorkTask 权威状态重核协调器](0039-work-task-reconciliation-worker.md)

## 背景

预处理 Run 成功且 QC 已批准或显式跳过，只能证明执行与 QC 门槛通过。它没有证明用户冻结方案要求的每个受试者、指标和缩放结果都已登记。旧协调器因此把任务置于 `waiting_dependency`，但没有确定性检查器；Worker 也不会恢复这类任务，任务会一直等待。

DPABI 执行编译器已知本次预处理请求的受试者、目标指标、缩放、文件路径和登记类型。任务完成判定应复用同一份领域输出合同，不能把 Skill 的宽泛 artifact 类型字符串或模型自由文本当作完整性证明。

## 决策

领域层定义不可变的 DPABI 预处理目标产物合同，并由执行编译器与 WorkTask 共用。预处理步骤持久化从冻结 SkillPlan 参数及其 manifest 推导出的精确目标集合；应用服务核验 Plan/SkillPlan 哈希、项目、数据集和冻结 manifest 绑定后才接受此集合。旧任务可从同一冻结来源重建目标集合。

当绑定 Run 成功且权威 QC 为 approved 或存在显式 skip 记录时，协调器逐一核对目标 Artifact 的类型、相对路径、唯一 ID、SHA-256、非空大小、受试者身份、指标缩放、manifest 谱系和已验证影像元数据。指标图还必须引用同项目、类型正确且网格兼容的掩膜；DPABI 自动掩膜必须对应同一受试者的本次输出。全部匹配时，预处理步骤与 WorkTask 原子转为 `succeeded`/`completed` 并保存 Artifact ID。缺失、重复、跨项目或谱系矛盾时任务转为 `blocked`；读取期间 Run 版本不稳定仍返回 `indeterminate`。

Worker 只额外扫描 `preprocessing_run` 步骤绑定同一 Run 的 `waiting_dependency/downstream:<run_id>` 目标，以恢复 0023 之前已经落库的预处理等待。普通 dependency、统计结果等待以及研究方案/用户决定等待保持各自既有语义。此决策只完成当前预处理目标；它不自动创建统计设计、启动下游 Run、生成报告或作 QC 科研决定。

## 后果

完成判定从模糊的“Run 成功”变成可检查的科学产物合同，且执行和编排对输出身份共用一个定义。严格检查会把不完整或来源不明的登记结果阻断，便于用户定位缺失目标。现有 `waiting_dependency` 行无需手动清理即可通过 Worker 重核；通用 dependency 等待仍不会被过度领取。

自动角色交接和统计/报告下一步准备仍属于计划 0014 的后续工作。通过本检查只证明冻结预处理任务完整，不代表人工 QC、统计方案确认或研究结论审核可以省略。
