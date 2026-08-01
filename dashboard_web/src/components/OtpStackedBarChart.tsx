"use client";

import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

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
        <CartesianGrid horizontal={false} stroke="var(--gridline)" />
        <XAxis type="number" tick={{ fill: "var(--text-muted)", fontSize: 12 }} stroke="var(--baseline)" />
        <YAxis
          type="category"
          dataKey="route_id"
          width={90}
          tick={{ fill: "var(--text-secondary)", fontSize: 12 }}
          stroke="var(--baseline)"
        />
        <Tooltip contentStyle={{ background: "var(--surface)", border: "1px solid var(--border-hairline)", fontSize: 13 }} />
        <Legend formatter={(value) => STATUS_LABELS[value as keyof typeof STATUS_LABELS] ?? value} />
        <Bar dataKey="on_time_count" stackId="otp" fill={STATUS_COLORS.on_time_count} radius={[0, 0, 0, 0]} />
        <Bar dataKey="early_count" stackId="otp" fill={STATUS_COLORS.early_count} />
        <Bar dataKey="late_count" stackId="otp" fill={STATUS_COLORS.late_count} radius={[0, 4, 4, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
