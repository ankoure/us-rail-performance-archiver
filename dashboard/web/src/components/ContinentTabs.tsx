"use client";

import { continentLabel } from "@/lib/agencyTypes";
import type { AgencySummary } from "@/lib/types";

interface ContinentTabsProps {
  agencies: AgencySummary[];
  selected: string | null;
  onSelect: (continent: string | null) => void;
}

export function ContinentTabs({ agencies, selected, onSelect }: ContinentTabsProps) {
  const counts = new Map<string, number>();
  for (const a of agencies) {
    const key = a.continent ?? "other";
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const continents = [...counts.keys()].sort((a, b) => counts.get(b)! - counts.get(a)!);

  return (
    <div className="tab-row" role="tablist" aria-label="Continent">
      <button
        type="button"
        role="tab"
        aria-selected={selected === null}
        className={`tab${selected === null ? " active" : ""}`}
        onClick={() => onSelect(null)}
      >
        All ({agencies.length})
      </button>
      {continents.map((c) => (
        <button
          key={c}
          type="button"
          role="tab"
          aria-selected={selected === c}
          className={`tab${selected === c ? " active" : ""}`}
          onClick={() => onSelect(c)}
        >
          {c === "other" ? "Other" : continentLabel(c)} ({counts.get(c)})
        </button>
      ))}
    </div>
  );
}
