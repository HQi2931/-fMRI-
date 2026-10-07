import { useState, type ReactNode } from "react";
import type { WorkspaceCheck } from "../api/client";

type Props = {
  path: string;
  check: WorkspaceCheck | null;
  checking: boolean;
  checkError: string;
  selectedStage: string;
  stageConfirmed?: boolean;
  busy: boolean;
  consentBusy: boolean;
  consentGranted: boolean;
  canAuthorize: boolean;
  authorizationUnavailableReason: string;
  onChoose: () => void;
  onStageChange: (stage: string) => void;
  onConsent: () => void;
  onPathSubmit?: (path: string) => void;
  children?: ReactNode;
};

export function WorkspacePanel(props: Props) {
  const [typedPath, setTypedPath] = useState(props.path);
  const [pathEditorOpen, setPathEditorOpen] = useState(false);
  const name = props.path.replace(/\\/g, "/").split("/").filter(Boolean).at(-1) || props.path;
  const candidates = props.check?.stage_candidates ?? [];
  const selected = candidates.find((item) => item.stage === props.selectedStage);

  return <section className="work-folder" aria-label="工作区设置">
    <div className="work-folder-heading">
      <div className="work-folder-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h4l3 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" /><path d="M3 9h18" /></svg>
      </div>
      <div className="work-folder-identity">
        <span className="work-folder-eyebrow">当前工作区</span>
        <strong title={props.path || undefined}>{props.path ? name : "从一个工作文件夹开始"}</strong>
        {!props.path && <p>选择数据或项目所在的文件夹，开始整理分析方案。</p>}
        {props.path && <details className="work-folder-path"><summary>查看完整路径</summary><code>{props.path}</code></details>}
      </div>
      <button className={`button ${props.path ? "button-secondary" : "button-primary"}`} type="button" disabled={props.busy} onClick={props.onChoose}>{props.path ? "更换工作区" : "选择工作区"}</button>
    </div>
    {props.onPathSubmit && <details onToggle={(event) => setPathEditorOpen(event.currentTarget.open)}><summary>手动填写工作区路径</summary>{pathEditorOpen && <><label>工作区路径<input value={typedPath} disabled={props.busy} onChange={(event) => setTypedPath(event.target.value)} /></label><button className="button button-secondary" type="button" disabled={props.busy || !typedPath.trim()} onClick={() => props.onPathSubmit?.(typedPath.trim())}>使用此工作区</button></>}</details>}
    {props.path && <>
      {props.checking && <p className="work-folder-notice" role="status">正在检查工作区中的起始阶段…</p>}
      {props.checkError && <p className="work-folder-notice work-folder-error" role="status">工作区检查未完成：{props.checkError}</p>}
      {candidates.length > 0 && <details className="work-stage-picker">
        <summary>
          <span className="work-stage-summary"><strong>预处理起点</strong><span>{selected ? `${selected.stage} · ${props.stageConfirmed ? "已确认" : "待确认"}` : "尚未选择"}</span></span>
          <span className="work-stage-count">{candidates.length} 个阶段<span className="work-stage-chevron" aria-hidden="true">⌄</span></span>
        </summary>
        <fieldset className="work-stage-options" disabled={props.busy}>
          <legend className="sr-only">DPABI 预处理起始阶段</legend>
          {candidates.map((item) => { const Container = candidates.length === 1 ? "div" : "label"; return <Container className={`work-stage-option${props.selectedStage === item.stage ? " is-selected" : ""}${!item.selectable ? " is-unavailable" : ""}`} key={item.stage}>
            {candidates.length === 1
              ? <button type="button" disabled={props.busy || !item.selectable || props.selectedStage === item.stage} onClick={() => props.onStageChange(item.stage)}>{props.selectedStage === item.stage ? props.stageConfirmed ? "起点已确认" : "已选择，待确认" : `选择 ${item.stage}`}</button>
              : <input type="radio" name="workspace-input-stage" value={item.stage} checked={props.selectedStage === item.stage} disabled={!item.selectable} onChange={() => props.onStageChange(item.stage)} />}
            <span className="work-stage-description"><strong>{item.stage}</strong><span>{item.label}</span><small>{item.subject_count} 个受试者 · {item.file_count} 个文件</small>{item.issues.length > 0 && <small className="work-stage-issue">{item.issues.join("；")}</small>}</span>
          </Container>; })}
        </fieldset>
        <div className="work-stage-help"><p>可以先告诉助手已完成哪些处理，由模型建议起点。选择后，使用方案中的确认入口核对起始阶段。</p>{props.selectedStage && <button className="work-folder-link" type="button" disabled={props.busy} onClick={() => props.onStageChange("")}>清除选择</button>}</div>
      </details>}
      <div className="work-folder-access">
        <span><span className={`work-access-dot${props.consentGranted ? " is-granted" : ""}`} aria-hidden="true" />{props.consentGranted ? "文件分析已授权" : "文件分析未授权"}<small>{props.consentGranted ? "授权绑定当前工作区、模型和配置" : props.canAuthorize ? "授权时会先发送不含文件内容的工具测试" : props.authorizationUnavailableReason}</small></span>
        <button className="work-folder-link" type="button" disabled={props.busy || props.consentBusy || (!props.consentGranted && !props.canAuthorize)} onClick={props.onConsent}>{props.consentBusy ? "测试并授权…" : props.consentGranted ? "撤销文件授权" : "授权文件分析"}</button>
      </div>
      {props.children}
    </>}
  </section>;
}
