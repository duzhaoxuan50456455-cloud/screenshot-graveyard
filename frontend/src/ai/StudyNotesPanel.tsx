import { useCallback, useEffect, useRef, useState } from 'react';
import {
  generateStudyNote,
  getConsent,
  listStudyNotes,
  setConsent,
  type StudyNoteSummary,
} from '../api/ai';
import { imageUrl, type Screenshot } from '../api/client';
import { describeOperationOutcome, describeRequestFailure } from './messages';
import { PrivacyNotice, useProviderMode } from './PrivacyNotice';
import { StatusBadge } from './StatusBadge';
import { StudyNoteView } from './StudyNoteView';
import {
  DEFAULT_MAX_POLLS,
  DEFAULT_POLL_INTERVAL_MS,
  useOperationPolling,
} from './useOperationPolling';

// Mirrors the server policy (Task 2.6). The server remains authoritative.
export const MIN_SELECTION = 1;
export const MAX_SELECTION = 12;
export const RECOMMENDED_MIN = 4;
export const RECOMMENDED_MAX = 8;

interface Props {
  screenshots: Screenshot[];
  pollIntervalMs?: number;
  maxPolls?: number;
}

type ConsentMap = Record<number, boolean | 'unknown'>;

export function StudyNotesPanel({
  screenshots,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  maxPolls = DEFAULT_MAX_POLLS,
}: Props) {
  const [selected, setSelected] = useState<number[]>([]); // user order
  const [consent, setConsentMap] = useState<ConsentMap>({});
  const [notes, setNotes] = useState<StudyNoteSummary[]>([]);
  const [notesError, setNotesError] = useState(false);
  const [openNote, setOpenNote] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const [serverWarning, setServerWarning] = useState<string | null>(null);
  const { state: poll, start, reset } = useOperationPolling(pollIntervalMs, maxPolls);
  const providerMode = useProviderMode();

  const byId = new Map(screenshots.map((s) => [s.screenshotId, s]));
  const nameOf = (id: number) => byId.get(id)?.originalFilename || `Screenshot #${id}`;

  const refreshNotes = useCallback(async () => {
    const res = await listStudyNotes();
    if (res.ok) {
      setNotes(res.data);
      setNotesError(false);
      return res.data;
    }
    setNotesError(true);
    return null;
  }, []);

  // Read-only on mount: list existing notes. No AI call.
  useEffect(() => {
    void refreshNotes();
  }, [refreshNotes]);

  // Drop selections whose screenshot no longer exists in the list.
  useEffect(() => {
    const present = new Set(screenshots.map((s) => s.screenshotId));
    setSelected((cur) => (cur.every((id) => present.has(id)) ? cur : cur.filter((id) => present.has(id))));
  }, [screenshots]);

  // A completed operation carries its exact server-persisted artifact ID. Never
  // infer the result from a list difference: another concurrent note could be
  // newer, and a reload can lose the pre-operation snapshot.
  useEffect(() => {
    if (poll.phase !== 'done') return;
    void refreshNotes();
    if (typeof poll.artifactId === 'number') setOpenNote(poll.artifactId);
  }, [poll, refreshNotes]);

  const loadConsent = async (id: number) => {
    const res = await getConsent(id);
    setConsentMap((m) => ({ ...m, [id]: res.ok ? res.data : 'unknown' }));
  };

  const toggle = (id: number) => {
    setMessage(null);
    if (selected.includes(id)) {
      setSelected((cur) => cur.filter((x) => x !== id));
      return;
    }
    if (selected.length >= MAX_SELECTION) {
      setMessage({ kind: 'error', text: `You can select at most ${MAX_SELECTION} screenshots.` });
      return;
    }
    setSelected((cur) => [...cur, id]);
    void loadConsent(id);
  };

  const move = (index: number, delta: -1 | 1) =>
    setSelected((cur) => {
      const j = index + delta;
      if (j < 0 || j >= cur.length) return cur;
      const next = [...cur];
      [next[index], next[j]] = [next[j], next[index]];
      return next;
    });

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

  const ungranted = selected.filter((id) => consent[id] !== true);
  const sub = (n: number) => `${n} screenshot${n === 1 ? '' : 's'}`;
  const polling = poll.phase === 'polling';
  const countOk = selected.length >= MIN_SELECTION && selected.length <= MAX_SELECTION;
  const outsideRecommended =
    selected.length > 0 && (selected.length < RECOMMENDED_MIN || selected.length > RECOMMENDED_MAX);
  const canGenerate =
    providerMode !== 'unknown' && countOk && ungranted.length === 0 && !busy && !polling;
  const authorizeLabel =
    providerMode === 'fake'
      ? `I understand and agree: authorize simulated demo processing for ${sub(ungranted.length)}`
      : providerMode === 'openai'
        ? `I understand and agree: allow AI to read ${sub(ungranted.length)}`
        : 'Provider status unavailable';
  const generateLabel =
    providerMode === 'fake'
      ? 'Revive: generate study note (simulated demo)'
      : providerMode === 'openai'
        ? 'Revive: generate study note'
        : 'Revive: generate study note (unavailable: provider status unconfirmed)';

  const onAuthorize = () =>
    guarded(async () => {
      setMessage(null);
      let failed = 0;
      for (const id of ungranted) {
        const res = await setConsent(id, true);
        if (res.ok) setConsentMap((m) => ({ ...m, [id]: true }));
        else failed += 1;
      }
      setMessage(
        failed === 0
          ? {
              kind: 'info',
              text:
                providerMode === 'fake'
                  ? 'Consent recorded for the selected simulated demo workflow.'
                  : 'AI consent granted for the selected screenshots.',
            }
          : { kind: 'error', text: `Could not record consent for ${failed} screenshot(s). Nothing was processed.` },
      );
    });

  const onGenerate = () =>
    guarded(async () => {
      setMessage(null);
      setServerWarning(null);
      reset();
      const res = await generateStudyNote(selected);
      if (!res.ok) {
        if (res.status === 403) {
          // Server says some consent is missing: re-read the truth for each.
          await Promise.all(selected.map(loadConsent));
        }
        setMessage({ kind: 'error', text: describeRequestFailure(res) });
        return;
      }
      setServerWarning(res.data.warning);
      start(res.data.operationId);
    });

  return (
    <section className="ai-notes" aria-labelledby="ai-notes-h">
      <h2 id="ai-notes-h" className="gy-section__h">
        Study Notes
      </h2>
      <p className="gy-section__sub">
        Pick related screenshots yourself (1 to 12; 4 to 8 usually works best)
        and turn them into one structured, editable note.
      </p>

      <PrivacyNotice scope="many" mode={providerMode} />

      <h3>1. Choose screenshots</h3>
      {screenshots.length === 0 ? (
        <p className="gy-empty">Upload some screenshots first, then pick them here.</p>
      ) : (
        <ul className="ai-pick">
          {screenshots.map((s) => {
            const on = selected.includes(s.screenshotId);
            const name = nameOf(s.screenshotId);
            return (
              <li key={s.screenshotId} className="ai-pick__item">
                <img src={imageUrl(s.screenshotId)} alt="" className="ai-pick__thumb" />
                <span className="ai-pick__name">
                  {name}
                  {s.state === 'archived' && ' (in The Crypt)'}
                </span>
                <button
                  type="button"
                  className="gy-btn"
                  aria-pressed={on}
                  aria-label={`${on ? 'Remove' : 'Add'} ${name} ${on ? 'from' : 'to'} the study note selection`}
                  onClick={() => toggle(s.screenshotId)}
                >
                  {on ? 'Selected \u2714' : 'Select'}
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <h3>2. Review your selection</h3>
      <p role="status" aria-live="polite" data-testid="selection-count">
        {sub(selected.length)} selected.
      </p>
      {selected.length === 0 ? (
        <p className="ai-hint">Nothing selected yet. Order matters: sources are kept in the order below.</p>
      ) : (
        <ol className="ai-order" aria-label="Selected screenshots in order">
          {selected.map((id, i) => (
            <li key={id}>
              <span>
                {nameOf(id)} &mdash;{' '}
                {consent[id] === true
                  ? '\u2714 AI authorized'
                  : consent[id] === false
                    ? '\u2716 AI not authorized'
                    : 'Checking consent\u2026'}
              </span>
              <button type="button" className="gy-btn" onClick={() => move(i, -1)} disabled={i === 0} aria-label={`Move ${nameOf(id)} up`}>
                Up
              </button>
              <button
                type="button"
                className="gy-btn"
                onClick={() => move(i, 1)}
                disabled={i === selected.length - 1}
                aria-label={`Move ${nameOf(id)} down`}
              >
                Down
              </button>
            </li>
          ))}
        </ol>
      )}
      {outsideRecommended && countOk && (
        <p className="gy__notice gy__notice--info" role="status">
          Study notes work best with 4 to 8 related screenshots; you can still
          continue with this selection.
        </p>
      )}
      {selected.length > MAX_SELECTION && (
        <p role="alert">Select at most {MAX_SELECTION} screenshots.</p>
      )}

      <h3>3. AI consent for the selection</h3>
      {selected.length > 0 && ungranted.length > 0 ? (
        <>
          <p>
            {sub(ungranted.length)} still need your permission before anything is
            processed.
          </p>
          <button
            type="button"
            className="gy-btn gy-btn--revive"
            onClick={onAuthorize}
            disabled={busy || providerMode === 'unknown'}
          >
            {authorizeLabel}
          </button>
        </>
      ) : (
        <p className="ai-hint">
          {selected.length === 0 ? 'Select screenshots to review consent.' : '\u2714 Every selected screenshot is authorized.'}
        </p>
      )}

      <h3>4. Generate</h3>
      <button type="button" className="gy-btn gy-btn--revive" onClick={onGenerate} disabled={!canGenerate}>
        {busy || polling ? 'Generating\u2026' : generateLabel}
      </button>
      {!canGenerate && !busy && !polling && (
        <p className="ai-hint">
          {selected.length === 0
            ? 'Select at least 1 screenshot.'
            : providerMode === 'unknown'
              ? 'Provider status is unavailable; generation is disabled until it can be confirmed.'
              : !countOk
              ? `Select between ${MIN_SELECTION} and ${MAX_SELECTION} screenshots.`
              : 'Grant AI consent for every selected screenshot to continue.'}
        </p>
      )}

      {message && (
        <p
          className={`gy__notice gy__notice--${message.kind}`}
          role={message.kind === 'error' ? 'alert' : 'status'}
        >
          {message.text}
        </p>
      )}
      {serverWarning && (
        <p className="gy__notice gy__notice--info" role="status">
          {serverWarning}
        </p>
      )}

      <div aria-live="polite" className="ai-opstatus">
        {poll.phase === 'polling' && (
          <>
            <StatusBadge status="pending" />
            <p>
              {providerMode === 'fake'
                ? 'The demo is preparing a simulated note.'
                : providerMode === 'openai'
                  ? 'The AI is writing your note. This can take a little while.'
                  : 'Processing status is being checked.'}
            </p>
          </>
        )}
        {poll.phase === 'done' && (
          <>
            <StatusBadge status={poll.status} />
            <p>{describeOperationOutcome(poll.status, poll.errorDetail)}</p>
            {poll.status === 'uncertain' && (
              <p>
                The note was saved with the uncertain parts flagged. Please check
                it against the original screenshots.
              </p>
            )}
          </>
        )}
        {poll.phase === 'timeout' && (
          <p role="alert">
            Still not finished after waiting. Stopped checking so we do not poll
            forever; refresh the list below later to see whether it completed.
          </p>
        )}
        {poll.phase === 'error' && <p role="alert">{poll.message}</p>}
      </div>

      <h3>Your study notes</h3>
      {notesError && (
        <p role="alert" className="gy__status gy__status--error">
          Could not load your study notes.{' '}
          <button type="button" className="gy-btn" onClick={() => void refreshNotes()}>
            Try again
          </button>
        </p>
      )}
      {!notesError && notes.length === 0 && <p className="gy-empty">No study notes yet.</p>}
      {notes.length > 0 && (
        <ul className="ai-notelist">
          {notes.map((n) => (
            <li key={n.artifactId}>
              <button
                type="button"
                className="gy-btn"
                aria-pressed={openNote === n.artifactId}
                onClick={() => setOpenNote(openNote === n.artifactId ? null : n.artifactId)}
              >
                {n.topic} (v{n.versionNumber}, from {sub(n.sourceScreenshotIds.length)})
              </button>
            </li>
          ))}
        </ul>
      )}
      {openNote !== null && (
        <StudyNoteView
          key={openNote}
          artifactId={openNote}
          onVersionsChanged={() => void refreshNotes()}
        />
      )}
    </section>
  );
}
