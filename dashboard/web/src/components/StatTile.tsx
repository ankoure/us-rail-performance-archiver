export interface StatDelta {
  direction: "up" | "down" | "flat";
  text: string;
}

const DELTA_ARROW: Record<StatDelta["direction"], string> = { up: "▲", down: "▼", flat: "→" };

export function StatTile({
  label,
  value,
  delta,
}: {
  label: string;
  value: string;
  delta?: StatDelta;
}) {
  return (
    <div className="stat-tile">
      <span className="value">{value}</span>
      <span className="label">{label}</span>
      {delta && (
        <span className="stat-delta">
          {DELTA_ARROW[delta.direction]} {delta.text}
        </span>
      )}
    </div>
  );
}
