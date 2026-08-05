"use client";

import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { MetricSeries } from "./MetricBarChart";

export function TimeSeriesChart<T extends object>({
  data,
  dateKey,
  series,
  height = 260,
  valueFormatter,
}: {
  data: T[];
  dateKey: string;
  series: MetricSeries[];
  height?: number;
  valueFormatter?: (value: number) => string;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data as Record<string, unknown>[]} margin={{ left: 8, right: 16, top: 8 }}>
        <CartesianGrid stroke="var(--gridline)" />
        <XAxis dataKey={dateKey} tick={{ fill: "var(--text-muted)", fontSize: 12 }} stroke="var(--baseline)" />
        <YAxis
          tick={{ fill: "var(--text-muted)", fontSize: 12 }}
          stroke="var(--baseline)"
          tickFormatter={valueFormatter}
        />
        <Tooltip
          contentStyle={{ background: "var(--surface)", border: "1px solid var(--border-hairline)", fontSize: 13 }}
          formatter={(value) => (valueFormatter && typeof value === "number" ? valueFormatter(value) : value)}
        />
        {series.length > 1 && (
          <Legend formatter={(value) => series.find((s) => s.dataKey === value)?.label ?? value} />
        )}
        {series.map((s) => (
          <Line
            key={s.dataKey}
            type="monotone"
            dataKey={s.dataKey}
            stroke={s.color}
            dot={false}
            strokeWidth={2}
            connectNulls
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
