type Tone = "neutral" | "good" | "warn" | "danger" | "info";

export function StatusPill({ children, tone = "neutral" }: { children: React.ReactNode; tone?: Tone }) {
  return <span className={`status-pill status-${tone}`}>{children}</span>;
}
