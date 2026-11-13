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
import { ScreenshotDetail } from './ai/ScreenshotDetail';
import { StudyNotesPanel } from './ai/StudyNotesPanel';

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

  const active = screenshots.filter((s) => s.state === 'active');
  const archived = screenshots.filter((s) => s.state === 'archived');

  const renderCard = (s: Screenshot, kind: 'ghost' | 'crypt') => {
    const label = s.originalFilename || `Screenshot #${s.screenshotId}`;
    const isPending = pendingId === s.screenshotId;
    return (
      <li key={s.screenshotId} className={`gy-card gy-card--${kind}`}>
        <img
          className="gy-card__img"
          src={imageUrl(s.screenshotId)}
          alt={`Screenshot ${label}`}
          loading="lazy"
        />
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
        <div className="gy-card__actions">
          <button
            type="button"
            className="gy-btn"
            onClick={() => setOpenId(s.screenshotId)}
            aria-label={`Open ${label} (consent and AI)`}
          >
            Open
          </button>
          {kind === 'ghost' ? (
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
            Ghosts <span className="gy-section__count">({active.length})</span>
          </h2>
          <p className="gy-section__sub">Active screenshots, still wandering.</p>
          {active.length === 0 ? (
            <p className="gy-empty">
              No ghosts yet. Upload a screenshot to begin.
            </p>
          ) : (
            <ul className="gy-grid">{active.map((s) => renderCard(s, 'ghost'))}</ul>
          )}
        </section>

        <section className="gy-section" aria-labelledby="gy-crypt-h">
          <h2 id="gy-crypt-h" className="gy-section__h">
            The Crypt <span className="gy-section__count">({archived.length})</span>
          </h2>
          <p className="gy-section__sub">
            Archived screenshots. Recoverable — nothing is ever deleted.
          </p>
          {archived.length === 0 ? (
            <p className="gy-empty">The Crypt is empty.</p>
          ) : (
            <ul className="gy-grid">
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
        />
      )}

      <StudyNotesPanel screenshots={screenshots} />
    </section>
  );
}

export default Graveyard;
