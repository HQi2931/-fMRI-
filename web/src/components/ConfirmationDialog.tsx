import { useEffect, useRef, useState } from "react";

import type { ConfirmationRequest } from "./confirmation";

export function ConfirmationDialog({ request, busy, error, onConfirm, onDismiss, onEdit, onStopWaiting }: {
  request: ConfirmationRequest;
  busy: boolean;
  error: string;
  onConfirm: (command: string) => void;
  onDismiss: () => void;
  onEdit: () => void;
  onStopWaiting?: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [selected, setSelected] = useState<string>(request.options.length === 1 ? request.options[0].command : "");
  const selectedCommand = request.options.length === 1 ? request.options[0].command : request.options.some((option) => option.command === selected) ? selected : "";
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const element = dialog.current;
    element?.showModal();
    return () => { element?.close(); previous?.focus(); };
  }, []);

  return <dialog ref={dialog} className="confirmation-dialog" aria-labelledby="confirmation-title"
    aria-describedby="confirmation-description" onCancel={(event) => { event.preventDefault(); if (!busy) onDismiss(); }}
    onKeyDown={(event) => {
      if (event.key !== "Tab") return;
      const controls = [...event.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)")];
      const current = controls.indexOf(document.activeElement as HTMLElement);
      if (!controls.length || (event.shiftKey ? current <= 0 : current < 0 || current === controls.length - 1)) {
        event.preventDefault();
        (event.shiftKey ? controls.at(-1) : controls[0])?.focus();
      }
    }}>
    <div className="confirmation-heading"><span>需要你的确认</span><button type="button" aria-label="关闭确认" disabled={busy} onClick={onDismiss}>×</button></div>
    <h2 id="confirmation-title">{request.title}</h2>
    <p id="confirmation-description">请核对本次操作。修改方案后需要重新确认。</p>
    <div className="confirmation-summary">{request.summary}</div>
    {request.options.length === 1 ? <p className="confirmation-single-option"><strong>{request.options[0].label}</strong><br />{request.options[0].description}</p> : <fieldset disabled={busy} className="confirmation-options">
      <legend>本次操作</legend>
      {request.options.map((option) => <label key={option.command} className={selected === option.command ? "selected" : ""}>
        <input type="radio" name="confirmation-option" value={option.command} checked={selected === option.command} onChange={() => setSelected(option.command)} />
        <span><strong>{option.label}</strong><small>{option.description}</small></span>
      </label>)}
    </fieldset>}
    {error && <p role="alert" className="confirmation-error">{error}</p>}
    {busy && onStopWaiting && <p role="status">正在等待提交结果。停止等待不会取消计算。</p>}
    <div className="confirmation-footer">
      {busy && onStopWaiting && <button type="button" className="button button-secondary" onClick={onStopWaiting}>停止等待</button>}
      <button type="button" className="button button-secondary" disabled={busy} onClick={onDismiss}>保留方案</button>
      <button type="button" className="button button-secondary" disabled={busy} onClick={onEdit}>返回修改</button>
      <button type="button" className="button button-primary" disabled={busy || !selectedCommand} onClick={() => onConfirm(selectedCommand)}>{busy ? "正在提交…" : request.options.length === 1 ? request.options[0].label : "确认选择"}</button>
    </div>
  </dialog>;
}
