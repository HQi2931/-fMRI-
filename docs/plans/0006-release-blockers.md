# 计划 0006：真实执行发布阻断收口

- 状态：实施中
- 目标：保持 ADR 0006 的真实执行范围，补齐公共预处理路径、真实软件证据和发布验证。

## 实施范围

1. 对照用户本机安装的 DPABI 源码核验统计签名及输出，修复已证实缺陷。
2. 从冻结 SkillPlan、manifest 和已登记 Artifact 编译预处理 JobSpec；复制到独立 staging，执行前校验源文件哈希，运行后登记实际影像元数据。
3. 软件版本从实际 MATLAB/SPM/DPABI 运行环境获取，用户填写标签仅作为配置证据。
4. 使用仓库外确定性合成数据验证预处理、三类 t 检验、FDR/GRF 和报告，不使用受试者原始数据。
5. 完成质量门禁、多角色复核、GitHub 认证/CI 检查和正式版本发布。

## 验收与边界

- 任何必需产物、输入哈希、环境锁或元数据失配均失败关闭；失败不登记科研成功。
- 合成 smoke 的方法参数只用于软件测试，不成为产品科学默认值。
- 未取得可用 Provider 凭据或 GitHub 登录时，记录具体缺口，不写入通过结论。
- 本机源码已证实统计调用的 7/6/6/9/12 参数签名正确；此前“参数过多”的评审结论撤回。

## 2026-09-04 进度

- 已实现公共预处理编译/执行接线、独立 attempt/staging、原始输入哈希校验、实际影像 metadata 与 lineage 登记。
- 已修复硬编码统计版本证据、协变量设计下的效应量比例、正负簇分离和未校正簇的描述性标记；方法定义见 [ADR 0007](../adr/0007-adjusted-statistical-effects.md)。
- 真实统计 smoke 与模板代数验证在报告元数据哈希改为真实来源后重跑，4 tests passed，105.42 秒；实际环境为 MATLAB R2024b、SPM25 25.01.02、DPABI V9.0_250415。
- 真实预处理 smoke 已验证删除 4/124 个初始 volume、保留 120 个、detrend、公共 Worker 到 QC，以及源文件 SHA-256 不变；组合 ALFF/fALFF/ReHo smoke 也通过公共 Worker 到 QC 并登记实际指标和谱系。
- 真实 Provider `zhipu / glm-5.3-flash` smoke 成功，未发送受试者信息；上下文哈希见 [验证记录](../development/mvp-verification.md)。
- GitHub CLI 已安装并登录成功；`main` 的严格 `agent-review`/`quality-gate`、管理员约束、线性历史、会话解决和禁止强推/删除已通过 API 回读确认。最终树全量门禁、多角色终审、远程 CI 与正式 tag 仍按发布流程完成，在 tag 创建前不标记 `v0.1.0` 已发布。

当前输入边界：单会话 4D；metric-only 单主体；需要不同阶段掩膜的 OnResults normalization 与组合指标方案及无指标输出的纯预处理 OnResults 操作暂时拒绝；可能触发 DPARSFA GUI 的 T1 分割/DARTEL 在 headless 执行器拒绝，EPI template normalization 要求 realignment mean image。smoke 不扩大这些边界，也不承诺任意版本、任意数据集均兼容。

指标输入和掩膜均使用 DPABI `y_ReadRPI` 统一轴方向，然后严格核对维度和物理网格；只做轴翻转，不重采样，也不放宽掩膜空间一致性要求。
