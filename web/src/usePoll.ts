import { useCallback, useEffect, useRef, useState } from "react";

interface PollState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/**
 * Poll an endpoint on an interval.
 *
 * A single admin watching one worker does not need a live stream; polling
 * removes reconnect, buffering and replay from the picture entirely.
 */
export function usePoll<T>(
  load: () => Promise<T>,
  intervalMs = 4000,
  deps: unknown[] = [],
): PollState<T> & { refresh: () => void } {
  const [state, setState] = useState<PollState<T>>({
    data: null,
    error: null,
    loading: true,
  });
  const mounted = useRef(true);
  // The caller states its own dependencies; `load` is recreated every render.
  const run = useCallback(load, deps);

  const refresh = useCallback(() => {
    run()
      .then((data) => {
        if (mounted.current) setState({ data, error: null, loading: false });
      })
      .catch((error: Error) => {
        if (mounted.current)
          setState((previous) => ({
            ...previous,
            error: error.message,
            loading: false,
          }));
      });
  }, [run]);

  useEffect(() => {
    mounted.current = true;
    refresh();
    const timer = setInterval(refresh, intervalMs);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, [refresh, intervalMs]);

  return { ...state, refresh };
}
