import { useCallback, useEffect, useRef, useState } from 'react';
import {
  archiveScreenshot,
  getWorkspace,
  imageUrl,
  listScreenshots,
  restoreScreenshot,
  uploadScreenshots,
  type Screenshot,
} from './api/client';
import type { Analysis } from './api/ai';
import { EpitaphEditor } from './ai/EpitaphEditor';
import { describeOrigin } from './ai/messages';
import { useProviderMode } from './ai/PrivacyNotice';
import { ScreenshotDetail } from './ai/ScreenshotDetail';
import { StudyNotesPanel } from './ai/StudyNotesPanel';
import { partitionScreenshots, type CardKind } from './graveyardState';

type LoadPhase =
  | { phase: 'starting' }
  | { phase: 'ready' }
  | { phase: 'error'; message: string };

// A short, factual batch outcome message. No AI/classification claims.
type Notice = { kind: 'info' | 'error'; text: string } | null;

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function Graveyard() {
  const [load, setLoad] = useState<LoadPhase>({ phase: 'starting' });
  const [screenshots, setScreenshots] = useState<Screenshot[]>([]);
  const [listError, setListError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const provider = useProviderMode(); // read-only; 'unknown' until confirmed
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    const result = await listScreenshots();
    if (result.ok) {
      setScreenshots(result.screenshots);
      setListError(null);
    } else {
      setListError(result.error);
    }
  }, []);

  // Bootstrap/resume the Workspace on first load, then load the list.
  useEffect(() => {
    let active = true;
    (async () => {
      const ws = await getWorkspace();
      if (!active) return;
      if (!ws.ok || !ws.active) {
        setLoad({
          phase: 'error',
          message: ws.ok
            ? 'Your workspace could not be started.'
            : `Could not reach your workspace (${ws.error}).`,
        });
        return;
      }
      setLoad({ phase: 'ready' });
      await refresh();
    })();
    return () => {
      active = false;
    };
  }, [refresh]);

  // The server's analysis is the source of truth: when an analysis is created or
  // edited elsewhere in the UI, mirror it into the list so the Ghost / Revived
  // views change immediately and without another request. Stable identity on
  // purpose (children keep it in effects).
  const onAnalysisChanged = useCallback((analysis: Analysis) => {
    setScreenshots((current) =>
      current.map((s) =>
        s.screenshotId === analysis.screenshotId
          ? {
              ...s,
              analysis: {
                category: analysis.classification.category,
                uncertain: analysis.classification.uncertain,
                epitaph: analysis.epitaph.text,
                epitaphOrigin: analysis.epitaph.origin,
              },
            }
          : s,
      ),
    );
  }, []);

  const onUpload = useCallback(
    async (fileList: FileList | null) => {
      if (!fileList || fileList.length === 0) return;
      const files = Array.from(fileList);
      setUploading(true);
      setNotice(null);
      const result = await uploadScreenshots(files);
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
      if (!result.ok) {
        setNotice({ kind: 'error', text: `Upload failed: ${result.error}` });
        return;
      }
      const dupes = result.results.filter(
        (r) => r.status === 'accepted' && r.isDuplicate,
      ).length;
      let text: string;
      let kind: 'info' | 'error' = 'info';
      if (result.accepted > 0 && result.rejected > 0) {
        // 207 partial success — report both sides honestly.
        text = `Saved ${result.accepted}, could not save ${result.rejected}. ${result.results
          .filter((r) => r.status === 'rejected')
          .map((r) => `${r.filename}: ${r.error ?? 'rejected'}`)
          .join('; ')}`;
        kind = 'error';
      } else if (result.accepted > 0) {
        text = `Saved ${result.accepted} screenshot${result.accepted === 1 ? '' : 's'}.`;
      } else {
        text = `No screenshots were saved. ${result.results
          .map((r) => `${r.filename}: ${r.error ?? 'rejected'}`)
          .join('; ')}`;
        kind = 'error';
      }
      if (dupes > 0) {
        text += ` ${dupes} looked identical to one you already have.`;
      }
      setNotice({ kind, text });
      await refresh();
    },
    [refresh],
  );

  const onArchive = useCallback(
    async (id: number) => {
      setBusy(true);
      setPendingId(id);
      const result = await archiveScreenshot(id);
      setBusy(false);
      setPendingId(null);
      if (!result.ok) {
        setNotice({ kind: 'error', text: `Could not archive: ${result.error}` });
        return;
      }
      if (result.status !== 'successful') {
        setNotice({ kind: 'error', text: 'Safe Burial did not complete.' });
        await refresh();
        return;
      }
      // Keep the visible metaphor truthful even if the follow-up list request
      // is temporarily unavailable; refresh remains the source-of-truth sync.
      setScreenshots((current) =>
        current.map((s) =>
          s.screenshotId === result.screenshotId ? { ...s, state: result.state } : s,
        ),
      );
      setNotice({ kind: 'info', text: 'Moved to The Crypt.' });
      await refresh();
    },
    [refresh],
  );

  const onRestore = useCallback(
    async (id: number) => {
      setBusy(true);
      setPendingId(id);
      const result = await restoreScreenshot(id);
      setBusy(false);
      setPendingId(null);
      if (!result.ok) {
        setNotice({ kind: 'error', text: `Could not restore: ${result.error}` });
        return;
      }
      if (result.status === 'failed') {
        // Honest: integrity check failed; it stayed archived.
        setNotice({
          kind: 'error',
          text: 'Restore failed an integrity check; the screenshot stayed in The Crypt.',
        });
      } else {
        // Keep the card in the state confirmed by the lifecycle response even
        // if a transient refresh failure follows.
        setScreenshots((current) =>
          current.map((s) =>
            s.screenshotId === result.screenshotId ? { ...s, state: result.state } : s,
          ),
        );
        setNotice({ kind: 'info', text: 'Resurrected to the active graveyard.' });
      }
      await refresh();
    },
    [refresh],
  );

  if (load.phase === 'starting') {
    return (
      <section className="gy" aria-busy="true">
        <p className="gy__status" role="status" aria-live="polite">
          Waking the graveyard...
        </p>
      </section>
    );
  }

  if (load.phase === 'error') {
    return (
      <section className="gy">
        <p className="gy__status gy__status--error" role="alert">
          {load.message}
        </p>
      </section>
    );
  }

  const { ghosts, revived, crypt: archived } = partitionScreenshots(screenshots);

  const renderCard = (s: Screenshot, kind: CardKind) => {
    const label = s.originalFilename || `Screenshot #${s.screenshotId}`;
    const isPending = pendingId === s.screenshotId;
    return (
      <li key={s.screenshotId} className={`gy-card gy-card--${kind}`} data-kind={kind}>
        <img
          className="gy-card__img"
          src={imageUrl(s.screenshotId)}
          alt={`Screenshot ${label}`}
          loading="lazy"
        />
        <p className={`gy-card__state gy-card__state--${kind}`}>
          <span aria-hidden="true" className="gy-card__glyph">
            {kind === 'ghost' ? '◌' : kind === 'revived' ? '✦' : '▣'}
          </span>{' '}
          {kind === 'ghost' && 'Ghost: Pending, not analyzed yet'}
          {kind === 'revived' && 'Revived: analyzed'}
          {kind === 'crypt' && 'In The Crypt: archived, original kept'}
        </p>
        <div className="gy-card__meta">
          <span className="gy-card__name" title={label}>
            {label}
          </span>
          <span className="gy-card__size">{formatBytes(s.byteSize)}</span>
          {s.isDuplicate && (
            <span className="gy-card__dupe" title="Looks identical to another screenshot you have">
              <span aria-hidden="true">⦰</span> Duplicate
            </span>
          )}
        </div>
        {s.analysis === null ? (
          <p className="gy-card__pending">
            No Epitaph yet. {kind === 'crypt' ? 'It was archived before analysis.' : 'Revive it to analyze it.'}
          </p>
        ) : (
          <div className="gy-card__analysis">
            <p className="gy-card__category">
              <strong>Category:</strong> {s.analysis.category}
              {s.analysis.uncertain && ' (? the AI marked this uncertain)'}
            </p>
            <p className="gy-card__epitaph">
              <strong>Epitaph:</strong> <span data-testid={`epitaph-${s.screenshotId}`}>{s.analysis.epitaph}</span>
            </p>
            <p className="ai-hint">Epitaph {describeOrigin(s.analysis.epitaphOrigin, provider)}.</p>
            <EpitaphEditor
              screenshotId={s.screenshotId}
              name={label}
              epitaph={s.analysis.epitaph}
              onSaved={onAnalysisChanged}
            />
          </div>
        )}
        <div className="gy-card__actions">
          {kind === 'ghost' ? (
            <button
              type="button"
              className="gy-btn gy-btn--revive"
              onClick={() => setOpenId(s.screenshotId)}
              aria-label={`Revive (start AI analysis): open ${label} to review consent first`}
            >
              Revive…
            </button>
          ) : (
            <button
              type="button"
              className="gy-btn"
              onClick={() => setOpenId(s.screenshotId)}
              aria-label={`Open ${label} (consent and AI)`}
            >
              Open
            </button>
          )}
          {kind !== 'crypt' ? (
            <button
              type="button"
              className="gy-btn gy-btn--bury"
              onClick={() => onArchive(s.screenshotId)}
              disabled={busy}
              aria-label={`Safe Burial: archive ${label}`}
            >
              {isPending ? 'Burying...' : 'Safe Burial'}
            </button>
          ) : (
            <button
              type="button"
              className="gy-btn gy-btn--revive"
              onClick={() => onRestore(s.screenshotId)}
              disabled={busy}
              aria-label={`Resurrection: restore ${label}`}
            >
              {isPending ? 'Resurrecting...' : 'Resurrection'}
            </button>
          )}
        </div>
      </li>
    );
  };

  return (
    <section className="gy">
      {provider !== 'unknown' && (
        <p className={`gy-provider gy-provider--${provider}`} role="note" data-testid="ai-provider-note">
          {provider === 'fake' ? (
            <>
              <strong>Demo AI mode.</strong> Results are deterministic placeholders and do
              not read your images. Nothing is sent to an external AI service.
            </>
          ) : (
            <>
              <strong>Real AI mode.</strong> When you Revive a screenshot (after you
              consent), its image content is sent to an external AI service. Results are
              suggestions and can be wrong.
            </>
          )}
        </p>
      )}
      <form
        className="gy-upload"
        onSubmit={(e) => {
          e.preventDefault();
          onUpload(fileInputRef.current?.files ?? null);
        }}
      >
        <label className="gy-upload__label" htmlFor="gy-file">
          Add screenshots to the graveyard
        </label>
        <input
          id="gy-file"
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          className="gy-upload__input"
          onChange={(e) => onUpload(e.target.files)}
          disabled={uploading}
        />
        <p className="gy-upload__hint">
          Images only, up to 15 MB each. Your screenshots stay in your private
          workspace.
        </p>
      </form>

      {uploading && (
        <p className="gy__status" role="status" aria-live="polite">
          Uploading...
        </p>
      )}

      {notice && (
        <p
          className={`gy__notice gy__notice--${notice.kind}`}
          role={notice.kind === 'error' ? 'alert' : 'status'}
          aria-live="polite"
        >
          {notice.text}
        </p>
      )}

      {listError && (
        <p className="gy__status gy__status--error" role="alert">
          Could not load your screenshots ({listError}).{' '}
          <button type="button" className="gy-btn" onClick={() => refresh()}>
            Try again
          </button>
        </p>
      )}

      <div className="gy-sections">
        <section className="gy-section" aria-labelledby="gy-ghost-h">
          <h2 id="gy-ghost-h" className="gy-section__h">
            Ghosts <span className="gy-section__count">({ghosts.length})</span>
          </h2>
          <p className="gy-section__sub">
            Pending screenshots: uploaded but not analyzed yet.
          </p>
          {ghosts.length === 0 ? (
            <p className="gy-empty">
              {revived.length === 0 && archived.length === 0
                ? 'No ghosts yet. Upload a screenshot to begin.'
                : 'No ghosts: nothing is waiting for analysis.'}
            </p>
          ) : (
            <ul className="gy-grid" aria-label="Ghosts">
              {ghosts.map((s) => renderCard(s, 'ghost'))}
            </ul>
          )}
        </section>

        <section className="gy-section" aria-labelledby="gy-revived-h">
          <h2 id="gy-revived-h" className="gy-section__h">
            Revived <span className="gy-section__count">({revived.length})</span>
          </h2>
          <p className="gy-section__sub">
            Analyzed screenshots, with their category and Epitaph.
          </p>
          {revived.length === 0 ? (
            <p className="gy-empty">Nothing revived yet.</p>
          ) : (
            <ul className="gy-grid" aria-label="Revived screenshots">
              {revived.map((s) => renderCard(s, 'revived'))}
            </ul>
          )}
        </section>

        <section className="gy-section" aria-labelledby="gy-crypt-h">
          <h2 id="gy-crypt-h" className="gy-section__h">
            The Crypt <span className="gy-section__count">({archived.length})</span>
          </h2>
          <p className="gy-section__sub">
            Exactly your archived (Safe Buried) screenshots. Recoverable — nothing is ever deleted.
          </p>
          {archived.length === 0 ? (
            <p className="gy-empty">The Crypt is empty.</p>
          ) : (
            <ul className="gy-grid" aria-label="The Crypt">
              {archived.map((s) => renderCard(s, 'crypt'))}
            </ul>
          )}
        </section>
      </div>

      {openId !== null && screenshots.some((s) => s.screenshotId === openId) && (
        <ScreenshotDetail
          key={openId}
          screenshot={screenshots.find((s) => s.screenshotId === openId) as Screenshot}
          onClose={() => setOpenId(null)}
          onAnalysisChanged={onAnalysisChanged}
        />
      )}

      <StudyNotesPanel screenshots={screenshots} />
    </section>
  );
}

export default Graveyard;
