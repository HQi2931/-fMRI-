# fMRI 领域层

本包定义静息态 fMRI 的纯领域模型与科学约束，不依赖 Web、数据库或进程执行框架。

当前能力包括：

- 失败关闭的不可变 Artifact 谱系：执行端证据哈希、实际 TR 和指标消费时的实际保留 volume 数共同决定 metadata 是否 verified。
- ALFF/fALFF、ReHo 统一强制 typed 脑掩膜，并校验 TR、有效频率分辨率、网格和处理顺序。
- 完整、强类型且带参数来源的通用预处理协议：TR、删除初始时间点、层间时间校正、头动校正、协变量回归、标准化、去趋势、滤波、Scrubbing 与平滑均须显式选择。
- 人工 QC revision、受试者冻结顺序、统计设计及 FDR/GRF 校正合同。
- 真实/合成模式分离的 `StatisticalResultManifest`：真实结果缺少设计矩阵、contrast、统计图、effect map、簇表、日志或版本证据时失败关闭；合成占位必须明确标记不可用于科学推断。
- 不调用 Agent/Provider 的确定性 Markdown/JSON 复现报告。当前公共 Worker 仍不生成真实统计图、效应量或显著簇表。
- 数据检查、通用预处理、指标计算、QC 和统计分析的内置 `SkillSpec`。

科学参数没有隐藏默认值。模型只验证结构、顺序与已确认的 DPABI V8.2 约束；频带、阈值、是否回归全局信号等研究选择必须来自用户、研究方案、数据元信息或经审核的预设。

`skillpacks/` 保存内置科研协议。领域模型和 Skill 只处理结构化元数据，不直接访问文件系统或启动 MATLAB。注册 Tool 可以在批准后的受控流程中读取 manifest 绑定的源文件，并把副本写入独立 staging 工作区，但不得移动、覆盖或修改源数据；执行器仍只接受类型化 JobSpec，不能接受自由命令。完整设计见 [fMRI Skill 层架构](../../../docs/architecture/fmri-skill-layer.md)。
