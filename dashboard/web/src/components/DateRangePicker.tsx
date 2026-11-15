"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { addDays, clampRange, defaultRange, MAX_RANGE_DAYS } from "@/lib/dates";

/**
 * The page's selected range, clamped to `maxDays` so a hand-edited or stale
 * URL never asks the API for a range it will reject. Pass the same `maxDays`
 * as the page's <DateRangePicker>.
 */
export function useDateRange(maxDays: number = MAX_RANGE_DAYS): { start: string; end: string } {
  const searchParams = useSearchParams();
  const fallback = defaultRange(7);
  return clampRange(
    searchParams.get("start") ?? fallback.start,
    searchParams.get("end") ?? fallback.end,
    maxDays,
  );
}

export function DateRangePicker({ maxDays = MAX_RANGE_DAYS }: { maxDays?: number }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { start, end } = useDateRange(maxDays);

  // Moving one end past the limit drags the other along with it, so the
  // range stays valid even when the browser doesn't enforce min/max (typed
  // input, some mobile pickers).
  function setRange(next: { start: string; end: string }) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("start", next.start);
    params.set("end", next.end);
    router.push(`${pathname}?${params.toString()}`);
  }

  function setStart(value: string) {
    if (!value) return;
    const latestEnd = addDays(value, maxDays);
    setRange({ start: value, end: end < value ? value : end > latestEnd ? latestEnd : end });
  }

  function setEnd(value: string) {
    if (!value) return;
    const earliestStart = addDays(value, -maxDays);
    setRange({
      start: start > value ? value : start < earliestStart ? earliestStart : start,
      end: value,
    });
  }

  const limitHint = `Ranges are limited to ${maxDays} days`;

  return (
    <>
      <div className="field">
        <label htmlFor="start-date">Start date</label>
        <input
          id="start-date"
          type="date"
          value={start}
          max={end}
          title={limitHint}
          onChange={(e) => setStart(e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="end-date">End date</label>
        <input
          id="end-date"
          type="date"
          value={end}
          min={start}
          title={limitHint}
          onChange={(e) => setEnd(e.target.value)}
        />
      </div>
    </>
  );
}
