import Link from "next/link";
import type { AlertRow } from "@/lib/types";

const SEVERITY_STYLE: Record<string, { color: string; background: string }> = {
  SEVERE: {
    color: "var(--status-critical)",
    background: "color-mix(in srgb, var(--status-critical) 15%, transparent)",
  },
  WARNING: {
    color: "var(--status-warning)",
    background: "color-mix(in srgb, var(--status-warning) 18%, transparent)",
  },
  INFO: {
    color: "var(--status-good)",
    background: "color-mix(in srgb, var(--status-good) 15%, transparent)",
  },
};

function firstTranslation(field: AlertRow["alert"]["header_text"]): string | null {
  return (
    field?.translation.find((t) => t.language.startsWith("en"))?.text ??
    field?.translation[0]?.text ??
    null
  );
}

function formatUnix(seconds: number | null): string {
  if (seconds === null) return "—";
  return new Date(seconds * 1000).toLocaleString();
}

export function AlertCard({ row, agency, day }: { row: AlertRow; agency?: string; day?: string }) {
  const { alert } = row;
  const header = firstTranslation(alert.header_text) ?? "(no header text)";
  const description = firstTranslation(alert.description_text);
  const severity = alert.severity_level ?? "UNKNOWN_SEVERITY";
  const severityStyle = SEVERITY_STYLE[severity];
  const routes = [
    ...new Set(alert.informed_entity.map((e) => e.route_id).filter(Boolean)),
  ] as string[];

  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <h2>{header}</h2>
        <span
          className="status-pill"
          style={{
            color: severityStyle?.color ?? "var(--text-muted)",
            background: severityStyle?.background ?? "var(--surface)",
          }}
        >
          {severity}
        </span>
      </div>
      {description && <p style={{ fontSize: 14 }}>{description}</p>}
      <div className="legend">
        {alert.cause && <span>Cause: {alert.cause}</span>}
        {alert.effect && <span>Effect: {alert.effect}</span>}
        {routes.length > 0 && (
          <span>
            Routes:{" "}
            {routes.map((r, i) => (
              <span key={r}>
                {i > 0 && ", "}
                {agency && day ? (
                  <Link href={`/route?agency=${agency}&line=${r}&start=${day}&end=${day}`}>
                    {r}
                  </Link>
                ) : (
                  r
                )}
              </span>
            ))}
          </span>
        )}
      </div>
      <p className="card-hint">
        First seen {formatUnix(row.first_seen)} · last seen {formatUnix(row.last_seen)} ·{" "}
        {row.poll_count} polls
      </p>
    </div>
  );
}
