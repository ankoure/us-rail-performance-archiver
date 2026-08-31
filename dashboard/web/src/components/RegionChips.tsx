"use client";

import { useState } from "react";
import type { AgencySummary } from "@/lib/types";

interface RegionChipsProps {
  agencies: AgencySummary[]; // pre-filtered to the selected continent
  selected: string | null;
  onSelect: (region: string | null) => void;
}

const UNSPECIFIED = "Unspecified";

export function RegionChips({ agencies, selected, onSelect }: RegionChipsProps) {
  const [expanded, setExpanded] = useState(false);

  const counts = new Map<string, number>();
  for (const a of agencies) {
    const key = a.region ?? UNSPECIFIED;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const regions = [...counts.keys()].sort((a, b) => counts.get(b)! - counts.get(a)!);

  // A single region tells the user nothing they don't already know from the
  // continent tab -- skip the chip row entirely rather than show one chip.
  if (regions.length <= 1) return null;

  // Most regions here have exactly one agency (the `region` field is
  // free-text per agency, not a coarse taxonomy) -- showing every one of
  // them at once just recreates the "wall of everything" this drill-down is
  // meant to fix. Show clusters worth browsing (>=2 agencies) plus whatever
  // is already selected, and collapse the rest behind a toggle.
  const worthShowing = regions.filter((r) => counts.get(r)! > 1 || r === selected);
  const rest = regions.filter((r) => !worthShowing.includes(r));
  const visible = expanded ? regions : worthShowing;

  return (
    <div className="chip-row" role="group" aria-label="Region">
      <button
        type="button"
        className={`chip${selected === null ? " active" : ""}`}
        onClick={() => onSelect(null)}
      >
        All regions
      </button>
      {visible.map((r) => (
        <button
          key={r}
          type="button"
          className={`chip${selected === r ? " active" : ""}`}
          onClick={() => onSelect(r)}
        >
          {r} ({counts.get(r)})
        </button>
      ))}
      {!expanded && rest.length > 0 && (
        <button type="button" className="chip chip-more" onClick={() => setExpanded(true)}>
          +{rest.length} more
        </button>
      )}
    </div>
  );
}
