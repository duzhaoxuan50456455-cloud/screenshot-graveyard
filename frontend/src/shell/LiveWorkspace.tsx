import '@fontsource-variable/inter';
import '@fontsource/cormorant-garamond/600.css';
import '@fontsource/cormorant-garamond/700.css';
import '../ui/theme.css';
import '../ui/components.css';
import './shell.css';
import { useEffect, useRef, useState } from 'react';
import { getWorkspace, imageUrl, listScreenshots, uploadScreenshots, type ListResult, type Screenshot } from '../api/client';
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
  const [detail, setDetail] = useState<{ shot: ShotVM; opener: HTMLElement } | null>(null);
  const uploadInFlight = useRef(false);

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
      const result = await listScreenshots();
      if (active) setGallery(galleryState(result));
    });
    return () => { active = false; };
  }, [attempt]);

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
      setNotice({ error: rejected.length > 0, text: [saved, ...rejected].join(' ') });
      if (result.accepted > 0) setGallery(galleryState(await listScreenshots()));
    } finally {
      uploadInFlight.current = false;
      setUploading(false);
    }
  };

  const archived = view === 'crypt';
  const screenshots = gallery.phase === 'ready' ? gallery.screenshots : [];
  const shots = screenshots.filter((shot) => (shot.state === 'archived') === archived).map(toShot);

  return (
    <div className="sg" data-testid="live-workspace">
      <a className="sg-skip" href="#sg-main">Skip to content</a>
      <AppHeader modeLabel="Private workspace" demo={false} />
      <div className="sg-layout" data-assistant="collapsed">
        <div className="sg-col">
          <NavTabs
            active={view}
            onNavigate={setView}
            cryptCount={screenshots.filter((shot) => shot.state === 'archived').length}
            pendingCount={0}
            views={['workspace', 'crypt']}
          />
          <main id="sg-main" className="sg-panel" tabIndex={-1} aria-busy={gallery.phase === 'loading'}>
            {archived ? <h2 className="sg-display sg-wshead__title">The Crypt</h2> : <WorkspaceHeading demo={false} />}
            {gallery.phase === 'ready' && !archived && (
              <UploadDropZone
                onFiles={upload}
                disabled={uploading}
                filterByMime={false}
                hint="PNG, JPEG or WebP, up to 15 MB each. Files are stored in your private workspace."
              />
            )}
            {uploading && <p role="status">Uploading screenshots…</p>}
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
                  const shot = shots.find((item) => item.id === id);
                  if (shot) setDetail({ shot, opener });
                }}
                emptyMessage={archived ? 'The Crypt is empty.' : 'No screenshots yet. Upload one to get started.'}
              />
            )}
          </main>
        </div>
        <aside className="sg-side ui-card sg-live-links" aria-label="Other workflows">
          <h2 className="sg-display">More workflows</h2>
          <p>Use the classic workspace for Revive, Study Notes, Safe Burial and restore.</p>
          <a className="sg-link" href="?view=classic">Open classic workspace</a>
          <a className="sg-link" href="?demo=1">Explore the visual demo</a>
        </aside>
      </div>
      {detail && (
        <Drawer title={detail.shot.title} opener={detail.opener} onClose={() => setDetail(null)}>
          <div className="sg-drawer__art">
            <ShotArt kind="image" src={detail.shot.imageSrc} alt={`Preview of ${detail.shot.title}`} />
          </div>
          <dl className="sg-facts">
            <dt>Saved</dt><dd>{detail.shot.savedLabel}</dd>
            <dt>Size</dt><dd>{detail.shot.sizeLabel}</dd>
            <dt>Status</dt><dd>{detail.shot.statusLabel}</dd>
          </dl>
        </Drawer>
      )}
    </div>
  );
}
