# Tools 层

`neuroagent.tools` 提供边界清晰、确定性且可测试的类型化工具。工具不解释科研意图、不推进工作流状态、不执行任意命令，也不修改原始数据。

当前能力包括：

- 冻结的 `ToolRegistry` 与 capability/version 绑定。
- `ReadOnlyInputClassifier`：只读识别 DICOM、BIDS NIfTI、普通 NIfTI 和 DPABI-ready 目录，并阻断混合输入、符号链接与扫描上限问题。
- `StagingCopyTool`：根据冻结 manifest 生成稳定复制计划；执行时校验来源大小、SHA-256、路径边界和源文件元数据，只写独立运行目录且拒绝覆盖不同内容。
- `DpabiBidsConverterPlanTool`：仅为已确认的 DICOM 输入生成受控 `DPABI_BIDS_Converter_run` 计划，不直接转换或启动 MATLAB。
- `DpabiV82Adapter`：将完整预处理参数、ALFF/fALFF、ReHo、统计设计、FDR 和 GRF 显式投影到本机 DPABI V8.2 已核验字段。
- `fmri.artifact.verify_metadata` capability：定义预处理产物的执行端头信息验证门，成功登记必须绑定实际 TR、实际保留 volume 数、网格、mask 和证据哈希；当前 Mock 不伪造 verified 科研产物。

DPABI 预处理投影必须叠加到经批准且已注册的基础 `.mat` Cfg Artifact。适配器保留未建模的基础字段供人工复核，同时显式关闭交互入口、格式转换和未请求指标；模型不能补充科学默认值。指标投影必须提供运行目录内的显式 mask 相对路径，不允许空值或隐藏默认 mask。

真实执行属于 `execution` 层，并且仍需用户明确授权。日常测试只生成计划、Cfg 快照和 Mock 结果。
