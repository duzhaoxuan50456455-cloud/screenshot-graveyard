import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Screenshot } from '../api/client';

const m = vi.hoisted(() => ({
  getConsent: vi.fn(),
  setConsent: vi.fn(),
  classifyScreenshot: vi.fn(),
  getOperation: vi.fn(),
  getAnalysis: vi.fn(),
  editAnalysis: vi.fn(),
  getProviderMode: vi.fn(),
}));
vi.mock('../api/ai', async (orig) => ({ ...(await orig<typeof import('../api/ai')>()), ...m }));

import { QuietAssistant } from '../assistant/QuietAssistant';
import { ScreenshotDetail } from './ScreenshotDetail';

const shot = (id: number): Screenshot => ({
  screenshotId: id, state: 'active', mimeType: 'image/png', originalFilename: `s${id}.png`,
  isDuplicate: false, duplicateOf: null, byteSize: 1, uploadedAt: '2026-01-01T00:00:00+00:00',
  analysis: null,
});
const ok = <T,>(data: T) => Promise.resolve({ ok: true as const, data });
const fail = (status: number, code = 'x') => Promise.resolve({ ok: false as const, status, code, message: 'm' });
const accepted = (operationId: number) => ok({ operationId, kind: 'classify', status: 'pending', errorDetail: null, warning: null });
const opResult = (operationId: number, status: string, errorDetail: string | null = null) =>
  ok({ operationId, kind: 'classify', status, errorDetail, artifactId: null });

const renderBoth = (id: number, extra: { maxPolls?: number } = {}) =>
  render(
    <>
      <QuietAssistant
        selected={[shot(id)]} refreshToken={0} onOpen={vi.fn()} onSetUpStudyNote={vi.fn()}
        onAnalysisChanged={vi.fn()} pollIntervalMs={1} maxPolls={extra.maxPolls ?? 50}
      />
      <ScreenshotDetail screenshot={shot(id)} onClose={() => {}} pollIntervalMs={1} maxPolls={extra.maxPolls ?? 50} />
    </>,
  );

const assistantRevive = () => screen.findByRole('button', { name: /Revive 1 ghost/ });
const detailRevive = () => screen.findByRole('button', { name: /revive: analyze with ai/i });

beforeEach(() => {
  vi.clearAllMocks();
  m.getConsent.mockImplementation(() => ok(true));
  m.getAnalysis.mockImplementation(() => fail(404, 'not_found'));
  m.getProviderMode.mockImplementation(() => ok('fake'));
});

describe('Revive registry shared by Quiet Assistant and Screenshot Detail', () => {
  it('assistant first, then detail: classify is called once', async () => {
    m.classifyScreenshot.mockImplementation(() => accepted(5));
    m.getOperation.mockImplementation(() => opResult(5, 'pending'));
    renderBoth(1);
    fireEvent.click(await assistantRevive());
    await waitFor(() => expect(m.classifyScreenshot).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('qa-op')).toHaveAttribute('data-state', 'pending'));
    fireEvent.click(await detailRevive());
    expect(await screen.findByText(/already being revived elsewhere/)).toBeInTheDocument();
    expect(m.classifyScreenshot).toHaveBeenCalledTimes(1);
  });

  it('detail first, then assistant: classify is called once', async () => {
    m.classifyScreenshot.mockImplementation(() => accepted(5));
    m.getOperation.mockImplementation(() => opResult(5, 'pending'));
    renderBoth(1);
    fireEvent.click(await detailRevive());
    await waitFor(() => expect(m.classifyScreenshot).toHaveBeenCalledTimes(1));
    fireEvent.click(await assistantRevive());
    await waitFor(() => expect(screen.getByText(/already being revived elsewhere/)).toBeInTheDocument());
    expect(m.classifyScreenshot).toHaveBeenCalledTimes(1);
  });

  it('after a poll timeout the operation is reconciled before another submission', async () => {
    m.classifyScreenshot.mockImplementation(() => accepted(5));
    m.getOperation.mockImplementation(() => opResult(5, 'pending'));
    renderBoth(1, { maxPolls: 3 });
    fireEvent.click(await assistantRevive());
    await screen.findByRole('button', { name: 'Check again' });
    m.getOperation.mockClear();
    // Still pending on the server: a second submit from the detail is refused.
    fireEvent.click(await detailRevive());
    expect(await screen.findByText(/not been confirmed as finished/)).toBeInTheDocument();
    expect(m.getOperation).toHaveBeenCalledWith(5);
    expect(m.classifyScreenshot).toHaveBeenCalledTimes(1);
    // Once the server reports it final, a new submission is allowed.
    m.getOperation.mockImplementation(() => opResult(5, 'failed', 'AI analysis failed'));
    m.classifyScreenshot.mockImplementation(() => accepted(6));
    fireEvent.click(await detailRevive());
    await waitFor(() => expect(m.classifyScreenshot).toHaveBeenCalledTimes(2));
  });

  it('different screenshot ids are independent', async () => {
    m.classifyScreenshot.mockImplementation((id: number) => accepted(id * 10));
    m.getOperation.mockImplementation(() => opResult(10, 'pending'));
    render(
      <>
        <QuietAssistant
          selected={[shot(1)]} refreshToken={0} onOpen={vi.fn()} onSetUpStudyNote={vi.fn()}
          onAnalysisChanged={vi.fn()} pollIntervalMs={1} maxPolls={50}
        />
        <ScreenshotDetail screenshot={shot(2)} onClose={() => {}} pollIntervalMs={1} maxPolls={50} />
      </>,
    );
    fireEvent.click(await assistantRevive());
    await waitFor(() => expect(m.classifyScreenshot).toHaveBeenCalledWith(1));
    fireEvent.click(await detailRevive());
    await waitFor(() => expect(m.classifyScreenshot).toHaveBeenCalledWith(2));
    expect(m.classifyScreenshot).toHaveBeenCalledTimes(2);
  });

  it('releases the registry when the operation finishes so a later Revive is possible', async () => {
    m.classifyScreenshot.mockImplementation(() => accepted(5));
    m.getOperation.mockImplementation(() => opResult(5, 'uncertain'));
    renderBoth(1);
    fireEvent.click(await assistantRevive());
    await waitFor(() => expect(screen.getByTestId('qa-op')).toHaveAttribute('data-state', 'uncertain'));
    fireEvent.click(await detailRevive());
    await waitFor(() => expect(m.classifyScreenshot).toHaveBeenCalledTimes(2));
  });
});

describe('consent sequencing', () => {
  it('drops an older consent response that resolves after a newer one', async () => {
    const resolvers: Array<(v: unknown) => void> = [];
    m.getConsent.mockImplementation(() => new Promise((r) => resolvers.push(r)));
    render(<ScreenshotDetail screenshot={shot(1)} onClose={() => {}} />);
    await waitFor(() => expect(resolvers).toHaveLength(1));
    act(() => { window.dispatchEvent(new Event('focus')); }); // second, newer request
    await waitFor(() => expect(resolvers).toHaveLength(2));
    await act(async () => { resolvers[1]({ ok: true, data: false }); });
    expect(screen.getByTestId('consent-state')).toHaveTextContent(/NOT authorized/);
    await act(async () => { resolvers[0]({ ok: true, data: true }); }); // stale: must be ignored
    expect(screen.getByTestId('consent-state')).toHaveTextContent(/NOT authorized/);
    expect(screen.getByTestId('consent-state')).not.toHaveTextContent(/is authorized/);
  });

  it('a 403 from classify clears stale granted consent in the assistant', async () => {
    m.classifyScreenshot.mockImplementation(() => fail(403, 'consent_required'));
    renderBoth(1);
    fireEvent.click(await assistantRevive());
    await waitFor(() => expect(screen.getByText(/AI consent is required first/)).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId('qa-text')).toHaveTextContent(/still needs your consent/));
    expect(screen.queryByRole('button', { name: /Revive 1 ghost/ })).toBeNull();
  });

  it('words a failed lookup differently from revoked consent', async () => {
    m.getConsent.mockImplementation(() => fail(500));
    const { unmount } = render(<ScreenshotDetail screenshot={shot(1)} onClose={() => {}} />);
    expect(await screen.findByText(/Could not read consent/)).toBeInTheDocument();
    unmount();
    m.getConsent.mockImplementation(() => ok(false));
    render(
      <QuietAssistant selected={[shot(1)]} refreshToken={0} onOpen={vi.fn()} onSetUpStudyNote={vi.fn()} onAnalysisChanged={vi.fn()} />,
    );
    expect(await screen.findByText(/still needs your consent/)).toBeInTheDocument();
  });

  it('assistant shows lookup failure wording and checking wording', async () => {
    let release: (v: unknown) => void = () => {};
    m.getConsent.mockImplementation(() => new Promise((r) => { release = r; }));
    render(
      <QuietAssistant selected={[shot(1)]} refreshToken={0} onOpen={vi.fn()} onSetUpStudyNote={vi.fn()} onAnalysisChanged={vi.fn()} />,
    );
    expect(await screen.findByText(/Checking consent/)).toBeInTheDocument();
    await act(async () => { release({ ok: false, status: 500, code: 'x', message: 'm' }); });
    expect(await screen.findByText(/Could not check consent/)).toBeInTheDocument();
    expect(screen.queryByText(/still needs your consent/)).toBeNull();
  });
});

describe('Quiet Assistant operation states', () => {
  const run = async (maxPolls = 3) => {
    m.classifyScreenshot.mockImplementation(() => accepted(5));
    render(
      <QuietAssistant
        selected={[shot(1)]} refreshToken={0} onOpen={vi.fn()} onSetUpStudyNote={vi.fn()}
        onAnalysisChanged={vi.fn()} pollIntervalMs={1} maxPolls={maxPolls}
      />,
    );
    fireEvent.click(await screen.findByRole('button', { name: /Revive 1 ghost/ }));
  };
  const state = () => screen.findByTestId('qa-op');

  it('pending (bounded wait ended): not failed, with a secondary Check again', async () => {
    m.getOperation.mockImplementation(() => opResult(5, 'pending'));
    await run();
    await screen.findByRole('button', { name: 'Check again' });
    expect(await state()).toHaveAttribute('data-state', 'pending');
  });

  it('completed', async () => {
    m.getOperation.mockImplementation(() => opResult(5, 'successful'));
    m.getAnalysis.mockImplementation(() => ok({ screenshotId: 1 }));
    await run();
    await waitFor(() => expect(screen.getByTestId('qa-op')).toHaveAttribute('data-state', 'completed'));
    expect(screen.queryByRole('button', { name: 'Check again' })).toBeNull();
  });

  it('uncertain is not reported as a failure', async () => {
    m.getOperation.mockImplementation(() => opResult(5, 'uncertain'));
    await run();
    await waitFor(() => expect(screen.getByTestId('qa-op')).toHaveAttribute('data-state', 'uncertain'));
    expect(screen.getByTestId('qa-op')).toHaveTextContent(/not sure/);
    expect(screen.getByTestId('qa-op')).not.toHaveTextContent(/did not finish|failed/i);
  });

  it('failed', async () => {
    m.getOperation.mockImplementation(() => opResult(5, 'failed', 'AI operation quota exceeded'));
    await run();
    await waitFor(() => expect(screen.getByTestId('qa-op')).toHaveAttribute('data-state', 'failed'));
    expect(screen.getByTestId('qa-op')).toHaveTextContent(/allowance is used up/);
  });

  it('poll request failures are status_unavailable, not pending, and Check again reconciles', async () => {
    m.getOperation.mockImplementation(() => fail(0));
    await run();
    await waitFor(() => expect(screen.getByTestId('qa-op')).toHaveAttribute('data-state', 'status_unavailable'));
    expect(screen.getByTestId('qa-op')).toHaveTextContent(/temporarily unavailable/);
    m.getOperation.mockImplementation(() => opResult(5, 'successful'));
    m.getAnalysis.mockImplementation(() => ok({ screenshotId: 1 }));
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await waitFor(() => expect(screen.getByTestId('qa-op')).toHaveAttribute('data-state', 'completed'));
  });

  it('a 404 from the poll is final', async () => {
    m.getOperation.mockImplementation(() => fail(404, 'not_found'));
    await run();
    await waitFor(() => expect(screen.getByTestId('qa-op')).toHaveAttribute('data-state', 'failed'));
    expect(m.getOperation).toHaveBeenCalledTimes(1);
  });
});
