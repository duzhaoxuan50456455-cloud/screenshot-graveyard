import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ListResult, Screenshot, WorkspaceResult } from './api/client';

// Mock the whole API client so these tests never touch the network.
const getWorkspace = vi.fn<() => Promise<WorkspaceResult>>();
const listScreenshots = vi.fn<() => Promise<ListResult>>();
const archiveScreenshot = vi.fn();
const restoreScreenshot = vi.fn();
const uploadScreenshots = vi.fn();

vi.mock('./api/client', () => ({
  getWorkspace: () => getWorkspace(),
  listScreenshots: () => listScreenshots(),
  archiveScreenshot: (id: number) => archiveScreenshot(id),
  restoreScreenshot: (id: number) => restoreScreenshot(id),
  uploadScreenshots: (files: File[]) => uploadScreenshots(files),
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

import Graveyard from './Graveyard';

function shot(partial: Partial<Screenshot> & { screenshotId: number }): Screenshot {
  return {
    state: 'active',
    mimeType: 'image/png',
    originalFilename: `file-${partial.screenshotId}.png`,
    isDuplicate: false,
    duplicateOf: null,
    byteSize: 2048,
    uploadedAt: '2026-11-07T00:00:00+00:00',
    ...partial,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  getWorkspace.mockResolvedValue({ ok: true, active: true });
  listScreenshots.mockResolvedValue({ ok: true, screenshots: [] });
});

describe('Graveyard', () => {
  it('shows a loading state while the workspace starts', () => {
    getWorkspace.mockReturnValue(new Promise(() => {}));
    render(<Graveyard />);
    expect(screen.getByText(/waking the graveyard/i)).toBeInTheDocument();
  });

  it('shows an error state when the workspace cannot start', async () => {
    getWorkspace.mockResolvedValue({ ok: false, error: 'down' });
    render(<Graveyard />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not reach/i);
  });

  it('renders empty Ghost and Crypt sections when there are no screenshots', async () => {
    render(<Graveyard />);
    expect(await screen.findByText(/no ghosts yet/i)).toBeInTheDocument();
    expect(screen.getByText(/the crypt is empty/i)).toBeInTheDocument();
  });

  it('places active screenshots under Ghosts and archived under The Crypt', async () => {
    listScreenshots.mockResolvedValue({
      ok: true,
      screenshots: [
        shot({ screenshotId: 1, state: 'active' }),
        shot({ screenshotId: 2, state: 'archived' }),
      ],
    });
    render(<Graveyard />);
    // Each active screenshot gets a Safe Burial button; archived gets Resurrection.
    expect(
      await screen.findByRole('button', { name: /safe burial: archive file-1/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /resurrection: restore file-2/i }),
    ).toBeInTheDocument();
  });

  it('shows a non-intrusive duplicate indicator', async () => {
    listScreenshots.mockResolvedValue({
      ok: true,
      screenshots: [shot({ screenshotId: 3, isDuplicate: true, duplicateOf: 1 })],
    });
    render(<Graveyard />);
    expect(await screen.findByText(/duplicate/i)).toBeInTheDocument();
  });

  it('renders accessible alt text on image previews', async () => {
    listScreenshots.mockResolvedValue({
      ok: true,
      screenshots: [shot({ screenshotId: 4, originalFilename: 'notes.png' })],
    });
    render(<Graveyard />);
    const img = await screen.findByRole('img', { name: /screenshot notes\.png/i });
    expect(img).toHaveAttribute('src', '/api/screenshots/4/image');
  });

  it('archives a screenshot and refreshes the list', async () => {
    listScreenshots
      .mockResolvedValueOnce({ ok: true, screenshots: [shot({ screenshotId: 5 })] })
      .mockResolvedValueOnce({
        ok: true,
        screenshots: [shot({ screenshotId: 5, state: 'archived' })],
      });
    archiveScreenshot.mockResolvedValue({
      ok: true,
      screenshotId: 5,
      status: 'successful',
      state: 'archived',
    });
    render(<Graveyard />);
    const btn = await screen.findByRole('button', { name: /safe burial/i });
    fireEvent.click(btn);
    await waitFor(() => expect(archiveScreenshot).toHaveBeenCalledWith(5));
    expect(await screen.findByText(/moved to the crypt/i)).toBeInTheDocument();
  });

  it('keeps the card in The Crypt when archive succeeds but refresh fails', async () => {
    listScreenshots
      .mockResolvedValueOnce({ ok: true, screenshots: [shot({ screenshotId: 55 })] })
      .mockResolvedValueOnce({ ok: false, error: 'HTTP 503' });
    archiveScreenshot.mockResolvedValue({
      ok: true,
      screenshotId: 55,
      status: 'successful',
      state: 'archived',
    });
    render(<Graveyard />);
    fireEvent.click(await screen.findByRole('button', { name: /safe burial/i }));
    expect(
      await screen.findByRole('button', { name: /resurrection: restore file-55/i }),
    ).toBeInTheDocument();
  });

  it('does not claim archive success for a failed lifecycle response', async () => {
    listScreenshots.mockResolvedValue({ ok: true, screenshots: [shot({ screenshotId: 56 })] });
    archiveScreenshot.mockResolvedValue({
      ok: true,
      screenshotId: 56,
      status: 'failed',
      state: 'active',
    });
    render(<Graveyard />);
    fireEvent.click(await screen.findByRole('button', { name: /safe burial/i }));
    expect(await screen.findByText(/safe burial did not complete/i)).toBeInTheDocument();
    expect(screen.queryByText(/moved to the crypt/i)).not.toBeInTheDocument();
  });

  it('reports an honest failure when restore fails its integrity check', async () => {
    listScreenshots.mockResolvedValue({
      ok: true,
      screenshots: [shot({ screenshotId: 6, state: 'archived' })],
    });
    restoreScreenshot.mockResolvedValue({
      ok: true,
      screenshotId: 6,
      status: 'failed',
      state: 'archived',
    });
    render(<Graveyard />);
    const btn = await screen.findByRole('button', { name: /resurrection/i });
    fireEvent.click(btn);
    expect(await screen.findByText(/integrity check/i)).toBeInTheDocument();
  });

  it('reports partial success (207) with both saved and rejected counts', async () => {
    uploadScreenshots.mockResolvedValue({
      ok: true,
      accepted: 1,
      rejected: 1,
      results: [
        { filename: 'ok.png', status: 'accepted', screenshotId: 7, isDuplicate: false, error: null },
        { filename: 'bad.txt', status: 'rejected', screenshotId: null, isDuplicate: null, error: 'File is not a valid, supported image' },
      ],
    });
    render(<Graveyard />);
    await screen.findByText(/no ghosts yet/i);
    const input = screen.getByLabelText(/add screenshots/i) as HTMLInputElement;
    const files = [
      new File(['x'], 'ok.png', { type: 'image/png' }),
      new File(['y'], 'bad.txt', { type: 'text/plain' }),
    ];
    fireEvent.change(input, { target: { files } });
    const alert = await screen.findByText(/saved 1, could not save 1/i);
    expect(alert).toHaveTextContent(/bad\.txt/i);
  });

  it('surfaces a list load error with a retry control', async () => {
    listScreenshots.mockResolvedValue({ ok: false, error: 'HTTP 500' });
    render(<Graveyard />);
    expect(await screen.findByText(/could not load your screenshots/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });
});
