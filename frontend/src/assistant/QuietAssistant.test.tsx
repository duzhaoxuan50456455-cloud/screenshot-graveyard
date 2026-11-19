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
  getProviderMode: vi.fn(),
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

/** Prepare (suggestion), Review, then Approve: nothing is submitted until Confirm. */
const reviveAndConfirm = async (name: RegExp) => {
  fireEvent.click(await screen.findByRole('button', { name }));
  fireEvent.click(await screen.findByRole('button', { name: /Confirm and Revive/ }));
};

beforeEach(() => {
  vi.clearAllMocks();
  m.getConsent.mockImplementation(() => ok(true));
  m.getProviderMode.mockImplementation(() => ok('openai'));
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
    await reviveAndConfirm(/Revive 1 ghost/);
    await waitFor(() => expect(props.onAnalysisChanged).toHaveBeenCalledWith(analysis));
    expect(m.classifyScreenshot).toHaveBeenCalledWith(1);
    expect(m.setConsent).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText(/1 screenshot revived/)).toBeInTheDocument());
  });

  it('reports a failed classify honestly and does not update cards', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementation(() => ok({ operationId: 5, status: 'failed', errorDetail: 'x', artifactId: null }));
    const props = setup([shot(1)]);
    await reviveAndConfirm(/Revive 1 ghost/);
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
    await reviveAndConfirm(/Revive 1 ghost/);
    await waitFor(() => expect(screen.getByTestId('qa-text')).toHaveTextContent(/working on your selection/i));
    expect(props.onAnalysisChanged).not.toHaveBeenCalled();
  });

  it('shows the provider hint and blocks Revive while the provider is unconfirmed', async () => {
    m.getProviderMode.mockImplementation(() => Promise.resolve({ ok: false, status: 0, code: 'network', message: 'down' }));
    setup([shot(1)]);
    const btn = await screen.findByRole('button', { name: /Revive 1 ghost/ });
    expect(btn).toBeDisabled();
    expect(screen.getByTestId('qa-provider-hint')).toHaveTextContent(/could not be confirmed/i);
    fireEvent.click(btn);
    expect(m.classifyScreenshot).not.toHaveBeenCalled();
  });

  it('names the external provider next to Revive in openai mode', async () => {
    setup([shot(1)]);
    await screen.findByRole('button', { name: /Revive 1 ghost/ });
    await waitFor(() => expect(screen.getByTestId('qa-provider-hint')).toHaveTextContent(/external AI provider/));
  });

  it('labels uncertain suggestions as simulated in demo mode', async () => {
    m.getProviderMode.mockImplementation(() => ok('fake'));
    setup([shot(1, true)]);
    await waitFor(() => expect(screen.getByTestId('qa-label')).toHaveTextContent('Simulated demo suggestion'));
  });

  it('reports a 403 from classify and does not poll', async () => {
    m.classifyScreenshot.mockImplementation(() => Promise.resolve({ ok: false, status: 403, code: 'consent_required', message: 'Consent is required.' }));
    const props = setup([shot(1)]);
    await reviveAndConfirm(/Revive 1 ghost/);
    await waitFor(() => expect(screen.getByText(/0 revived/)).toBeInTheDocument());
    expect(m.getOperation).not.toHaveBeenCalled();
    expect(props.onAnalysisChanged).not.toHaveBeenCalled();
  });

  it('reports poll exhaustion honestly', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementation(() => ok({ operationId: 5, status: 'pending' }));
    setup([shot(1)], { maxPolls: 2 });
    await reviveAndConfirm(/Revive 1 ghost/);
    await waitFor(() => expect(screen.getByText(/Still waiting on the server/)).toBeInTheDocument());
    expect(m.getOperation).toHaveBeenCalledTimes(2);
  });

  it('stops a batch on the first failure', async () => {
    m.classifyScreenshot.mockImplementation(() => Promise.resolve({ ok: false, status: 403, code: 'consent_required', message: 'Consent is required.' }));
    setup([shot(1), shot(2)]);
    await reviveAndConfirm(/Revive 2 ghosts/);
    await waitFor(() => expect(screen.getByText(/0 revived/)).toBeInTheDocument());
    expect(m.classifyScreenshot).toHaveBeenCalledTimes(1);
  });

  it('does not update state or continue a batch after unmount', async () => {
    let release: (v: unknown) => void = () => {};
    m.classifyScreenshot.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    const props = {
      selected: [shot(1), shot(2)], refreshToken: 0, onOpen: vi.fn(), onSetUpStudyNote: vi.fn(),
      onAnalysisChanged: vi.fn(), pollIntervalMs: 1, maxPolls: 5,
    };
    const { unmount } = render(<QuietAssistant {...props} />);
    await reviveAndConfirm(/Revive 2 ghosts/);
    await waitFor(() => expect(m.classifyScreenshot).toHaveBeenCalledTimes(1));
    unmount();
    release({ ok: true, data: { operationId: 9, kind: 'classify', status: 'pending', errorDetail: null, warning: null } });
    m.getOperation.mockImplementation(() => ok({ operationId: 9, status: 'successful' }));
    await new Promise((r) => setTimeout(r, 20));
    expect(props.onAnalysisChanged).not.toHaveBeenCalled();
    expect(m.classifyScreenshot).toHaveBeenCalledTimes(1);
  });

  it('shows a distinct loading hint (not "unconfirmed") while provider status is pending, Revive stays blocked', async () => {
    let release: (v: unknown) => void = () => {};
    m.getProviderMode.mockImplementation(() => new Promise((r) => { release = r; }));
    setup([shot(1)]);
    const btn = await screen.findByRole('button', { name: /Revive 1 ghost/ });
    expect(btn).toBeDisabled();
    expect(screen.getByTestId('qa-provider-hint')).toHaveTextContent(/checking the ai provider/i);
    expect(screen.getByTestId('qa-provider-hint')).not.toHaveTextContent(/unconfirmed/i);
    release({ ok: true, data: 'openai' });
    await waitFor(() => expect(screen.getByTestId('qa-provider-hint')).toHaveTextContent(/external AI provider/));
    expect(screen.getByRole('button', { name: /Revive 1 ghost/ })).toBeEnabled();
  });

  it('stops offering Revive when consent is withdrawn in another tab and the window regains focus', async () => {
    setup([shot(1)]);
    await screen.findByRole('button', { name: /Revive 1 ghost/ });
    m.getConsent.mockImplementation(() => ok(false));
    fireEvent.focus(window);
    expect(await screen.findByRole('button', { name: 'Review consent' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Revive 1 ghost/ })).toBeNull();
  });

  it('re-reads consent before running a batch and never classifies a withdrawn image', async () => {
    setup([shot(1)]);
    const btn = await screen.findByRole('button', { name: /Revive 1 ghost/ });
    m.getConsent.mockImplementation(() => ok(false));
    fireEvent.click(btn);
    fireEvent.click(await screen.findByRole('button', { name: /Confirm and Revive/ }));
    await waitFor(() => expect(screen.getByText(/Consent is no longer on record/)).toBeInTheDocument());
    expect(m.classifyScreenshot).not.toHaveBeenCalled();
    expect(m.setConsent).not.toHaveBeenCalled();
  });

  it('checks consent per image so a mid-batch withdrawal stops the rest', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementation(() => ok({ operationId: 5, status: 'successful', errorDetail: null, artifactId: null }));
    m.getAnalysis.mockImplementation(() => ok({ screenshotId: 1 }));
    setup([shot(1), shot(2)]);
    const btn = await screen.findByRole('button', { name: /Revive 2 ghosts/ });
    m.getConsent.mockImplementation((id: number) => ok(id === 1));
    fireEvent.click(btn);
    fireEvent.click(await screen.findByRole('button', { name: /Confirm and Revive/ }));
    await waitFor(() => expect(screen.getByText(/1 revived/)).toBeInTheDocument());
    expect(m.classifyScreenshot).toHaveBeenCalledTimes(1);
    expect(m.classifyScreenshot).toHaveBeenCalledWith(1);
  });
});
