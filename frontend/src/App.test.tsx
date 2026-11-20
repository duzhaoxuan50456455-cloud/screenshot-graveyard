import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import type { HealthResult, ListResult, WorkspaceResult } from './api/client';

const client = vi.hoisted(() => ({
  getWorkspace: vi.fn<() => Promise<WorkspaceResult>>(),
  listScreenshots: vi.fn<() => Promise<ListResult>>(),
  uploadScreenshots: vi.fn(),
  archiveScreenshot: vi.fn(),
  restoreScreenshot: vi.fn(),
}));

vi.mock('./api/client', () => ({
  getHealth: (): Promise<HealthResult> => new Promise<HealthResult>(() => {}),
  ...client,
  imageUrl: (id: number) => `/api/screenshots/${id}/image`,
}));

// The AI panels call ./api/ai on mount (read-only). Keep these tests off the network.
vi.mock('./api/ai', () => ({
  listStudyNotes: () => Promise.resolve({ ok: true, data: [] }),
  getProviderMode: () => Promise.resolve({ ok: true, data: 'fake' }),
  getConsent: () => Promise.resolve({ ok: true, data: false }),
  getAnalysis: () => Promise.resolve({ ok: false, status: 404, code: 'not_found', message: 'Not Found' }),
  setConsent: vi.fn(),
  classifyScreenshot: vi.fn(),
  getOperation: vi.fn(),
  editAnalysis: vi.fn(),
  generateStudyNote: vi.fn(),
  getStudyNote: vi.fn(),
  listStudyNoteVersions: vi.fn(),
  saveStudyNoteEdit: vi.fn(),
  studyNoteExportUrl: () => '/export',
  CATEGORIES: [],
}));

import App from './App';
import type { Screenshot } from './api/client';
import { StrictMode } from 'react';

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState({}, '', '/');
  client.getWorkspace.mockResolvedValue({ ok: true, active: true });
  client.listScreenshots.mockResolvedValue({ ok: true, screenshots: [] });
});

const screenshot: Screenshot = {
  screenshotId: 42, state: 'active', mimeType: 'image/png', originalFilename: 'real.png',
  isDuplicate: false, duplicateOf: null, byteSize: 512, uploadedAt: '2026-11-19T20:00:00Z',
  analysis: null,
};

describe('App', () => {
  it('renders the app title', async () => {
    render(<App />);
    expect(
      screen.getByRole('heading', { name: /screenshot graveyard/i, level: 1 }),
    ).toBeInTheDocument();
    await screen.findByText(/No screenshots yet/);
  });

  it('renders visible tagline content', async () => {
    render(<App />);
    expect(
      screen.getByText(/finish their unfinished business/i),
    ).toBeInTheDocument();
    await screen.findByText(/No screenshots yet/);
  });

  it('shows loading, then the real empty workspace', async () => {
    render(<App />);
    expect(screen.getByText('Loading your workspace…')).toBeInTheDocument();
    expect(await screen.findByText(/No screenshots yet/)).toBeInTheDocument();
  });
});

describe('App mode selection', () => {
  it('bootstraps before listing, once in Strict Mode, and keeps the classic route discoverable', async () => {
    render(<StrictMode><App /></StrictMode>);
    await screen.findByText(/No screenshots yet/);
    expect(screen.getByTestId('live-workspace')).toBeInTheDocument();
    expect(screen.queryByTestId('demo-app')).not.toBeInTheDocument();
    expect(client.getWorkspace).toHaveBeenCalledTimes(1);
    expect(client.getWorkspace.mock.invocationCallOrder[0]).toBeLessThan(client.listScreenshots.mock.invocationCallOrder[0]);
    expect(screen.getByRole('link', { name: 'Open classic workspace' })).toHaveAttribute('href', '?view=classic');
  });

  it('renders and uses the demo shell without backend calls for ?demo=1', async () => {
    window.history.pushState({}, '', '/?demo=1');
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    try {
      render(<App />);
      expect(await screen.findByTestId('demo-app')).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(client.getWorkspace).not.toHaveBeenCalled();
      expect(client.listScreenshots).not.toHaveBeenCalled();
      expect(client.uploadScreenshots).not.toHaveBeenCalled();
    } finally {
      fetchSpy.mockRestore();
      window.history.pushState({}, '', '/');
    }
  });

  it('keeps the classic workflows available', async () => {
    window.history.replaceState({}, '', '/?view=classic');
    render(<App />);
    expect(screen.getByRole('link', { name: 'Back to the new Workspace' })).toBeInTheDocument();
    expect(await screen.findByRole('region', { name: /ghosts/i })).toBeInTheDocument();
  });

  it('uses real archived records in the Crypt and does not treat demo=0 as demo', async () => {
    window.history.replaceState({}, '', '/?demo=0');
    client.listScreenshots.mockResolvedValue({
      ok: true, screenshots: [screenshot, { ...screenshot, screenshotId: 43, originalFilename: 'buried.png', state: 'archived' }],
    });
    render(<App />);
    expect(await screen.findByRole('article', { name: 'real.png' })).toBeInTheDocument();
    expect(screen.queryByRole('article', { name: 'buried.png' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /The Crypt/ }));
    expect(screen.getByRole('article', { name: 'buried.png' })).toHaveTextContent('Archived');
    expect(screen.queryByRole('article', { name: 'real.png' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resurrection for buried.png' })).toBeEnabled();
    expect(screen.getByText(/does not remove the photo from your phone gallery/)).toBeInTheDocument();
  });

  describe('Safe Burial and Resurrection', () => {
    const buried = { ...screenshot, state: 'archived' as const };
    const openDrawer = async () => {
      render(<App />);
      fireEvent.click(await screen.findByRole('button', { name: 'Open details for real.png' }));
      return screen.getByRole('button', { name: 'Safe Burial' });
    };

    it('moves to The Crypt only after server confirmation, once, and re-syncs from the list', async () => {
      client.listScreenshots.mockResolvedValue({ ok: true, screenshots: [screenshot] });
      let finish!: (r: Awaited<ReturnType<typeof client.archiveScreenshot>>) => void;
      client.archiveScreenshot.mockReturnValue(new Promise((r) => { finish = r; }));
      const button = await openDrawer();
      fireEvent.click(button);
      fireEvent.click(button);
      expect(client.archiveScreenshot).toHaveBeenCalledTimes(1);
      expect(client.archiveScreenshot).toHaveBeenCalledWith(42, expect.any(Function));
      expect(button).toBeDisabled();
      expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
      client.listScreenshots.mockResolvedValue({ ok: true, screenshots: [buried] });
      finish({ ok: true, screenshotId: 42, status: 'successful', state: 'archived' });
      await waitFor(() => expect(screen.queryByRole('article', { name: 'real.png' })).not.toBeInTheDocument());
      expect(screen.getByText(/Moved to The Crypt/)).toBeInTheDocument();
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: /The Crypt/ }));
      expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
    });

    it('keeps the screenshot in place and shows an error when archiving fails', async () => {
      client.listScreenshots.mockResolvedValue({ ok: true, screenshots: [screenshot] });
      client.archiveScreenshot.mockResolvedValue({ ok: false, error: 'HTTP 404', outcome: 'rejected' });
      fireEvent.click(await openDrawer());
      expect(await screen.findByRole('alert')).toHaveTextContent('Safe Burial did not complete (HTTP 404). Nothing was changed.');
      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Safe Burial' })).toBeEnabled();
    });

    it('does not move a screenshot when the server reports a non-successful archive', async () => {
      client.listScreenshots.mockResolvedValue({ ok: true, screenshots: [screenshot] });
      client.archiveScreenshot.mockResolvedValue({ ok: true, screenshotId: 42, status: 'failed', state: 'active' });
      fireEvent.click(await openDrawer());
      expect(await screen.findByRole('alert')).toHaveTextContent('stayed in your Workspace');
      expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
    });

    it('resurrects from The Crypt after confirmation and reports a failed integrity check honestly', async () => {
      window.history.replaceState({}, '', '/');
      client.listScreenshots.mockResolvedValue({ ok: true, screenshots: [buried] });
      client.restoreScreenshot.mockResolvedValueOnce({ ok: true, screenshotId: 42, status: 'failed', state: 'archived' });
      render(<App />);
      fireEvent.click(await screen.findByRole('button', { name: /The Crypt/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Resurrection for real.png' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('integrity check');
      expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
      client.restoreScreenshot.mockResolvedValueOnce({ ok: true, screenshotId: 42, status: 'successful', state: 'active' });
      client.listScreenshots.mockResolvedValue({ ok: true, screenshots: [screenshot] });
      fireEvent.click(screen.getByRole('button', { name: 'Resurrection for real.png' }));
      await waitFor(() => expect(screen.queryByRole('article', { name: 'real.png' })).not.toBeInTheDocument());
      expect(client.restoreScreenshot).toHaveBeenCalledTimes(2);
      fireEvent.click(screen.getByRole('button', { name: /Workspace/ }));
      expect(await screen.findByRole('article', { name: 'real.png' })).toBeInTheDocument();
    });
  });

  it('reloads real uploads from the server and uses authorized same-origin previews', async () => {
    client.uploadScreenshots.mockResolvedValueOnce({
      ok: true, accepted: 1, rejected: 1,
      results: [{ filename: 'bad.png', status: 'rejected', error: 'Not a valid image' }],
    });
    const initial = render(<App />);
    await screen.findByText(/No screenshots yet/);
    client.listScreenshots.mockResolvedValue({ ok: true, screenshots: [screenshot] });
    const file = new File(['bytes'], 'real.png');
    fireEvent.change(screen.getByLabelText('Choose screenshot files'), { target: { files: [file] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Saved 1 screenshot. bad.png: Not a valid image');
    expect(client.uploadScreenshots).toHaveBeenCalledWith([file]);
    expect(await screen.findByRole('article', { name: 'real.png' })).toBeInTheDocument();
    expect(initial.container.querySelector('img')).toHaveAttribute('src', '/api/screenshots/42/image');
    fireEvent.click(screen.getByRole('button', { name: 'Open details for real.png' }));
    expect(screen.getByRole('dialog', { name: 'real.png' })).toBeInTheDocument();
    initial.unmount();

    const reopened = render(<App />);
    expect(await screen.findByRole('article', { name: 'real.png' })).toBeInTheDocument();
    fireEvent.error(reopened.container.querySelector('img')!);
    expect(screen.getByText('Preview unavailable')).toBeInTheDocument();
  });

  it('fails closed on workspace initialization and retries listing errors', async () => {
    client.getWorkspace.mockResolvedValueOnce({ ok: false, error: 'Unavailable' });
    client.listScreenshots.mockResolvedValueOnce({ ok: false, error: 'List unavailable' });
    render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not start your workspace.');
    expect(client.listScreenshots).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Browse' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not load screenshots.'));
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('button', { name: 'Browse' })).toBeInTheDocument();
  });

  describe('ambiguous outcomes, stale refreshes and drawer lifetime', () => {
    const other = { ...screenshot, screenshotId: 43, originalFilename: 'second.png' };
    const asArchived = (shot: Screenshot): Screenshot => ({ ...shot, state: 'archived' });
    type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void };
    const deferred = <T,>(): Deferred<T> => {
      let resolve!: (value: T) => void;
      const promise = new Promise<T>((r) => { resolve = r; });
      return { promise, resolve };
    };
    const ok = (screenshots: Screenshot[]): ListResult => ({ ok: true, screenshots });
    const burial = () => screen.getByRole('button', { name: 'Safe Burial' });
    const openFor = async (name: string) => {
      fireEvent.click(await screen.findByRole('button', { name: `Open details for ${name}` }));
    };

    it('reconciles a lost archive response against server state and does not retry the POST', async () => {
      client.listScreenshots.mockResolvedValueOnce(ok([screenshot])).mockResolvedValue(ok([asArchived(screenshot)]));
      client.archiveScreenshot.mockResolvedValue({ ok: false, error: 'Network request failed', outcome: 'unknown' });
      render(<App />);
      await openFor('real.png');
      fireEvent.click(burial());
      expect(await screen.findByText(/Moved to The Crypt/)).toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(screen.queryByRole('article', { name: 'real.png' })).not.toBeInTheDocument();
      expect(client.archiveScreenshot).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole('button', { name: /The Crypt/ }));
      expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
    });

    it('treats malformed JSON after a committed restore as unknown, not as an integrity failure', async () => {
      client.listScreenshots.mockResolvedValueOnce(ok([asArchived(screenshot)])).mockResolvedValue(ok([screenshot]));
      client.restoreScreenshot.mockResolvedValue({ ok: false, error: 'Invalid JSON response', outcome: 'unknown' });
      render(<App />);
      fireEvent.click(await screen.findByRole('button', { name: /The Crypt/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Resurrection for real.png' }));
      expect(await screen.findByText(/Resurrected\./)).toBeInTheDocument();
      expect(document.body).not.toHaveTextContent(/integrity/i);
      expect(client.restoreScreenshot).toHaveBeenCalledTimes(1);
    });

    it('treats a mismatched response identity as uncertain, blocks repeats, and recovers on a manual check', async () => {
      client.listScreenshots.mockResolvedValue(ok([screenshot, other]));
      client.archiveScreenshot.mockResolvedValue({ ok: true, screenshotId: 43, status: 'successful', state: 'archived' });
      render(<App />);
      await openFor('real.png');
      fireEvent.click(burial());
      const status = await screen.findByText(/We could not confirm whether Safe Burial completed for real\.png/);
      expect(status).toBeInTheDocument();
      expect(document.body).not.toHaveTextContent(/integrity|did not complete/i);
      expect(burial()).toBeDisabled();
      expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
      // Server says it did commit after all.
      client.listScreenshots.mockResolvedValue(ok([asArchived(screenshot), other]));
      fireEvent.click(screen.getByRole('button', { name: 'Check status' }));
      expect(await screen.findByText(/Moved to The Crypt/)).toBeInTheDocument();
      expect(client.archiveScreenshot).toHaveBeenCalledTimes(1);
    });

    it('stays uncertain, not failed, when the reconciliation read also fails', async () => {
      client.listScreenshots.mockResolvedValueOnce(ok([screenshot])).mockResolvedValue({ ok: false, error: 'HTTP 503' });
      client.archiveScreenshot.mockResolvedValue({ ok: false, error: 'HTTP 503', outcome: 'unknown' });
      render(<App />);
      await openFor('real.png');
      fireEvent.click(burial());
      expect(await screen.findByText(/status check also failed/)).toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(burial()).toBeDisabled();
      expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
    });

    it('stops waiting after the bounded timeout and reconciles instead of reporting failure', async () => {
      client.listScreenshots.mockResolvedValueOnce(ok([screenshot])).mockResolvedValue(ok([asArchived(screenshot)]));
      client.archiveScreenshot.mockReturnValue(new Promise(() => {}));
      render(<App />);
      await openFor('real.png');
      vi.useFakeTimers();
      try {
        fireEvent.click(burial());
        expect(screen.getByRole('button', { name: 'Burying…' })).toBeDisabled();
        await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
        expect(screen.getByText(/Moved to The Crypt/)).toBeInTheDocument();
        expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
      expect(client.archiveScreenshot).toHaveBeenCalledTimes(1);
    });

    it('keeps the action blocked when checks show the old state, then confirms a POST that commits late', async () => {
      let committed = false;
      client.listScreenshots.mockImplementation(() => Promise.resolve(ok([committed ? asArchived(screenshot) : screenshot])));
      client.archiveScreenshot.mockReturnValue(new Promise(() => {})); // never answers
      render(<App />);
      await openFor('real.png');
      vi.useFakeTimers();
      try {
        fireEvent.click(burial());
        await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
        expect(screen.getByText(/may complete later/)).toBeInTheDocument();
        // Two further checks still show the old state: nothing authorizes another POST.
        for (let n = 0; n < 2; n += 1) {
          await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Check status' })); });
          expect(screen.getByText(/may complete later/)).toBeInTheDocument();
          expect(burial()).toBeDisabled();
          expect(screen.getByRole('button', { name: 'Check status' })).toBeEnabled();
        }
        expect(screen.queryByText(/Moved to The Crypt/)).not.toBeInTheDocument();
        // The server commits only now.
        committed = true;
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Check status' })); });
        expect(screen.getByText(/Moved to The Crypt/)).toBeInTheDocument();
      } finally {
        vi.useRealTimers();
      }
      expect(client.archiveScreenshot).toHaveBeenCalledTimes(1);
    });

    it.each([
      {
        name: 'archive',
        initial: [screenshot, other], target: 'real.png', neighbour: 'second.png',
        fresh: [screenshot, asArchived(other)], stale: [asArchived(screenshot), other],
      },
      {
        name: 'restore',
        initial: [asArchived(screenshot), asArchived(other)], target: 'real.png', neighbour: 'second.png',
        fresh: [asArchived(screenshot), other], stale: [screenshot, asArchived(other)],
      },
    ])('does not let a superseded reconciliation read decide the $name outcome', async ({ name, initial, target, neighbour, fresh, stale }) => {
      const isArchive = name === 'archive';
      const slow = deferred<ListResult>();
      client.listScreenshots
        .mockResolvedValueOnce(ok(initial)) // initial load
        .mockReturnValueOnce(slow.promise) // reconciliation read for `target`, held back
        .mockResolvedValue(ok(fresh)); // everything after: the newer authoritative state
      const unknown = { ok: false as const, error: 'Network request failed', outcome: 'unknown' as const };
      const confirm = (id: number) => ({ ok: true as const, screenshotId: id, status: 'successful' as const, state: isArchive ? 'archived' as const : 'active' as const });
      const call = (id: number) => (id === 42 ? Promise.resolve(unknown) : Promise.resolve(confirm(id)));
      client.archiveScreenshot.mockImplementation(call);
      client.restoreScreenshot.mockImplementation(call);
      render(<App />);
      if (isArchive) {
        await openFor(target);
        fireEvent.click(burial());
        await screen.findByText(/Checking whether Safe Burial completed/);
        fireEvent.keyDown(document.body, { key: 'Escape' });
        await openFor(neighbour);
        fireEvent.click(burial());
      } else {
        fireEvent.click(await screen.findByRole('button', { name: /The Crypt/ }));
        fireEvent.click(screen.getByRole('button', { name: `Resurrection for ${target}` }));
        await screen.findByText(/Checking whether Resurrection completed/);
        fireEvent.click(screen.getByRole('button', { name: `Resurrection for ${neighbour}` }));
      }
      await screen.findByText(isArchive ? /Moved to The Crypt/ : /Resurrected\./);
      await waitFor(() => expect(client.listScreenshots).toHaveBeenCalledTimes(3));
      // The older read finally returns, showing the OPPOSITE of the newer state.
      await act(async () => { slow.resolve(ok(stale)); });
      expect(await screen.findByText(/newer refresh replaced this check/)).toBeInTheDocument();
      // No success was decided from the stale read: the target did not move.
      if (!isArchive) fireEvent.click(screen.getByRole('button', { name: /The Crypt/ }));
      else fireEvent.click(screen.getByRole('button', { name: /Workspace/ }));
      expect(screen.getByRole('article', { name: target })).toBeInTheDocument();
      expect(screen.queryByRole('article', { name: neighbour })).not.toBeInTheDocument();
      if (!isArchive) {
        expect(screen.getByRole('button', { name: `Resurrection for ${target}` })).toBeDisabled();
      }
      expect(screen.getByRole('button', { name: 'Check status' })).toBeEnabled();
      // A fresh authoritative check is what resolves it.
      expect(isArchive ? client.archiveScreenshot : client.restoreScreenshot).toHaveBeenCalledTimes(2);
    });

    it('bounds a stalled reconciliation read, aborts it, and keeps the action blocked', async () => {
      const signals: AbortSignal[] = [];
      const realFetch = globalThis.fetch;
      globalThis.fetch = vi.fn((_u: RequestInfo | URL, init?: RequestInit) => {
        if (init?.signal) signals.push(init.signal);
        return new Promise<Response>(() => {});
      }) as unknown as typeof fetch;
      client.listScreenshots.mockImplementation((f?: typeof fetch) =>
        f ? (f('/api/screenshots') as Promise<Response>).then(() => ok([screenshot])) : Promise.resolve(ok([screenshot])));
      client.archiveScreenshot.mockResolvedValue({ ok: false, error: 'Network request failed', outcome: 'unknown' });
      try {
        render(<App />);
        await openFor('real.png');
        vi.useFakeTimers();
        try {
          fireEvent.click(burial());
          await act(async () => { await vi.advanceTimersByTimeAsync(0); });
          expect(screen.getByText(/Checking whether Safe Burial completed/)).toBeInTheDocument();
          expect(screen.getByRole('button', { name: 'Check status' })).toBeDisabled();
          await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
          expect(screen.getByText(/status check timed out/)).toBeInTheDocument();
          expect(signals).toHaveLength(1);
          expect(signals[0].aborted).toBe(true);
          expect(screen.getByRole('button', { name: 'Check status' })).toBeEnabled();
          expect(burial()).toBeDisabled();
          expect(vi.getTimerCount()).toBe(0);
        } finally {
          vi.useRealTimers();
        }
        expect(client.archiveScreenshot).toHaveBeenCalledTimes(1);
      } finally {
        globalThis.fetch = realFetch;
      }
    });

    it('aborts in-flight status reads and clears their timers on unmount', async () => {
      const signals: AbortSignal[] = [];
      const realFetch = globalThis.fetch;
      globalThis.fetch = vi.fn((_u: RequestInfo | URL, init?: RequestInit) => {
        if (init?.signal) signals.push(init.signal);
        return new Promise<Response>(() => {});
      }) as unknown as typeof fetch;
      client.listScreenshots.mockImplementation((f?: typeof fetch) =>
        f ? (f('/api/screenshots') as Promise<Response>).then(() => ok([screenshot])) : Promise.resolve(ok([screenshot])));
      client.archiveScreenshot.mockResolvedValue({ ok: false, error: 'Network request failed', outcome: 'unknown' });
      try {
        const view = render(<App />);
        await openFor('real.png');
        vi.useFakeTimers();
        try {
          fireEvent.click(burial());
          await act(async () => { await vi.advanceTimersByTimeAsync(0); });
          expect(signals).toHaveLength(1);
          view.unmount();
          expect(signals[0].aborted).toBe(true);
          expect(vi.getTimerCount()).toBe(0);
        } finally {
          vi.useRealTimers();
        }
      } finally {
        globalThis.fetch = realFetch;
      }
    });

    // Exact Codex ordering: the reconciliation GET resolves and is applied (it
    // WAS authoritative at that moment), then a newer refresh applies a different
    // state, and only then does reconciliation resume and decide.
    it.each([
      { kind: 'archive' as const, start: screenshot, stale: asArchived(screenshot), newer: screenshot },
      { kind: 'restore' as const, start: asArchived(screenshot), stale: screenshot, newer: asArchived(screenshot) },
    ])('rejects a $kind reconciliation result superseded after it was applied', async ({ kind, start, stale, newer }) => {
      const reconcileRead = deferred<ListResult>();
      const uploadRead = deferred<ListResult>();
      client.listScreenshots
        .mockResolvedValueOnce(ok([start])) // 1: initial load
        .mockReturnValueOnce(reconcileRead.promise) // 2: reconciliation read (older ticket)
        .mockReturnValueOnce(uploadRead.promise) // 3: upload refresh (newer ticket)
        .mockResolvedValue(ok([newer]));
      const unknown = { ok: false as const, error: 'Network request failed', outcome: 'unknown' as const };
      client.archiveScreenshot.mockResolvedValue(unknown);
      client.restoreScreenshot.mockResolvedValue(unknown);
      client.uploadScreenshots.mockResolvedValue({ ok: true, accepted: 1, rejected: 0, results: [] });
      render(<App />);
      if (kind === 'archive') {
        await openFor('real.png');
        fireEvent.click(burial());
        fireEvent.keyDown(document.body, { key: 'Escape' });
      } else {
        fireEvent.click(await screen.findByRole('button', { name: /The Crypt/ }));
        fireEvent.click(screen.getByRole('button', { name: 'Resurrection for real.png' }));
        fireEvent.click(screen.getByRole('button', { name: /Workspace/ }));
      }
      await waitFor(() => expect(client.listScreenshots).toHaveBeenCalledTimes(2));
      fireEvent.change(screen.getByLabelText('Choose screenshot files'), { target: { files: [new File(['x'], 'n.png')] } });
      await waitFor(() => expect(client.listScreenshots).toHaveBeenCalledTimes(3));
      // Registered after the component's own reaction to the reconciliation read,
      // so the newer refresh is released between "applied" and "reconcile resumes".
      void reconcileRead.promise.then(() => uploadRead.resolve(ok([newer])));
      await act(async () => { reconcileRead.resolve(ok([stale])); });
      await screen.findByText(/Saved 1 screenshot/);
      const label = kind === 'archive' ? 'Safe Burial' : 'Resurrection';
      // The stale read must not have resolved the uncertainty (success path), so
      // the action is still blocked against a duplicate POST.
      if (kind === 'archive') {
        expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Open details for real.png' }));
        expect(burial()).toBeDisabled();
      } else {
        expect(screen.queryByRole('article', { name: 'real.png' })).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: /The Crypt/ }));
        expect(screen.getByRole('button', { name: 'Resurrection for real.png' })).toBeDisabled();
      }
      expect(screen.getByText(new RegExp(`could not confirm whether ${label} completed`, 'i'))).toBeInTheDocument();
      expect(screen.getByText(/newer refresh replaced this check/)).toBeInTheDocument();
      expect(screen.queryByText(kind === 'archive' ? /Moved to The Crypt/ : /Resurrected\./)).not.toBeInTheDocument();
      expect(kind === 'archive' ? client.archiveScreenshot : client.restoreScreenshot).toHaveBeenCalledTimes(1);
    });

    it.each(['archive', 'restore'] as const)(
      'does nothing after unmount when a pending %s POST is aborted',
      async (kind) => {
        const signals: AbortSignal[] = [];
        const realFetch = globalThis.fetch;
        // A POST that only settles when aborted, as the browser does.
        globalThis.fetch = vi.fn((_u: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            if (init?.signal) {
              signals.push(init.signal);
              init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
            }
          })) as unknown as typeof fetch;
        const viaFetch = (path: string) => (_id: number, f?: typeof fetch) =>
          (f ?? fetch)(path, { method: 'POST' }).then(
            () => ({ ok: true as const, screenshotId: 42, status: 'successful' as const, state: 'archived' as const }),
            () => ({ ok: false as const, error: 'Request timed out', outcome: 'unknown' as const }),
          );
        client.archiveScreenshot.mockImplementation(viaFetch('/api/screenshots/42/archive'));
        client.restoreScreenshot.mockImplementation(viaFetch('/api/screenshots/42/restore'));
        client.listScreenshots.mockResolvedValue(ok([kind === 'archive' ? screenshot : asArchived(screenshot)]));
        const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
        try {
          const view = render(<App />);
          if (kind === 'archive') {
            await openFor('real.png');
            fireEvent.click(burial());
          } else {
            fireEvent.click(await screen.findByRole('button', { name: /The Crypt/ }));
            fireEvent.click(screen.getByRole('button', { name: 'Resurrection for real.png' }));
          }
          await waitFor(() => expect(signals).toHaveLength(1));
          const readsBefore = client.listScreenshots.mock.calls.length;
          vi.useFakeTimers();
          try {
            view.unmount();
            expect(signals[0].aborted).toBe(true);
            // Let the aborted POST settle and every continuation run.
            await vi.advanceTimersByTimeAsync(20_000);
            expect(client.listScreenshots).toHaveBeenCalledTimes(readsBefore);
            expect(vi.getTimerCount()).toBe(0);
          } finally {
            vi.useRealTimers();
          }
          expect(errors).not.toHaveBeenCalled();
        } finally {
          errors.mockRestore();
          globalThis.fetch = realFetch;
        }
      },
    );

    it.each([
      { kind: 'archive' as const, label: 'successful', reply: { ok: true as const, screenshotId: 42, status: 'successful' as const, state: 'archived' as const } },
      { kind: 'archive' as const, label: 'failed', reply: { ok: true as const, screenshotId: 42, status: 'failed' as const, state: 'active' as const } },
      { kind: 'restore' as const, label: 'successful', reply: { ok: true as const, screenshotId: 42, status: 'successful' as const, state: 'active' as const } },
      { kind: 'restore' as const, label: 'rejected', reply: { ok: false as const, error: 'HTTP 404', outcome: 'rejected' as const } },
    ])('sends no follow-up request when a $kind POST that ignored the abort settles after unmount ($label)', async ({ kind, reply }) => {
      const post = deferred<typeof reply>();
      client.archiveScreenshot.mockReturnValue(post.promise);
      client.restoreScreenshot.mockReturnValue(post.promise);
      client.listScreenshots.mockResolvedValue(ok([kind === 'archive' ? screenshot : asArchived(screenshot)]));
      const view = render(<App />);
      if (kind === 'archive') {
        await openFor('real.png');
        fireEvent.click(burial());
      } else {
        fireEvent.click(await screen.findByRole('button', { name: /The Crypt/ }));
        fireEvent.click(screen.getByRole('button', { name: 'Resurrection for real.png' }));
      }
      const readsBefore = client.listScreenshots.mock.calls.length;
      view.unmount();
      await act(async () => { post.resolve(reply); });
      await new Promise((r) => setTimeout(r, 0));
      expect(client.listScreenshots).toHaveBeenCalledTimes(readsBefore);
    });

    it('never lets a delayed upload refresh overwrite newer archive state', async () => {
      const delayed = deferred<ListResult>();
      client.listScreenshots
        .mockResolvedValueOnce(ok([screenshot]))
        .mockReturnValueOnce(delayed.promise)
        .mockResolvedValue(ok([asArchived(screenshot)]));
      client.uploadScreenshots.mockResolvedValue({ ok: true, accepted: 1, rejected: 0, results: [] });
      client.archiveScreenshot.mockResolvedValue({ ok: true, screenshotId: 42, status: 'successful', state: 'archived' });
      render(<App />);
      await openFor('real.png');
      // The upload's refresh is issued first and held back.
      fireEvent.change(screen.getByLabelText('Choose screenshot files'), { target: { files: [new File(['x'], 'new.png')] } });
      await waitFor(() => expect(client.listScreenshots).toHaveBeenCalledTimes(2));
      fireEvent.click(burial());
      await screen.findByText(/Moved to The Crypt/);
      await waitFor(() => expect(client.listScreenshots).toHaveBeenCalledTimes(3));
      expect(screen.queryByRole('article', { name: 'real.png' })).not.toBeInTheDocument();
      // Now the older response arrives, still showing the screenshot as active.
      await act(async () => { delayed.resolve(ok([screenshot])); });
      await screen.findByText(/Saved 1 screenshot/);
      expect(screen.queryByRole('article', { name: 'real.png' })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: /The Crypt/ }));
      expect(screen.getByRole('article', { name: 'real.png' })).toBeInTheDocument();
    });

    it('lets Escape and Close dismiss the drawer during a pending action without a second request', async () => {
      const post = deferred<Awaited<ReturnType<typeof client.archiveScreenshot>>>();
      client.listScreenshots.mockResolvedValueOnce(ok([screenshot])).mockResolvedValue(ok([asArchived(screenshot)]));
      client.archiveScreenshot.mockReturnValue(post.promise);
      render(<App />);
      await openFor('real.png');
      const opener = screen.getByRole('button', { name: 'Open details for real.png' });
      fireEvent.click(burial());
      expect(screen.queryByRole('button', { name: 'Safe Burial' })).not.toBeInTheDocument(); // now "Burying…"
      fireEvent.keyDown(document.body, { key: 'Escape' });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(opener).toHaveFocus();
      expect(await screen.findByText(/Burying real\.png/)).toBeInTheDocument();
      // Reopen and use the Close button while still pending.
      fireEvent.click(opener);
      expect(screen.getByRole('button', { name: 'Burying…' })).toBeDisabled();
      fireEvent.click(screen.getByRole('button', { name: 'Close details' }));
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(client.archiveScreenshot).toHaveBeenCalledTimes(1);
      await act(async () => { post.resolve({ ok: true, screenshotId: 42, status: 'successful', state: 'archived' }); });
      expect(await screen.findByText(/Moved to The Crypt/)).toBeInTheDocument();
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });

    it('does not close a different screenshot\'s drawer when an older action completes late', async () => {
      const post = deferred<Awaited<ReturnType<typeof client.archiveScreenshot>>>();
      client.listScreenshots.mockResolvedValueOnce(ok([screenshot, other]))
        .mockResolvedValue(ok([asArchived(screenshot), other]));
      client.archiveScreenshot.mockReturnValue(post.promise);
      render(<App />);
      await openFor('real.png');
      fireEvent.click(burial());
      fireEvent.keyDown(document.body, { key: 'Escape' });
      await openFor('second.png');
      expect(screen.getByRole('dialog', { name: 'second.png' })).toBeInTheDocument();
      await act(async () => { post.resolve({ ok: true, screenshotId: 42, status: 'successful', state: 'archived' }); });
      expect(await screen.findByText(/Moved to The Crypt/)).toBeInTheDocument();
      const dialog = screen.getByRole('dialog', { name: 'second.png' });
      expect(within(dialog).getByRole('button', { name: 'Safe Burial' })).toBeEnabled();
      expect(screen.queryByRole('article', { name: 'real.png' })).not.toBeInTheDocument();
    });
  });
});
