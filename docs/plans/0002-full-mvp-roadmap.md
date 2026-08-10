# 计划 0002：rs-fMRI Agent 全量 MVP

- 状态：候选基线；纯合成垂直闭环与报告合同已实现，真实执行、真实结果登记与首次发布尚未完成
- 目标版本：v0.1.0
- 环境：Windows、MATLAB R2023b、SPM12、DPABI V8.2

## 阶段

| 阶段 | 交付 | 当前状态（2026-08-07） |
| --- | --- | --- |
| 0 | 仓库基线、锁文件、安全扫描、CI、阶段发布脚本 | 设施已形成候选树；最终门禁、首次推送、分支保护和远程 CI 尚待完成 |
| 1 | FastAPI、SQLite/Alembic、Worker、React 壳 | 已实现本地应用骨架、迁移和独立 Worker |
| 2 | 数据检查、人口学映射、受试者清单和划分 | 已实现只读扫描、revision、对齐和受试者级划分规则 |
| 3 | Skill Registry/Resolver/Validator/Compiler 和审批 | 已实现稳定计划哈希、结构化阻断和审批失效规则 |
| 4 | Workflow、Tool、Mock Executor 和 SSE | 已实现队列、状态、恢复、SSE 和直接注入的 Mock Executor；SkillPlan DAG 到类型化 Tool 的运行时分派尚未接线，不生成科学结果 |
| 5 | DPABI V8.2 adapter 和 MATLAB 模板 | 已实现静态映射、模板、dry-run、超时/取消合同；未接公共 Worker，真实 smoke 未执行 |
| 6 | ALFF/fALFF、ReHo、Artifact 谱系和 QC | 已实现领域规则、类型化合同和人工 QC 服务；公共 Worker 尚不能生成真实指标 Artifact |
| 7 | 三类 t 检验、协变量、FDR 和 GRF | 已实现设计/校正校验、静态模板、结果完整性合同和确定性复现报告；真实统计图、效应量与簇表仍未生成或登记 |
| 8 | 多 Provider Agent、路由和去标识化 | 已实现网关、路由、脱敏和两个 Mock Provider；真实 Provider smoke 未执行 |
| 9 | 完整中文 UI、恢复、文档和发布 | 已实现中文工作台、Mock UI 流程与扫描到合成报告的后端 E2E；真实结果 UI、外部 smoke 与发布未完成 |

## 交付状态（2026-08-07）

- Stage 0–9 的部分代码、测试、文档与本地自动化已形成同一待审候选树；上表中的未完成项仍是范围缺口，不得以阶段编号存在代替验收通过。
- Python 严格类型、领域/后端测试、前端单元测试、Playwright Mock E2E、依赖审计和仓库安全门禁由 `scripts/quality-gate.ps1` 汇总。
- 本次交付不把历史伪装成十个已经合并的阶段 PR；首次空仓库基线采用一个受审提交，之后严格使用阶段分支。
- GitHub 推送、分支保护和标签仍取决于一次 `gh auth login`。
- 真实 MATLAB smoke 与真实 Provider smoke 未自动执行：前者需要用户对小型合成/脱敏作业单独授权，后者需要仅存于本机环境的有效 API Key。
- 当前合成演示覆盖只读 BIDS 扫描、ALFF Skill、审批、Mock Worker、测试夹具 Artifact、人工 QC、单样本 t + FDR 设计、统计 Mock 和确定性报告。所有指标与结果证据均为显式 `synthetic_non_scientific` 占位，不代表真实 ALFF、统计数值或科学结论。

## v0.1.0 发布阻断项

- 对最终候选树完成多角色审查和完整本地门禁，并在远程 GitHub Actions 中验证 `quality-gate`。
- 完成首次推送、`main` 保护规则、PR/自动合并流程验证；不得创建虚假的历史阶段 PR 或通过报告。
- 将已批准 `SkillPlan` 的 DAG 接入类型化 Tool 分派和运行事件/Artifact 收口；当前 Mock Worker 不能作为该执行链已经完成的证据。
- 经用户单独授权，使用合成或脱敏小数据完成真实 MATLAB/DPABI smoke，并记录软件版本、输入和结果。
- 使用本地密钥完成至少一个真实 Provider 轻量 smoke，确认外发内容经过脱敏策略且密钥不落库、不入日志。
- 明确 v0.1.0 是否补齐真实统计执行、效应量、显著簇表、结果登记和结果查询；确定性报告合同已经实现，但只有真实证据完整时才能生成真实模式报告。

## 阶段完成定义

- 实现、测试、文档和 CHANGELOG 同步。
- 多角色审查报告为 `decision: pass`，无 P0–P2。
- 安全、Python、前端、领域和 Mock 集成门禁通过。
- 提交树与被审查树哈希一致。
- Stage 0 以外全部通过阶段分支、Draft PR 和 squash merge。

`phase-close.ps1` 只执行一轮门禁、提交和 GitHub 操作。最多三轮修复由主 Agent 记录和编排，脚本自身不持久化重试次数，也不会自动修改代码。

真实 MATLAB/DPABI 长作业、真实数据写入、受试者排除和科学参数选择不属于代码自动交付权限。
