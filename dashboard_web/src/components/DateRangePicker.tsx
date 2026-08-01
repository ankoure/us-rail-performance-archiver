"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { defaultRange } from "@/lib/dates";

export function useDateRange(): { start: string; end: string } {
  const searchParams = useSearchParams();
  const fallback = defaultRange(7);
  return {
    start: searchParams.get("start") ?? fallback.start,
    end: searchParams.get("end") ?? fallback.end,
  };
}

export function DateRangePicker() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { start, end } = useDateRange();

  function setParam(key: string, value: string) {
    const params = new URLSearchParams(searchParams.toString());
    params.set(key, value);
    router.push(`${pathname}?${params.toString()}`);
  }

  return (
    <>
      <div className="field">
        <label htmlFor="start-date">Start date</label>
        <input
          id="start-date"
          type="date"
          value={start}
          max={end}
          onChange={(e) => setParam("start", e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="end-date">End date</label>
        <input
          id="end-date"
          type="date"
          value={end}
          min={start}
          onChange={(e) => setParam("end", e.target.value)}
        />
      </div>
    </>
  );
}
