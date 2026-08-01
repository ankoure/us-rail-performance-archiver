export function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat-tile">
      <span className="value">{value}</span>
      <span className="label">{label}</span>
    </div>
  );
}
