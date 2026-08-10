import { StatusPill } from "./StatusPill";

export function PageHeader({ eyebrow, title, description, action }: {
  eyebrow: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}

export function MetricCard({ label, value, detail, tone = "neutral" }: {
  label: string;
  value: string;
  detail: string;
  tone?: "neutral" | "good" | "warn" | "danger" | "info";
}) {
  return (
    <article className="metric-card">
      <div className={`metric-accent accent-${tone}`} />
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

export function ProgressBar({ value }: { value: number }) {
  return (
    <div
      className="progress"
      role="progressbar"
      aria-label={`完成 ${value}%`}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.min(100, Math.max(0, value))}
    >
      <span style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </div>
  );
}

export function Feedback({ message, error = false }: { message: string; error?: boolean }) {
  if (!message) return null;
  return (
    <div className={`inline-message${error ? " inline-error" : ""}`} role={error ? "alert" : "status"}>
      {message}
    </div>
  );
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-state">
      <span aria-hidden="true">○</span>
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
}

export function SafetyNotice() {
  return (
    <div className="safety-notice">
      <StatusPill tone="good">安全边界</StatusPill>
      <p>数据检查不会修改源文件；执行前会再次核对方案审批、输入哈希和工作目录。</p>
    </div>
  );
}
