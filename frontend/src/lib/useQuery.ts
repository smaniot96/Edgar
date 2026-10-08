import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

export interface QueryResult<T> {
  /** Latest data for the current key (kept while refetching, cleared when the key changes). */
  data: T | undefined;
  /** Error from the latest attempt, or null. */
  error: unknown;
  /** True while a request for the current key/refetch is in flight. */
  loading: boolean;
  /** True until the first response (data or error) for the current key arrives. */
  pending: boolean;
  /** Re-run the fetcher (e.g. after a mutation). Keeps showing current data meanwhile. */
  refetch: () => void;
  /** Locally replace/patch the cached data (optimistic updates). */
  setData: (update: (prev: T | undefined) => T | undefined) => void;
}

interface Settled<T> {
  key: string;
  tag: string;
  data: T | undefined;
  error: unknown;
}

/**
 * Tiny `useQuery`: fetches whenever `key` changes (pass `null` to skip), aborts on unmount or
 * key change, and exposes loading/error/data plus `refetch`.
 *
 * The fetcher is read from a ref, so it may close over fresh props without being memoised;
 * anything that should trigger a refetch must be encoded in `key`.
 */
export function useQuery<T>(
  key: string | null,
  fetcher: (signal: AbortSignal) => Promise<T>,
): QueryResult<T> {
  const fetcherRef = useRef(fetcher);
  useLayoutEffect(() => {
    fetcherRef.current = fetcher;
  });

  const [nonce, setNonce] = useState(0);
  const [state, setState] = useState<Settled<T>>({
    key: "",
    tag: "",
    data: undefined,
    error: null,
  });

  const tag = key == null ? null : `${key}\u0000${nonce}`;

  useEffect(() => {
    if (key == null || tag == null) return;
    const ctrl = new AbortController();
    fetcherRef.current(ctrl.signal).then(
      (data) => {
        if (!ctrl.signal.aborted) setState({ key, tag, data, error: null });
      },
      (error: unknown) => {
        if (ctrl.signal.aborted) return;
        setState((prev) => ({
          key,
          tag,
          data: prev.key === key ? prev.data : undefined,
          error,
        }));
      },
    );
    return () => ctrl.abort();
  }, [key, tag]);

  const refetch = useCallback(() => setNonce((n) => n + 1), []);
  const setData = useCallback(
    (update: (prev: T | undefined) => T | undefined) =>
      setState((prev) => ({ ...prev, data: update(prev.data) })),
    [],
  );

  const sameKey = key != null && state.key === key;
  return {
    data: sameKey ? state.data : undefined,
    error: sameKey ? state.error : null,
    loading: key != null && state.tag !== tag,
    pending: key != null && !sameKey,
    refetch,
    setData,
  };
}
