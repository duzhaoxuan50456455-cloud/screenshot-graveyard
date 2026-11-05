import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { HealthResult } from './api/client';

// Mock the API client so component tests never hit the real network.
const getHealthMock = vi.fn<() => Promise<HealthResult>>();
vi.mock('./api/client', () => ({
  getHealth: () => getHealthMock(),
}));

import HealthStatus from './HealthStatus';

describe('HealthStatus', () => {
  beforeEach(() => {
    getHealthMock.mockReset();
  });

  it('shows the checking state while the request is pending', () => {
    // A promise that never resolves keeps the component in the loading phase.
    getHealthMock.mockReturnValue(new Promise<HealthResult>(() => {}));
    render(<HealthStatus />);
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent(/checking/i);
    expect(status).toHaveAttribute('data-phase', 'checking');
  });

  it('shows Backend Online when the health check succeeds', async () => {
    getHealthMock.mockResolvedValue({ ok: true, status: 'ok' });
    render(<HealthStatus />);
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/backend online/i);
    });
    expect(screen.getByRole('status')).toHaveAttribute('data-phase', 'online');
  });

  it('shows Backend Offline / Error when the health check fails', async () => {
    getHealthMock.mockResolvedValue({
      ok: false,
      error: 'Network request failed',
    });
    render(<HealthStatus />);
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(
        /backend offline \/ error/i,
      );
    });
    expect(screen.getByRole('status')).toHaveAttribute('data-phase', 'offline');
  });

  it('does not report online when a 2xx returns an unexpected status value', async () => {
    getHealthMock.mockResolvedValue({ ok: true, status: 'degraded' });
    render(<HealthStatus />);
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveAttribute(
        'data-phase',
        'offline',
      );
    });
    expect(screen.getByRole('status')).not.toHaveTextContent(/backend online/i);
  });
});
