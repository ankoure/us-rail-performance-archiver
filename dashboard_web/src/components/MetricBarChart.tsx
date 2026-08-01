"use client";

import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export interface MetricSeries {
  dataKey: string;
  label: string;
  color: string;
}

export function MetricBarChart<T extends object>({
  data,
  categoryKey,
  series,
  height,
}: {
  data: T[];
  categoryKey: string;
  series: MetricSeries[];
  height?: number;
}) {
  return (
    <ResponsiveContainer width="100%" height={height ?? Math.max(200, data.length * 36)}>
      <BarChart data={data as Record<string, unknown>[]} layout="vertical" margin={{ left: 8, right: 16 }}>
        <CartesianGrid horizontal={false} stroke="var(--gridline)" />
        <XAxis type="number" tick={{ fill: "var(--text-muted)", fontSize: 12 }} stroke="var(--baseline)" />
        <YAxis
          type="category"
          dataKey={categoryKey}
          width={90}
          tick={{ fill: "var(--text-secondary)", fontSize: 12 }}
          stroke="var(--baseline)"
        />
        <Tooltip
          contentStyle={{ background: "var(--surface)", border: "1px solid var(--border-hairline)", fontSize: 13 }}
        />
        {series.length > 1 && (
          <Legend formatter={(value) => series.find((s) => s.dataKey === value)?.label ?? value} />
        )}
        {series.map((s, i) => (
          <Bar
            key={s.dataKey}
            dataKey={s.dataKey}
            fill={s.color}
            radius={i === series.length - 1 ? [0, 4, 4, 0] : undefined}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}
