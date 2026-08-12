"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/apiClient";
import type { AgencySummary } from "@/lib/types";

export function useAgencies(): string[] {
  const searchParams = useSearchParams();
  const raw = searchParams.get("agencies") ?? "";
  return raw ? raw.split(",").filter(Boolean) : [];
}

export function AgencyMultiPicker() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const selected = useAgencies();

  const [agencies, setAgencies] = useState<AgencySummary[] | null>(null);

  useEffect(() => {
    api.agencies().then((rows) => setAgencies([...rows].sort((a, b) => a.name.localeCompare(b.name))));
  }, []);

  function onChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const values = Array.from(e.target.selectedOptions).map((o) => o.value);
    const params = new URLSearchParams(searchParams.toString());
    if (values.length > 0) {
      params.set("agencies", values.join(","));
    } else {
      params.delete("agencies");
    }
    router.push(`${pathname}?${params.toString()}`);
  }

  return (
    <div className="field">
      <label htmlFor="agency-multi-picker">Agencies (ctrl/cmd-click for multiple)</label>
      <select id="agency-multi-picker" multiple value={selected} onChange={onChange} size={6}>
        {agencies?.map((a) => (
          <option key={a.agency_id} value={a.agency_id}>
            {a.name}
          </option>
        ))}
      </select>
    </div>
  );
}
