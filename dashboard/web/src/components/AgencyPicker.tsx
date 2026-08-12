"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/apiClient";
import type { AgencySummary } from "@/lib/types";

export function AgencyPicker({ railOnly = false }: { railOnly?: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const agency = searchParams.get("agency") ?? "";

  const [agencies, setAgencies] = useState<AgencySummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .agencies({ railOnly })
      .then((rows) => setAgencies([...rows].sort((a, b) => a.name.localeCompare(b.name))))
      .catch((e) => setError(String(e)));
  }, [railOnly]);

  function onChange(nextAgency: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (nextAgency) {
      params.set("agency", nextAgency);
    } else {
      params.delete("agency");
    }
    router.push(`${pathname}?${params.toString()}`);
  }

  return (
    <div className="field">
      <label htmlFor="agency-picker">Agency</label>
      <select id="agency-picker" value={agency} onChange={(e) => onChange(e.target.value)}>
        <option value="">Select an agency…</option>
        {error && <option disabled>Failed to load agencies</option>}
        {agencies && agency && !agencies.some((a) => a.agency_id === agency) && (
          <option value={agency}>{agency} (no rail data)</option>
        )}
        {agencies?.map((a) => (
          <option key={a.agency_id} value={a.agency_id}>
            {a.name}
          </option>
        ))}
      </select>
    </div>
  );
}
