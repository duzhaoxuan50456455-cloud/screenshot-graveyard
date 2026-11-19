import { getOperation, type OperationStatus } from '../api/ai';

/** Stop after this many poll requests in a row failed (network, 5xx, ...). */
export const MAX_CONSECUTIVE_POLL_FAILURES = 3;

export type PollOutcome =
  | {
      kind: 'done';
      status: Exclude<OperationStatus, 'pending'>;
      errorDetail: string | null;
      artifactId: number | null;
    }
  /** The bound was reached while the server still said pending. */
  | { kind: 'timeout' }
  /** Poll requests kept failing, so the real status is unknown. */
  | { kind: 'unavailable' }
  /** 404: the server has no such operation. Final. */
  | { kind: 'not_found' }
  | { kind: 'cancelled' };

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Bounded polling of `GET /api/operations/{id}`. Poll request failures are
 * counted and reported as `unavailable`; they are never treated as "pending".
 */
export async function pollOperation(
  operationId: number,
  opts: { intervalMs: number; maxPolls: number; isCancelled?: () => boolean },
): Promise<PollOutcome> {
  let failures = 0;
  for (let i = 0; i < opts.maxPolls; i += 1) {
    const res = await getOperation(operationId);
    if (opts.isCancelled?.()) return { kind: 'cancelled' };
    if (res.ok) {
      failures = 0;
      if (res.data.status !== 'pending') {
        return {
          kind: 'done',
          status: res.data.status,
          errorDetail: res.data.errorDetail,
          artifactId: res.data.artifactId,
        };
      }
    } else if (res.status === 404) {
      return { kind: 'not_found' };
    } else {
      failures += 1;
      if (failures >= MAX_CONSECUTIVE_POLL_FAILURES) return { kind: 'unavailable' };
    }
    if (i < opts.maxPolls - 1) await sleep(opts.intervalMs);
  }
  return failures > 0 ? { kind: 'unavailable' } : { kind: 'timeout' };
}
