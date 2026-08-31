"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/apiClient";
import type { AgencySummary } from "@/lib/types";
import { ContinentTabs } from "@/components/ContinentTabs";
import { RegionChips } from "@/components/RegionChips";
import { AgencyTypeGrid } from "@/components/AgencyTypeGrid";

export default function Home() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const continent = searchParams.get("continent");
  const region = searchParams.get("region");

  const [agencies, setAgencies] = useState<AgencySummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    api
      .agencies()
      .then((rows) => setAgencies([...rows].sort((a, b) => a.name.localeCompare(b.name))))
      .catch((e) => setError(String(e)));
  }, []);

  function setParam(key: string, value: string | null) {
    const params = new URLSearchParams(searchParams.toString());
    if (value) {
      params.set(key, value);
    } else {
      params.delete(key);
    }
    if (key === "continent") params.delete("region");
    const qs = params.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname);
  }

  const searching = query.trim().length > 0;

  const searchMatches = useMemo(() => {
    if (!agencies || !searching) return null;
    const q = query.trim().toLowerCase();
    return agencies.filter((a) => a.name.toLowerCase().includes(q));
  }, [agencies, query, searching]);

  const byContinent = useMemo(() => {
    if (!agencies) return null;
    return continent ? agencies.filter((a) => (a.continent ?? "other") === continent) : agencies;
  }, [agencies, continent]);

  const byRegion = useMemo(() => {
    if (!byContinent) return null;
    return region ? byContinent.filter((a) => (a.region ?? "Unspecified") === region) : byContinent;
  }, [byContinent, region]);

  return (
    <main>
      <div className="card">
        <h2>Agencies</h2>
        <p className="card-hint">
          Browse by region and system type, or search directly, to view OTP, headway/dwell, alert,
          and line-delay metrics.
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
      </div>

      {searching && searchMatches && (
        <div className="card">
          {searchMatches.length === 0 ? (
            <p className="empty-state">No agencies match &ldquo;{query}&rdquo;.</p>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Agency</th>
                    <th>Region</th>
                    <th>Timezone</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {searchMatches.map((a) => (
                    <tr key={a.agency_id}>
                      <td>{a.name}</td>
                      <td>{a.region ?? "—"}</td>
                      <td>{a.timezone}</td>
                      <td>
                        <Link href={`/agency/${a.agency_id}`}>View metrics →</Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {!searching && agencies && agencies.length > 0 && (
        <>
          <div className="card">
            <ContinentTabs
              agencies={agencies}
              selected={continent}
              onSelect={(c) => setParam("continent", c)}
            />
            <RegionChips
              agencies={byContinent ?? []}
              selected={region}
              onSelect={(r) => setParam("region", r)}
            />
          </div>
          <AgencyTypeGrid agencies={byRegion ?? []} />
        </>
      )}
    </main>
  );
}
