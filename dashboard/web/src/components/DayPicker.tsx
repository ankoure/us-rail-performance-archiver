"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { defaultDay } from "@/lib/dates";

export function useDay(): string {
  const searchParams = useSearchParams();
  return searchParams.get("day") ?? defaultDay();
}

export function DayPicker() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const day = useDay();

  function setDay(value: string) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("day", value);
    router.push(`${pathname}?${params.toString()}`);
  }

  return (
    <div className="field">
      <label htmlFor="day-picker">Day</label>
      <input id="day-picker" type="date" value={day} onChange={(e) => setDay(e.target.value)} />
    </div>
  );
}
