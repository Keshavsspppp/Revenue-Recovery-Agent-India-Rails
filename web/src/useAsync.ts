import { useEffect, useState } from "react";

export interface Async<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
}

/** One fetch, its own state.
 *
 * Each panel owns its request rather than waiting on a shared one. The single-file page
 * this replaces resolved the scoreboard first and only then rendered anything, so a
 * seven-second bootstrap left every section sitting on the word "loading" — including the
 * five that had nothing to do with it. Here the trail, the account picker and the live
 * Razorpay panel all paint as soon as their own call returns.
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): Async<T> {
  const [state, setState] = useState<Async<T>>({ data: null, error: null, loading: true });

  useEffect(() => {
    let live = true;
    setState((s) => ({ data: s.data, error: null, loading: true }));
    fn().then(
      (data) => live && setState({ data, error: null, loading: false }),
      (error) => live && setState({ data: null, error, loading: false }),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
