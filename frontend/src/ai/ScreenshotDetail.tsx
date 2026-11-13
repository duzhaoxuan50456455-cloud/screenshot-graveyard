import { useCallback, useEffect, useRef, useState } from 'react';
import {
  CATEGORIES,
  classifyScreenshot,
  editAnalysis,
  getAnalysis,
  getConsent,
  setConsent,
  type Analysis,
  type Category,
} from '../api/ai';
import { imageUrl, type Screenshot } from '../api/client';
import { describeOperationOutcome, describeRequestFailure } from './messages';
import { PrivacyNotice, useProviderMode } from './PrivacyNotice';
import { StatusBadge } from './StatusBadge';
import {
  DEFAULT_MAX_POLLS,
  DEFAULT_POLL_INTERVAL_MS,
  useOperationPolling,
} from './useOperationPolling';

type ConsentState = 'loading' | 'granted' | 'not_granted' | 'error';
type AnalysisState =
  | { phase: 'loading' }
  | { phase: 'none' }
  | { phase: 'ready'; analysis: Analysis }
  | { phase: 'error' };

interface Props {
  screenshot: Screenshot;
  onClose: () => void;
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
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  maxPolls = DEFAULT_MAX_POLLS,
}: Props) {
  const id = screenshot.screenshotId;
  const label = screenshot.originalFilename || `Screenshot #${id}`;
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const [consent, setConsentState] = useState<ConsentState>('loading');
  const [analysis, setAnalysis] = useState<AnalysisState>({ phase: 'loading' });
  const [busy, setBusy] = useState(false); // request in flight (duplicate-click guard)
  const busyRef = useRef(false);
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const { state: poll, start, reset } = useOperationPolling(pollIntervalMs, maxPolls);

  const [draftCategory, setDraftCategory] = useState<Category>(CATEGORIES[0]);
  const [draftEpitaph, setDraftEpitaph] = useState('');
  const providerMode = useProviderMode();

  const loadAnalysis = useCallback(async () => {
    const res = await getAnalysis(id);
    if (res.ok) {
      setAnalysis({ phase: 'ready', analysis: res.data });
      setDraftCategory(res.data.classification.category);
      setDraftEpitaph(res.data.epitaph.text);
    } else if (res.status === 404) {
      setAnalysis({ phase: 'none' });
    } else {
      setAnalysis({ phase: 'error' });
    }
  }, [id]);

  // Read-only on open: consent + any existing result. No AI call happens here.
  useEffect(() => {
    let active = true;
    headingRef.current?.focus();
    (async () => {
      const [c, a] = await Promise.all([getConsent(id), getAnalysis(id)]);
      if (!active) return;
      setConsentState(c.ok ? (c.data ? 'granted' : 'not_granted') : 'error');
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
        setConsentState('granted');
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
        setConsentState('not_granted');
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
      reset();
      const res = await classifyScreenshot(id);
      if (!res.ok) {
        if (res.status === 403) setConsentState('not_granted');
        setMessage({ kind: 'error', text: describeRequestFailure(res) });
        return;
      }
      start(res.data.operationId);
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
        setMessage({ kind: 'info', text: 'Saved as a new version. Earlier versions are kept.' });
      } else {
        setMessage({ kind: 'error', text: describeRequestFailure(res) });
      }
    });

  const polling = poll.phase === 'polling';
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
        : 'Provider status unavailable';
  const reviveLabel =
    providerMode === 'fake'
      ? 'Revive: analyze with AI (simulated demo)'
      : providerMode === 'openai'
        ? 'Revive: analyze with AI'
        : 'Revive: analyze with AI (unavailable: provider status unconfirmed)';

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

      <PrivacyNotice scope="one" mode={providerMode} />

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
              className="gy-btn gy-btn--revive"
              onClick={onRevive}
              disabled={!canRevive}
            >
              {busy || polling ? 'Reviving\u2026' : reviveLabel}
            </button>
            {providerMode === 'unknown' ? (
              <p className="ai-hint">Provider status must be confirmed before Revive is available.</p>
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
          {poll.phase === 'error' && <p role="alert">{poll.message}</p>}
        </div>
      </div>

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
