# Execution 层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：matlab_dpabi_engineer
- 更新日期：2026-10-11
- 关联：[项目结构](../../docs/architecture/project-structure.md)

`neuroagent.execution` 负责结构化 Job、固定模板、受控进程和产物边界。它不决定科研参数、不修改工作流状态，也不接受 Agent 生成的任意 Shell 或 MATLAB 文本。

当前实现包括：

- `MatlabJobSpec`、路径绑定、冻结 SkillPlan/manifest 编译和必需产物合同。
- 执行器协议、`ExecutionResult` 与 MATLAB 编译器所需的窄产物目录端口由 `execution/ports.py` 所有；Worker 领取记录由 `workflow/models.py` 所有。Application 仅为旧调用点保留兼容重导出。编译器与预处理投影均不再导入 Application DTO；冻结 manifest 由 `execution/manifest.py` 的结构化只读协议描述，数据集分类枚举归领域层所有。
- 固定 MATLAB 模板渲染与 dry-run，可审查输入清单、Cfg、脚本、运行目录和预期产物。
- `MockMatlabExecutor`，用于 CI 和完整工作流测试。
- 默认禁止真实运行的 `ControlledMatlabExecutor` 已接入公共 Worker，支持 Windows 空格路径、超时、取消、退出码、日志和必需产物检查。执行前会快照预期输出；成功后仅登记本次新建或内容发生变化的非空普通文件，并记录大小和 SHA-256。目录、符号链接、零字节文件以及前次失败或重试遗留的未变化产物都会被拒绝。stdout/stderr 持续写入 `logs/<job_id>/attempt-NNN/`，重试保留历史；`MatlabJobResult` 只携带最多 1 MiB 的日志尾部，避免大输出阻塞或占满内存。Artifact API 提供元数据及受权限、路径与完整性检查约束的文件下载。
- 公共 MATLAB 适配器给每次执行建立独立 attempt 工作目录，重新从只读输入准备 staging，避免重试复用已删除 dummy volumes 的时间序列。
- DPABI 原位续跑读取冻结 manifest 的 `selected_input_stage`；`FunImgAR`、`FunImgARW`、`FunImgARWS` 等已核验 checkpoint 会关闭相应已完成步骤，禁止重复 dummy-volume 删除，并在运行前核对伴随文件哈希。头动 QC provenance 会区分 `prior_checkpoint` 与 `current_execution`。缺少显式阶段、步骤冲突或输出目录碰撞时失败关闭。
- 统计软件证据来自运行时 MATLAB/SPM 版本和 DPABI 源码 Release 标记；效应量使用冻结设计矩阵的对比方差，见 [ADR 0007](../../docs/adr/0007-adjusted-statistical-effects.md)。
- 新统计运行登记实际执行脚本、bootstrap、配置和暂存输入清单；执行前冻结、执行后及结果登记时复核哈希。结果报告收录四类证据，现有受权限约束的 Artifact 内容接口提供下载；历史结果保持兼容，不重建或冒充其原执行脚本。
- GRF 保存 DPABI 返回的最小簇、实际 DLH/来源、可获得的 FWHM 和有效掩膜体素数；自动来源按相同统计头或未阈值化 Z 图取证，保持校正参数不变。校正原统计量图与 Z 图均为必需产物。没有实际 FWHM 时不以指定 DLH 推算三轴数值。
- 指标影像与掩膜通过 DPABI `y_ReadRPI` 做相同轴方向规范化后核对维度和物理网格；仅做轴翻转，不重采样。
- `dpabi_auto` 登记本次 `EffectiveCfg.MaskFileForEachSubject` 指定的实际指标掩膜。标准化后使用与指标同空间的 DPABI 掩膜，不将原生空间 `AutoMasks` 文件冒充指标计算掩膜；缺少实际掩膜时阻断登记。基础设施仅在逐受试者实际掩膜内容、空间和网格全部一致时建立共同组级引用，保留原始逐受试者产物和来源哈希；不同掩膜仍须另行明确统计掩膜。

每次 MATLAB 作业必须使用独立运行目录。真实运行只接受验证后的结构化 JobSpec，且需调用方显式授权。常规测试不启动 MATLAB；明确 opt-in 的仓库外小型合成 smoke 记录在 [验证文档](../../docs/development/mvp-verification.md)。预处理当前限单会话 4D 输入，metric-only 限单主体；需要不同阶段掩膜的 OnResults normalization 与组合指标方案暂时拒绝。

T1 准备默认作为同一个已批准预处理作业的第一阶段自动执行，没有中途审查或额外批准。仅在 staging 副本上通过固定且引用完整路径的模板调用 DPABI 所附 legacy dcm2nii 重定向/裁剪工具，验证真实新建裁剪输出和 3D 头信息，保留原始影像；不使用当前源码中与旧参数不兼容的 dcm2niix 包装器。可选 PNG 三视图标注体素轴、世界坐标仿射和原点十字线，供结果检查。Segment/New Segment 与标准化分开建模，DARTEL 要求 New Segment；固定模板验证组织图、DARTEL 输入、形变场与六级模板，登记冻结集合谱系，始终关闭 GUI。所有已选择的预处理与指标步骤由同一次 `DPARSFA_run` 执行，结束后统一登记最终产物；不在 T1 阶段单独暂停。真实 T1/DARTEL 路径尚未经过 MATLAB smoke 验证。
