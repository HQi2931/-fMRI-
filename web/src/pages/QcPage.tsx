import { useEffect, useMemo, useState } from "react";

import { api, describeError, type Artifact, type QcReview, type Run } from "../api/client";
import { EmptyState, Feedback, MetricCard, PageHeader } from "../components/Ui";
import { StatusPill } from "../components/StatusPill";
import { updateWorkspace, useWorkspace } from "../workspace";

function parseExclusions(value: string): Array<[string, string]> {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
    const [subjectId, ...reason] = line.split("|");
    return [subjectId.trim(), reason.join("|").trim()] as [string, string];
  });
}

function toggleId(current: string[], artifactId: string, checked: boolean): string[] {
  return checked ? [...current, artifactId] : current.filter((item) => item !== artifactId);
}

export function QcPage() {
  const workspace = useWorkspace();
  const [run, setRun] = useState<Run | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [review, setReview] = useState<QcReview | null>(null);
  const [selectedMetricIds, setSelectedMetricIds] = useState<string[]>([]);
  const [checkCode, setCheckCode] = useState("");
  const [checkSeverity, setCheckSeverity] = useState<"info" | "warning" | "blocking" | "">("");
  const [checkPassed, setCheckPassed] = useState<"yes" | "no" | "">("");
  const [checkEvidenceIds, setCheckEvidenceIds] = useState<string[]>([]);
  const [checkMessage, setCheckMessage] = useState("");
  const [included, setIncluded] = useState("");
  const [exclusionDecision, setExclusionDecision] = useState<"none" | "some" | "">("");
  const [excluded, setExcluded] = useState("");
  const [approvalActor, setApprovalActor] = useState("");
  const [approvalReason, setApprovalReason] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    async function restoreReview(): Promise<void> {
      let runId = workspace.runId;
      if (workspace.qcReviewId) {
        const stored = await api.qcReview(workspace.qcReviewId, controller.signal);
        setReview(stored);
        setSelectedMetricIds(stored.review.metric_artifact_ids);
        const firstCheck = stored.review.checks[0];
        setCheckCode(firstCheck?.code ?? "");
        setCheckSeverity(firstCheck?.severity ?? "");
        setCheckPassed(firstCheck ? (firstCheck.passed ? "yes" : "no") : "");
        setCheckEvidenceIds(firstCheck?.evidence_artifact_ids ?? []);
        setCheckMessage(firstCheck?.message ?? "");
        setIncluded(stored.review.included_subject_ids.join("\n"));
        setExclusionDecision(stored.review.excluded_subject_ids.length ? "some" : "none");
        setExcluded(stored.review.exclusion_reasons.map(([subjectId, reason]) => `${subjectId} | ${reason}`).join("\n"));
        updateWorkspace({
          qcReviewVersion: stored.version,
          qcReviewHash: stored.review.content_hash,
        });
        runId = stored.run_id;
      }
      if (runId) {
        const [storedRun, storedArtifacts] = await Promise.all([
          api.run(runId, controller.signal),
          api.artifacts(runId, controller.signal),
        ]);
        setRun(storedRun);
        setArtifacts(storedArtifacts);
      }
    }
    restoreReview().catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(describeError(caught));
    });
    return () => controller.abort();
  }, [workspace.qcReviewId, workspace.runId]);

  const metricArtifacts = useMemo(() => artifacts.filter((artifact) => artifact.artifact_type.startsWith("metric.")), [artifacts]);
  const exclusionRows = useMemo(() => exclusionDecision === "some" ? parseExclusions(excluded) : [], [excluded, exclusionDecision]);
  const includedIds = useMemo(() => included.split(/\s+/).map((item) => item.trim()).filter(Boolean), [included]);
  const excludedIds = useMemo(() => exclusionRows.map(([subjectId]) => subjectId), [exclusionRows]);
  const exclusionsValid = exclusionDecision !== "" && (
    exclusionDecision === "none"
    || (exclusionRows.length > 0 && exclusionRows.every(([subjectId, reason]) => subjectId && reason))
  );
  const subjectDecisionsValid = useMemo(() => {
    if (!includedIds.length || new Set(includedIds).size !== includedIds.length) return false;
    if (!exclusionsValid || new Set(excludedIds).size !== excludedIds.length) return false;
    const includedSet = new Set(includedIds);
    if (excludedIds.some((subjectId) => includedSet.has(subjectId))) return false;
    const expected = workspace.subjectIds ?? [];
    if (!expected.length) return true;
    const decided = [...includedIds, ...excludedIds];
    return decided.length === expected.length && new Set(decided).size === expected.length && expected.every((subjectId) => decided.includes(subjectId));
  }, [excludedIds, exclusionsValid, includedIds, workspace.subjectIds]);
  const checkValid = Boolean(checkCode.trim() && checkSeverity && checkPassed && checkEvidenceIds.length && checkMessage.trim());
  const approvalValid = Boolean(approvalActor.trim() && approvalReason.trim());
  const hasBlockingFailure = Boolean(review?.review.checks.some((check) => check.severity === "blocking" && !check.passed));

  async function createReview(): Promise<void> {
    if (!run) return;
    setBusy(true);
    setError("");
    try {
      if (!selectedMetricIds.length || selectedMetricIds.some((artifactId) => !metricArtifacts.some((artifact) => artifact.artifact_id === artifactId))) {
        throw new Error("必须显式选择至少一个 metric.* Artifact。");
      }
      if (!checkValid) throw new Error("必须完整填写至少一个 QC check 及其证据。");
      if (!subjectDecisionsValid) throw new Error("纳入、排除和排除理由必须显式覆盖冻结清单且不能重复。");
      const created = await api.createQcReview({
        run_id: run.run_id,
        expected_run_version: run.version,
        metric_artifact_ids: selectedMetricIds,
        checks: [{
          code: checkCode.trim(),
          severity: checkSeverity as "info" | "warning" | "blocking",
          passed: checkPassed === "yes",
          evidence_artifact_ids: checkEvidenceIds,
          message: checkMessage.trim(),
        }],
        included_subject_ids: includedIds,
        excluded_subject_ids: excludedIds,
        exclusion_reasons: exclusionRows,
      });
      setReview(created);
      updateWorkspace({
        qcReviewId: created.review.review_revision_id,
        qcReviewVersion: created.version,
        qcReviewHash: created.review.content_hash,
      });
      setMessage("QC revision 已冻结。请再次核对纳入顺序、排除理由和产物后再批准。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function approve(): Promise<void> {
    if (!review || !run) return;
    setBusy(true);
    setError("");
    try {
      if (!approvalValid) throw new Error("必须填写 QC 审批人和审批理由。");
      const approved = await api.approveQcReview(review.review.review_revision_id, {
        expected_review_version: review.version,
        expected_run_version: run.version,
        review_hash: review.review.content_hash,
        actor: approvalActor.trim(),
        approved: true,
        reason: approvalReason.trim(),
      });
      const updatedRun = await api.run(run.run_id);
      setReview(approved);
      setRun(updatedRun);
      updateWorkspace({
        qcReviewId: approved.review.review_revision_id,
        qcReviewVersion: approved.version,
        qcReviewHash: approved.review.content_hash,
        runVersion: updatedRun.version,
        runState: updatedRun.state,
      });
      setMessage("人工 QC 已批准，统计输入顺序已冻结。 ");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader eyebrow="质量控制" title="先审查，再进入统计" description="自动检查只提供证据；任何纳入或排除决定都会形成新的、可追溯的人工 QC revision。" />
      <Feedback message={error || message} error={Boolean(error)} />
      <section className="metric-grid three">
        <MetricCard label="待审运行" value={run ? "1" : "0"} detail={run?.state ?? "尚未选择"} tone={run?.state === "qc_review" ? "warn" : "neutral"} />
        <MetricCard label="可选指标产物" value={String(metricArtifacts.length)} detail={`已显式选择 ${selectedMetricIds.length} 项 metric.*`} tone={selectedMetricIds.length ? "info" : "neutral"} />
        <MetricCard label="QC 状态" value={review?.state ?? "未创建"} detail={review ? `revision ${review.revision}` : "统计仍被阻断"} tone={review?.state === "approved" ? "good" : "warn"} />
      </section>
      {review && (
        <section className="panel frozen-review" aria-label="冻结的 QC revision 内容">
          <div className="panel-heading"><div><span className="eyebrow">冻结证据</span><h2>QC revision {review.revision}</h2></div><code>{review.review.content_hash.slice(0, 12)}…</code></div>
          <dl className="detail-list">
            <div><dt>完整内容哈希</dt><dd className="hash-value">{review.review.content_hash}</dd></div>
            <div><dt>输入 manifest 哈希</dt><dd className="hash-value">{review.review.input_manifest_hash}</dd></div>
            <div><dt>指标 Artifact</dt><dd>{review.review.metric_artifact_ids.join("、")}</dd></div>
            <div><dt>纳入顺序</dt><dd>{review.review.included_subject_ids.join(" → ") || "无"}</dd></div>
            <div><dt>排除受试者</dt><dd>{review.review.excluded_subject_ids.join("、") || "无排除"}</dd></div>
          </dl>
          <h3>全部 QC checks</h3>
          <ul className="compact-list">{review.review.checks.map((check, index) => <li key={`${check.code}-${index}`}><StatusPill tone={check.passed ? "good" : check.severity === "blocking" ? "danger" : "warn"}>{check.passed ? "通过" : "未通过"} · {check.severity}</StatusPill> {check.code}<small>{check.message} · 证据：{check.evidence_artifact_ids.join("、")}</small></li>)}</ul>
          <h3>排除理由</h3>
          {review.review.exclusion_reasons.length ? <ul className="compact-list">{review.review.exclusion_reasons.map(([subjectId, reason]) => <li key={subjectId}>{subjectId}<small>{reason}</small></li>)}</ul> : <p className="muted">此 revision 明确记录为无排除。</p>}
        </section>
      )}
      {!run ? <section className="panel"><EmptyState title="没有可审核的运行" detail="先让已批准计划完成 Mock 或经明确授权的 MATLAB 运行。" /></section> : (
        <div className="two-column wide-left">
          <section className="panel">
            <span className="eyebrow">QC revision 输入</span><h2>逐项确认指标、检查和受试者</h2>
            <fieldset>
              <legend>指标 Artifact（只允许 metric.*，至少一项）</legend>
              {metricArtifacts.length ? metricArtifacts.map((artifact) => <label className="check-field" key={artifact.artifact_id}><input type="checkbox" checked={selectedMetricIds.includes(artifact.artifact_id)} onChange={(event) => setSelectedMetricIds((current) => toggleId(current, artifact.artifact_id, event.target.checked))} disabled={Boolean(review)} /> 选择指标 Artifact {artifact.artifact_id}（{artifact.artifact_type}）</label>) : <p className="muted">本次运行没有完整的 metric.* Artifact；其他日志、掩膜或配置不能冒充指标图。</p>}
            </fieldset>
            <fieldset>
              <legend>至少一个自动或人工 QC check</legend>
              <div className="form-grid">
                <label>QC check code<input value={checkCode} onChange={(event) => setCheckCode(event.target.value)} disabled={Boolean(review)} placeholder="例如 motion.visual_review" /></label>
                <label>QC check severity<select value={checkSeverity} onChange={(event) => setCheckSeverity(event.target.value as typeof checkSeverity)} disabled={Boolean(review)}><option value="">明确选择</option><option value="info">info</option><option value="warning">warning</option><option value="blocking">blocking</option></select></label>
                <label>QC check passed<select value={checkPassed} onChange={(event) => setCheckPassed(event.target.value as typeof checkPassed)} disabled={Boolean(review)}><option value="">明确选择</option><option value="yes">通过</option><option value="no">未通过</option></select></label>
                <label>QC check message<textarea value={checkMessage} onChange={(event) => setCheckMessage(event.target.value)} disabled={Boolean(review)} placeholder="记录观察结果、阈值依据或人工判断" /></label>
              </div>
              <fieldset>
                <legend>QC check evidence（至少一个 Artifact）</legend>
                {artifacts.map((artifact) => <label className="check-field" key={artifact.artifact_id}><input type="checkbox" checked={checkEvidenceIds.includes(artifact.artifact_id)} onChange={(event) => setCheckEvidenceIds((current) => toggleId(current, artifact.artifact_id, event.target.checked))} disabled={Boolean(review)} /> QC check 证据 {artifact.artifact_id}（{artifact.artifact_type}）</label>)}
              </fieldset>
            </fieldset>
            <span className="eyebrow">受试者决定</span><h2>显式冻结纳入、排除与理由</h2>
            <div className="form-grid">
              <label>纳入受试者（每行一个，顺序会进入统计）<textarea value={included} onChange={(event) => setIncluded(event.target.value)} disabled={Boolean(review)} /></label>
              <label>是否排除受试者<select value={exclusionDecision} onChange={(event) => setExclusionDecision(event.target.value as typeof exclusionDecision)} disabled={Boolean(review)}><option value="">明确选择</option><option value="none">无排除</option><option value="some">有排除并逐项填写理由</option></select></label>
              {exclusionDecision === "some" && <label>排除受试者（每行“ID | 理由”）<textarea value={excluded} onChange={(event) => setExcluded(event.target.value)} disabled={Boolean(review)} placeholder="subject-x | 头动超出课题预注册阈值" /></label>}
            </div>
            <div className="issue-box"><StatusPill tone={subjectDecisionsValid ? "good" : "danger"}>{subjectDecisionsValid ? "决定完整" : "需要完整决定"}</StatusPill><p>纳入与排除必须无重复、无交集，并显式覆盖当前冻结清单；系统不会自动排除受试者。</p></div>
            <div className="form-grid">
              <label>QC 审批人<input value={approvalActor} onChange={(event) => setApprovalActor(event.target.value)} disabled={!review || review.state !== "draft"} /></label>
              <label>QC 审批理由<textarea value={approvalReason} onChange={(event) => setApprovalReason(event.target.value)} disabled={!review || review.state !== "draft"} placeholder="说明已核对的证据、纳入顺序和排除理由" /></label>
            </div>
            <div className="button-row">
              <button className="button button-secondary" type="button" disabled={busy || Boolean(review) || selectedMetricIds.length === 0 || !checkValid || !subjectDecisionsValid || run.state !== "qc_review"} onClick={createReview}>创建 QC revision</button>
              <button className="button button-primary" type="button" disabled={busy || !review || review.state !== "draft" || !approvalValid || hasBlockingFailure} onClick={approve}>人工批准并冻结</button>
            </div>
          </section>
          <aside className="panel">
            <span className="eyebrow">本次运行登记产物</span><h2>{artifacts.length} 项</h2>
            {artifacts.length ? <ul className="compact-list">{artifacts.map((artifact) => <li key={artifact.artifact_id}>{artifact.artifact_type}<small>{artifact.artifact_id}{artifact.artifact_type.startsWith("metric.") ? " · 可选指标" : " · 仅作证据"}</small></li>)}</ul> : <EmptyState title="尚无完整产物" detail="部分或失败输出不能进入 QC 批准。" />}
          </aside>
        </div>
      )}
    </>
  );
}
