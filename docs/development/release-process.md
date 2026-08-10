# 阶段审查与自动发布

## 一次性准备

运行 `scripts/bootstrap.ps1`，然后由仓库维护者执行 `gh auth login`。Stage 0 首次推送后运行 `scripts/configure-github.ps1`，启用 `main` 的 PR、`agent-review`、`quality-gate`、线性历史和禁止强推规则。

## 阶段分支

- Stage 0：唯一允许直接提交并推送 `main` 的阶段。
- 后续：`codex/phase-XX-<slug>`。
- 提交：`feat(phase-XX): <title>`；修复追加新提交，不 amend、不 force-push。
- 合并：Draft PR -> required checks -> Ready -> auto squash -> 删除远程分支。

## Agent 审查矩阵

- 始终执行：QA、文档。
- fMRI、Skill、Workflow、DPABI：方法学、Skill/Workflow、MATLAB/DPABI。
- API、模型或跨层边界：系统架构、后端。
- 用户界面：前端体验。

审查由 Codex 主任务按 `.agents/` 角色契约组织，`phase-close.ps1` 本身不启动 Agent。审查对象必须是最终暂存树。P0–P2 阻断；P3 必须写入 PR 并由维护者或后续自动化创建跟踪任务。

审查结果写入 `docs/reviews/phase-XX-<slug>.md`。报告必须同时包含独立一行 `decision: pass` 和 `reviewed-tree: <git-tree-hash>`；后者是排除审查报告自身、但包含其余完整候选提交内容的 Git tree hash。终审未通过时不得预先创建 pass 报告。审查完成后若任何候选文件变化，必须重新门禁、重新审查并更新该哈希。

## 自动化命令

```powershell
.\scripts\quality-gate.ps1
.\scripts\phase-close.ps1 `
  -Phase 3 `
  -Slug skills `
  -Title 'implement immutable fMRI skill plans' `
  -Paths @('neuroagent/skills', 'neuroagent/domain/fmri', 'tests/science', 'docs')
```

`phase-close.ps1` 对一次阶段关闭执行以下步骤：

1. 检查审查报告的 `decision: pass` 和 `reviewed-tree` 声明。
2. 在创建分支或暂存之前检查 GitHub CLI 和认证；`-SkipPush` 只用于本地演练。
3. 仅暂存 `-Paths` 和审查报告，拒绝继承范围外的索引内容。
4. 运行安全、文档、Python、Web 和 Mock E2E 门禁，再检查最终暂存树。
5. 在审查报告尚未暂存时计算候选内容 tree hash，并与 `reviewed-tree` 精确比较；随后暂存审查报告、记录最终提交 tree hash，提交并再次验证。
6. 推送分支，创建 Draft PR，发布 `agent-review=success`。
7. 用 `gh pr checks --watch --fail-fast` 等待 required checks；任一 check 失败即停止。
8. checks 通过后转为 Ready，并设置自动 squash merge。

脚本对关键 `gh` 调用逐项检查退出码。Stage 0 在推送 `main` 后停止，不创建 PR，然后需要单独运行仓库保护配置。

## 合并后本地同步

当 GitHub 完成自动合并后，当前脚本不会继续等待并删除本地分支。维护者应在确认 PR 已合并后执行：

```powershell
git switch main
git pull --ff-only origin main
git branch -d codex/phase-XX-<slug>
```

不要在 PR 尚未合并或本地分支仍有未提交修改时删除分支。

## 失败与停止条件

`phase-close.ps1` 每次只执行一轮，在首个失败处退出。“最多自动修复三轮”由主 Agent 的阶段编排记录执行，当前脚本不持久化重试计数，也不会自动修改代码。

以下情况必须停止并报告维护者：

- 同一阻断条件在有记录的三轮修复中重复出现。
- 出现真实受试者数据、密钥或大型科研文件。
- DPABI 映射未被本机 V8.2 源码证实。
- 科学参数来源不明确。
- GitHub 认证、远程连接、required checks 或分支保护配置失败。

发现已推送密钥时先在提供方撤销密钥，然后暂停自动发布；不自动改写 Git 历史。
