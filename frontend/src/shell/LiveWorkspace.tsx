import '@fontsource-variable/inter';
import '@fontsource/cormorant-garamond/600.css';
import '@fontsource/cormorant-garamond/700.css';
import '../ui/theme.css';
import '../ui/components.css';
import './shell.css';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  archiveScreenshot,
  getWorkspace,
  imageUrl,
  listScreenshots,
  restoreScreenshot,
  uploadScreenshots,
  type LifecycleResult,
  type ListResult,
  type Screenshot,
} from '../api/client';
import { CRYPT_GALLERY_NOTE } from '../ai/messages';
import { Button } from '../ui/primitives';
import { ShotArt } from '../ui/ShotArt';
import { Drawer } from './DetailDrawer';
import { AppHeader, NavTabs } from './Header';
import { ScreenshotGallery, UploadDropZone, WorkspaceHeading } from './Workspace';
import type { ShotVM, ViewId } from './types';

type GalleryState =
  | { phase: 'loading' }
  | { phase: 'error'; message: string }
  | { phase: 'ready'; screenshots: Screenshot[] };

type Notice = { error: boolean; text: string };
type Lifecycle = 'archive' | 'restore';
/** `session` identifies the drawer instance that started the action (null for Crypt cards). */
type Pending = { id: number; kind: Lifecycle; session: number | null } | null;
/** An action whose outcome we could not confirm. The server may or may not have committed it. */
type Uncertain = { id: number; kind: Lifecycle; session: number | null; checking: boolean; note: string };
type Detail = { id: string; opener: HTMLElement; session: number };
type Verdict = { t: 'confirmed' } | { t: 'failed'; text: string } | { t: 'unknown' };

/** Upper bound for one archive/restore request before we stop waiting and reconcile. */
export const LIFECYCLE_TIMEOUT_MS = 15_000;
const LABEL: Record<Lifecycle, string> = { archive: 'Safe Burial', restore: 'Resurrection' };
const EXPECTED: Record<Lifecycle, 'archived' | 'active'> = { archive: 'archived', restore: 'active' };

/** Upper bound for one lifecycle status read (a gallery GET) before it is treated as unresolved. */
export const STATUS_CHECK_TIMEOUT_MS = 10_000;

/** Cancel callbacks of requests still in flight, so unmounting clears every timer and aborts every fetch. */
type Registry = Set<() => void>;

/**
 * Run one request with a bounded wait. On timeout the fetch is aborted and
 * `timedOut` is returned. The timer is always cleared once the request settles,
 * and a late result after the timeout is discarded (the promise settles once).
 */
function bounded<T>(
  registry: Registry,
  ms: number,
  run: (fetchImpl: typeof fetch) => Promise<T>,
  timedOut: T,
  failed: T,
): Promise<T> {
  const controller = new AbortController();
  const withSignal: typeof fetch = (input, init) => fetch(input, { ...init, signal: controller.signal });
  return new Promise<T>((resolve) => {
    const cancel = () => {
      clearTimeout(timer);
      controller.abort();
      registry.delete(cancel);
    };
    const settle = (value: T, abort: boolean) => {
      clearTimeout(timer);
      registry.delete(cancel);
      if (abort) controller.abort();
      resolve(value);
    };
    const timer = setTimeout(() => settle(timedOut, true), ms);
    registry.add(cancel);
    run(withSignal).then(
      (value) => settle(value, false),
      () => settle(failed, false),
    );
  });
}

/**
 * One POST with a bounded wait. A timeout is an UNRESOLVED outcome, never a
 * failure: the server may still commit the change after we stop waiting.
 */
function postLifecycle(registry: Registry, id: number, kind: Lifecycle): Promise<LifecycleResult> {
  return bounded<LifecycleResult>(
    registry,
    LIFECYCLE_TIMEOUT_MS,
    (f) => (kind === 'archive' ? archiveScreenshot(id, f) : restoreScreenshot(id, f)),
    { ok: false, error: 'Request timed out', outcome: 'unknown' },
    { ok: false, error: 'Network request failed', outcome: 'unknown' },
  );
}

/**
 * A gallery read and the ticket it was issued with. Freshness is NOT captured
 * here: a caller that decides something from `result` must call `isCurrent(ticket)`
 * synchronously at the moment of the decision, because a newer read or a
 * confirmed change can be applied between this read resolving and the caller
 * resuming after its `await`.
 */
type GalleryRead = { result: ListResult; ticket: number };

/**
 * Decide what a lifecycle response proves. Only a matching, explicit answer is
 * treated as success or failure; anything else is unknown and must be reconciled.
 */
function judge(result: LifecycleResult, id: number, kind: Lifecycle): Verdict {
  if (!result.ok) {
    return result.outcome === 'rejected'
      ? { t: 'failed', text: `${LABEL[kind]} did not complete (${result.error}). Nothing was changed.` }
      : { t: 'unknown' };
  }
  if (result.screenshotId !== id) return { t: 'unknown' };
  if (result.status === 'successful' && result.state === EXPECTED[kind]) return { t: 'confirmed' };
  if (result.status === 'failed' && result.state === (kind === 'archive' ? 'active' : 'archived')) {
    return {
      t: 'failed',
      text:
        kind === 'restore'
          ? 'Resurrection failed an integrity check. The screenshot stayed in The Crypt.'
          : 'Safe Burial did not complete. The screenshot stayed in your Workspace.',
    };
  }
  return { t: 'unknown' };
}

function galleryState(result: ListResult): GalleryState {
  return result.ok
    ? { phase: 'ready', screenshots: result.screenshots }
    : { phase: 'error', message: `Could not load screenshots. ${result.error}` };
}

function toShot(screenshot: Screenshot): ShotVM {
  const uploaded = Date.parse(screenshot.uploadedAt);
  const bytes = screenshot.byteSize;
  const state = screenshot.state === 'archived' ? 'Archived' : screenshot.analysis ? 'Revived' : 'Ghost';
  return {
    id: String(screenshot.screenshotId),
    title: screenshot.originalFilename || `Screenshot #${screenshot.screenshotId}`,
    savedLabel: Number.isFinite(uploaded) ? new Date(uploaded).toLocaleDateString() : 'Date unavailable',
    savedDaysAgo: Number.isFinite(uploaded) ? Math.max(0, Math.floor((Date.now() - uploaded) / 86_400_000)) : 0,
    sizeLabel: bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`,
    art: 'image',
    topic: 'other',
    imageSrc: imageUrl(screenshot.screenshotId),
    statusLabel: screenshot.isDuplicate ? `${state} · Duplicate` : state,
  };
}

export default function LiveWorkspace() {
  const [gallery, setGallery] = useState<GalleryState>({ phase: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const [view, setView] = useState<ViewId>('workspace');
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [detail, setDetailState] = useState<Detail | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [uncertain, setUncertain] = useState<Record<number, Uncertain>>({});
  const [drawerMsg, setDrawerMsg] = useState<{ session: number; text: string } | null>(null);
  const uploadInFlight = useRef(false);
  const lifecycleInFlight = useRef(false);
  const uncertainRef = useRef<Record<number, Uncertain>>({});
  const detailRef = useRef<Detail | null>(null);
  const sessionCounter = useRef(0);
  // Gallery refresh sequencing. Every list request takes a ticket; a response is
  // applied only if its ticket is newer than the last applied one, so a delayed
  // older response can never overwrite newer authoritative state.
  const ticketCounter = useRef(0);
  const appliedTicket = useRef(0);
  const inflight = useRef<Registry>(new Set());

  // Mount generation. Async continuations capture it and stop (no state
  // updates, no new requests) once it no longer matches `liveGeneration`.
  const generationCounter = useRef(0);
  const liveGeneration = useRef<number | null>(null);
  useEffect(() => {
    const registry = inflight.current;
    liveGeneration.current = ++generationCounter.current;
    return () => {
      // Mark inactive FIRST: aborting below settles pending requests, and their
      // continuations must already see this mount as gone.
      liveGeneration.current = null;
      [...registry].forEach((cancel) => cancel());
    };
  }, []);
  /** Snapshot the current mount; the returned check is false after unmount (or a later remount). */
  const captureLive = () => {
    const generation = liveGeneration.current;
    return () => generation !== null && liveGeneration.current === generation;
  };

  const setDetail = (next: Detail | null) => {
    detailRef.current = next;
    setDetailState(next);
  };
  const putUncertain = (id: number, value: Uncertain | null) => {
    const next = { ...uncertainRef.current };
    if (value) next[id] = value;
    else delete next[id];
    uncertainRef.current = next;
    setUncertain(next);
  };

  /**
   * The single path for gallery list requests. A response is applied only if its
   * ticket is newer than everything already applied, and only while this mount
   * is live. Returns the ticket so a caller can re-check freshness with
   * `isCurrent` at its own decision point. `timeoutMs` bounds the read.
   */
  const refreshGallery = useCallback(async (timeoutMs?: number): Promise<GalleryRead> => {
    const ticket = ++ticketCounter.current;
    const generation = liveGeneration.current;
    const result =
      timeoutMs === undefined
        ? await listScreenshots()
        : await bounded<ListResult>(
            inflight.current,
            timeoutMs,
            (f) => listScreenshots(f),
            { ok: false, error: 'Request timed out' },
            { ok: false, error: 'Network request failed' },
          );
    const live = generation !== null && liveGeneration.current === generation;
    if (live && result.ok && ticket > appliedTicket.current) {
      appliedTicket.current = ticket;
      setGallery({ phase: 'ready', screenshots: result.screenshots });
    }
    return { result, ticket };
  }, []);
  /** True only if `ticket` is the most recently applied gallery state (nothing newer applied since). */
  const isCurrent = (ticket: number) => ticket === appliedTicket.current;

  /** Reflect a server-confirmed state change; invalidates any list request issued before it. */
  const applyConfirmed = (id: number, state: 'active' | 'archived') => {
    appliedTicket.current = ++ticketCounter.current;
    setGallery((g) =>
      g.phase === 'ready'
        ? { phase: 'ready', screenshots: g.screenshots.map((s) => (s.screenshotId === id ? { ...s, state } : s)) }
        : g,
    );
  };

  useEffect(() => {
    let active = true;
    // Skip the discarded setup in React Strict Mode before minting a cookie.
    void Promise.resolve().then(async () => {
      if (!active) return;
      const workspace = await getWorkspace();
      if (!active) return;
      if (!workspace.ok || !workspace.active) {
        setGallery({
          phase: 'error',
          message: workspace.ok ? 'Could not start your workspace.' : `Could not start your workspace. ${workspace.error}`,
        });
        return;
      }
      const { result } = await refreshGallery();
      if (active && !result.ok) setGallery(galleryState(result));
    });
    return () => { active = false; };
  }, [attempt, refreshGallery]);

  const upload = async (files: File[]) => {
    if (gallery.phase !== 'ready' || uploadInFlight.current) return;
    uploadInFlight.current = true;
    setUploading(true);
    setNotice(null);
    try {
      const result = await uploadScreenshots(files);
      if (!result.ok) {
        setNotice({ error: true, text: `Upload failed. ${result.error}` });
        return;
      }
      const saved = `Saved ${result.accepted} screenshot${result.accepted === 1 ? '' : 's'}.`;
      const rejected = result.results
        .filter((file) => file.status === 'rejected')
        .map((file) => `${file.filename}: ${file.error ?? 'Could not save this file.'}`);
      let refreshFailed = false;
      if (result.accepted > 0) refreshFailed = !(await refreshGallery()).result.ok;
      setNotice({
        error: rejected.length > 0 || refreshFailed,
        text: [saved, ...rejected, ...(refreshFailed ? ['Could not refresh the list. Reload to see it.'] : [])].join(' '),
      });
    } finally {
      uploadInFlight.current = false;
      setUploading(false);
    }
  };

  /** Report to the drawer that started the action if it is still open, otherwise to the page. */
  const report = (session: number | null, text: string, error: boolean) => {
    if (session !== null && detailRef.current?.session === session) setDrawerMsg({ session, text });
    else setNotice({ error, text });
  };

  /** Success for exactly the action that started it: never closes a different drawer. */
  const finishConfirmed = (id: number, kind: Lifecycle, session: number | null) => {
    putUncertain(id, null);
    applyConfirmed(id, EXPECTED[kind]);
    setNotice({
      error: false,
      text:
        kind === 'archive'
          ? 'Moved to The Crypt. Your original is kept safe and can be resurrected any time.'
          : 'Resurrected. The screenshot is back in your Workspace.',
    });
    if (session !== null && detailRef.current?.session === session) {
      setDetail(null);
      setDrawerMsg(null);
      requestAnimationFrame(() => document.getElementById('sg-main')?.focus());
    }
    void refreshGallery();
  };

  /**
   * Ask the server what the screenshot's state really is. Never sends a POST.
   * Only a read that is still authoritative may decide anything. An unchanged
   * state is NOT proof that the earlier request failed: it may still commit, so
   * the action stays blocked until a read shows the expected state.
   */
  const reconcile = async (id: number, kind: Lifecycle, session: number | null) => {
    const live = captureLive();
    if (!live()) return;
    const base = uncertainRef.current[id] ?? { id, kind, session, checking: true, note: '' };
    putUncertain(id, { ...base, checking: true });
    const { result, ticket } = await refreshGallery(STATUS_CHECK_TIMEOUT_MS);
    // No await below this line: the liveness and freshness checks and the
    // decision that follows them run in one synchronous step.
    if (!live()) return;
    if (!result.ok) {
      const timedOut = /timed out/i.test(result.error);
      putUncertain(id, {
        ...base,
        checking: false,
        note: timedOut
          ? ' The status check timed out. Check status again in a moment.'
          : ' The status check also failed. Check status again in a moment.',
      });
      return;
    }
    if (!isCurrent(ticket)) {
      // Superseded: either never applied, or a newer read / confirmed change was
      // applied after it. It must not decide archive or restore outcomes.
      putUncertain(id, { ...base, checking: false, note: ' A newer refresh replaced this check. Check status again.' });
      return;
    }
    const current = result.screenshots.find((s) => s.screenshotId === id);
    if (!current) {
      putUncertain(id, null);
      setNotice({ error: true, text: `${LABEL[kind]} could not be confirmed, and the screenshot is no longer in your workspace.` });
      return;
    }
    if (current.state === EXPECTED[kind]) {
      finishConfirmed(id, kind, session);
      return;
    }
    putUncertain(id, {
      ...base,
      checking: false,
      note: ' The server still shows the previous state, but the request may complete later. Check status again, or reload the page to start from the server\'s current state.',
    });
  };

  // Safe Burial / Resurrection. The server is the only thing that moves a
  // screenshot between views; nothing is moved optimistically. One request at a
  // time, and no new request for a screenshot whose last outcome is uncertain.
  const runLifecycle = async (id: number, kind: Lifecycle, session: number | null) => {
    if (lifecycleInFlight.current || uncertainRef.current[id]) return;
    const live = captureLive();
    if (!live()) return;
    lifecycleInFlight.current = true;
    setPending({ id, kind, session });
    setNotice(null);
    setDrawerMsg(null);
    // postLifecycle never rejects: errors, aborts and timeouts resolve as 'unknown'.
    const response = await postLifecycle(inflight.current, id, kind);
    // Unmounted while waiting (the cleanup aborted this POST): stop here. No
    // state updates, no reconciliation, no further requests.
    if (!live()) return;
    lifecycleInFlight.current = false;
    setPending(null);
    const verdict: Verdict = judge(response, id, kind);
    if (verdict.t === 'confirmed') {
      finishConfirmed(id, kind, session);
    } else if (verdict.t === 'failed') {
      report(session, verdict.text, true);
      void refreshGallery();
    } else {
      putUncertain(id, { id, kind, session, checking: true, note: '' });
      await reconcile(id, kind, session);
    }
  };

  const archived = view === 'crypt';
  const screenshots = gallery.phase === 'ready' ? gallery.screenshots : [];
  const detailRecord = detail ? screenshots.find((item) => String(item.screenshotId) === detail.id) : undefined;
  const detailShot = detailRecord ? toShot(detailRecord) : null;
  const detailArchived = detailRecord?.state === 'archived';
  const shots = screenshots.filter((shot) => (shot.state === 'archived') === archived).map(toShot);
  const titleOf = (id: number) => {
    const record = screenshots.find((s) => s.screenshotId === id);
    return record ? toShot(record).title : `Screenshot #${id}`;
  };

  const uncertainNotice = (u: Uncertain) => (
    <div role="status" className="sg-uncertain" key={u.id}>
      <p>
        {u.checking
          ? `Checking whether ${LABEL[u.kind]} completed for ${titleOf(u.id)}…`
          : `We could not confirm whether ${LABEL[u.kind]} completed for ${titleOf(u.id)}.${u.note}`}
      </p>
      <Button size="sm" disabled={u.checking} onClick={() => void reconcile(u.id, u.kind, u.session)}>
        Check status
      </Button>
    </div>
  );
  const detailId = detail ? Number(detail.id) : null;
  const offDrawerUncertain = Object.values(uncertain).filter((u) => u.id !== detailId);
  const pendingId = pending?.id ?? null;

  return (
    <div className="sg" data-testid="live-workspace">
      <a className="sg-skip" href="#sg-main">Skip to content</a>
      <AppHeader modeLabel="Private workspace" demo={false} />
      <div className="sg-layout" data-assistant="collapsed">
        <div className="sg-col">
          <NavTabs
            active={view}
            onNavigate={(next) => {
              setView(next);
              setNotice(null);
            }}
            cryptCount={screenshots.filter((shot) => shot.state === 'archived').length}
            pendingCount={0}
            views={['workspace', 'crypt']}
          />
          <main id="sg-main" className="sg-panel" tabIndex={-1} aria-busy={gallery.phase === 'loading'}>
            {archived ? (
              <div className="sg-viewhead">
                <h2 className="sg-display sg-wshead__title">The Crypt</h2>
                <p className="sg-wshead__sub">Archived screenshots rest here. Resurrect any of them at any time.</p>
                <p className="sg-note">{CRYPT_GALLERY_NOTE}</p>
              </div>
            ) : (
              <WorkspaceHeading demo={false} />
            )}
            {gallery.phase === 'ready' && !archived && (
              <UploadDropZone
                onFiles={upload}
                disabled={uploading}
                filterByMime={false}
                hint="PNG, JPEG or WebP, up to 15 MB each. Files are stored in your private workspace."
              />
            )}
            {uploading && <p role="status">Uploading screenshots…</p>}
            {pending && pendingId !== detailId && (
              <p role="status">{pending.kind === 'archive' ? 'Burying' : 'Resurrecting'} {titleOf(pending.id)}…</p>
            )}
            {offDrawerUncertain.map(uncertainNotice)}
            {notice && <p role={notice.error ? 'alert' : 'status'}>{notice.text}</p>}
            {gallery.phase === 'loading' && <p role="status">Loading your workspace…</p>}
            {gallery.phase === 'error' && (
              <div className="sg-empty">
                <p role="alert">{gallery.message}</p>
                <Button onClick={() => {
                  setGallery({ phase: 'loading' });
                  setAttempt((value) => value + 1);
                }}>Try again</Button>
              </div>
            )}
            {gallery.phase === 'ready' && (
              <ScreenshotGallery
                shots={shots}
                onOpen={(id, opener) => {
                  setDrawerMsg(null);
                  setDetail({ id, opener, session: ++sessionCounter.current });
                }}
                renderExtra={
                  archived
                    ? (shot) => {
                        const id = Number(shot.id);
                        const busy = pendingId === id;
                        return (
                          <div className="sg-shot__foot">
                            <Button
                              size="sm"
                              icon="restore"
                              disabled={pending !== null || Boolean(uncertain[id])}
                              aria-busy={busy}
                              aria-label={`Resurrection for ${shot.title}`}
                              onClick={() => void runLifecycle(id, 'restore', null)}
                            >
                              {busy ? 'Resurrecting…' : 'Resurrection'}
                            </Button>
                          </div>
                        );
                      }
                    : undefined
                }
                emptyMessage={archived ? 'The Crypt is empty.' : 'No screenshots yet. Upload one to get started.'}
              />
            )}
          </main>
        </div>
        <aside className="sg-side ui-card sg-live-links" aria-label="Other workflows">
          <h2 className="sg-display">More workflows</h2>
          <p>Use the classic workspace for Revive and Study Notes.</p>
          <a className="sg-link" href="?view=classic">Open classic workspace</a>
          <a className="sg-link" href="?demo=1">Explore the visual demo</a>
        </aside>
      </div>
      {detail && detailShot && (
        <Drawer
          key={detail.session}
          title={detailShot.title}
          opener={detail.opener}
          onClose={() => setDetail(null)}
          footer={
            <div className="sg-drawer__actions">
              <Button
                icon={detailArchived ? 'restore' : 'box'}
                disabled={pending !== null || Boolean(uncertain[Number(detailShot.id)])}
                aria-busy={pendingId === Number(detailShot.id)}
                onClick={() =>
                  void runLifecycle(Number(detailShot.id), detailArchived ? 'restore' : 'archive', detail.session)
                }
              >
                {pendingId === Number(detailShot.id)
                  ? detailArchived ? 'Resurrecting…' : 'Burying…'
                  : detailArchived ? 'Resurrection' : 'Safe Burial'}
              </Button>
              <p className="sg-note">{CRYPT_GALLERY_NOTE}</p>
            </div>
          }
        >
          <div className="sg-drawer__art">
            <ShotArt kind="image" src={detailShot.imageSrc} alt={`Preview of ${detailShot.title}`} />
          </div>
          <dl className="sg-facts">
            <dt>Saved</dt><dd>{detailShot.savedLabel}</dd>
            <dt>Size</dt><dd>{detailShot.sizeLabel}</dd>
            <dt>Status</dt><dd>{detailShot.statusLabel}</dd>
          </dl>
          {pending && pendingId === Number(detailShot.id) && (
            <p role="status">{pending.kind === 'archive' ? 'Burying…' : 'Resurrecting…'} You can close this panel; the request keeps going.</p>
          )}
          {uncertain[Number(detailShot.id)] && uncertainNotice(uncertain[Number(detailShot.id)])}
          {drawerMsg && drawerMsg.session === detail.session && (
            <p role="alert" className="sg-form-error">{drawerMsg.text}</p>
          )}
        </Drawer>
      )}
    </div>
  );
}
