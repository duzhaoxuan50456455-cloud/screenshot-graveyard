import { useEffect, useMemo, useRef, useState } from 'react';
import { classifyScreenshot, getAnalysis, getConsent, getOperation, type Analysis } from '../api/ai';
import type { Screenshot } from '../api/client';
import { describeRequestFailure } from '../ai/messages';
import { useProviderMode } from '../ai/PrivacyNotice';
import {
  deriveSuggestions,
  pickPrimary,
  type ConsentStatus,
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
  pollIntervalMs?: number;
  maxPolls?: number;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export function QuietAssistant({
  selected,
  refreshToken,
  onOpen,
  onSetUpStudyNote,
  onAnalysisChanged,
  pollIntervalMs = 1000,
  maxPolls = 60,
}: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const [consent, setConsent] = useState<Record<number, ConsentStatus>>({});
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set()); // session only
  const [skip, setSkip] = useState(0);
  const [processing, setProcessing] = useState(false);
  const [result, setResult] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const provider = useProviderMode();
  const aliveRef = useRef(true);
  const busyRef = useRef(false);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const selectedKey = selected.map((s) => s.screenshotId).join(',');

  // Read-only consent lookups for the selection. Never grants consent.
  useEffect(() => {
    if (selectedKey === '') return;
    let active = true;
    const ids = selectedKey.split(',').map(Number);
    void Promise.all(
      ids.map(async (id) => {
        const res = await getConsent(id);
        return [id, res.ok ? res.data : ('unknown' as const)] as const;
      }),
    ).then((entries) => {
      if (!active) return;
      setConsent((cur) => ({ ...cur, ...Object.fromEntries(entries) }));
    });
    return () => {
      active = false;
    };
  }, [selectedKey, refreshToken]);

  // A changed selection starts a fresh suggestion; "something else" resets.
  useEffect(() => {
    setSkip(0);
    setResult(null);
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
        : 'Provider status is unconfirmed, so Revive is unavailable here. Open a card to continue.';
  const acceptBlocked = isClassify && provider === 'unknown';
  const labelText =
    primary?.source !== 'ai'
      ? 'Suggested by app rules'
      : provider === 'fake'
        ? 'Simulated demo suggestion'
        : 'AI suggestion';
  const hasMore = all.filter((s) => !dismissed.has(s.key)).length > skip + 1;

  const classifyOne = async (id: number): Promise<'ok' | string> => {
    const accepted = await classifyScreenshot(id);
    if (!accepted.ok) return describeRequestFailure(accepted);
    for (let i = 0; i < maxPolls; i += 1) {
      const op = await getOperation(accepted.data.operationId);
      if (!aliveRef.current) return 'cancelled';
      if (op.ok && op.data.status !== 'pending') {
        if (op.data.status !== 'successful') return 'The analysis did not finish. Nothing was changed.';
        const analysis = await getAnalysis(id);
        if (!analysis.ok) return 'The analysis finished but could not be loaded.';
        onAnalysisChanged(analysis.data);
        return 'ok';
      }
      await sleep(pollIntervalMs);
    }
    return 'Still waiting on the server. Check the card later.';
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
    busyRef.current = true;
    setProcessing(true);
    setResult(null);
    let done = 0;
    let failure: string | null = null;
    for (const id of s.targetIds) {
      const r = await classifyOne(id);
      if (!aliveRef.current) return;
      if (r === 'ok') done += 1;
      else {
        failure = r;
        break;
      }
    }
    busyRef.current = false;
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
        {primary && primary.kind !== 'idle' && primary.kind !== 'processing' && (
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
