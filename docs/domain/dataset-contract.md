# 数据集输入与只读契约

## 支持的检查对象

- DICOM 目录（当前仅建立只读 inventory，不推断序列角色）。
- BIDS NIfTI/JSON。
- 普通 NIfTI/JSON（当前仅接受明确的 subject/modality 目录层级）。
- 已整理的 DPABI/DPARSF 输入目录。

检查阶段只读取目录、文件元数据、文件内容哈希和用于识别无扩展名 DICOM 的最小签名，不重命名、移动、删除或覆盖。每次检查生成 `DatasetProfile` 和带 revision/hash 的 `SubjectManifest`，明确列出 subject、session、T1、功能像及缺失项；manifest hash 覆盖影像、BIDS JSON sidecar、未被选作科学输入的 fmap/dwi/mask/derivative 文件和目录中的其他实际输入文件及大小。

BIDS 科学候选采用失败关闭的角色识别：只有原始 subject/session 路径下 `func/*_bold.nii[.gz]` 可进入功能像候选，只有 `anat/*_T1w.nii[.gz]` 可进入结构像候选。`fmap`、`dwi`、mask 和 `derivatives/` 下的影像仍在不可变 inventory 与哈希中，但不能静默冒充功能像或 T1。一个 subject/session 出现多个 BOLD run 时，计划校验返回候选清单并阻断；当前 MVP 未提供 run 选择字段，不能按文件名排序隐式选取，必须先形成显式、无歧义的新输入清单。

普通非 BIDS/DPABI 目录只有 `subject/{func|functional|rest|bold}/...` 或 `{func|functional|rest|bold}/subject/...` 进入功能候选，`anat`/`t1` 层级进入结构候选；mask、ALFF/fALFF/ReHo/VMHC 结果、results/output/qc/derivatives 目录及无明确 modality 的 NIfTI 只进入 inventory/hash。普通目录出现多个功能候选时计划失败关闭，不按排序隐式选取。受试者目录名会清洗为安全标识；如果两个原始目录名清洗为同一 `subject_id`，扫描立即失败，不合并其文件。DICOM 文件只登记到 `dicom_files` inventory；在尚无受控序列角色映射和转换 revision 的情况下，纯 DICOM manifest 以及仅靠 DICOM 补足功能像的混合 manifest 均不得创建预处理或指标计划。

DPABI-ready 目录必须精确存在一个受支持的功能输入 stage root：`FunRaw` 或 `FunImg`。两者同时存在时，因为当前请求没有显式 stage 选择字段，扫描以歧义失败；只有 `FunImgARW`、`FunImgARCWF` 等处理中间 checkpoint 而没有受支持输入 stage 时同样失败。唯一 stage 为 `FunRaw` 时只配对 `T1Raw/{subject}`，唯一 stage 为 `FunImg` 时只配对 `T1Img/{subject}`。其他 `FunImg*` checkpoint、未配对 T1 stage、`RealignParameter` 和 `Results` 中的文件仍进入不可变 inventory/hash，但不会生成伪受试者、不会进入 `functional_files`，也不能与选定 checkpoint 混合。形似 DPABI stage 但不符合已知命名的顶层目录会以不支持的 stage 失败关闭。

当前 API 扫描不把文件名推断当作 NIfTI 头信息：TR、时间点、网格和空间必须来自后续受控检查、已登记 Artifact lineage 或研究者明确确认。缺少明确功能 BOLD 的受试者会阻断计划，DICOM inventory 不作为功能像兜底；选择 T1 Segment 或 DARTEL 时，缺少或存在多份结构像也会阻断。运行创建前会只读重扫源目录，任何文件内容变化都会使旧审批失效。

人口学 CSV/TSV/XLSX 必须显式映射 subject ID、组别和协变量。重复、缺失、编码异常、未知受试者和多对一映射均生成结构化问题。统计分析严格按照冻结 manifest 对齐，不使用文件系统排序。

数据整理生成复制/转换 preview。实际执行时写入独立 staging，源文件仍只读。机器学习或探索/验证划分按 subject 分组，使用记录在 revision 中的随机种子和分层字段；传统组水平 t 检验不自动拆分训练/测试集。
