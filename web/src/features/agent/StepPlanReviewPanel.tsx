import { useEffect, useState } from "react";

import { describeError } from "../../api/transport";
import type { StepPlanReviewDetail, StepPlanReviewSummary } from "../../api/types";
import { agentApi } from "../../api/agent";
import type { components } from "../../api/schema.generated";

type Schemas = components["schemas"];

type Props = {
  conversationId: string;
  projectId: string;
  conversationVersion: number;
  refreshWhileActive: boolean;
};

type ViewState = {
  reviews: StepPlanReviewSummary[];
  loading: boolean;
  error: string;
  truncated: boolean;
  conversationId: string;
  projectId: string;
};
function enabled(value: boolean | null): string {
  return value === true ? "开启" : value === false ? "关闭" : "未注明";
}

function band(low: number | null, high: number | null): string | null {
  return low === null || high === null ? null : `${low}–${high} Hz`;
}

function preprocessingFacts(summary: NonNullable<StepPlanReviewSummary["preprocessing"]>): Array<[string, string]> {
  const metrics = summary.requested_metrics.map((item) =>
    item === "alff" ? "ALFF" : item === "falff" ? "fALFF" : item === "reho" ? "ReHo" : item,
  ).join("、");
  const scalings = summary.requested_scalings.map((item) =>
    item === "raw" ? "原始值" : item === "global_mean" ? "全局均值标准化" : item === "z_score" ? "Z 分数" : item,
  ).join("、");
  const motionLabels: Record<number, string> = {
    0: "关闭", 1: "6 参数", 2: "12 参数", 3: "Friston-24 扩展模型", 4: "Friston-24",
  };
  const values: Array<[string, string | null]> = [
    ["TR", summary.tr_seconds === null ? null : `${summary.tr_seconds} 秒`],
    ["时间点", summary.expected_time_points === null ? null : String(summary.expected_time_points)],
    ["丢弃起始点", summary.dummy_scans === null ? null : String(summary.dummy_scans)],
    ["线性去趋势", summary.detrend === null ? null : enabled(summary.detrend)],
    ["切片时间校正", summary.slice_timing_enabled === true
      ? `开启，共 ${String(summary.slice_count ?? "未注明")} 层；参考第 ${String(summary.reference_slice ?? "未注明")} 层`
      : summary.slice_timing_enabled === false ? "关闭" : null],
    ["头动校正", summary.realignment_enabled === null ? null : enabled(summary.realignment_enabled)],
    ["头动回归", summary.head_motion_model === null
      ? null
      : motionLabels[summary.head_motion_model] ?? `模型 ${summary.head_motion_model}`],
    ["白质回归", summary.white_matter_regression_enabled === null ? null : enabled(summary.white_matter_regression_enabled)],
    ["脑脊液回归", summary.csf_regression_enabled === null ? null : enabled(summary.csf_regression_enabled)],
    ["全局信号回归", summary.global_signal_regression_enabled === null ? null : enabled(summary.global_signal_regression_enabled)],
    ["标准化", summary.normalization_mode === null
      ? null
      : summary.normalization_mode === 2 ? "T1 Segment"
        : summary.normalization_mode === 3 ? "DARTEL"
          : summary.normalization_mode === 1 ? "EPI 模板"
            : "关闭"],
    ["指标", metrics || null],
    ["指标频段", band(summary.frequency_low_hz, summary.frequency_high_hz)],
    ["脑掩膜", summary.mask_source === null
      ? null
      : summary.mask_source === "dpabi_auto" ? "DPABI AutoMask" : "已登记掩膜"],
    ["指标值", scalings || null],
    ["指标平滑", summary.metric_smoothing === null ? null : enabled(summary.metric_smoothing)],
  ];
  return values.filter((item): item is [string, string] => item[1] !== null);
}

function statisticalFacts(summary: NonNullable<StepPlanReviewSummary["statistical"]>): Array<[string, string]> {
  const testLabels: Record<string, string> = {
    one_sample_t: "单样本 t 检验",
    independent_two_sample_t: "独立样本 t 检验",
    paired_t: "配对 t 检验",
    correlation: "相关分析",
    regression: "回归分析",
    one_way_anova_ancova: "单因素方差分析 / 协方差分析",
  };
  const tailLabels: Record<string, string> = {
    one_sided_positive: "单侧正向",
    one_sided_negative: "单侧负向",
    two_sided: "双侧",
  };
  const values: Array<[string, string]> = [
    ["检验方法", testLabels[summary.test] ?? summary.test],
    ["组别数", String(summary.group_count)],
    ["条件数", String(summary.condition_count)],
    ["纳入影像数", String(summary.image_count)],
    ["协变量数", String(summary.covariate_count)],
    ["对比权重", summary.contrast.join(", ")],
    ["检验方向", tailLabels[summary.tail] ?? summary.tail],
    ["缺失值处理", summary.missing_value_policy === "error" ? "发现缺失即停止" : "显式排除"],
  ];
  return values;
}

function stepLabel(kind: string): string {
  if (kind === "preprocessing_run") return "预处理与指标计算";
  if (kind === "statistical_run") return "统计分析";
  return "工作流步骤";
}

function planStateLabel(state: StepPlanReviewSummary["plan_state"]): string {
  switch (state) {
    case "awaiting_approval": return "等待审查";
    case "approved": return "已批准";
    case "draft": return "草稿";
    case "validating": return "校验中";
    case "superseded": return "已被新版本替代";
  }
}

function subjectTable(detail: StepPlanReviewDetail) {
  if (detail.images.length === 0) return <p>未找到可显示的统计纳入明细。方案可能已变化，请刷新重试。</p>;

  return <div className="step-plan-detail">
    <p className="step-plan-local-data-note">以下明细仅在用户主动展开后从本机读取；包含被试编号、组别和协变量，不会发送给路由模型。</p>
    <div className="step-plan-table-scroll">
      <table>
        <caption>统计影像纳入顺序</caption>
        <thead><tr><th scope="col">序号</th><th scope="col">被试编号</th><th scope="col">组别</th><th scope="col">条件</th></tr></thead>
        <tbody>{detail.images.map((image, index) => <tr key={`${image.subject_id}:${String(image.condition)}:${index}`}>
          <td>{index + 1}</td>
          <td>{image.subject_id}</td>
          <td>{image.group ?? "—"}</td>
          <td>{image.condition ?? "—"}</td>
        </tr>)}</tbody>
      </table>
    </div>
    {detail.covariates.map((covariate, covariateIndex) => <details key={`${covariate.name}:${covariateIndex}`}>
        <summary>协变量：{covariate.name}</summary>
        <p>中心化：{covariate.centering}</p>
        <div className="step-plan-table-scroll"><table>
          <caption>协变量取值</caption>
          <thead><tr><th scope="col">被试编号</th><th scope="col">数值</th></tr></thead>
          <tbody>{covariate.values.map((value, index) => <tr key={`${value.subject_id}:${index}`}>
            <td>{value.subject_id}</td>
            <td>{value.value}</td>
          </tr>)}</tbody>
        </table></div>
      </details>)}
  </div>;
}

function StepPlanReviewCard({
  conversationId,
  summary,
  onChanged,
}: {
  conversationId: string;
  summary: StepPlanReviewSummary;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<StepPlanReviewDetail | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [executionBackend, setExecutionBackend] = useState<Schemas["ExecutionBackend"]>("mock");
  const [workspaceMode, setWorkspaceMode] = useState<Schemas["ExecutionWorkspaceMode"]>("isolated");
  const [qcMode, setQcMode] = useState<Schemas["QcExecutionMode"]>("require");
  const [realExecutionConfirmed, setRealExecutionConfirmed] = useState(false);
  const [authorization, setAuthorization] = useState<Schemas["StepRunAuthorizationView"] | null>(null);
  const facts = summary.preprocessing
    ? preprocessingFacts(summary.preprocessing)
    : summary.statistical ? statisticalFacts(summary.statistical) : [];

  const toggleExactDetails = async () => {
    if (detail) {
      setDetailsOpen((open) => !open);
      return;
    }
    setLoadingDetail(true);
    setDetailError("");
    try {
      const result = await agentApi.stepPlanReview(conversationId, summary.task_id, summary.step_id);
      if (result.conversation_id !== conversationId
        || result.project_id !== summary.project_id
        || result.task_id !== summary.task_id
        || result.task_version !== summary.task_version
        || result.step_id !== summary.step_id
        || result.step_sequence !== summary.step_sequence
        || result.step_version !== summary.step_version
        || result.plan_revision_id !== summary.plan_revision_id
        || result.plan_revision !== summary.plan_revision
        || result.plan_version !== summary.plan_version
        || result.plan_hash !== summary.plan_hash) {
        throw new Error("方案身份已变化，请刷新列表后重新打开。");
      }
      setDetail(result);
      setDetailsOpen(true);
    } catch (error: unknown) {
      setDetailError(describeError(error));
    } finally {
      setLoadingDetail(false);
    }
  };

  const actionIdentity = {
    task_version: summary.task_version,
    step_version: summary.step_version,
    plan_revision_id: summary.plan_revision_id,
    plan_version: summary.plan_version,
    plan_hash: summary.plan_hash,
  };

  const approve = async () => {
    setBusy(true);
    setActionError("");
    try {
      const result = await agentApi.approveStepPlan(conversationId, summary.task_id, summary.step_id, {
        ...actionIdentity,
        actor: "用户",
        reason: "用户在步骤级方案审查面板中明确批准此冻结 Plan。",
      });
      if (result.conversation_id !== conversationId
        || result.project_id !== summary.project_id
        || result.task_id !== summary.task_id
        || result.step_id !== summary.step_id
        || result.plan_revision_id !== summary.plan_revision_id
        || result.plan_hash !== summary.plan_hash
        || result.approval.plan_hash !== summary.plan_hash) {
        throw new Error("批准结果与当前步骤方案不匹配，请刷新状态后检查。");
      }
      setAuthorization(null);
      onChanged();
    } catch (error: unknown) {
      setActionError(describeError(error));
      setAuthorization(null);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const issueRunAuthorization = async () => {
    setBusy(true);
    setActionError("");
    try {
      const result = await agentApi.createStepRunAuthorization(
        conversationId,
        summary.task_id,
        summary.step_id,
        {
          ...actionIdentity,
          execution_backend: executionBackend,
          workspace_mode: workspaceMode,
          real_execution_confirmed: realExecutionConfirmed,
          qc_mode: summary.step_kind === "statistical_run" ? "require" : qcMode,
        },
      );
      if (result.conversation_id !== conversationId
        || result.project_id !== summary.project_id
        || result.task_id !== summary.task_id
        || result.step_id !== summary.step_id
        || result.plan_revision_id !== summary.plan_revision_id
        || result.plan_hash !== summary.plan_hash) {
        throw new Error("授权与当前步骤方案不匹配，请刷新后重新确认。");
      }
      setAuthorization(result);
    } catch (error: unknown) {
      setActionError(describeError(error));
      setAuthorization(null);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const startRun = async () => {
    if (!authorization) return;
    setBusy(true);
    setActionError("");
    try {
      const result = await agentApi.startStepRun(conversationId, summary.task_id, summary.step_id, {
        ...actionIdentity,
        confirmation_id: authorization.confirmation_id,
        execution_backend: executionBackend,
        workspace_mode: workspaceMode,
        real_execution_confirmed: realExecutionConfirmed,
        qc_mode: summary.step_kind === "statistical_run" ? "require" : qcMode,
        max_attempts: 1,
        mock_outcome: "succeed",
        mock_delay_ms: 0,
      });
      if (result.conversation_id !== conversationId
        || result.project_id !== summary.project_id
        || result.task_id !== summary.task_id
        || result.step_id !== summary.step_id
        || result.plan_revision_id !== summary.plan_revision_id
        || result.run.project_id !== summary.project_id
        || result.run.plan_revision_id !== summary.plan_revision_id) {
        throw new Error("Run 返回身份与当前步骤方案不匹配，请刷新任务状态。");
      }
      setAuthorization(null);
      onChanged();
    } catch (error: unknown) {
      setActionError(describeError(error));
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const invalidateAuthorization = () => setAuthorization(null);

  return <li>
    <article className="step-plan-review-card">
      <h3>{`第 ${summary.step_sequence} 步：${stepLabel(summary.step_kind)}`}</h3>
      <p><strong>方案状态：</strong>{planStateLabel(summary.plan_state)} · {summary.step_status === "ready" ? "步骤已就绪" : "步骤状态已变化"}</p>
      {facts.length > 0 && <dl>{facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
      {summary.expected_output_count > 0 && <p><strong>已核验输出：</strong>{summary.expected_output_count} 项（不显示被试文件名）</p>}
      {summary.statistical && <div>
        <button className="button button-secondary" type="button" disabled={loadingDetail} onClick={() => void toggleExactDetails()}>
          {loadingDetail ? "正在读取纳入明细…" : detailsOpen ? "收起精确纳入明细" : "查看精确纳入明细"}
        </button>
        {detailError && <p className="step-plan-review-error" role="alert">{detailError}</p>}
        {detailsOpen && detail && subjectTable(detail)}
      </div>}
      <details>
        <summary>查看版本核验信息</summary>
        <p>方案版本 {summary.plan_revision} · 内容校验码 {summary.plan_hash.slice(0, 12)}…</p>
        <p>Task v{summary.task_version} · Step v{summary.step_version} · Plan v{summary.plan_version}</p>
      </details>
      {summary.plan_state === "awaiting_approval" && <div className="step-plan-actions">
        <p>审查摘要与版本后，批准将只记录在此步骤绑定的 Plan 上。</p>
        <button className="button button-primary" type="button" disabled={busy} onClick={() => void approve()}>
          {busy ? "正在记录批准…" : "批准此步骤方案"}
        </button>
      </div>}
      {summary.plan_state === "approved" && <div className="step-plan-actions">
        <h4>启动已批准的步骤</h4>
        <label>执行后端
          <select value={executionBackend} disabled={busy} onChange={(event) => {
            const nextBackend = event.target.value as Schemas["ExecutionBackend"];
            setExecutionBackend(nextBackend);
            setRealExecutionConfirmed(false);
            if (nextBackend === "mock") setWorkspaceMode("isolated");
            invalidateAuthorization();
          }}>
            <option value="mock">Mock 验收</option>
            <option value="matlab">本机 MATLAB / DPABI 真实运行</option>
          </select>
        </label>
        {executionBackend === "matlab" && <>
          {summary.step_kind === "preprocessing_run" && <label>工作区模式
            <select value={workspaceMode} disabled={busy} onChange={(event) => {
              setWorkspaceMode(event.target.value as Schemas["ExecutionWorkspaceMode"]);
              invalidateAuthorization();
            }}>
              <option value="isolated">隔离工作区</option>
              <option value="in_place">原目录执行</option>
            </select>
          </label>}
          <label className="step-plan-confirmation">
            <input type="checkbox" checked={realExecutionConfirmed} disabled={busy} onChange={(event) => {
              setRealExecutionConfirmed(event.target.checked);
              invalidateAuthorization();
            }} />
            我明确授权启动本机 MATLAB / DPABI 真实计算。
          </label>
        </>}
        {summary.step_kind === "preprocessing_run" ? <label>计算后的 QC
          <select value={qcMode} disabled={busy} onChange={(event) => {
            setQcMode(event.target.value as Schemas["QcExecutionMode"]);
            invalidateAuthorization();
          }}>
            <option value="require">保留 QC 审核门</option>
            <option value="skip">明确跳过 QC</option>
          </select>
        </label> : <p>统计步骤使用冻结设计中的 QC 来源，启动确认不能跳过 QC。</p>}
        {!authorization ? <button
          className="button button-primary"
          type="button"
          disabled={busy || (executionBackend === "matlab" && !realExecutionConfirmed)}
          onClick={() => void issueRunAuthorization()}
        >
          {busy ? "正在签发一次性授权…" : "签发此步骤的一次性启动授权"}
        </button> : <div className="step-plan-authorization" role="status">
          <p>授权已绑定本步骤、Plan 哈希和当前版本，将于 {new Date(authorization.expires_at).toLocaleTimeString()} 到期。</p>
          <button className="button button-primary" type="button" disabled={busy} onClick={() => void startRun()}>
            {busy ? "正在创建并绑定 Run…" : "确认并创建 Run"}
          </button>
        </div>}
      </div>}
      {actionError && <p className="step-plan-review-error" role="alert">{actionError}</p>}
    </article>
  </li>;
}

export function StepPlanReviewPanel({ conversationId, projectId, conversationVersion, refreshWhileActive }: Props) {
  const [state, setState] = useState<ViewState>({
    reviews: [], loading: true, error: "", truncated: false, conversationId: "", projectId: "",
  });
  const [refreshNonce, setRefreshNonce] = useState(0);
  const scopeMatches = state.conversationId === conversationId && state.projectId === projectId;
  const reviews = scopeMatches ? state.reviews : [];
  const loading = !scopeMatches || state.loading;
  const error = scopeMatches ? state.error : "";
  const truncated = scopeMatches && state.truncated;

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setState((existing) => existing.conversationId === conversationId && existing.projectId === projectId
      ? { ...existing, loading: true, error: "" }
      : { reviews: [], loading: true, error: "", truncated: false, conversationId, projectId });
    const scheduleRefresh = (delay: number) => {
      timer = setTimeout(() => {
        if (current) setRefreshNonce((value) => value + 1);
      }, delay);
    };
    agentApi.stepPlanReviews(conversationId, controller.signal).then((result) => {
      if (!current || controller.signal.aborted) return;
      const scoped = result.items.filter((review) =>
        review.conversation_id === conversationId && review.project_id === projectId,
      );
      setState({
        reviews: scoped,
        loading: false,
        truncated: result.truncated,
        error: scoped.length !== result.items.length ? "部分方案与当前会话或项目不匹配，已隐藏；请刷新后重试。" : "",
        conversationId,
        projectId,
      });
      if (refreshWhileActive || scoped.length > 0) scheduleRefresh(10_000);
    }).catch((error: unknown) => {
      if (!current || controller.signal.aborted) return;
      setState({
        reviews: [], loading: false, truncated: false, error: describeError(error), conversationId, projectId,
      });
      scheduleRefresh(30_000);
    });
    return () => {
      current = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [conversationId, projectId, conversationVersion, refreshWhileActive, refreshNonce]);

  return <section className="panel step-plan-review" aria-label="多步骤方案审查">
    <div className="panel-heading">
      <div>
        <span className="eyebrow">Work 自动化</span>
        <h2>待审查方案</h2>
        <p>方案身份按 Task/Step/Plan 版本核验。批准、一次性启动授权和 Run 创建分别确认并绑定到当前步骤。</p>
      </div>
      <button className="button button-secondary" type="button" disabled={state.loading} onClick={() => setRefreshNonce((value) => value + 1)}>
        刷新方案
      </button>
    </div>
    {loading && reviews.length === 0 && <p role="status">正在读取当前会话的步骤方案…</p>}
    {error && <p className="step-plan-review-error" role="alert">{error}</p>}
    {!loading && !error && reviews.length === 0 && <p>当前会话没有等待审查的多步骤方案。</p>}
    {truncated && <p role="status">待审查候选超过 100 项，目前显示的项目可能不完整。请缩小当前会话的待处理目标后刷新。</p>}
    {reviews.length > 0 && <ol className="step-plan-review-list">
      {reviews.map((review) => <StepPlanReviewCard key={`${conversationId}:${projectId}:${review.task_id}:${review.step_id}:${review.plan_revision_id}:${review.plan_hash}`} conversationId={conversationId} summary={review} onChanged={() => setRefreshNonce((value) => value + 1)} />)}
    </ol>}
    {refreshWhileActive && <p className="chat-technical" role="status">自动化进行中，每 10 秒检查一次步骤方案；空闲后停止轮询。</p>}
  </section>;
}
