import { useEffect, useState } from "react";

interface KeyedResult<T> {
  key: string;
  data: T;
}

interface KeyedError {
  key: string;
  message: string;
}

/**
 * Fetches `fetcher()` whenever `key` changes, keying the result so a
 * still-in-flight request for a stale key never clobbers newer state
 * (avoids the synchronous setState-in-effect anti-pattern of resetting
 * data to null before kicking off the fetch).
 */
export function useApiData<T>(key: string, fetcher: () => Promise<T>, enabled = true) {
  const [result, setResult] = useState<KeyedResult<T> | null>(null);
  const [error, setError] = useState<KeyedError | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    fetcher()
      .then((data) => {
        if (!cancelled) setResult({ key, data });
      })
      .catch((e) => {
        if (!cancelled) setError({ key, message: String(e) });
      });
    return () => {
      cancelled = true;
    };
    // fetcher is intentionally excluded: it's a fresh closure each render,
    // and `key` already encodes everything it depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled]);

  const data = result?.key === key ? result.data : null;
  const errorMessage = error?.key === key ? error.message : null;
  const loading = enabled && data === null && errorMessage === null;

  return { data, error: errorMessage, loading };
}
