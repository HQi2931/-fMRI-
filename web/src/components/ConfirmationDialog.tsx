import { useEffect, useRef, useState } from "react";

import type { ConfirmationRequest } from "./confirmation";

export function ConfirmationDialog({ request, busy, error, onConfirm, onDismiss, onEdit }: {
  request: ConfirmationRequest;
  busy: boolean;
  error: string;
  onConfirm: (command: string) => void;
  onDismiss: () => void;
  onEdit: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [selected, setSelected] = useState<string>("");
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const element = dialog.current;
    element?.showModal();
    return () => { element?.close(); previous?.focus(); };
  }, []);

  return <dialog ref={dialog} className="confirmation-dialog" aria-labelledby="confirmation-title"
    aria-describedby="confirmation-description" onCancel={(event) => { event.preventDefault(); if (!busy) onDismiss(); }}>
    <div className="confirmation-heading"><span>需要你的确认</span><button type="button" aria-label="关闭确认" disabled={busy} onClick={onDismiss}>×</button></div>
    <h2 id="confirmation-title">{request.title}</h2>
    <p id="confirmation-description">请审查以下内容，再选择如何继续。</p>
    <div className="confirmation-summary">{request.summary}</div>
    <fieldset disabled={busy} className="confirmation-options">
      <legend>本次操作</legend>
      {request.options.map((option) => <label key={option.command} className={selected === option.command ? "selected" : ""}>
        <input type="radio" name="confirmation-option" value={option.command} checked={selected === option.command} onChange={() => setSelected(option.command)} />
        <span><strong>{option.label}</strong><small>{option.description}</small></span>
      </label>)}
    </fieldset>
    {error && <p role="alert" className="confirmation-error">{error}</p>}
    <div className="confirmation-footer">
      <button type="button" className="button button-secondary" disabled={busy} onClick={onDismiss}>暂不执行</button>
      <button type="button" className="button button-secondary" disabled={busy} onClick={onEdit}>修改要求</button>
      <button type="button" className="button button-primary" disabled={busy || !selected} onClick={() => onConfirm(selected)}>{busy ? "正在提交…" : "确认选择"}</button>
    </div>
  </dialog>;
}
