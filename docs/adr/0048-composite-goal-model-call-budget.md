# ADR 0048：复合目标共享模型调用预算

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-17
- 更新日期：2026-10-18
- 决策日期：2026-10-17
- 决策者：system_architect
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[Agent 通用自动化运行架构](../architecture/agent-automation-runtime.md)、[MVP 验证记录](../development/mvp-verification.md)、[ADR 0041](0041-proposal-only-analysis-continuation-outbox.md)、[ADR 0042](0042-statistical-result-interpretation-outbox.md)

## 背景

计划 0014 的 `TaskBudget` 声明了模型步数、调用数、token 和耗时上限，但生产调用路径没有把 Provider 实际消耗累计回任务。显式复合目标由分析提案续接和统计结果解释两个独立 outbox 生成模型内容；若每个 outbox 独立执行本地预算，阶段切换、租约重领或人工重试会重复获得额度。

## 决策

为每个持久 `CompositeGoal` 建立独立的模型预算记录和 Provider 调用预留账本。预算不写入 identity-only `composite_goal_links`，也不复制 `WorkTask`、Plan、Run 或 outbox 的状态。新建复合根时，从其精确预处理 WorkTask 的 `TaskBudget` 复制上限；SQLite 迁移为历史根回填相同上限。

每次模型 Gateway 调用（工具循环中的一轮或结构化单次回退）必须先原子预留一步、一次调用、估算 token 和剩余 elapsed allowance。预留时先扣除不超过剩余额度的整数秒，并用该时长限制本地 Gateway await；只有 `reserved` 结果允许分发。`duplicate` 或 `exhausted` 结果都不再次调用模型，且分别报告 reservation 冲突与预算耗尽。Gateway 返回、超时、异常或取消后，按 reservation ID 幂等结算：有 usage 时用实际 token 调整预留额，没有 usage 时保留预留额；若成功响应报告多个 profile attempt，则对额外 attempt 每次加收一份该请求估算 token。实际观测耗时向上取整后替换预扣值；取消清理延迟可能使其超过 reservation 并记录 elapsed overrun。未结算的进程退出会保留完整预扣额度，因此重试不能取回这段 allowance。

预算中的“一次调用”表示一次应用层 Gateway invocation。Gateway 内部可能按 profile 执行多次 provider attempt；成功结果中可见的额外 attempts 会估算计费，但失败且没有 usage 的内部 attempts 只由原 reservation estimate 覆盖，不能声称按 HTTP/provider attempt 精确计数。超出估算的实际 token 会完整记账并阻止后续预留。elapsed timeout 请求取消本地 await；取消清理耗时计入 elapsed 账本并可能形成超额。失去 outbox fence 会取消本地生成任务、阻止其结果提交；远端 Provider 是否在取消后继续处理或计费不由本地预算账本保证。

调用身份按 intent、outbox 阶段、retry generation、fencing token 和本次 worker 尝试中的顺序生成稳定哈希。这样同一 claim 的调用重放被账本识别为 duplicate；新一代重试或新 fencing claim 获得新的调用身份，但仍消耗同一个 intent 预算。分析提案与统计结果解释共享该预算。预算耗尽时对应 outbox 进入 `WAITING_USER`、不开放重试按钮，并说明重试不会重置额度。此机制不批准 Plan、不创建或启动 Run。

没有持久 `CompositeGoal` 的历史 standalone outbox 保留原有消费路径，不创建虚构预算根。预算只约束持久目标建立后、已接入这两个 outbox 的后台 Provider 调用；前台建根前生成和尚未接入的通用调度阶段仍是计划 0014 的后续范围。

## 历史数据与限制

0031 迁移无法从现存消息、模型响应或 outbox attempt 数量可靠还原历史 Provider 的实际调用、token 和墙钟用量。因此旧 CompositeGoal 的额度上限从原预处理 WorkTask 回填，而历史 `used_*` 计数从零开始。该迁移不会声称旧目标已经消耗零预算；升级后的账本只完整记录此功能启用后的调用。新建目标从根记录创建时开始累计。

## 后果

- 两类后台续接不会各自获得一套重置额度，模型重试也不会抹去已结算用量。
- Provider 分发前的原子预留和 outbox 的 fencing/幂等边界保持一致；数据库故障时请求失败关闭。
- 估算会偏保守，缺失 usage 时额度可能高估；Provider 已接收请求但进程在结算前崩溃时，预留额度仍被计入。
- 每次本地 Gateway await 有预留 elapsed deadline；超时取消的清理阶段可能继续运行，结算记录实际观测时长并阻止后续分发。进程退出时保留时间预扣。远端服务取消后的行为不可由本地保证，失败且无 usage 的隐藏 profile attempts 也不能精确计数。
- claim fence 丢失会取消本地续接协程并阻止其提交；已被远端接收的请求仍可能继续处理或计费。
- 历史根的迁移后额度不能重建为完整的全生命周期预算；未来统一 dispatcher 需复用本账本，并明确纳入前台建根前生成的预算策略。
