import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Screenshot } from '../api/client';

const m = vi.hoisted(() => ({
  getConsent: vi.fn(),
  setConsent: vi.fn(),
  classifyScreenshot: vi.fn(),
  getOperation: vi.fn(),
  getAnalysis: vi.fn(),
  generateStudyNote: vi.fn(),
}));
vi.mock('../api/ai', async (orig) => ({ ...(await orig<typeof import('../api/ai')>()), ...m }));

import { QuietAssistant } from './QuietAssistant';

const shot = (id: number, uncertain: boolean | null = null): Screenshot => ({
  screenshotId: id, state: 'active', mimeType: 'image/png', originalFilename: `s${id}.png`,
  isDuplicate: false, duplicateOf: null, byteSize: 1, uploadedAt: '2026-01-01T00:00:00+00:00',
  analysis: uncertain === null ? null : { category: 'Notes', uncertain, epitaph: 'e', epitaphOrigin: 'ai_generate' },
});
const ok = <T,>(data: T) => Promise.resolve({ ok: true as const, data });

const setup = (selected: Screenshot[], extra = {}) => {
  const props = {
    selected, refreshToken: 0, onOpen: vi.fn(), onSetUpStudyNote: vi.fn(), onAnalysisChanged: vi.fn(),
    pollIntervalMs: 1, maxPolls: 5, ...extra,
  };
  render(<QuietAssistant {...props} />);
  return props;
};

beforeEach(() => {
  vi.clearAllMocks();
  m.getConsent.mockImplementation(() => ok(true));
});

describe('QuietAssistant', () => {
  it('shows an idle hint without actions when nothing is selected', () => {
    setup([]);
    expect(screen.getByTestId('qa-text')).toHaveTextContent(/select screenshots/i);
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull();
  });

  it('shows exactly one primary suggestion, labelled as app rules', async () => {
    setup([shot(1), shot(2)]);
    expect(await screen.findByRole('button', { name: /Revive 2 ghosts/ })).toBeInTheDocument();
    expect(screen.getAllByTestId('qa-text')).toHaveLength(1);
    expect(screen.getByTestId('qa-label')).toHaveTextContent('Suggested by app rules');
    expect(screen.getByTestId('qa-label')).toHaveAttribute('data-source', 'rules');
  });

  it('Accept calls the existing classify endpoint, then polls and loads the analysis', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementationOnce(() => ok({ operationId: 5, status: 'pending' }))
      .mockImplementation(() => ok({ operationId: 5, status: 'successful', errorDetail: null, artifactId: null }));
    const analysis = { screenshotId: 1 };
    m.getAnalysis.mockImplementation(() => ok(analysis));
    const props = setup([shot(1)]);
    fireEvent.click(await screen.findByRole('button', { name: /Revive 1 ghost/ }));
    await waitFor(() => expect(props.onAnalysisChanged).toHaveBeenCalledWith(analysis));
    expect(m.classifyScreenshot).toHaveBeenCalledWith(1);
    expect(m.setConsent).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText(/1 screenshot revived/)).toBeInTheDocument());
  });

  it('reports a failed classify honestly and does not update cards', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementation(() => ok({ operationId: 5, status: 'failed', errorDetail: 'x', artifactId: null }));
    const props = setup([shot(1)]);
    fireEvent.click(await screen.findByRole('button', { name: /Revive 1 ghost/ }));
    await waitFor(() => expect(screen.getByText(/0 revived/)).toBeInTheDocument());
    expect(props.onAnalysisChanged).not.toHaveBeenCalled();
  });

  it('consent-blocked: offers review, never classifies or grants consent', async () => {
    m.getConsent.mockImplementation(() => ok(false));
    const props = setup([shot(7)]);
    fireEvent.click(await screen.findByRole('button', { name: 'Review consent' }));
    expect(props.onOpen).toHaveBeenCalledWith(7);
    expect(m.classifyScreenshot).not.toHaveBeenCalled();
    expect(m.setConsent).not.toHaveBeenCalled();
  });

  it('Something else shows the next alternative; Accept hands the selection to Study Notes', async () => {
    const props = setup([shot(2, false), shot(1, false)]);
    expect(await screen.findByRole('button', { name: 'Set up Study Note' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Set up Study Note' }));
    expect(props.onSetUpStudyNote).toHaveBeenCalledWith([2, 1]);
    expect(m.generateStudyNote).not.toHaveBeenCalled();
  });

  it('Something else advances, and Dismiss removes it for the session', async () => {
    setup([shot(1)]);
    await screen.findByRole('button', { name: /Revive 1 ghost/ });
    fireEvent.click(screen.getByRole('button', { name: 'Something else' }));
    expect(screen.getByRole('button', { name: 'Set up Study Note' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Something else' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.getByRole('button', { name: /Revive 1 ghost/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.getByTestId('qa-text')).toHaveTextContent(/no more suggestions/i);
  });

  it('labels classifier-derived suggestions as AI', async () => {
    setup([shot(1, true)]);
    await waitFor(() => expect(screen.getByTestId('qa-label')).toHaveTextContent('AI suggestion'));
    expect(screen.getByTestId('qa-label')).toHaveAttribute('data-source', 'ai');
  });

  it('is a polite live region, collapsible, and keyboard operable', () => {
    setup([]);
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite');
    const toggle = screen.getByRole('button', { name: 'Hide' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Show' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('reacts to selection changes and shows processing state', async () => {
    m.classifyScreenshot.mockImplementation(() => new Promise(() => {}));
    const props = setup([shot(1)]);
    fireEvent.click(await screen.findByRole('button', { name: /Revive 1 ghost/ }));
    await waitFor(() => expect(screen.getByTestId('qa-text')).toHaveTextContent(/working on your selection/i));
    expect(props.onAnalysisChanged).not.toHaveBeenCalled();
  });
});
