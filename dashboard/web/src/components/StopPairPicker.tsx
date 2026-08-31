"use client";

import type { OrderedStop } from "@/lib/useStopPair";
import type { StopPair } from "@/lib/useStopPair";

/**
 * From/to selectors for a route, plus a direction toggle and a swap button.
 * Stops are listed in travel order so the pair reads like a journey.
 */
export function StopPairPicker({
  stops,
  pair,
  directions,
}: {
  stops: OrderedStop[] | null;
  pair: StopPair;
  directions: number[];
}) {
  return (
    <>
      {directions.length > 1 && (
        <div className="field">
          <label htmlFor="direction-picker">Direction</label>
          <select
            id="direction-picker"
            value={String(pair.direction)}
            onChange={(e) => pair.set({ direction: Number(e.target.value) })}
          >
            {directions.map((d) => (
              <option key={d} value={d}>
                Direction {d}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="field">
        <label htmlFor="from-stop">From</label>
        <select
          id="from-stop"
          value={pair.from}
          onChange={(e) => pair.set({ from: e.target.value })}
          disabled={!stops}
        >
          <option value="">Select a stop…</option>
          {(stops ?? []).map((s) => (
            <option key={s.stop_id} value={s.stop_id}>
              {s.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="to-stop">To</label>
        <select
          id="to-stop"
          value={pair.to}
          onChange={(e) => pair.set({ to: e.target.value })}
          disabled={!stops}
        >
          <option value="">Select a stop…</option>
          {(stops ?? []).map((s) => (
            <option key={s.stop_id} value={s.stop_id}>
              {s.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="swap-stops">&nbsp;</label>
        <button
          id="swap-stops"
          type="button"
          className="button-secondary"
          onClick={pair.swap}
          disabled={!pair.from || !pair.to}
        >
          ⇄ Swap
        </button>
      </div>
    </>
  );
}
