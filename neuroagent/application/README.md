# 应用服务层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-09-27
- 关联：[系统设计](../../docs/architecture/system-design.md)、[目标架构](../../docs/architecture/target-architecture.md)

`neuroagent.application` 编排完整用例：数据检查、方案构建与审批、运行提交、QC 审核、统计设计与结果登记。它持有事务边界和权限检查，不实现影像算法。

当前能力：

- `services.py` 与 `service_mixins/` 组成应用门面，管理 `PlanRevision` 生命周期与追加式审批记录；
- `contracts.py` 定义传输无关的公共契约；`ports.py` 声明执行器、仓储和环境等窄端口；
- `conversation_work.py`、`work_dialogue.py`、`work_draft.py` 把 Work 自然语言收敛为受限动作与未批准草稿；
- `statistics_draft.py` 只负责统计设计的取值来源与顺序，科学校验仍由领域层负责；
- `work_actions.py` 是 Work 可选动作的唯一事实来源；
- `conversation_work.py` 与 `work_dialogue.py` 将 DPABI 阶段发现、模型建议和用户确认分开：模型只能引用服务端候选，`selected_input_stage` 由服务端复查后写入冻结 manifest；
- `reporting.py` 生成不调用模型的确定性复现报告。

本层不导入 `neuroagent.infrastructure`（由导入边界测试守护），不实现具体影像算法，不直接启动进程。HTTP 细节属于 API 层。
