"use client";

import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { formatClock, formatDuration } from "@/lib/durations";
import type { TripRun } from "@/lib/types";

/**
 * One dot per vehicle run: when it left the origin (x) against how long it
 * took to reach the destination (y). Scatter rather than a line because runs
 * are discrete events, and a slow run shows up as a dot lifted off the band.
 */
export function TripRunScatter({ runs, height = 300 }: { runs: TripRun[]; height?: number }) {
  const points = runs.map((r) => ({
    x: r.departure_unix,
    y: r.travel_time_s,
    trip_id: r.trip_id,
    headway_s: r.headway_s,
  }));

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ScatterChart margin={{ left: 8, right: 16, top: 8, bottom: 8 }}>
        <CartesianGrid stroke="var(--gridline)" />
        <XAxis
          type="number"
          dataKey="x"
          domain={["dataMin", "dataMax"]}
          tickFormatter={formatClock}
          tick={{ fill: "var(--text-muted)", fontSize: 12 }}
          stroke="var(--baseline)"
        />
        <YAxis
          type="number"
          dataKey="y"
          tickFormatter={formatDuration}
          tick={{ fill: "var(--text-muted)", fontSize: 12 }}
          stroke="var(--baseline)"
        />
        <ZAxis range={[36, 36]} />
        <Tooltip
          cursor={{ stroke: "var(--border-hairline)" }}
          contentStyle={{
            background: "var(--surface)",
            border: "1px solid var(--border-hairline)",
            fontSize: 13,
          }}
          formatter={(value, name) => {
            if (name === "y") return [formatDuration(Number(value)), "Travel time"];
            if (name === "x") return [formatClock(Number(value)), "Left origin"];
            return [value, name];
          }}
        />
        <Scatter data={points} fill="var(--series-1)" fillOpacity={0.75} />
      </ScatterChart>
    </ResponsiveContainer>
  );
}
