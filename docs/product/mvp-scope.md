# MVP 产品范围

状态：`v0.1.0` 候选，未发布
更新日期：2026-08-07

## 产品定位

rs-fMRI Agent 是 Windows 本地优先、单用户的静息态 fMRI 科研工作流辅助平台。它把数据检查、科学参数来源、计划审批、受控执行、产物谱系、QC 和统计设计放在一个可审核流程中。

产品只用于科研流程辅助，不提供临床诊断、治疗建议或显著性优化。

## 目标用户与任务

- 需要检查 DICOM、BIDS NIfTI、普通 NIfTI/JSON 或 DPABI-ready 目录的研究者。
- 需要显式记录 DPABI 预处理、ALFF/fALFF、ReHo 顺序和参数来源的方法人员。
- 需要按冻结受试者清单对齐人口学、QC 和统计设计的项目维护者。
- 需要用 Agent 解释计划、总结已登记日志或起草报告，但不允许模型直接执行代码的用户。

## 当前候选基线

| 能力 | 当前状态 | 边界 |
| --- | --- | --- |
| 项目、数据集与只读扫描 | 已实现 | 扫描生成元数据和 manifest，不修改源目录 |
| 人口学导入与数据集划分 | 已实现 | 需显式字段映射；划分以 subject 为单位 |
| Skill Registry/Resolver/Validator/Compiler | 已实现 | 统一 ToolRuntime 只接受已批准计划 |
| 计划审批、SQLite 队列、SSE 与恢复 | 已实现 | Worker 通过统一 ToolRuntime 路由 Mock 或受控 MATLAB |
| 用户选择 MATLAB/SPM/DPABI 路径与环境探测 | 已接线 | 路径保存于本机工作目录；版本标签不代表通用兼容 |
| DPABI 受控 Cfg 投影与 MATLAB 固定模板 | 已接线 | 以用户选定目录中的受控入口为准，真实 smoke 仍需本机环境与人工授权 |
| ALFF/fALFF 与 ReHo 顺序、谱系、QC 合同 | 已实现领域规则 | 真实指标产物必须通过元数据与新鲜度验证 |
| 人工 QC revision | 已实现 | 只接受类型化、谱系完整的指标 Artifact |
| 三类 t 检验、相关/回归领域模型与 FDR/GRF | 三类 t + FDR/GRF 纳入 v0.1 | 相关/回归明确延后；真实执行需 smoke |
| 统计图、效应量、簇表与可复现报告 | 真实结果合同已实现 | 缺任一必需证据角色即失败关闭 |
| 多 Provider Agent | 已实现结构化网关和 Mock 测试 | 真实 Provider smoke 取决于本地 Key，尚未验证 |
| 中文 Web 工作台 | 已实现候选流程 | Playwright 使用 Mock API，不证明真实 MATLAB/统计执行 |

## 不得被隐藏的缺口

- `scripts/synthetic-demo.py` 已覆盖数据扫描到确定性合成报告，但通过内部测试 seam 注入 typed 占位 Artifact；它不证明真实影像算法或统计执行正确。
- Mock 产物 `mock.result` 不是 ALFF/fALFF、ReHo、脑掩膜或统计图，不得冒充科研输入。
- 当前 Artifact API 只返回元数据，不提供文件下载。
- 受控 MATLAB Executor 已注册不等于真实科研运行已获授权或已通过 smoke。
- Agent 报告草稿不等于确定性复现报告；后者不调用 Provider，并对真实结果证据失败关闭。

## `v0.1.0` 发布条件

候选基线只有在以下条件全部满足后才能标记 `v0.1.0`：

1. 当前最终树的安全、Python、Web、领域和 Mock E2E 门禁全部通过。
2. 多角色终审无 P0–P2，审查报告绑定最终暂存树。
3. 首次 `main` 推送、GitHub Actions 和仓库保护规则实际成功。
4. 至少一个真实 Provider 轻量 smoke 在不暴露密钥的情况下通过。
5. 真实 MATLAB/DPABI 小数据 smoke 获得用户单独授权并记录验证结果。
6. 真实统计三类 t 检验、可选 FDR/GRF、效应量、簇表和报告闭环通过仓库外确定性合成 smoke。

## 非目标

- 多用户权限、云部署、PACS、微服务和通用插件市场。
- 长期记忆、通用 RAG 和多 Agent 产品运行时。
- 自动决定排除受试者、统计方向、频段、阈值或多重比较方法。
- 临床诊断、临床风险分级或自动科学结论。

开发阶段和完成情况见 [全量 MVP 路线图](../plans/0002-full-mvp-roadmap.md)，当前验证边界见 [MVP 验证与已知限制](../development/mvp-verification.md)，版本决策见 [ADR 0006](../adr/0006-v0.1-real-execution.md)。
