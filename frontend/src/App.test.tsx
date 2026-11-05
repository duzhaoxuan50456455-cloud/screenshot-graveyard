import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import type { HealthResult } from './api/client';

// App mounts HealthStatus, which calls the API client on mount. Mock it so
// these presentational tests never touch the network.
vi.mock('./api/client', () => ({
  getHealth: (): Promise<HealthResult> =>
    new Promise<HealthResult>(() => {}),
}));

import App from './App';

describe('App', () => {
  it('renders the app title', () => {
    render(<App />);
    expect(
      screen.getByRole('heading', { name: /screenshot graveyard/i }),
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
    expect(screen.getByRole('status')).toBeInTheDocument();
  });
});
