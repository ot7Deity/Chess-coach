"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { isAbort } from "./api-errors";

interface Settled<T> {
  key: string;
  data?: T;
  error?: unknown;
}

export interface AsyncState<T> {
  /** The latest data, kept while a newer request is in flight so views can dim instead of blank. */
  data: T | undefined;
  /** Set only if the latest request failed. */
  error: unknown;
  /** True while the request for the current key has not settled. */
  loading: boolean;
  reload: () => void;
}

/**
 * Runs `load` whenever `key` changes, aborting the previous request so a slow
 * response for an old key can never overwrite a newer one.
 */
export function useAsync<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>): AsyncState<T> {
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });

  const [nonce, setNonce] = useState(0);
  const [settled, setSettled] = useState<Settled<T>>({ key: "" });
  const fullKey = key === null ? null : `${key}#${nonce}`;

  useEffect(() => {
    if (fullKey === null) return;
    const ac = new AbortController();
    loadRef.current(ac.signal).then(
      (data) => setSettled({ key: fullKey, data }),
      (error: unknown) => {
        if (!isAbort(error)) setSettled((prev) => ({ key: fullKey, data: prev.data, error }));
      },
    );
    return () => ac.abort();
  }, [fullKey]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const current = settled.key === fullKey;
  return {
    data: settled.data,
    error: current ? settled.error : undefined,
    loading: fullKey !== null && !current,
    reload,
  };
}
