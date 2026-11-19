import { useEffect, useMemo, useRef, useState } from 'react';
import { classifyScreenshot, getAnalysis, type Analysis } from '../api/ai';
import type { Screenshot } from '../api/client';
import { describeOperationOutcome, describeRequestFailure, STATUS_UNAVAILABLE_TEXT } from '../ai/messages';
import { pollOperation, type PollOutcome } from '../ai/pollOperation';
import {
  acquireRevive,
  markReviveActive,
  markReviveUnresolved,
  reconcileRevive,
  releaseRevive,
  REVIVE_IN_FLIGHT_TEXT,
  REVIVE_UNRESOLVED_TEXT,
} from '../ai/reviveCoordinator';
import { useConsent } from '../ai/useConsent';
import { useProviderStatus } from '../ai/PrivacyNotice';
import {
  deriveSuggestions,
  pickPrimary,
  type Suggestion,
} from './suggestions';

interface Props {
  /** Selected screenshots in user order. */
  selected: Screenshot[];
  /** Bump to re-read consent (for example after the detail dialog closes). */
  refreshToken: number;
  onOpen: (screenshotId: number) => void;
  onSetUpStudyNote: (ids: number[]) => void;
  onAnalysisChanged: (analysis: Analysis) => void;
  /** Reports the locked batch while a Revive batch runs (null when idle). */
  onBatchChange?: (batch: { ids: number[]; done: number } | null) => void;
  pollIntervalMs?: number;
  maxPolls?: number;
}

/** Per-operation state shown by the assistant. */
export type OpState = 'pending' | 'completed' | 'uncertain' | 'failed' | 'status_unavailable';

interface OpView {
  screenshotId: number;
  operationId: number;
  state: OpState;
  /** The bounded wait ended while the server still said pending. */
  stalled: boolean;
  text: string;
}

const STILL_WAITING_TEXT = 'Still waiting on the server. Check the card later.';

export function QuietAssistant({
  selected,
  refreshToken,
  onOpen,
  onSetUpStudyNote,
  onAnalysisChanged,
  onBatchChange,
  pollIntervalMs = 1000,
  maxPolls = 60,
}: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const [ops, setOps] = useState<Record<number, OpView>>({});
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set()); // session only
  const [skip, setSkip] = useState(0);
  const [processing, setProcessing] = useState(false);
  const [result, setResult] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const { mode: provider, loading: providerLoading } = useProviderStatus();
  // Review step: Prepare (suggestion), Review (this panel), Approve (Confirm). Nothing is sent before Confirm.
  const [review, setReview] = useState<number[] | null>(null);
  const batchRef = useRef(onBatchChange);
  useEffect(() => {
    batchRef.current = onBatchChange;
  }, [onBatchChange]);
  const aliveRef = useRef(true);
  const busyRef = useRef(false);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const selectedKey = selected.map((s) => s.screenshotId).join(',');
  const selectedIds = useMemo(() => (selectedKey === '' ? [] : selectedKey.split(',').map(Number)), [selectedKey]);
  // Read-only consent lookups with per-screenshot request sequencing. Never grants consent.
  const { status: consent, refresh: refreshConsent, set: setConsentStatus } = useConsent(selectedIds, refreshToken);

  // A changed selection starts a fresh suggestion; "something else" resets.
  useEffect(() => {
    setSkip(0);
    setResult(null);
    setReview(null);
  }, [selectedKey]);

  const all = useMemo(
    () => deriveSuggestions({ selected, consent, processing }),
    [selected, consent, processing],
  );
  const primary = pickPrimary(all, dismissed, skip);
  const isClassify = primary?.kind === 'classify';
  const providerHint =
    provider === 'fake'
      ? 'Demo mode: nothing is sent to an external AI service.'
      : provider === 'openai'
        ? 'Image content of the selected screenshots may be sent to an external AI provider.'
        : providerLoading
          ? 'Checking the AI provider. Revive is unavailable until this finishes.'
          : 'Provider status could not be confirmed, so Revive is unavailable here. Open a card to continue.';
  const acceptBlocked = isClassify && provider === 'unknown';
  const labelText =
    primary?.source !== 'ai'
      ? 'Suggested by app rules'
      : provider === 'fake'
        ? 'Simulated demo suggestion'
        : 'AI suggestion';
  const hasMore = all.filter((s) => !dismissed.has(s.key)).length > skip + 1;

  const showOp = (screenshotId: number, view: Omit<OpView, 'screenshotId'>) => {
    if (aliveRef.current) setOps((cur) => ({ ...cur, [screenshotId]: { screenshotId, ...view } }));
  };

  /** Turn a poll outcome into the visible state. Returns the state plus whether the run may continue. */
  const applyOutcome = async (id: number, operationId: number, outcome: PollOutcome): Promise<'ok' | string> => {
    if (outcome.kind === 'cancelled') return 'cancelled';
    if (outcome.kind === 'done') {
      releaseRevive(id);
      if (outcome.status === 'successful') {
        const analysis = await getAnalysis(id);
        if (!aliveRef.current) return 'cancelled';
        if (!analysis.ok) {
          showOp(id, { operationId, state: 'completed', stalled: false, text: 'The analysis finished but could not be loaded.' });
          return 'The analysis finished but could not be loaded.';
        }
        onAnalysisChanged(analysis.data);
        showOp(id, { operationId, state: 'completed', stalled: false, text: `Screenshot #${id} revived.` });
        return 'ok';
      }
      const text = describeOperationOutcome(outcome.status, outcome.errorDetail);
      showOp(id, { operationId, state: outcome.status, stalled: false, text });
      return text;
    }
    if (outcome.kind === 'not_found') {
      releaseRevive(id);
      const text = 'The server has no record of this operation, so nothing was saved.';
      showOp(id, { operationId, state: 'failed', stalled: false, text });
      return text;
    }
    // timeout or unavailable: keep the operation id so it can be reconciled.
    markReviveUnresolved(id);
    if (outcome.kind === 'unavailable') {
      showOp(id, { operationId, state: 'status_unavailable', stalled: true, text: STATUS_UNAVAILABLE_TEXT });
      return STATUS_UNAVAILABLE_TEXT;
    }
    showOp(id, { operationId, state: 'pending', stalled: true, text: STILL_WAITING_TEXT });
    return STILL_WAITING_TEXT;
  };

  const classifyOne = async (id: number): Promise<'ok' | string> => {
    const lock = await acquireRevive(id);
    if (!lock.ok) return lock.reason === 'in_flight' ? REVIVE_IN_FLIGHT_TEXT : REVIVE_UNRESOLVED_TEXT;
    const accepted = await classifyScreenshot(id);
    if (!accepted.ok) {
      releaseRevive(id);
      // The server says consent is missing: drop any stale "granted".
      if (accepted.status === 403) setConsentStatus(id, 'not_granted');
      return describeRequestFailure(accepted);
    }
    const operationId = accepted.data.operationId;
    markReviveActive(id, operationId);
    showOp(id, { operationId, state: 'pending', stalled: false, text: `Working on screenshot #${id}\u2026` });
    const outcome = await pollOperation(operationId, {
      intervalMs: pollIntervalMs,
      maxPolls,
      isCancelled: () => !aliveRef.current,
    });
    if (outcome.kind === 'cancelled') markReviveUnresolved(id);
    return applyOutcome(id, operationId, outcome);
  };

  /** Secondary action: one reconciliation check for a stalled operation. */
  const checkAgain = async (view: OpView) => {
    const r = await reconcileRevive(view.screenshotId);
    if (!aliveRef.current) return;
    if (r.kind === 'terminal') {
      await applyOutcome(view.screenshotId, view.operationId, r.outcome);
    } else if (r.kind === 'gone') {
      await applyOutcome(view.screenshotId, view.operationId, { kind: 'not_found' });
    } else if (r.kind === 'pending') {
      showOp(view.screenshotId, { operationId: view.operationId, state: 'pending', stalled: true, text: STILL_WAITING_TEXT });
    } else {
      showOp(view.screenshotId, { operationId: view.operationId, state: 'status_unavailable', stalled: true, text: STATUS_UNAVAILABLE_TEXT });
    }
  };

  const onAccept = async (s: Suggestion) => {
    if (s.kind === 'consent_needed' || s.kind === 'review_uncertain') {
      onOpen(s.targetIds[0]);
      return;
    }
    if (s.kind === 'study_note') {
      onSetUpStudyNote(s.targetIds);
      return;
    }
    if (s.kind !== 'classify' || busyRef.current || provider === 'unknown') return;
    setResult(null);
    setReview(s.targetIds); // Review first: nothing is submitted yet.
  };

  const onConfirm = async (ids: number[]) => {
    if (busyRef.current || provider === 'unknown') return;
    busyRef.current = true;
    setReview(null);
    setProcessing(true);
    setResult(null);
    let done = 0;
    let failure: string | null = null;
    batchRef.current?.({ ids, done });
    for (const id of ids) {
      // Consent may have been withdrawn elsewhere: confirm it right before each image.
      const fresh = await refreshConsent([id]);
      if (!aliveRef.current) return;
      if (fresh[id] !== 'granted') {
        failure =
          fresh[id] === 'lookup_failed'
            ? 'Could not confirm consent for the next screenshot, so nothing more was analyzed.'
            : 'Consent is no longer on record for the next screenshot, so nothing more was analyzed.';
        break;
      }
      const r = await classifyOne(id);
      if (!aliveRef.current) return;
      if (r === 'ok') {
        done += 1;
        batchRef.current?.({ ids, done });
      } else {
        failure = r;
        break;
      }
    }
    busyRef.current = false;
    batchRef.current?.(null);
    setProcessing(false);
    setResult(
      failure
        ? { kind: 'error', text: `${done} revived. ${failure}` }
        : { kind: 'info', text: `${done} screenshot${done === 1 ? '' : 's'} revived. See the cards for the results.` },
    );
  };

  const dismiss = (s: Suggestion) => {
    setDismissed((cur) => new Set(cur).add(s.key));
    setSkip(0);
  };

  return (
    <aside className="qa" aria-label="Quiet Assistant" data-collapsed={collapsed}>
      <div className="qa__bar">
        <strong className="qa__title">Quiet Assistant</strong>
        <button
          type="button"
          className="gy-btn qa__toggle"
          aria-expanded={!collapsed}
          aria-controls="qa-body"
          onClick={() => setCollapsed((c) => !c)}
        >
          {collapsed ? 'Show' : 'Hide'}
        </button>
      </div>
      <div id="qa-body" hidden={collapsed} className="qa__body">
        <div role="status" aria-live="polite" className="qa__live">
          {result && (
            <p className={`qa__result qa__result--${result.kind}`}>{result.text}</p>
          )}
          {primary ? (
            <>
              <p className="qa__label" data-testid="qa-label" data-source={primary.source}>
                {labelText}
              </p>
              <p className="qa__text" data-testid="qa-text">
                {primary.text}
              </p>
              {isClassify && (
                <p className="ai-hint" data-testid="qa-provider-hint">
                  {providerHint}
                </p>
              )}
            </>
          ) : (
            <p className="qa__text" data-testid="qa-text">
              No more suggestions. You can use the actions on each card.
            </p>
          )}
        </div>
        <ul className="qa__ops" aria-label="Revive progress">
          {selectedIds
            .map((id) => ops[id])
            .filter((o): o is OpView => o !== undefined)
            .map((o) => (
              <li key={o.screenshotId} data-testid="qa-op" data-state={o.state}>
                <span>{o.text}</span>
                {o.stalled && !processing && (
                  <button type="button" className="gy-btn" onClick={() => void checkAgain(o)}>
                    Check again
                  </button>
                )}
              </li>
            ))}
        </ul>
        {review && !processing && (
          <section className="qa__review" aria-label="Review before Revive" data-testid="qa-review">
            <p className="qa__text">
              <strong>Review: Revive {review.length} screenshot{review.length === 1 ? '' : 's'}</strong>
            </p>
            <ul className="qa__review-list">
              {review.map((id) => {
                const sc = selected.find((x) => x.screenshotId === id);
                return (
                  <li key={id}>
                    {sc?.originalFilename || `Screenshot #${id}`}{' '}
                    <span className="ai-hint">
                      {consent[id] === 'granted' ? 'consent on record' : 'consent not confirmed'}
                    </span>
                  </li>
                );
              })}
            </ul>
            <p className="ai-hint">{providerHint}</p>
            <p className="ai-hint">Consent is checked again before each screenshot. Nothing is sent until you confirm.</p>
            <div className="qa__actions">
              <button
                type="button"
                className="gy-btn gy-btn--revive"
                disabled={provider === 'unknown'}
                onClick={() => void onConfirm(review)}
              >
                Confirm and Revive {review.length}
              </button>
              <button type="button" className="gy-btn" onClick={() => setReview(null)}>
                Cancel
              </button>
            </div>
          </section>
        )}
        {!review && primary && primary.kind !== 'idle' && primary.kind !== 'processing' && (
          <div className="qa__actions">
            {primary.acceptLabel && (
              <button type="button" className="gy-btn gy-btn--revive" disabled={acceptBlocked} onClick={() => onAccept(primary)}>
                {primary.acceptLabel}
              </button>
            )}
            <button type="button" className="gy-btn" onClick={() => dismiss(primary)}>
              Dismiss
            </button>
            {hasMore && (
              <button type="button" className="gy-btn" onClick={() => setSkip((n) => n + 1)}>
                Something else
              </button>
            )}
          </div>
        )}
        {!primary && skip > 0 && (
          <div className="qa__actions">
            <button type="button" className="gy-btn" onClick={() => setSkip(0)}>
              Back to first suggestion
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
