# rs-fMRI Agent

面向静息态 fMRI 研究的本地工作流应用。系统检查并冻结输入清单，使用声明式 Skill 生成可审查计划，经用户批准后由独立 Worker 执行受控任务，并保存产物谱系、QC 和统计设计。

> 当前项目仅用于科研流程辅助，不提供临床诊断。原始 DICOM、NIfTI 和人口学源数据默认只读，禁止提交到 Git。

## 核心边界

- Agent 负责理解、解释和生成结构化建议，不能直接执行任意命令。
- Skill 声明科研能力并编译不可变计划，不能直接执行 Tool。
- Workflow 是运行状态唯一真源；只有获批且未失效的计划可以启动。
- MATLAB/DPABI 只在独立 staging 工作目录运行，真实长任务必须由用户明确批准。
- 外部模型只能接收经过策略校验的去标识化摘要。

## 当前实现状态

- 可运行：项目/数据集、只读扫描、人口学映射、受试者级划分、Skill 解析与计划审批、SQLite 队列、SSE、统一 ToolRuntime Mock 执行、人工 QC、三类 t 检验的设计/审批、FDR/GRF 参数校验、模型路由与中文 Web 工作台。
- 已实现：用户选择本机 MATLAB/SPM/DPABI 路径、入口探测、受控 DPABI `Cfg` 投影、固定 MATLAB 模板、Windows 空格路径、超时/取消、预期产物完整性检查，以及真实统计结果的证据登记与确定性 Markdown/JSON 复现报告。
- 已接线但需本机授权/环境验证：公共 Worker 的受控 MATLAB/DPABI 统计适配、三类真实 t 检验、FDR/GRF、效应量、26 邻接簇表和结果证据登记；不能用未完成 smoke 的状态替代真实验证。
- 已实现为本地确定性预览：长任务阶段事件与失败诊断、ROI 长宽表合同、DPABI 整理预览、CSV/TSV/XLSX 检查、subject-level ML 模板、cluster 坐标匹配和 rs-fMRI 本地证据问答。它们不启动 MATLAB、训练模型或联网检索。
- 尚需本机授权验证：仓库外确定性合成数据的真实 MATLAB 小型 smoke 与至少一个真实模型 Provider 调用。运行页默认 Mock，MATLAB 选项需要不可跳过的逐次确认。

`v0.1.0` 尚未发布。当前是待审候选基线，详细范围和未完成项见 [MVP 范围](docs/product/mvp-scope.md)。

## 本地开发

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
.\scripts\bootstrap.ps1
uv run alembic upgrade head
uv run rsfmri-api
```

另开终端启动 Worker：

```powershell
uv run rsfmri-worker
```

前端开发服务器：

```powershell
Set-Location web
npm run dev
```

运行完整本地门禁：

```powershell
.\scripts\quality-gate.ps1
```

生成纯合成、无影像数据的后端演示：

```powershell
uv run python scripts\synthetic-demo.py --root tmp\synthetic-demo
```

该演示使用真实应用服务、SQLite 和 Mock Worker（不经过 HTTP），覆盖“只读 BIDS 扫描 → Skill 计划与审批 → Mock 运行 → 明确的测试夹具 Artifact 注入 → 人工 QC → 单样本 t 检验与 FDR 设计审批 → 统计 Mock → 确定性 Markdown/JSON 报告”。所有占位 Artifact 和报告均醒目标记为 `synthetic_non_scientific`，不包含统计数值，也不能用于科学或临床推断。

## 文档入口

- [开发代理规则](AGENTS.md)
- [项目结构](docs/architecture/project-structure.md)
- [Skill 层设计](docs/architecture/fmri-skill-layer.md)
- [系统设计](docs/architecture/system-design.md)
- [开发路线图](docs/plans/0002-full-mvp-roadmap.md)
- [开发与发布](docs/development/release-process.md)
- [API v1 契约](docs/api/api-v1.md)
- [MVP 验证与限制](docs/development/mvp-verification.md)
- [MVP 范围](docs/product/mvp-scope.md)
- [ADR 0006：v0.1.0 真实执行范围](docs/adr/0006-v0.1-real-execution.md)
- [计划 0005：用户选择本机科学软件环境](docs/plans/0005-user-selected-local-environment.md)
- [本地运行与恢复](docs/operations/local-operations.md)
- [Provider 配置与 smoke](docs/operations/provider-setup.md)

## 本机集成

实际路径通过未跟踪的 `.env` 配置。CI 只运行 Mock Executor、ToolRuntime/DAG 与静态映射测试，不启动真实 MATLAB；本机真实执行还需 `RSFMRI_ENABLE_REAL_EXECUTION=true`、环境探测通过和逐次确认。
