import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import type { HealthResult, ListResult, WorkspaceResult } from './api/client';

const client = vi.hoisted(() => ({
  getWorkspace: vi.fn<() => Promise<WorkspaceResult>>(),
  listScreenshots: vi.fn<() => Promise<ListResult>>(),
  uploadScreenshots: vi.fn(),
}));

vi.mock('./api/client', () => ({
  getHealth: (): Promise<HealthResult> => new Promise<HealthResult>(() => {}),
  ...client,
  archiveScreenshot: vi.fn(),
  restoreScreenshot: vi.fn(),
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

  it('uses real archived records in the read-only Crypt and does not treat demo=0 as demo', async () => {
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
    expect(screen.queryByRole('button', { name: 'Restore' })).not.toBeInTheDocument();
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
});
