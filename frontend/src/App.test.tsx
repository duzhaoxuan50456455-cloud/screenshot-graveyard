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
