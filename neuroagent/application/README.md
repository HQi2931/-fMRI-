# 应用服务层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-09-27
- 关联：[系统设计](../../docs/architecture/system-design.md)、[目标架构](../../docs/architecture/target-architecture.md)

`neuroagent.application` 编排完整用例：数据检查、方案构建与审批、运行提交、QC 审核、统计设计与结果登记。它持有事务边界和权限检查，不实现影像算法。

当前能力：

- Work 完整草稿可自动编译待审查候选，使用独立 `proposal` 来源；确认接口直接复用服务端签发的操作与执行授权，不调用模型解释启动意图。来源与校验状态分别展示，审批仍为独立记录。完整体验验收仍按[计划 0012](../../docs/plans/0012-work-conversation-ux.md)执行。
- 确认请求使用独立幂等作用域并绑定完整请求；先恢复已保存响应，再校验当前草稿与签发选项，保证响应丢失后的重放不重复批准或创建运行。显式选择输入阶段时冻结完整文件证据，快速概况检查不能替代冻结。

- T1 重定向、裁剪与后续分割/DARTEL 合并为一次预处理运行；不再提供独立准备或中途审查 API；
- Work 参数面板通过幂等 PATCH 编辑草稿，保存不调用模型；版本冲突拒绝，草稿修改使旧确认失效；

- `services.py` 与 `service_mixins/` 组成应用门面，管理 `PlanRevision` 生命周期与追加式审批记录；
- `contracts.py` 定义传输无关的公共契约；`ports.py` 声明执行器、仓储和环境等窄端口；
- `conversation_work.py`、`work_dialogue.py`、`work_draft.py` 把 Work 自然语言收敛为受限动作与未批准草稿；
- `statistics_draft.py` 只负责统计设计的取值来源与顺序，科学校验仍由领域层负责；
- `work_actions.py` 是 Work 可选动作的唯一事实来源；
- `conversation_work.py` 与 `work_dialogue.py` 将 DPABI 阶段发现、模型建议和用户确认分开：模型只能引用服务端候选，`selected_input_stage` 由服务端复查后写入冻结 manifest；
- 首次工作区在用户回复服务端签发的阶段确认口令后登记项目/数据集并冻结清单；页面短校验码先验证前缀，再以完整候选哈希复查，不要求用户先完成另一个隐含登记步骤。
- 文件授权按稳定模型配置哈希绑定具体模型；工作区只读工具接受列表返回的匿名路径，所有本地解析仍受范围、目录排除和链接限制。NIfTI 仅返回数值头事实，不出站描述文本或体素数据。
- Work 草稿在模型建议后生成服务端确定性的 `plan_preview`，展示公共预处理、ALFF/fALFF 与 ReHo 分支及消费的中间检查点；该预览只用于审查，仍由 Skill 编译器和一次性执行确认控制实际运行；
- Work 草稿同时生成 `planning_decisions`，标出关键参数的用户草稿、候选建议、缺失及校验冲突状态；模型上下文、对话回复和参数面板更新共用该摘要。它仅解释草稿，科研事实与执行资格仍以来源证据、用户确认及 SkillValidator 为准；
- 运行失败后，Work 的只读诊断工具可对受限 attempt 日志调用确定性 DPABI 分类器；只向模型返回分类、摘要和建议，不传递日志证据文本，也不会自行变更方案或启动重试；
- `reporting.py` 生成不调用模型的确定性复现报告。

本层不导入 `neuroagent.infrastructure`（由导入边界测试守护），不实现具体影像算法，不直接启动进程。HTTP 细节属于 API 层。
