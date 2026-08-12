"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/apiClient";
import type { AgencySummary } from "@/lib/types";

export default function Home() {
  const [agencies, setAgencies] = useState<AgencySummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    api
      .agencies()
      .then((rows) => setAgencies([...rows].sort((a, b) => a.name.localeCompare(b.name))))
      .catch((e) => setError(String(e)));
  }, []);

  const filtered =
    agencies?.filter((a) => a.name.toLowerCase().includes(query.trim().toLowerCase())) ?? null;

  return (
    <main>
      <div className="card">
        <h2>Agencies</h2>
        <p className="card-hint">
          Pick an agency to view its OTP, headway/dwell, alert, and line-delay metrics.
        </p>
        {error && <p className="error-state">Failed to load agencies: {error}</p>}
        {!agencies && !error && <p className="empty-state">Loading…</p>}
        {agencies && agencies.length > 0 && (
          <div className="field">
            <label htmlFor="agency-search">Search</label>
            <input
              id="agency-search"
              type="text"
              placeholder="Filter by agency name…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        )}
        {agencies && agencies.length === 0 && (
          <p className="empty-state">No agencies configured.</p>
        )}
        {filtered && agencies && agencies.length > 0 && filtered.length === 0 && (
          <p className="empty-state">No agencies match &ldquo;{query}&rdquo;.</p>
        )}
        {filtered && filtered.length > 0 && (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Agency</th>
                  <th>Timezone</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {filtered.map((a) => (
                  <tr key={a.agency_id}>
                    <td>{a.name}</td>
                    <td>{a.timezone}</td>
                    <td>
                      <Link href={`/otp?agency=${a.agency_id}`}>View metrics →</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  );
}
