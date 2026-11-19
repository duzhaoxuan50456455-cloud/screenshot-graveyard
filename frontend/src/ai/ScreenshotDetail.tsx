import { useCallback, useEffect, useRef, useState } from 'react';
import {
  CATEGORIES,
  classifyScreenshot,
  editAnalysis,
  getAnalysis,
  setConsent,
  type Analysis,
  type Category,
} from '../api/ai';
import { imageUrl, type Screenshot } from '../api/client';
import {
  CRYPT_GALLERY_NOTE,
  describeOperationOutcome,
  describeRequestFailure,
  STATUS_UNAVAILABLE_TEXT,
} from './messages';
import { pollOperation } from './pollOperation';
import { PrivacyNotice, useProviderStatus } from './PrivacyNotice';
import { StatusBadge } from './StatusBadge';
import {
  acquireRevive,
  markReviveActive,
  markReviveUnresolved,
  releaseRevive,
  REVIVE_IN_FLIGHT_TEXT,
  REVIVE_UNRESOLVED_TEXT,
} from './reviveCoordinator';
import { useConsent } from './useConsent';
import { DEFAULT_MAX_POLLS, DEFAULT_POLL_INTERVAL_MS, type PollState } from './useOperationPolling';

/** Detail's poll state: the shared phases plus "status unavailable" (poll requests failed). */
type DetailPoll = PollState | { phase: 'unavailable'; operationId: number };

type AnalysisState =
  | { phase: 'loading' }
  | { phase: 'none' }
  | { phase: 'ready'; analysis: Analysis }
  | { phase: 'error' };

interface Props {
  screenshot: Screenshot;
  onClose: () => void;
  /**
   * Called with the server's CURRENT analysis whenever it changes while this panel
   * is open (after a successful Revive or a saved edit), so the Ghost / Revived /
   * Crypt views can reflect the real state without a reload.
   */
  onAnalysisChanged?: (analysis: Analysis) => void;
  /** Secondary lifecycle actions, owned by the parent. */
  onArchive?: (screenshotId: number) => void | Promise<void>;
  onRestore?: (screenshotId: number) => void | Promise<void>;
  lifecycleBusy?: boolean;
  pollIntervalMs?: number;
  maxPolls?: number;
}


/**
 * Open-screenshot panel: privacy warning, explicit consent, deliberate Revive,
 * operation status, the persisted result and editing. Opening this panel only
 * READS consent/analysis; nothing is processed until the user deliberately
 * presses Revive with consent granted. Provider mode determines whether that
 * later processing is simulated or external.
 */
export function ScreenshotDetail({
  screenshot,
  onClose,
  onAnalysisChanged,
  onArchive,
  onRestore,
  lifecycleBusy = false,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  maxPolls = DEFAULT_MAX_POLLS,
}: Props) {
  const id = screenshot.screenshotId;
  const label = screenshot.originalFilename || `Screenshot #${id}`;
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const { status: consentMap, set: setConsentLookup } = useConsent([id]);
  const lookup = consentMap[id] ?? 'checking';
  // Keep the labels used by the markup below.
  const consent =
    lookup === 'checking' ? 'loading' : lookup === 'lookup_failed' ? 'error' : lookup;
  const [analysis, setAnalysis] = useState<AnalysisState>({ phase: 'loading' });
  const [busy, setBusy] = useState(false); // request in flight (duplicate-click guard)
  const busyRef = useRef(false);
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const [poll, setPoll] = useState<DetailPoll>({ phase: 'idle' });
  const aliveRef = useRef(true);
  const runRef = useRef(0); // identifies the newest poll run; older runs stop quietly
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);
  // Keep the latest callback in a ref so an unstable parent function can never
  // re-trigger the effects below (which would refetch in a loop).
  const changedRef = useRef(onAnalysisChanged);
  useEffect(() => {
    changedRef.current = onAnalysisChanged;
  }, [onAnalysisChanged]);

  const [draftCategory, setDraftCategory] = useState<Category>(CATEGORIES[0]);
  const [draftEpitaph, setDraftEpitaph] = useState('');
  const { mode: providerMode, loading: providerLoading } = useProviderStatus();
  const providerWord = providerLoading ? 'checking the AI provider\u2026' : 'provider status could not be confirmed';

  // Escape dismisses the drawer. Ignore IME composition, handled keys and open dropdowns.
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing || e.defaultPrevented) return;
      // Do not discard an unsaved category/Epitaph draft or interrupt a request in flight.
      if (guardEscapeRef.current) return;
      closeRef.current();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  const guardEscapeRef = useRef(false);

  const loadAnalysis = useCallback(async () => {
    const res = await getAnalysis(id);
    if (res.ok) {
      setAnalysis({ phase: 'ready', analysis: res.data });
      setDraftCategory(res.data.classification.category);
      setDraftEpitaph(res.data.epitaph.text);
      changedRef.current?.(res.data);
    } else if (res.status === 404) {
      setAnalysis({ phase: 'none' });
    } else {
      setAnalysis({ phase: 'error' });
    }
  }, [id]);

  // Read-only on open: any existing result (consent is read by useConsent). No AI call happens here.
  useEffect(() => {
    let active = true;
    headingRef.current?.focus();
    (async () => {
      const a = await getAnalysis(id);
      if (!active) return;
      if (a.ok) {
        setAnalysis({ phase: 'ready', analysis: a.data });
        setDraftCategory(a.data.classification.category);
        setDraftEpitaph(a.data.epitaph.text);
      } else {
        setAnalysis(a.status === 404 ? { phase: 'none' } : { phase: 'error' });
      }
    })();
    return () => {
      active = false;
    };
  }, [id]);

  // When polling reaches a terminal state, fetch the persisted result.
  useEffect(() => {
    if (poll.phase === 'done' && poll.status === 'successful') {
      void loadAnalysis();
    }
  }, [poll, loadAnalysis]);

  // Release the shared Revive registry once polling is final. A timeout keeps the
  // operation id so the next submission reconciles first.
  useEffect(() => {
    if (poll.phase === 'done' || poll.phase === 'error') releaseRevive(id);
    else if (poll.phase === 'timeout' || poll.phase === 'unavailable') markReviveUnresolved(id);
  }, [poll, id]);
  // Closing the panel mid-poll leaves the operation unresolved, not forgotten.
  const pollPhaseRef = useRef(poll.phase);
  pollPhaseRef.current = poll.phase;
  useEffect(
    () => () => {
      if (pollPhaseRef.current === 'polling') markReviveUnresolved(id);
    },
    [id],
  );

  /** Bounded poll that reports unreachable status as `unavailable`, never as pending or timeout. */
  const runPoll = async (operationId: number) => {
    const run = (runRef.current += 1);
    setPoll({ phase: 'polling', operationId, attempts: 0 });
    const outcome = await pollOperation(operationId, {
      intervalMs: pollIntervalMs,
      maxPolls,
      isCancelled: () => !aliveRef.current || runRef.current !== run,
    });
    if (outcome.kind === 'cancelled') {
      if (!aliveRef.current) markReviveUnresolved(id); // closed mid-poll: keep the id, do not forget it
      return;
    }
    if (outcome.kind === 'done') {
      setPoll({ phase: 'done', operationId, status: outcome.status, errorDetail: outcome.errorDetail, artifactId: outcome.artifactId });
    } else if (outcome.kind === 'not_found') {
      setPoll({ phase: 'error', operationId, message: 'Operation not found' });
    } else if (outcome.kind === 'unavailable') {
      setPoll({ phase: 'unavailable', operationId });
    } else {
      setPoll({ phase: 'timeout', operationId });
    }
  };

  const guarded = async (fn: () => Promise<void>) => {
    if (busyRef.current) return; // duplicate-click prevention
    busyRef.current = true;
    setBusy(true);
    try {
      await fn();
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const onGrant = () =>
    guarded(async () => {
      setMessage(null);
      const res = await setConsent(id, true);
      if (res.ok) {
        setConsentLookup(id, 'granted');
        setMessage({ kind: 'info', text: 'AI consent granted for this screenshot.' });
      } else {
        setMessage({ kind: 'error', text: describeRequestFailure(res) });
      }
    });

  const onRevoke = () =>
    guarded(async () => {
      setMessage(null);
      const res = await setConsent(id, false);
      if (res.ok) {
        setConsentLookup(id, 'not_granted');
        setMessage({
          kind: 'info',
          text: 'AI consent withdrawn. Future AI use is blocked. Consent cannot recall any content that may already have been processed.',
        });
      } else {
        setMessage({ kind: 'error', text: describeRequestFailure(res) });
      }
    });

  const onRevive = () =>
    guarded(async () => {
      setMessage(null);
      runRef.current += 1; // abandon any earlier poll run
      setPoll({ phase: 'idle' });
      const lock = await acquireRevive(id);
      if (!lock.ok) {
        setMessage({
          kind: 'error',
          text: lock.reason === 'in_flight' ? REVIVE_IN_FLIGHT_TEXT : REVIVE_UNRESOLVED_TEXT,
        });
        return;
      }
      const res = await classifyScreenshot(id);
      if (!res.ok) {
        releaseRevive(id);
        // The server says consent is missing: clear any stale "granted".
        if (res.status === 403) setConsentLookup(id, 'not_granted');
        setMessage({ kind: 'error', text: describeRequestFailure(res) });
        return;
      }
      markReviveActive(id, res.data.operationId);
      if (!aliveRef.current) {
        // The panel was closed while classify was in flight: nobody will poll, keep the id for reconciliation.
        markReviveUnresolved(id);
        return;
      }
      void runPoll(res.data.operationId);
    });

  const onSaveEdit = () =>
    guarded(async () => {
      if (analysis.phase !== 'ready') return;
      const current = analysis.analysis;
      const edit: { category?: Category; epitaphText?: string } = {};
      if (draftCategory !== current.classification.category) edit.category = draftCategory;
      if (draftEpitaph.trim() !== current.epitaph.text) edit.epitaphText = draftEpitaph.trim();
      if (edit.category === undefined && edit.epitaphText === undefined) {
        setMessage({ kind: 'info', text: 'Nothing changed.' });
        return;
      }
      const res = await editAnalysis(id, edit);
      if (res.ok) {
        setAnalysis({ phase: 'ready', analysis: res.data });
        changedRef.current?.(res.data);
        setMessage({ kind: 'info', text: 'Saved as a new version. Earlier versions are kept.' });
      } else {
        setMessage({ kind: 'error', text: describeRequestFailure(res) });
      }
    });

  const polling = poll.phase === 'polling';
  const draftDirty =
    analysis.phase === 'ready' &&
    (draftCategory !== analysis.analysis.classification.category ||
      draftEpitaph.trim() !== analysis.analysis.epitaph.text);
  guardEscapeRef.current = draftDirty || busy;
  const canRevive =
    providerMode !== 'unknown' &&
    consent === 'granted' &&
    !busy &&
    !polling &&
    analysis.phase === 'none';
  const authorizeLabel =
    providerMode === 'fake'
      ? 'I understand and agree: authorize simulated demo analysis'
      : providerMode === 'openai'
        ? 'I understand and agree: allow AI to read this screenshot'
        : providerLoading
          ? 'Checking the AI provider\u2026'
          : 'Provider status could not be confirmed';
  const reviveLabel =
    providerMode === 'fake'
      ? 'Revive: analyze with AI (simulated demo)'
      : providerMode === 'openai'
        ? 'Revive: analyze with AI'
        : `Revive: analyze with AI (unavailable: ${providerWord})`;

  return (
    <section className="ai-detail" role="dialog" aria-modal="false" aria-labelledby="ai-detail-h">
      <div className="ai-detail__head">
        <h3 id="ai-detail-h" tabIndex={-1} ref={headingRef}>
          {label}
        </h3>
        <button type="button" className="gy-btn" onClick={onClose}>
          Close
        </button>
      </div>

      <img className="ai-detail__img" src={imageUrl(id)} alt={`Screenshot ${label}`} />

      <PrivacyNotice scope="one" mode={providerMode} loading={providerLoading} />

      <div className="ai-block" aria-labelledby="ai-consent-h">
        <h4 id="ai-consent-h">AI consent</h4>
        <p role="status" aria-live="polite" data-testid="consent-state">
          {consent === 'loading' && 'Checking consent\u2026'}
          {consent === 'granted' && '\u2714 AI processing is authorized for this screenshot.'}
          {consent === 'not_granted' && '\u2716 AI processing is NOT authorized.'}
          {consent === 'error' && 'Could not read consent; server-side authorization remains required.'}
        </p>
        {consent === 'granted' ? (
          <button type="button" className="gy-btn" onClick={onRevoke} disabled={busy}>
            Withdraw AI consent
          </button>
        ) : (
          <button
            type="button"
            className="gy-btn gy-btn--revive"
            onClick={onGrant}
            disabled={busy || consent === 'loading' || providerMode === 'unknown'}
          >
            {authorizeLabel}
          </button>
        )}
      </div>

      {message && (
        <p
          className={`gy__notice gy__notice--${message.kind}`}
          role={message.kind === 'error' ? 'alert' : 'status'}
        >
          {message.text}
        </p>
      )}

      <div className="ai-block" aria-labelledby="ai-revive-h">
        <h4 id="ai-revive-h">Revive</h4>
        {analysis.phase === 'none' && (
          <>
            <button
              type="button"
              className={consent === 'granted' ? 'gy-btn gy-btn--revive' : 'gy-btn'}
              onClick={onRevive}
              disabled={!canRevive}
            >
              {busy || polling ? 'Reviving\u2026' : reviveLabel}
            </button>
            {providerMode === 'unknown' ? (
              <p className="ai-hint">
                {providerLoading
                  ? 'Checking the AI provider. Revive is available once this finishes.'
                  : 'Provider status could not be confirmed, so Revive is unavailable.'}
              </p>
            ) : consent !== 'granted' && (
              <p className="ai-hint">Grant AI consent above to enable Revive.</p>
            )}
          </>
        )}
        {analysis.phase === 'loading' && <p className="ai-hint">Loading…</p>}
        {analysis.phase === 'error' && (
          <p role="alert" className="gy__status gy__status--error">
            Could not load the analysis. Please close and reopen this screenshot.
          </p>
        )}

        <div aria-live="polite" className="ai-opstatus">
          {poll.phase === 'polling' && (
            <>
              <StatusBadge status="pending" />
              <p>
                {providerMode === 'fake'
                  ? 'The demo is preparing a simulated result.'
                  : providerMode === 'openai'
                    ? 'The AI is working. This can take a little while.'
                    : 'Processing status is being checked.'}
              </p>
            </>
          )}
          {poll.phase === 'done' && (
            <>
              <StatusBadge status={poll.status} />
              <p>{describeOperationOutcome(poll.status, poll.errorDetail)}</p>
            </>
          )}
          {poll.phase === 'timeout' && (
            <p role="alert">
              Still not finished after waiting. Stopped checking so we do not poll
              forever; reopen this screenshot later to see whether it completed.
            </p>
          )}
          {poll.phase === 'unavailable' && (
            <p role="alert" data-testid="detail-op-unavailable">
              {STATUS_UNAVAILABLE_TEXT} (Operation #{poll.operationId}.)
            </p>
          )}
          {poll.phase === 'error' && <p role="alert">{poll.message}</p>}
        </div>
      </div>

      {(onArchive || onRestore) && (
        <div className="ai-block" aria-labelledby="ai-lifecycle-h">
          <h4 id="ai-lifecycle-h">{screenshot.state === 'archived' ? 'In The Crypt' : 'Safe Burial'}</h4>
          <p className="ai-hint" data-testid="detail-crypt-note">{CRYPT_GALLERY_NOTE}</p>
          {screenshot.state === 'archived' ? (
            onRestore && (
              <button type="button" className="gy-btn" disabled={lifecycleBusy} onClick={() => void onRestore(id)}>
                {lifecycleBusy ? 'Resurrecting...' : 'Resurrection: restore from The Crypt'}
              </button>
            )
          ) : (
            onArchive && (
              <button type="button" className="gy-btn" disabled={lifecycleBusy} onClick={() => void onArchive(id)}>
                {lifecycleBusy ? 'Burying...' : 'Safe Burial: move to The Crypt'}
              </button>
            )
          )}
        </div>
      )}

      {analysis.phase === 'ready' && (
        <div className="ai-block" aria-labelledby="ai-result-h">
          <h4 id="ai-result-h">Epitaph and category</h4>
          <dl className="ai-result">
            <dt>Category</dt>
            <dd data-testid="result-category">{analysis.analysis.classification.category}</dd>
            <dt>Confidence</dt>
            <dd>
              {analysis.analysis.classification.origin === 'manual_edit'
                ? 'Set by you'
                : `${Math.round(analysis.analysis.classification.confidence * 100)}%`}
            </dd>
            {analysis.analysis.classification.uncertain && (
              <>
                <dt>Certainty</dt>
                <dd>? The AI marked this category as uncertain.</dd>
              </>
            )}
            <dt>Epitaph</dt>
            <dd data-testid="result-epitaph">{analysis.analysis.epitaph.text}</dd>
            <dt>What is visible</dt>
            <dd data-testid="result-visible">{analysis.analysis.epitaph.visibleFacts || 'Not provided'}</dd>
            {analysis.analysis.epitaph.inferredIntent && (
              <>
                <dt>Possible reason (a guess)</dt>
                <dd>{analysis.analysis.epitaph.inferredIntent}</dd>
              </>
            )}
            {analysis.analysis.epitaph.uncertain && (
              <>
                <dt>Certainty</dt>
                <dd>? The purpose could not be reliably inferred.</dd>
              </>
            )}
            <dt>Source</dt>
            <dd>
              {analysis.analysis.classification.origin === 'manual_edit'
                ? `Edited by you (category v${analysis.analysis.classification.versionNumber}, Epitaph v${analysis.analysis.epitaph.versionNumber})`
                : providerMode === 'fake' ? 'Simulated demo result (not image interpretation)'
                : providerMode === 'openai' ? 'Generated by AI' : 'Provider mode unconfirmed'}
              {analysis.analysis.epitaph.origin === 'manual_edit' &&
                analysis.analysis.classification.origin !== 'manual_edit' &&
                ` (Epitaph edited by you, v${analysis.analysis.epitaph.versionNumber})`}
            </dd>
          </dl>
          {analysis.analysis.classification.category === 'Potentially Sensitive' && (
            <p className="gy__notice gy__notice--info" role="note">
              Advisory only: this suggests the screenshot may contain sensitive
              details. It is a hint, not a guarantee, and nothing was hidden or
              removed.
            </p>
          )}

          <form
            className="ai-edit"
            onSubmit={(e) => {
              e.preventDefault();
              void onSaveEdit();
            }}
          >
            <h5>Edit</h5>
            <label htmlFor="ai-edit-category">Category</label>
            <select
              id="ai-edit-category"
              value={draftCategory}
              onChange={(e) => setDraftCategory(e.target.value as Category)}
            >
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            <label htmlFor="ai-edit-epitaph">Epitaph</label>
            <textarea
              id="ai-edit-epitaph"
              value={draftEpitaph}
              maxLength={500}
              rows={3}
              onChange={(e) => setDraftEpitaph(e.target.value)}
            />
            <button type="submit" className="gy-btn" disabled={busy || draftEpitaph.trim() === ''}>
              Save edits
            </button>
            <p className="ai-hint">Each save keeps the earlier version. Your original image is never changed.</p>
          </form>
        </div>
      )}
    </section>
  );
}
