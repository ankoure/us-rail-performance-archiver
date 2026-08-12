"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface OtpBarDatum {
  route_id: string;
  on_time_count: number;
  early_count: number;
  late_count: number;
}

const STATUS_COLORS = {
  on_time_count: "var(--status-good)",
  early_count: "var(--status-warning)",
  late_count: "var(--status-critical)",
};

const STATUS_LABELS = {
  on_time_count: "On time",
  early_count: "Early",
  late_count: "Late",
};

export function OtpStackedBarChart({ data }: { data: OtpBarDatum[] }) {
  return (
    <ResponsiveContainer width="100%" height={Math.max(200, data.length * 36)}>
      <BarChart data={data} layout="vertical" margin={{ left: 8, right: 16 }}>
        <defs>
          {/* Late uses a hatch pattern, not just red, so on-time vs. late doesn't rely on green/red hue alone. */}
          <pattern
            id="latePattern"
            patternUnits="userSpaceOnUse"
            width="6"
            height="6"
            patternTransform="rotate(45)"
          >
            <rect width="6" height="6" fill={STATUS_COLORS.late_count} />
            <line x1="0" y1="0" x2="0" y2="6" stroke="rgba(0,0,0,0.35)" strokeWidth="2" />
          </pattern>
        </defs>
        <CartesianGrid horizontal={false} stroke="var(--gridline)" />
        <XAxis
          type="number"
          tick={{ fill: "var(--text-muted)", fontSize: 12 }}
          stroke="var(--baseline)"
        />
        <YAxis
          type="category"
          dataKey="route_id"
          width={90}
          tick={{ fill: "var(--text-secondary)", fontSize: 12 }}
          stroke="var(--baseline)"
        />
        <Tooltip
          contentStyle={{
            background: "var(--surface)",
            border: "1px solid var(--border-hairline)",
            fontSize: 13,
          }}
        />
        <Legend
          formatter={(value) => STATUS_LABELS[value as keyof typeof STATUS_LABELS] ?? value}
        />
        <Bar
          dataKey="on_time_count"
          stackId="otp"
          fill={STATUS_COLORS.on_time_count}
          radius={[0, 0, 0, 0]}
        />
        <Bar dataKey="early_count" stackId="otp" fill={STATUS_COLORS.early_count} />
        <Bar dataKey="late_count" stackId="otp" fill="url(#latePattern)" radius={[0, 4, 4, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
