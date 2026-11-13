"""Fail-closed DPABI/MATLAB log classification."""

from __future__ import annotations

import re
from pathlib import Path

from neuroagent.analysis.models import FailureCode, FailureDiagnosis

_RULES: tuple[tuple[FailureCode, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        FailureCode.INPUT_FORMAT,
        r"T1 converter failed|rsfmri:t1:ConverterFailed",
        ("T1 重定向与裁剪未完成",),
        (
            "检查本次转换工具日志与输入文件",
            "若 T1 已完成手动标定与裁剪, 在新方案中明确使用已准备 T1; 不要自动更改本次批准方案",
        ),
    ),
    (
        FailureCode.INPUT_MISSING,
        r"(file|directory|folder).*(not found|does not exist)|no such file|无法找到|不存在",
        ("输入文件或目录不存在",),
        ("重新扫描数据集并核对冻结 manifest", "确认 staging 包含所有绑定输入"),
    ),
    (
        FailureCode.NIFTI_HEADER,
        (
            r"(dimension|voxel size|header|orientation|mask|masking).*(mismatch|inconsistent|error)"
            r"|\bTR\b.*(mismatch|inconsistent|error)"
            r"|头信息.*(不一致|错误)|维度不一致"
        ),
        ("NIfTI 头信息、网格、掩膜或 TR 可能不一致",),
        ("检查输入与 mask 的网格和空间方向", "在方案中明确 TR 与有效时间点"),
    ),
    (
        FailureCode.DPABI_CONFIGURATION,
        r"no auto mask binding exists for",
        ("DPABI 未找到该受试者的 AutoMask 绑定",),
        (
            "检查本次 attempt 是否生成了 AutoMask 输出及对应的受试者绑定记录",
            "核对冻结清单中的受试者 ID 与 DPABI 输入映射; 修复后重新确认方案",
        ),
    ),
    (
        FailureCode.DPABI_CONFIGURATION,
        r"(cfg|configuration|parameter|field).*(invalid|unknown|missing)|字段.*错误|参数.*错误",
        ("DPABI Cfg 参数未通过校验",),
        ("重新生成结构化 Cfg 并查看字段映射", "不要直接编辑 MATLAB 文本"),
    ),
    (
        FailureCode.SOFTWARE_ENVIRONMENT,
        (
            r"(undefined function|function .* undefined|\bspm\b|\bdpabi\b|\bmatlab\b)"
            r".*(not found|undefined|cannot find)|undefined function|函数.*未找到"
        ),
        ("MATLAB, SPM12 或 DPABI 入口不可见",),
        ("重新运行环境探测", "核对 MATLAB、SPM 和 DPABI 路径"),
    ),
    (
        FailureCode.RESOURCE,
        r"(out of memory|insufficient memory|memory error|disk (full|space)|no space|"
        r"内存不足|磁盘空间不足)",
        ("运行资源不足",),
        ("检查工作盘剩余空间", "降低并发或分块参数; 修改资源参数需重新审批"),
    ),
    (
        FailureCode.PATH_POLICY,
        r"createfile failed.{0,160}(?:拒绝访问|access is denied).{0,80}(?:system:5)?",
        ("MATLAB 启动时创建文件被 Windows 拒绝访问",),
        (
            "确认启动 MATLAB 的进程能在其 TEMP 目录创建 mathworks-<uuid> 文件",
            "若临时文件已能创建，再确认本机 LocalAppData 下 "
            "MathWorks/mwEndpointRegistry 可创建；只改 LOCALAPPDATA 不会移动该文件",
            "独立 MATLAB_PREFDIR 只隔离偏好目录，不能代替上述创建",
            "重试前先检查本次 attempt 的 stderr 日志",
        ),
    ),
    (
        FailureCode.PATH_POLICY,
        r"(permission denied|access is denied|path traversal|权限拒绝|拒绝访问|目录越界)",
        ("路径或权限策略阻断了运行",),
        ("将输入放入允许的只读根目录", "将输出改到项目 staging 工作区"),
    ),
    (
        FailureCode.TIMEOUT,
        r"(timed out|timeout|超时)",
        ("作业超过已批准的超时限制",),
        ("检查日志和已生成产物", "如需延长超时, 创建新计划 revision 并重新审批"),
    ),
)

_UNRECOGNIZED_ERROR_LINE = re.compile(
    r"^(?:\?\?\?\s*)?(?:fatal(?:\s+startup)?\s+error|"
    r"error(?:\s+(?:using|in)\b|\s*:)|uncaught\s+(?:exception|error)\b|"
    r"(?:std::)?exception\b|assertion failed\b|matlab\s*:\s*error)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_START = re.compile(
    r"(?i)(?<![a-z0-9])(?:[a-z]:[\\/]|\\\\[^\\/\s]+[\\/]|"
    r"(?<![\w:/])/(?!/)(?=\S))"
)


def _safe_line(line: str) -> str:
    value = line.strip()
    # Windows and UNC paths can contain spaces, so redact from an absolute-path
    # prefix through the end of the line instead of stopping at the first space.
    path_start = _ABSOLUTE_PATH_START.search(value)
    if path_start is not None:
        value = f"{value[: path_start.start()]}<path>"
    value = re.sub(r"(?i)(subject|sub|participant)[-_ ]?[A-Za-z0-9]+", r"\1<id>", value)
    value = re.sub(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "<email>", value)
    return value[:500]


def diagnose_dpabi_log(log: str | Path, *, max_evidence: int = 8) -> FailureDiagnosis:
    """Classify a log without sending it to a model or exposing local paths."""

    text = Path(log).read_text(encoding="utf-8", errors="replace") if isinstance(log, Path) else log
    lines = [_safe_line(line) for line in text.splitlines() if line.strip()]
    lower = text.lower()
    for code, pattern, evidence, suggestions in _RULES:
        if re.search(pattern, lower, flags=re.IGNORECASE):
            matching = tuple(
                line for line in lines if re.search(pattern, line, flags=re.IGNORECASE)
            )
            return FailureDiagnosis(
                code=code,
                severity="error",
                summary=evidence[0],
                evidence=(matching or tuple(lines[-max_evidence:]))[-max_evidence:],
                suggestions=suggestions,
                confidence=0.6,
                requires_new_plan=code not in {FailureCode.INPUT_MISSING, FailureCode.PATH_POLICY},
            )
    error_lines = tuple(line for line in lines if _UNRECOGNIZED_ERROR_LINE.search(line))
    if error_lines:
        return FailureDiagnosis(
            code=FailureCode.UNKNOWN,
            severity="error",
            summary="发现尚未匹配已知规则的 MATLAB/DPABI 错误",
            evidence=error_lines[-max_evidence:],
            suggestions=(
                "检查脱敏日志线索及本次 attempt 的完整日志",
                "保留本次 attempt 的日志和产物",
            ),
            confidence=0.25,
            requires_new_plan=True,
        )
    if "cancel" in lower or "取消" in text:
        return FailureDiagnosis(
            code=FailureCode.CANCELLED,
            severity="warning",
            summary="作业被用户或进程取消",
            evidence=tuple(lines[-max_evidence:]),
            suggestions=("确认取消原因; 如需继续, 使用相同批准计划重新排队",),
            confidence=0.85,
            requires_new_plan=False,
        )
    return FailureDiagnosis(
        code=FailureCode.UNKNOWN,
        severity="info",
        summary="当前日志中尚未发现明确错误线索",
        evidence=(),
        suggestions=(),
        confidence=0.1,
        requires_new_plan=False,
    )
