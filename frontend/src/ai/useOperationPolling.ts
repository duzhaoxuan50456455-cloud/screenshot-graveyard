import { useCallback, useEffect, useRef, useState } from 'react';
import { getOperation, type OperationStatus } from '../api/ai';

export const DEFAULT_POLL_INTERVAL_MS = 1000;
export const DEFAULT_MAX_POLLS = 60;

export type PollState =
  | { phase: 'idle' }
  | { phase: 'polling'; operationId: number; attempts: number }
  | {
      phase: 'done';
      operationId: number;
      status: Exclude<OperationStatus, 'pending'>;
      errorDetail: string | null;
      artifactId: number | null;
    }
  | { phase: 'timeout'; operationId: number }
  | { phase: 'error'; operationId: number; message: string };

/**
 * Bounded polling of the existing `GET /api/operations/{id}` endpoint.
 *
 * - at most `maxPolls` requests, `intervalMs` apart, one request in flight;
 * - stops on the first terminal status (successful / failed / uncertain);
 * - stops (and reports `timeout`) at the bound instead of polling forever;
 * - every timer/request is cancelled on unmount or when a new operation starts.
 */
export function useOperationPolling(
  intervalMs: number = DEFAULT_POLL_INTERVAL_MS,
  maxPolls: number = DEFAULT_MAX_POLLS,
) {
  const [state, setState] = useState<PollState>({ phase: 'idle' });
  const [operationId, setOperationId] = useState<number | null>(null);
  const cancelRef = useRef<() => void>(() => {});

  useEffect(() => {
    if (operationId === null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;

    const tick = async () => {
      attempts += 1;
      const res = await getOperation(operationId);
      if (cancelled) return;
      if (!res.ok) {
        // Transient failures keep polling inside the bound; a 404 is final.
        if (res.status === 404) {
          setState({ phase: 'error', operationId, message: 'Operation not found' });
          return;
        }
      } else if (res.data.status !== 'pending') {
        setState({
          phase: 'done',
          operationId,
          status: res.data.status,
          errorDetail: res.data.errorDetail,
          artifactId: res.data.artifactId,
        });
        return;
      }
      if (attempts >= maxPolls) {
        setState({ phase: 'timeout', operationId });
        return;
      }
      setState({ phase: 'polling', operationId, attempts });
      timer = setTimeout(tick, intervalMs);
    };

    setState({ phase: 'polling', operationId, attempts: 0 });
    timer = setTimeout(tick, 0);
    cancelRef.current = () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [operationId, intervalMs, maxPolls]);

  const start = useCallback((id: number) => setOperationId(id), []);
  const reset = useCallback(() => {
    cancelRef.current();
    setOperationId(null);
    setState({ phase: 'idle' });
  }, []);

  return { state, start, reset };
}
