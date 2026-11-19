import { getOperation } from '../api/ai';
import type { PollOutcome } from './pollOperation';

/**
 * Module-level registry of Revive (classify) work per screenshot, shared by the
 * Quiet Assistant and the Screenshot Detail panel so the same screenshot is never
 * submitted twice while an earlier submission is unresolved. Different screenshot
 * ids are independent. The registry only guards the browser; the backend still
 * enforces consent, quota and operation checks.
 *
 * Phases:
 * - submitting: classify request sent, no operation id yet
 * - active: accepted and being polled by its owner
 * - unresolved: the owner stopped (timeout, status unavailable, closed). The
 *   operation id is kept and must be reconciled before another submission.
 * - reconciling: a getOperation check for an unresolved entry is in flight
 */
type Phase = 'submitting' | 'active' | 'unresolved' | 'reconciling';

interface Entry {
  phase: Phase;
  operationId: number | null;
}

const entries = new Map<number, Entry>();

export type AcquireResult =
  | { ok: true }
  | { ok: false; reason: 'in_flight' | 'unresolved'; operationId: number | null };

export type ReconcileResult =
  | { kind: 'terminal'; outcome: Extract<PollOutcome, { kind: 'done' }> }
  | { kind: 'gone' }
  | { kind: 'pending' }
  | { kind: 'unavailable' };

/** Read the registry (for tests and UI hints). */
export function getReviveEntry(screenshotId: number): Readonly<Entry> | undefined {
  return entries.get(screenshotId);
}

/** Test helper: forget everything. */
export function resetReviveCoordinator(): void {
  entries.clear();
}

/** One getOperation check for a kept operation id. Releases the entry when it is final. */
export async function reconcileRevive(screenshotId: number): Promise<ReconcileResult> {
  const entry = entries.get(screenshotId);
  if (!entry || entry.operationId === null) return { kind: 'unavailable' };
  const opId = entry.operationId;
  const res = await getOperation(opId);
  const cur = entries.get(screenshotId);
  const mine = cur !== undefined && cur.operationId === opId;
  if (res.ok && res.data.status !== 'pending') {
    if (mine) entries.delete(screenshotId);
    return {
      kind: 'terminal',
      outcome: {
        kind: 'done',
        status: res.data.status,
        errorDetail: res.data.errorDetail,
        artifactId: res.data.artifactId,
      },
    };
  }
  if (!res.ok && res.status === 404) {
    if (mine) entries.delete(screenshotId);
    return { kind: 'gone' };
  }
  if (mine) cur.phase = 'unresolved';
  return res.ok ? { kind: 'pending' } : { kind: 'unavailable' };
}

/**
 * Take the right to submit a classification for this screenshot. If an earlier
 * operation is unresolved it is reconciled first; submission is allowed only when
 * that operation is final.
 */
export async function acquireRevive(screenshotId: number): Promise<AcquireResult> {
  const entry = entries.get(screenshotId);
  if (!entry) {
    entries.set(screenshotId, { phase: 'submitting', operationId: null });
    return { ok: true };
  }
  if (entry.phase !== 'unresolved') {
    return { ok: false, reason: 'in_flight', operationId: entry.operationId };
  }
  const opId = entry.operationId;
  entry.phase = 'reconciling';
  const r = await reconcileRevive(screenshotId);
  if (r.kind === 'terminal' || r.kind === 'gone') {
    entries.set(screenshotId, { phase: 'submitting', operationId: null });
    return { ok: true };
  }
  return { ok: false, reason: 'unresolved', operationId: opId };
}

/** The server accepted the submission. */
export function markReviveActive(screenshotId: number, operationId: number): void {
  entries.set(screenshotId, { phase: 'active', operationId });
}

/** The owner stopped watching but the operation may still run. Keeps the id. */
export function markReviveUnresolved(screenshotId: number): void {
  const e = entries.get(screenshotId);
  if (e && e.operationId !== null) e.phase = 'unresolved';
  else if (e) entries.delete(screenshotId);
}

/** Terminal state reached, or the submission was rejected. */
export function releaseRevive(screenshotId: number): void {
  entries.delete(screenshotId);
}

export const REVIVE_IN_FLIGHT_TEXT =
  'This screenshot is already being revived elsewhere. Wait for that to finish before starting another.';
export const REVIVE_UNRESOLVED_TEXT =
  'An earlier Revive for this screenshot has not been confirmed as finished yet. Nothing new was started. Try again shortly.';
