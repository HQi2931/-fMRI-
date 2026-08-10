# AGENTS.md

本文件是 Codex 和其他代码智能体在本仓库中工作的项目级说明。开始任何开发任务前，先阅读本文件以及任务涉及目录中的 `README.md`。如果实现、文档和本文件存在冲突，不要静默选择；先核对最新用户要求，并在必要时更新文档或记录 ADR。

## 1. 项目目标

本项目面向静息态 fMRI 科研工作流，核心目标分为两部分：

1. 基于 MATLAB、SPM12 和 DPABI/DPARSF 的影像数据检查、整理与预处理。
2. 对预处理产物进行组水平统计分析，包括 t 检验、协变量处理和多重比较校正。

辅助能力包括人口学信息整理、受试者清单管理、数据集划分、质量控制、运行监控、产物管理和可复现报告。

项目参考 Datawhale Hello-Agents 的 Agent、Skill、Tool、Workflow、Memory、Context 和可观测性思想，但不复制其运行时。当前产品首先是一套可靠、可审核、可复现的静息态 fMRI 工作平台，而不是通用 Agent 框架或临床诊断系统。

## 2. 当前状态

- v0.1.0 候选基线已实现 FastAPI/SQLite/Worker、React 前端、数据清单、Skill 编译与审批、Mock 执行、QC、统计设计和多 Provider Agent 的本地闭环；只有最终审查、CI、外部 smoke 与发布条件全部满足后才能标记 v0.1.0。
- 公共运行接口当前默认且明确使用 Mock Executor。受控 MATLAB 模板、DPABI V8.2 参数投影、超时/取消和产物完整性检查已实现并通过静态/模拟测试，但尚未在本仓库自动启用真实 MATLAB 作业。
- 纯合成后端 E2E 与确定性统计复现报告合同已实现；其中 typed 指标和统计结果角色是醒目标记的测试占位，不得冒充真实科研产物。
- 真实 Provider 轻量调用取决于本机 API Key；CI 只使用 Mock Provider。真实 MATLAB smoke、真实 Provider smoke 和任何真实数据处理都必须单独授权并记录验证结果。
- `docs/architecture/neuroagent-framework-architecture.md` 是已标记为部分被替代的早期通用框架草案，其中的非目标和路线图不再作为当前实现依据；当前以本文件、fMRI Skill 架构和 ADR 为准。
- `skills` 已由用户明确纳入近期范围，用于表达 ALFF/fALFF、ReHo、统计分析等科研能力的适用条件、步骤顺序、参数、产物和 QC，并编译为受控 Workflow。
- `memory`、`retrieval`、`multi_agent` 和通用插件系统仍不是近期 MVP 的前置依赖，不要优先实现。
- 优先建立最小垂直闭环：数据检查 → Skill 解析与方案校验 → 人工确认 → Workflow/MATLAB/DPABI 执行 → QC → 统计分析 → 报告。

## 3. 已确认的本机科研软件环境

当前开发机已确认目标软件为 MATLAB R2023b、SPM12 和 DPABI V8.2_240510。实际绝对路径只允许写入未跟踪的本地 `.env`；仓库文档、代码和 `.env.example` 不记录机器私有路径。

规则：

- 以本机 DPABI V8.2_240510 的真实接口为实现基线，不混用 DPABI V9 的字段或行为。
- DPABI 安装路径包含空格，PowerShell、Python 和 MATLAB 调用必须正确引用完整路径。
- 不修改 MATLAB、SPM12 或 DPABI 安装目录中的任何文件。
- 影像工作目录和输出目录优先使用不含空格、中文和特殊字符的路径。
- 自动化前先验证 `matlab.exe`、`spm.m`、`dpabi.m` 和目标 DPABI 函数是否可见。
- 不在日常测试中启动真实长时间预处理；真实 MATLAB 集成运行必须由用户明确授权，并使用小型、脱敏或合成数据。

已确认的 DPABI V8.2 入口包括：

- `DPARSFA_run(CfgOrMat, WorkingDir, SubjectListFile, IsAllowGUI)`
- `DPARSF_run(CfgBasic)`
- `DPABI_BIDS_Converter_run(...)`
- `y_TTest1_Image(...)`
- `y_TTest2_Image(...)`
- `y_TTestPaired_Image(...)`
- `y_GroupAnalysis_Image(...)`
- `y_GroupAnalysis_PermutationTest_Image(...)`
- `y_FDR_Image(...)`
- `y_GRF_Threshold(...)`

自动化预处理优先调用 `DPARSFA_run`，使用包含 `Cfg` 的 `.mat` 配置，并在非交互作业中设置 `IsAllowGUI=0`。不要依靠 GUI 回调作为稳定的服务接口。

## 4. 系统架构方向

近期采用本地优先的模块化单体，不拆微服务：

```text
Web Frontend
→ FastAPI API
→ Application Services
→ fMRI Domain Services
→ Skill Resolver / Validator / Compiler
→ Workflow / Job Service
→ Registered Tools
→ MATLAB Executor
→ MATLAB + SPM12 + DPABI V8.2
```

同时使用：

- 元数据数据库保存项目、受试者、方案、运行状态、QC、产物引用和审计事件。
- 文件系统保存配置、MATLAB 脚本、日志、NIfTI 产物、QC 图和报告。
- REST 处理命令和查询；SSE 传递长作业状态与日志。
- 单独的 Worker 进程运行 MATLAB，避免阻塞 API 服务。

单机 MVP 可以使用 SQLite 和本地文件存储。只有在出现真实多用户或多计算节点需求后，才考虑 PostgreSQL、Redis、对象存储或微服务，并通过 ADR 记录变更原因。

## 5. 分层职责

### API 层

- 处理 HTTP、请求校验、响应和 SSE。
- 不包含 fMRI 业务规则，不直接启动 MATLAB，不直接拼接系统命令。

### 应用服务层

- 编排完整用例，例如扫描数据集、创建预处理方案、提交运行、审核 QC、执行统计分析。
- 管理 `PlanRevision` 的 `draft → validating → awaiting_approval → approved/superseded` 生命周期和追加式 `ApprovalRecord`；计划内容本身不可变。
- 负责事务边界、权限检查和跨领域服务协调。
- 不实现具体影像算法。

### fMRI 领域层

- 定义 Dataset、Subject、Scan、SubjectManifest、PreprocessingPlan、StatisticalDesign、QCResult 等模型和规则。
- 处理 T1/fMRI 配对、受试者顺序、参数约束、QC 和统计设计验证。
- 不依赖 FastAPI、数据库实现或 subprocess。

### Skill 层

- Skill 是版本化、声明式科研能力配方，负责适用条件、参数 schema、Artifact 合同、步骤偏序、能力需求、QC、证据和兼容性。
- `SkillResolver`、`SkillValidator` 和 `SkillCompiler` 将请求编译为不可变且不携带可变状态的 `SkillPlan`；批准后任何输入、参数或版本变化都必须生成新 revision。
- Skill 可以引用工作流模板和工具能力，但不能直接执行 Tool、保存运行状态、携带任意 Shell/MATLAB 文本或绕过审批。
- 首期 fMRI Skill 作为内置能力提供，不以通用插件系统为前置条件。
- 详细设计见 [`docs/architecture/fmri-skill-layer.md`](docs/architecture/fmri-skill-layer.md) 和 [`docs/adr/0001-skill-compiles-to-workflow.md`](docs/adr/0001-skill-compiles-to-workflow.md)。

### 工作流与作业层

- 只从已批准计划创建运行，管理 `queued → running → qc_review → succeeded` 以及失败、取消和恢复等运行状态；审批前计划状态不属于 WorkflowInstance。
- 支持失败、取消、超时、重试和恢复。
- Agent 和 Tool 只能请求状态变化，不能直接修改状态字段。
- WorkflowInstance 是运行状态唯一真源；不得为 Skill 再建立一套可变运行状态机。

### 工具层

- 一个工具只完成一个边界清晰、可测试的确定性操作。
- 每个工具声明输入/输出 schema、读写范围、风险级别、超时、产物和是否支持 dry-run。
- 工具不承担整条业务流程，不自由执行 Shell 或任意 MATLAB 文本。

### 执行与基础设施层

- MATLAB Executor 只接受经过验证的结构化 JobSpec。
- 在独立运行目录生成固定模板脚本和配置，再用 `matlab.exe -batch` 启动。
- 捕获 stdout、stderr、退出码、超时和进程树；输出必须注册为 Artifact。
- 数据库、文件存储和事件实现位于基础设施层，不把实现细节泄漏到领域模型。

### Agent 层

- Agent 可以理解用户意图、提出结构化方案、解释参数、总结日志和生成报告草稿。
- Agent 不能直接执行任意代码、覆盖文件、排除受试者、改变统计方法或推进工作流。
- 所有科学参数必须经过 schema 验证；关键预处理和统计方案必须由用户确认。
- Agent 只能推荐、解释和补全 SkillRequest，不能自由排列 Skill 步骤或覆盖 SkillValidator 的阻断问题。

## 6. 数据与科研安全

- 原始 DICOM、NIfTI、BIDS 和人口学源文件默认只读。
- 整理、转换和预处理结果写入独立工作区，不原地重命名、移动、删除或覆盖原始数据。
- 所有文件访问必须经过规范化路径和允许根目录校验，拒绝目录穿越和越界写入。
- 统计分析不得依赖文件系统隐式排序。影像、组别和协变量必须通过冻结的 `subject_id` 清单显式对齐。
- 每个指标 Artifact 必须记录完整处理谱系；Skill 的适用性校验不能只依据文件名或“已预处理”标签。
- 同一受试者的多个扫描在数据集划分中不得泄漏到不同集合。
- 传统组水平 t 检验默认不做训练集/测试集划分；只有机器学习或探索/验证设计需要划分。
- 文档、测试、日志和示例不得包含真实姓名、患者号、身份证号或可识别受试者信息。
- 项目不提供临床诊断。统计结果和 Agent 解释必须标明研究用途和方法学限制。
- 覆盖已有产物、删除文件、改变纳入/排除清单、启动长时间真实计算前需要明确确认。

## 7. MATLAB/DPABI 集成规范

每次运行使用独立目录，例如：

```text
runs/{run_id}/
├── input_manifest.tsv
├── subject_list.txt
├── config/
│   ├── preprocessing.json
│   └── DPARSFACfg.mat
├── scripts/
│   ├── bootstrap.m
│   └── run_preprocessing.m
├── logs/
│   ├── matlab.log
│   └── {job_id}/
│       ├── attempt-001/
│       │   ├── matlab.stdout.log
│       │   └── matlab.stderr.log
│       └── attempt-002/ ...
├── output/
├── qc/
└── provenance.json
```

要求：

- LLM 只提出结构化参数，不直接生成并执行自由文本命令。
- MATLAB 脚本由确定性模板渲染器生成，变量通过配置文件传入。
- 保存 MATLAB、SPM、DPABI 版本、输入清单哈希、参数哈希、模板版本和执行时间。
- 生成的 `.m`、`.mat`、受试者清单和设计矩阵都是正式运行产物，不是临时垃圾文件。
- 失败时保留日志和已经生成的只读产物，不把部分结果标记为成功。
- DICOM/NIfTI/BIDS 整理逻辑与科学预处理分开测试。
- ALFF/fALFF 与 ReHo 不能被视为消费同一个最终“已预处理”文件；必须按 Artifact 谱系和顺序约束声明各自消费的中间检查点。编译结果既可以是含类型化检查点的一次 DPARSFA 线性运行，也可以是显式分支。
- 本机高级入口 `IsCalALFF` 一次同时生成 ALFF 与 fALFF；标准 fALFF Skill 不接受已经带通滤波的输入。
- ReHo 计算输入必须保持未空间平滑；需要平滑时在结果图上执行，并校验 `CalReHo.SmoothReHo` 与全局结果平滑不会重复。

## 8. 统计分析规范

- 首期支持单样本 t 检验、两独立样本 t 检验、配对 t 检验、相关和带协变量回归。
- 设计必须显式记录受试者顺序、分组编码、协变量列、缺失值处理、中心化方法、对比向量、掩膜和尾部设置。
- 多重比较校正与统计检验分开建模，支持的具体方法必须与 DPABI V8.2 接口一致。
- 输出至少包含设计矩阵、对比、未校正统计图、校正结果、效应量、显著簇表、软件版本和日志。
- 不自动选择最“显著”的方法，不进行未记录的多重尝试。方法变化必须创建新版本的 StatisticalDesign 和 Run。
- 科学默认值、阈值和排除规则不得仅凭模型常识写入；需要来源、项目方案或用户确认。

## 9. 文档即代码

- 所有 Markdown 使用 UTF-8。
- 重要功能必须同时更新代码、测试和相关文档。
- 根 README 说明项目用途、快速开始、状态和文档入口。
- `docs/product/` 记录目标、范围、需求和路线图。
- `docs/architecture/` 记录系统、前后端、服务层、工具层、数据模型、安全和部署设计。
- `docs/domain/` 记录 DPABI 环境、输入契约、预处理、QC、统计和人口学规则。
- `docs/adr/` 记录不可逆或影响范围较大的设计决策。已接受 ADR 不覆盖历史；用新 ADR 标记替代。
- `docs/plans/` 为每个里程碑记录目标、范围、任务、风险和完成标准。
- `CHANGELOG.md` 记录用户可见变化。
- 不复制大段第三方文档；记录版本、入口和原始来源。

任何架构、公共接口、数据模型、DPABI 参数映射、工作流状态或安全边界的改变，都必须在同一变更中更新文档。若文档暂时无法同步，该功能不算完成。

## 10. 开发工作流

1. 阅读根 `AGENTS.md`、相关 README、产品需求、架构文档和 ADR。
2. 检查 Git 状态，保留并避开用户已有改动。
3. 对较大任务先写或更新需求、ADR 和实施计划，再编码。
4. 优先完成最小端到端闭环，不为未来需求提前建立复杂抽象。
5. 实现后运行与风险匹配的测试，并记录未验证部分。
6. 同步更新文档和 CHANGELOG。
7. 检查差异，确保没有数据、密钥、机器私有配置或大型科研产物进入 Git。
8. 除非用户明确要求，不自行提交、推送、创建分支或 Pull Request；本项目的阶段计划已明确授权门禁通过后的阶段提交与推送，但认证失败时必须停止并报告。

远程仓库：

```text
origin = https://github.com/HQi2931/-fMRI-.git
```

## 11. 编码与依赖约定

- Python 标识符、模块名、API 字段和代码注释优先使用英文；面向用户的文档和界面默认使用中文。
- 代码应有类型标注，领域模型避免使用无约束字典传递关键数据。
- 领域层保持纯净，不导入 Web、数据库或进程执行框架。
- 外部依赖必须有明确用途；不要在没有实际需求时引入大型框架、数据库或消息队列。
- 新增依赖时更新环境说明和锁定文件，并说明许可证或部署影响。
- 配置与密钥分离；密钥不写入仓库、日志、异常消息或模型上下文。
- 不硬编码用户数据目录。DPABI、SPM 和 MATLAB 路径通过配置提供，本文件中的路径只作为当前开发机默认值。
- 避免循环依赖；依赖总体由 API/Application 指向 Domain/Ports，再由 Infrastructure 实现端口。

## 12. 测试要求

- 单元测试覆盖领域规则、schema、受试者对齐、状态机和路径策略。
- 集成测试使用 Mock MATLAB Executor 或轻量探测，不运行完整预处理。
- MATLAB 包装器至少测试脚本渲染、路径引用、配置字段、日志解析、超时和失败映射。
- 使用合成、脱敏或公开许可的小型 fixture；大 NIfTI/DICOM 数据不直接提交 Git。
- 测试正常、输入错误、缺失文件、维度不一致、权限拒绝、超时、取消和恢复路径。
- 统计测试必须覆盖组别方向、配对顺序、协变量顺序、设计矩阵和对比向量。
- 修复缺陷时优先先写可复现测试。

## 13. 完成标准

一个功能只有同时满足以下条件才算完成：

- 需求和范围明确。
- 实现遵守分层边界和原始数据只读原则。
- 正常与关键失败路径有测试。
- 运行状态、错误和产物可追踪。
- 相关 README、架构、领域方法、API 或用户文档已更新。
- 用户可见变化已写入 CHANGELOG。
- 没有提交敏感数据、密钥、机器临时文件或大型运行产物。
- 对尚未真实验证的 MATLAB/DPABI 行为作出明确说明。

## 14. 遇到不确定情况时

- 科学方法不明确：停止猜测，列出需要用户或领域专家确认的参数。
- DPABI 接口不明确：只读检查本机 V8.2 源码和模板，不按其他版本推断。
- 数据格式不明确：先生成检查报告，不自动转换或移动原始数据。
- 架构选择影响较大：写 ADR，列出背景、决定、后果和替代方案。
- 任务可能覆盖、删除或长时间占用 MATLAB：先获得明确批准。

## 15. 开发协作 Agent

项目在 `.agents/` 中维护 Markdown 角色库。`.agents/*.md` 不是 Codex 原生 TOML 自定义 Agent 配置；主 Agent 必须先读取 [`.agents/README.md`](.agents/README.md)，再把选定角色文件作为子任务契约传给被委派的 Agent。

可用角色：

- `system_architect`：跨模块架构、ADR 和数据流。
- `backend_service_engineer`：API、服务、领域模型、工作流和持久化。
- `skill_workflow_engineer`：SkillSpec、解析、校验、编译和 Workflow 映射。
- `matlab_dpabi_engineer`：MATLAB、SPM12、DPABI V8.2 和执行器。
- `fmri_methodologist`：预处理、QC 和统计方法学只读审核。
- `frontend_ux_engineer`：非技术前端、长作业交互和结果展示。
- `qa_reviewer`：正确性、安全、测试和回归只读审核。
- `documentation_maintainer`：文档、ADR、计划和变更记录。

委派规则：

- 对至少包含两个独立、边界清晰工作流的复杂任务，主 Agent 应考虑使用对应角色并行协作；简单任务直接完成。
- 默认最多并行三个子 Agent，并服从客户端实际并发上限。
- 优先委派只读探索、方法审核、测试分析和文档检查。
- 写入型任务必须划分互不重叠的文件范围；不得让多个 Agent 并行编辑同一文件或紧密耦合模块。
- 主 Agent 在委派时提供明确目标、文件边界、禁止事项、验证要求和输出格式。
- 子 Agent 不得自行生成下级 Agent，除非主 Agent明确授权。
- 主 Agent 对最终设计、代码整合、差异检查、测试和用户交付负责。
- 委派不扩大权限；真实 MATLAB 长作业、原始数据写入、受试者排除、统计方案改变和外部写入仍需明确批准。
