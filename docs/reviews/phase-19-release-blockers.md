# Phase 19 发布阻断终审

decision: pass
reviewed-tree: 0fb856ccb6c06046ee0f8f91f01edbcf5a14c801

## 审查范围

- 冻结 SkillPlan/manifest 到公共 MATLAB 预处理 Worker 的编译、输入 staging、独立 attempt、输出证据和 Artifact lineage。
- MATLAB 统计三类 t 检验、FDR/GRF、调整后效应量、正负簇分离、运行时软件证据及确定性报告。
- 用户本机 MATLAB/SPM/DPABI 配置语义、真实运行确认文案和 GitHub 发布自动化。
- 原始输入只读、路径边界、审批/环境/工具锁漂移、重试恢复与失败关闭行为。

## 审查结论

QA、fMRI 方法、Skill/Workflow 与 MATLAB/DPABI 复核未发现 P0–P2 阻断。审查中发现并已修复：DPARSFA 的 T1 Segment/DARTEL headless GUI 路径、EPI template 对 realignment mean image 的依赖、无指标 OnResults 操作、未验证 mask、自报网格签名、错误 producer step hash，以及真实运行确认框的旧版本硬编码。

保留的范围限制均失败关闭：单会话且每主体一个 4D NIfTI，metric-only 限单主体；T1 Segment/DARTEL 不进入 headless 执行；需要不同阶段 mask 的 OnResults 组合及无指标 OnResults 操作不执行。EPI normalization、slice timing、realignment、nuisance、smoothing、scrubbing 和压缩 NIfTI 分支未逐一完成真实 smoke，不作超出验证范围的兼容承诺。

## 验证证据

- 最终候选树本地门禁：Python `457 passed, 3 skipped`，总覆盖率 `86.05%`；Ruff、strict mypy、pip-audit 通过。
- Web：`54 passed`，statement/branch/function/line 覆盖率均满足门槛；ESLint、TypeScript、生产构建、3 项 Playwright Mock E2E 和 npm audit 通过。
- 真实统计合成 smoke：三类 t 检验及模板代数共 `4 passed`；覆盖 FDR、负尾 GRF、协变量效应量和结果报告。
- 真实预处理合成 smoke：公共 Worker 两个作业成功到 QC；124 volumes 删除 4 个后保留 120 个，源 SHA-256 不变；组合 ALFF、fALFF、ReHo、时序、日志和 metadata evidence 均登记。
- 真实 Provider 轻量 smoke 成功，未发送受试者信息。
- GitHub `main` 保护和 merge 设置已通过 API 回读；PR 后的 Actions、squash merge 和 tag 仍由阶段发布流程验证。
- CI 修复复核：升级 gitleaks action 到 v3.0.0，并使 repository split 回归测试在 PR 合并检出缺少历史对象时回退到 `origin/main` merge-base。

本报告证明的是上述候选内容树。审查报告自身不包含在 `reviewed-tree` 中，随后由阶段关闭脚本单独暂存并验证。
