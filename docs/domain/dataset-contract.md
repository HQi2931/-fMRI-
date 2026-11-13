# 数据集输入与只读契约

- 状态：Implementing
- 文档类型：Domain
- 维护者：fmri_methodologist
- 更新日期：2026-09-27
- 关联：[组统计设计](statistics.md)

## 支持的检查对象

- DICOM 目录（当前仅建立只读 inventory，不推断序列角色）。
- BIDS NIfTI/JSON。
- 普通 NIfTI/JSON（当前仅接受明确的 subject/modality 目录层级）。
- 已整理的 DPABI/DPARSF 输入目录。

检查阶段只读取目录、文件元数据、文件内容哈希和用于识别无扩展名 DICOM 的最小签名，不重命名、移动、删除或覆盖。每次检查生成 `DatasetProfile` 和带 revision/hash 的 `SubjectManifest`，明确列出 subject、session、T1、功能像及缺失项；manifest hash 覆盖影像、BIDS JSON sidecar、未被选作科学输入的 fmap/dwi/mask/derivative 文件和目录中的其他实际输入文件及大小。

BIDS 科学候选采用失败关闭的角色识别：只有原始 subject/session 路径下 `func/*_bold.nii[.gz]` 可进入功能像候选，只有 `anat/*_T1w.nii[.gz]` 可进入结构像候选。`fmap`、`dwi`、mask 和 `derivatives/` 下的影像仍在不可变 inventory 与哈希中，但不能静默冒充功能像或 T1。一个 subject/session 出现多个 BOLD run 时，计划校验返回候选清单并阻断；当前 MVP 未提供 run 选择字段，不能按文件名排序隐式选取，必须先形成显式、无歧义的新输入清单。

普通非 BIDS/DPABI 目录只有 `subject/{func|functional|rest|bold}/...` 或 `{func|functional|rest|bold}/subject/...` 进入功能候选，`anat`/`t1` 层级进入结构候选；mask、ALFF/fALFF/ReHo/VMHC 结果、results/output/qc/derivatives 目录及无明确 modality 的 NIfTI 只进入 inventory/hash。普通目录出现多个功能候选时计划失败关闭，不按排序隐式选取。受试者目录名会清洗为安全标识；如果两个原始目录名清洗为同一 `subject_id`，扫描立即失败，不合并其文件。DICOM 文件只登记到 `dicom_files` inventory；在尚无受控序列角色映射和转换 revision 的情况下，纯 DICOM manifest 以及仅靠 DICOM 补足功能像的混合 manifest 均不得创建预处理或指标计划。

DPABI-ready 目录会把本轮发现的全部顶层 stage 作为候选返回，不按目录数量、排序或前缀自动选择。候选规则由已核验的 `DPARSFA_run.m` 版本化生成，首批允许 `FunRaw`、`FunImg`、`FunImgAR`、`FunImgARW`、`FunImgARWS`（以及同一规则表中明确登记的阶段）；未知后缀只进入 inventory，不能因为匹配 `FunImg*` 就获准运行。即使只有一个 `FunRaw` 或 `FunImg`，也必须由用户明确确认 `selected_input_stage`；Work 模型只能根据用户意图提出候选建议，不能代替确认。`FunRaw` 配对 `T1Raw/{subject}`，其余已登记 `FunImg*` 阶段配对 `T1Img/{subject}`。其他 checkpoint、未配对 T1、`RealignParameter` 和 `Results` 中的文件仍进入不可变 inventory/hash，但不会生成伪受试者、不会进入 `functional_files`，也不能与选定 checkpoint 混合。确认后，阶段名、规则版本和候选检查结果写入冻结 manifest 与计划；工作区或阶段变化会使原确认失效。形似 DPABI stage 但不符合已知命名的顶层目录会以不支持的 stage 失败关闭。

当前 API 扫描不把文件名推断当作 NIfTI 头信息：TR、时间点、网格和空间必须来自后续受控检查、已登记 Artifact lineage 或研究者明确确认。缺少明确功能 BOLD 的受试者会阻断计划，DICOM inventory 不作为功能像兜底；选择 T1 Segment 或 DARTEL 时，缺少或存在多份结构像也会阻断。运行创建前会只读重扫源目录，任何文件内容变化都会使旧审批失效。

工作区检查接口还会读取每个 NIfTI 的固定头标记（不加载影像体数据）；无法读取头标记的功能输入会直接阻断交给 DPABI，结构像或结果目录中的异常文件会作为警告列出。TR、维度和空间方向仍不由文件名猜测。

人口学 CSV/TSV/XLSX 必须显式映射 subject ID、组别和协变量。重复、缺失、编码异常、未知受试者和多对一映射均生成结构化问题。统计分析严格按照冻结 manifest 对齐，不使用文件系统排序。

数据整理生成复制/转换 preview。默认执行模式写入独立 staging，源文件仍只读。用户显式选择
`in_place` 时，目录必须是已确认阶段的 DPABI-ready 工作副本；它不应指向原始归档。若从
`FunImgAR`、`FunImgARW` 或 `FunImgARWS` 续跑，服务端按冻结规则关闭已完成步骤，检查
`RealignParameter`、FD、T1、掩膜等后续操作所需伴随证据，并禁止重复去除初始时间点。Worker 在启动前
复核 manifest 全部文件及伴随证据的大小和 SHA-256，把该目录作为 DPARSFA `WorkingDir`；目标结果目录
已存在、来源哈希变化或 DPABI 源码版本未核验时均失败关闭。该模式仍禁止 Agent 自由命名输出、移动或删除输入，
并要求已审批计划和本次真实运行确认。机器学习或探索/验证划分按 subject 分组，
使用记录在 revision 中的随机种子和分层字段；传统组水平 t 检验不自动拆分训练/测试集。
