import { useCallback, useEffect, useRef, useState } from 'react';
import { getConsent } from '../api/ai';

/**
 * checking: a read is in flight (or none has finished yet)
 * granted / not_granted: the server answered 200 with granted true / false
 * lookup_failed: the read itself failed, so consent is unknown (not "revoked")
 */
export type ConsentLookup = 'checking' | 'granted' | 'not_granted' | 'lookup_failed';

/**
 * Read-only consent lookups for a set of screenshots. Each screenshot has a
 * monotonically increasing request sequence, so a response that is not the
 * newest request is dropped and cannot overwrite newer state. Re-reads on
 * `refreshToken` change, window focus and tab visibility. Never grants consent.
 */
export function useConsent(ids: number[], refreshToken: number = 0) {
  const [status, setStatus] = useState<Record<number, ConsentLookup>>({});
  const seqRef = useRef<Record<number, number>>({});
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  /** Re-read consent. Resolves with this request's own answer; state only takes it if still newest. */
  const refresh = useCallback(async (target: number[]): Promise<Record<number, ConsentLookup>> => {
    const mine: Record<number, number> = {};
    for (const id of target) {
      mine[id] = (seqRef.current[id] ?? 0) + 1;
      seqRef.current[id] = mine[id];
    }
    if (aliveRef.current) {
      // Show "checking" only on first load; a re-read keeps the last known answer until the new one arrives.
      setStatus((cur) => {
        const firstLoad = target.filter((id) => cur[id] === undefined);
        if (firstLoad.length === 0) return cur;
        return { ...cur, ...Object.fromEntries(firstLoad.map((id) => [id, 'checking' as const])) };
      });
    }
    const entries = await Promise.all(
      target.map(async (id) => {
        const res = await getConsent(id);
        const next: ConsentLookup = res.ok ? (res.data ? 'granted' : 'not_granted') : 'lookup_failed';
        if (aliveRef.current && seqRef.current[id] === mine[id]) {
          setStatus((cur) => ({ ...cur, [id]: next }));
        }
        return [id, next] as const;
      }),
    );
    return Object.fromEntries(entries);
  }, []);

  /** Set a known state (for example after a 403 or a grant) and invalidate reads in flight. */
  const set = useCallback((id: number, value: ConsentLookup) => {
    seqRef.current[id] = (seqRef.current[id] ?? 0) + 1;
    if (aliveRef.current) setStatus((cur) => ({ ...cur, [id]: value }));
  }, []);

  const key = ids.join(',');
  useEffect(() => {
    if (key === '') return;
    const list = key.split(',').map(Number);
    void refresh(list);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh(list);
    };
    window.addEventListener('focus', onVisible);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('focus', onVisible);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [key, refreshToken, refresh]);

  return { status, refresh, set };
}
