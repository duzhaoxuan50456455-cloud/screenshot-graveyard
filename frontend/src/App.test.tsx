import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import type { HealthResult, ListResult, WorkspaceResult } from './api/client';

// App mounts HealthStatus and Graveyard, which call the API client on mount.
// Mock the client so these presentational tests never touch the network.
vi.mock('./api/client', () => ({
  getHealth: (): Promise<HealthResult> => new Promise<HealthResult>(() => {}),
  getWorkspace: (): Promise<WorkspaceResult> =>
    Promise.resolve({ ok: true, active: true }),
  listScreenshots: (): Promise<ListResult> =>
    Promise.resolve({ ok: true, screenshots: [] }),
  archiveScreenshot: vi.fn(),
  restoreScreenshot: vi.fn(),
  uploadScreenshots: vi.fn(),
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

describe('App', () => {
  it('renders the app title', () => {
    render(<App />);
    expect(
      screen.getByRole('heading', { name: /screenshot graveyard/i, level: 1 }),
    ).toBeInTheDocument();
  });

  it('renders visible tagline content', () => {
    render(<App />);
    expect(
      screen.getByText(/finish their unfinished business/i),
    ).toBeInTheDocument();
  });

  it('renders the backend health status region', () => {
    render(<App />);
    expect(screen.getAllByRole('status').length).toBeGreaterThan(0);
  });
});
