# ADR 0049：已有外部指标的独立统计目标

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-18
- 决策日期：2026-10-18
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[MVP 验证记录](../development/mvp-verification.md)、[ADR 0037](0037-fmri-input-identity-and-evidence.md)、[ADR 0048](0048-composite-goal-model-call-budget.md)

## 背景

Work 已允许研究者注册经过审核的外部指标输入，并以显式 `external_import_id` 为统计草稿提供来源。此前持久复合目标只表示 `preprocess_then_analyze`，独立分析已有来源时要么没有可在首次模型调用前建立的目标预算，要么会错误地借用预处理任务引用。

## 决策

为明确选择的、当前项目中已纳入的单个外部指标来源新增 `analysis_only_external_import` 目标类型。目标持久身份绑定 Work conversation、project、原始请求 hash，以及外部纳入 ID、修订版本、源树 hash、inclusion hash、输入 manifest hash 与组合 revision hash。服务端在任何草稿生成前再次读取并核对来源；变更或身份不一致时失败关闭。

独立分析目标不关联预处理 WorkTask、预处理 Run 或 proposal/result outbox。根记录与专属累计模型预算在第一次模型调用前建立，调用通过既有 reservation 账本预留和结算；统计 proposal 仍绑定原分析 intent 的 WorkTask。统计 Plan 审查、显式 Approval 和 Run 创建继续复用既有授权链路，目标身份不授予执行权限。数据库检查约束禁止 analysis-only root 同时声明 preprocessing task 或两个专用 outbox。

迁移 0033 将 `preprocessing_task_id` 改为可空，同时保留既有 root、analysis attempts、累计预算和调用预留记录。旧 root 明确标为 `preprocess_then_analyze`。SQLite FK 关联表按子表到根表的顺序备份、重建和恢复；若降级时已有 analysis-only root，则拒绝降级而不丢失来源身份。

## 范围与后果

- 只接入单个 Work conversation 中明确选定的一个已纳入外部来源，并复用现有统计设计装配器及已经支持的统计组合。
- 不创建虚构预处理任务，不从目录推断 session/run、设计、组别方向、协变量、尾部、缺失值策略、校正或其他科学参数；缺少决定时继续要求用户选择。
- 当前只在前台 AUTO Work 回合建立该目标和预算，不启用任意来源、跨会话/多 Run、多指标自动拆分、FC 或通用 dispatch。
- analysis-only root 通过分析 WorkTask 和现有 Plan/Approval/Run 授权链路持久关联；现有 CompositeGoal 列表/完成进度投影仍只服务预处理复合目标，不宣称此 root 已有后台续接或统一完成态。
- 新建独立目标时使用有界累计预算；现有复合预算账本机制不因前后台路径切换而重置。
