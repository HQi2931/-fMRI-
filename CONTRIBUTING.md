# 开发贡献约定

1. 阅读 `AGENTS.md`、相关架构文档和 ADR。
2. 从最新 `main` 创建 `codex/phase-XX-<slug>`。
3. 只修改当前阶段范围内文件；代码、测试、文档和 CHANGELOG 同步更新。
4. 使用合成数据和 Mock Executor；不得提交真实影像、人口学数据、密钥或本机配置。
5. 执行 `scripts/quality-gate.ps1`，完成项目角色审查并记录到 `docs/reviews/`。
6. 使用 `scripts/phase-close.ps1` 明确指定本阶段文件；脚本负责提交、推送和 Draft PR。

科学参数、真实 MATLAB 长任务、受试者排除和统计方法变更必须由用户批准，代码审查不能代替科研审批。
