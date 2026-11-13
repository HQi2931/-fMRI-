# MVP 产品范围

- 状态：Implementing
- 文档类型：Product
- 维护者：documentation_maintainer
- 更新日期：2026-10-09
- 关联：[验证记录](../development/mvp-verification.md)、[ADR 0006](../adr/0006-v0.1-real-execution.md)

## 产品定位

rs-fMRI Agent 是 Windows 本地优先、单用户的静息态 fMRI 科研工作流辅助平台。它把数据检查、科学参数来源、计划审批、受控执行、产物谱系、QC 和统计设计放在一个可审核流程中。

产品只用于科研流程辅助，不提供临床诊断、治疗建议或显著性优化。

## 目标用户与任务

- 需要检查 DICOM、BIDS NIfTI、普通 NIfTI/JSON 或 DPABI-ready 目录的研究者。
- 需要显式记录 DPABI 预处理、ALFF/fALFF、ReHo 顺序和参数来源的方法人员。
- 需要按冻结受试者清单对齐人口学、QC 和统计设计的项目维护者。
- 需要用 Agent 解释计划、总结已登记日志或起草报告，但不允许模型直接执行代码的用户。

## 当前能力与边界

| 能力 | 当前状态 | 边界 |
| --- | --- | --- |
| 项目、数据集与只读扫描 | 已实现 | 扫描生成元数据和 manifest，不修改源目录 |
| 人口学导入与数据集划分 | 已实现 | 需显式字段映射；划分以 subject 为单位 |
| Skill Registry/Resolver/Validator/Compiler | 已实现 | Work 预检会在 Plan 写入前阻断当前执行器不能表达的 preprocessing + metric + `ON_RESULTS` normalization、无 metric 的 `ON_RESULTS` normalization/smoothing，以及同计划 ReHo + CUT scrubbing；正式 metric-only resolve 还要求 manifest 恰有一个 subject 且输入 Artifact lineage.subject_id 与之匹配。预处理同 session 多 subject 仍允许；这些均为执行能力边界，不代表检查真实图像或掩膜。ReHo + CUT 需先核验 retained-volume checkpoint |
| 计划审批、SQLite 队列、SSE 与恢复 | 已实现 | Worker 通过统一 ToolRuntime 路由 Mock 或受控 MATLAB |
| 用户选择 MATLAB/SPM/DPABI 路径与环境探测 | 已接线 | 路径保存于本机工作目录；版本标签不代表通用兼容 |
| DPABI 受控 Cfg 投影与 MATLAB 固定模板 | 已接线 | 以用户选定目录中的受控入口为准，真实 smoke 仍需本机环境与人工授权 |
| ALFF/fALFF 与 ReHo 顺序、谱系、QC 合同 | 已实现领域规则 | 真实指标产物必须通过元数据与新鲜度验证 |
| 人工 QC revision | 已实现 | 只接受类型化、谱系完整的指标 Artifact |
| Work 预处理与数据分析双 Agent | 已实现角色路由、角色工具目录与独立草稿 | 真实 Provider 的路由表现仍取决于本机模型配置 |
| 六类 DPABI 统计设计、固定模板及 FDR/GRF | 六类已在本机 MATLAB R2024b/SPM25/DPABI V9.0_250415 通过真实 smoke | 本机验证不表示任意软件版本或所有设计配置均兼容 |
| 统计图、效应量、簇表与可复现报告 | 真实结果合同、图谱定位、簇/切片联动和 Markdown/JSON 下载已实现 | 缺任一必需证据角色或空间信息时失败关闭/阻止定位 |
| 外部 DPABI 结果目录登记 | 只读检查、独立纳入确认、mask 校验及服务/Worker 统计来源绑定已接线 | Work 对话来源选择、外部配对与人口学 revision 接入仍待补齐 |
| 多 Provider Agent | 已实现结构化网关和 Mock 测试 | 真实 Provider smoke 取决于本地 Key |
| 中文 Web 工作台 | 已实现候选流程 | Playwright 使用 Mock API，不证明真实 MATLAB/统计执行 |

## 不得被隐藏的缺口

- `scripts/synthetic-demo.py` 已覆盖数据扫描到确定性合成报告，但通过内部测试 seam 注入 typed 占位 Artifact；它不证明真实影像算法或统计执行正确。
- Mock 产物 `mock.result` 不是 ALFF/fALFF、ReHo、脑掩膜或统计图，不得冒充科研输入。
- 通用 Artifact API 返回元数据；已登记统计结果另有受权限和哈希校验的产物预览与下载接口。
- 受控 MATLAB Executor 已注册不等于真实科研运行已获授权或已通过 smoke。
- Agent 报告草稿不等于确定性复现报告；后者不调用 Provider，并对真实结果证据失败关闭。

## 当前范围边界

当前能力的边界只有在以下条件全部满足时才算经过完整验证：

1. 当前最终树的安全、Python、Web、领域和 Mock E2E 门禁全部通过。
2. 多角色终审无 P0–P2，审查报告绑定最终暂存树。
3. `main` 推送、GitHub Actions 和仓库保护规则实际成功。
4. 至少一个真实 Provider 轻量 smoke 在不暴露密钥的情况下通过。
5. 真实 MATLAB/DPABI 小数据 smoke 获得用户单独授权并记录验证结果。
6. 六类统计入口及 FDR/GRF 已通过当前本机 DPABI smoke；新增软件版本或设计范围仍需按对应安装单独验证，效应图、簇表、校正证据和报告合同保持完整。

已验证的部分与仍未执行的部分逐项记录在 [验证记录](../development/mvp-verification.md)，不得把局部验证当作全面验证。

## 非目标

- 多用户权限、云部署、PACS、微服务和通用插件市场。
- 长期记忆、通用 RAG 和多 Agent 产品运行时。
- 自动决定排除受试者、统计方向、频段、阈值或多重比较方法。
- 临床诊断、临床风险分级或自动科学结论。

当前验证边界见 [验证记录](../development/mvp-verification.md)，后续演进阶段见 [计划 0007](../plans/0007-capability-modularization-roadmap.md)，真实执行范围决策见 [ADR 0006](../adr/0006-v0.1-real-execution.md)。
