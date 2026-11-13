import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Analysis } from '../api/ai';
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

import { ScreenshotDetail } from './ScreenshotDetail';

const shot: Screenshot = {
  screenshotId: 7, state: 'active', mimeType: 'image/png', originalFilename: 'notes.png',
  isDuplicate: false, duplicateOf: null, byteSize: 100, uploadedAt: '2026-11-07T00:00:00+00:00',
};
const ok = <T,>(data: T) => Promise.resolve({ ok: true as const, data });
const fail = (status: number, code = 'x') => Promise.resolve({ ok: false as const, status, code, message: 'm' });
const notAnalysed = () => fail(404, 'not_found');
const op = (status: string, errorDetail: string | null = null) =>
  ok({ operationId: 5, kind: 'classify', status, errorDetail });
const analysis = (over: Partial<Analysis['classification']> = {}, epi: Partial<Analysis['epitaph']> = {}): Analysis => ({
  screenshotId: 7,
  classification: { category: 'Reference/Document', confidence: 0.75, uncertain: false, versionNumber: 1, origin: 'ai_generate', ...over },
  epitaph: { text: 'Synthetic reference fixture.', visibleFacts: 'A page of text.', inferredIntent: null, uncertain: false, versionNumber: 1, origin: 'ai_generate', ...epi },
});

const renderDetail = (extra = {}) =>
  render(<ScreenshotDetail screenshot={shot} onClose={() => {}} pollIntervalMs={1} maxPolls={20} {...extra} />);

beforeEach(() => {
  vi.clearAllMocks();
  m.getConsent.mockImplementation(() => ok(false));
  m.getAnalysis.mockImplementation(notAnalysed);
  m.getProviderMode.mockImplementation(() => ok('fake'));
});

describe('ScreenshotDetail: privacy + consent gating', () => {
  it('shows the privacy warning BEFORE the Revive control and before consent', async () => {
    renderDetail();
    const notice = await screen.findByRole('note', { name: /privacy notice/i });
    const revive = await screen.findByRole('button', { name: /revive: analyze with ai/i });
    expect(notice.compareDocumentPosition(revive) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(notice).toHaveTextContent(/deterministic fake adapter/i);
    expect(notice).toHaveTextContent(/No image is sent to an external AI provider/i);
    expect(notice).toHaveTextContent(/stored\s+privately/i);
  });

  it('uses external-processing language only for confirmed OpenAI mode', async () => {
    m.getProviderMode.mockImplementation(() => ok('openai'));
    renderDetail();
    expect(await screen.findByRole('note', { name: /privacy notice/i })).toHaveTextContent(
      /may be sent to an external AI provider/i,
    );
  });

  it('fails conservatively when provider mode cannot be confirmed', async () => {
    m.getConsent.mockImplementation(() => ok(true));
    m.getProviderMode.mockImplementation(() => fail(503));
    renderDetail();
    expect(await screen.findByText(/Provider mode could not be confirmed/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /revive: analyze with ai/i })).toBeDisabled();
    expect(screen.getByText(/Provider status must be confirmed/i)).toBeInTheDocument();
  });

  it('does NOT invoke AI on open: only reads consent and analysis', async () => {
    renderDetail();
    await screen.findByTestId('consent-state');
    await waitFor(() => expect(m.getAnalysis).toHaveBeenCalled());
    expect(m.classifyScreenshot).not.toHaveBeenCalled();
    expect(m.setConsent).not.toHaveBeenCalled();
  });

  it('blocks Revive until consent is granted, and sends nothing without it', async () => {
    renderDetail();
    expect(await screen.findByText(/NOT authorized/i)).toBeInTheDocument();
    const revive = screen.getByRole('button', { name: /revive: analyze with ai/i });
    expect(revive).toBeDisabled();
    fireEvent.click(revive);
    expect(m.classifyScreenshot).not.toHaveBeenCalled();
  });

  it('records explicit consent only on a deliberate click, then enables Revive', async () => {
    m.setConsent.mockImplementation(() => ok(true));
    renderDetail();
    const grant = await screen.findByRole('button', { name: /authorize simulated demo analysis/i });
    expect(m.setConsent).not.toHaveBeenCalled();
    fireEvent.click(grant);
    await waitFor(() => expect(m.setConsent).toHaveBeenCalledWith(7, true));
    expect(await screen.findByText(/AI processing is authorized/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /revive: analyze with ai/i })).toBeEnabled();
  });

  it('revocation blocks Revive again and does not claim to erase sent data', async () => {
    m.getConsent.mockImplementation(() => ok(true));
    m.setConsent.mockImplementation(() => ok(false));
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /withdraw ai consent/i }));
    await waitFor(() => expect(m.setConsent).toHaveBeenCalledWith(7, false));
    expect(await screen.findByText(/cannot recall/i)).toBeInTheDocument();
    expect(screen.getByText(/NOT authorized/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /revive: analyze with ai/i })).toBeDisabled();
    expect(screen.queryByText(/erased|deleted from/i)).not.toBeInTheDocument();
  });

  it('shows an honest message when the server refuses (403) and re-locks Revive', async () => {
    m.getConsent.mockImplementation(() => ok(true));
    m.classifyScreenshot.mockImplementation(() => fail(403, 'consent_required'));
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /revive: analyze with ai/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/consent is required first.*nothing was processed/i);
    expect(await screen.findByText(/NOT authorized/i)).toBeInTheDocument();
  });

  it('explains quota exhaustion (429)', async () => {
    m.getConsent.mockImplementation(() => ok(true));
    m.classifyScreenshot.mockImplementation(() => fail(429, 'quota_exceeded'));
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /revive: analyze with ai/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/allowance/i);
  });
});

describe('ScreenshotDetail: Revive, polling and results', () => {
  beforeEach(() => {
    m.getConsent.mockImplementation(() => ok(true));
  });

  it('starts classification once, polls the operation, then shows the persisted result', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementationOnce(() => op('pending')).mockImplementation(() => op('successful'));
    renderDetail();
    const revive = await screen.findByRole('button', { name: /revive: analyze with ai/i });
    await waitFor(() => expect(revive).toBeEnabled());
    // Result becomes available only after the operation succeeds.
    m.getAnalysis.mockImplementation(() => ok(analysis()));
    fireEvent.click(revive);
    fireEvent.click(revive); // duplicate click must not start a second operation
    expect(await screen.findByText(/Status: Successful/)).toBeInTheDocument();
    expect(m.classifyScreenshot).toHaveBeenCalledTimes(1);
    expect(m.getOperation).toHaveBeenCalledWith(5);
    expect(await screen.findByTestId('result-category')).toHaveTextContent('Reference/Document');
    expect(screen.getByTestId('result-epitaph')).toHaveTextContent('Synthetic reference fixture.');
    expect(screen.getByTestId('result-visible')).toHaveTextContent('A page of text.');
    expect(screen.getByText('75%')).toBeInTheDocument();
    expect(screen.getByText(/simulated demo result.*not image interpretation/i)).toBeInTheDocument();
  });

  it('shows pending as TEXT with a glyph, not just a color', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementation(() => op('pending'));
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /revive: analyze with ai/i }));
    const badge = await screen.findByText(/Status: Pending/);
    expect(badge.closest('[data-status="pending"]')).not.toBeNull();
    expect(badge.parentElement?.querySelector('[aria-hidden="true"]')?.textContent).toBeTruthy();
  });

  it('reports a FAILED operation in words and saves nothing', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementation(() => op('failed', 'AI analysis failed'));
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /revive: analyze with ai/i }));
    expect(await screen.findByText(/Status: Failed/)).toBeInTheDocument();
    expect(screen.getByText(/includes timeouts/i)).toBeInTheDocument();
    expect(screen.queryByTestId('result-category')).not.toBeInTheDocument();
  });

  it('reports UNCERTAIN distinctly from failed and successful', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementation(() => op('uncertain', 'AI result uncertain'));
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /revive: analyze with ai/i }));
    expect(await screen.findByText(/Status: Uncertain/)).toBeInTheDocument();
    expect(screen.getByText(/not sure.*no result was saved/i)).toBeInTheDocument();
    expect(screen.queryByText(/Status: Successful/)).not.toBeInTheDocument();
  });

  it('stops polling at the bound and says so instead of polling forever', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementation(() => op('pending'));
    renderDetail({ maxPolls: 3 });
    fireEvent.click(await screen.findByRole('button', { name: /revive: analyze with ai/i }));
    expect(await screen.findByText(/Stopped checking/i)).toBeInTheDocument();
    const calls = m.getOperation.mock.calls.length;
    expect(calls).toBe(3);
    await new Promise((r) => setTimeout(r, 30));
    expect(m.getOperation.mock.calls.length).toBe(calls);
  });

  it('cancels polling on unmount', async () => {
    m.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    m.getOperation.mockImplementation(() => op('pending'));
    const { unmount } = renderDetail({ maxPolls: 1000 });
    fireEvent.click(await screen.findByRole('button', { name: /revive: analyze with ai/i }));
    await waitFor(() => expect(m.getOperation).toHaveBeenCalled());
    unmount();
    await new Promise((r) => setTimeout(r, 10));
    const after = m.getOperation.mock.calls.length;
    await new Promise((r) => setTimeout(r, 40));
    expect(m.getOperation.mock.calls.length).toBe(after);
  });

  it('keeps Potentially Sensitive advisory (no hiding or removal)', async () => {
    m.getAnalysis.mockImplementation(() => ok(analysis({ category: 'Potentially Sensitive' })));
    renderDetail();
    expect(await screen.findByText(/advisory only/i)).toHaveTextContent(/not a guarantee/i);
    expect(screen.getByTestId('result-category')).toHaveTextContent('Potentially Sensitive');
    expect(screen.getByRole('img', { name: /notes\.png/ })).toBeInTheDocument();
  });

  it('hides Revive when an analysis already exists (redo is a later phase)', async () => {
    m.getAnalysis.mockImplementation(() => ok(analysis()));
    renderDetail();
    await screen.findByTestId('result-category');
    expect(screen.queryByRole('button', { name: /revive: analyze with ai/i })).not.toBeInTheDocument();
  });
});

describe('ScreenshotDetail: editing category and Epitaph', () => {
  beforeEach(() => {
    m.getConsent.mockImplementation(() => ok(true));
    m.getAnalysis.mockImplementation(() => ok(analysis()));
  });

  it('has labelled, keyboard-reachable edit controls for category and Epitaph', async () => {
    renderDetail();
    const select = await screen.findByLabelText('Category');
    const epitaph = screen.getByLabelText('Epitaph');
    expect(select.tagName).toBe('SELECT');
    expect(epitaph.tagName).toBe('TEXTAREA');
    expect(screen.getAllByRole('option')).toHaveLength(6);
  });

  it('saves a changed category + Epitaph as a new version and shows the edited result', async () => {
    m.editAnalysis.mockImplementation(() =>
      ok(analysis({ category: 'Study/Knowledge', origin: 'manual_edit', versionNumber: 2 }, { text: 'My caption', origin: 'manual_edit', versionNumber: 2 })),
    );
    renderDetail();
    fireEvent.change(await screen.findByLabelText('Category'), { target: { value: 'Study/Knowledge' } });
    fireEvent.change(screen.getByLabelText('Epitaph'), { target: { value: '  My caption ' } });
    fireEvent.click(screen.getByRole('button', { name: /save edits/i }));
    await waitFor(() =>
      expect(m.editAnalysis).toHaveBeenCalledWith(7, { category: 'Study/Knowledge', epitaphText: 'My caption' }),
    );
    expect(await screen.findByText(/Earlier versions are kept/i)).toBeInTheDocument();
    expect(screen.getByTestId('result-category')).toHaveTextContent('Study/Knowledge');
    expect(screen.getByText(/Edited by you/)).toBeInTheDocument();
    expect(screen.getByText('Set by you')).toBeInTheDocument();
  });

  it('sends only the field that changed and skips the request when nothing changed', async () => {
    m.editAnalysis.mockImplementation(() => ok(analysis({ category: 'Memory/Entertainment' })));
    renderDetail();
    fireEvent.click(await screen.findByRole('button', { name: /save edits/i }));
    expect(await screen.findByText(/nothing changed/i)).toBeInTheDocument();
    expect(m.editAnalysis).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Category'), { target: { value: 'Memory/Entertainment' } });
    fireEvent.click(screen.getByRole('button', { name: /save edits/i }));
    await waitFor(() => expect(m.editAnalysis).toHaveBeenCalledWith(7, { category: 'Memory/Entertainment' }));
  });

  it('surfaces an edit failure without pretending it saved', async () => {
    m.editAnalysis.mockImplementation(() => fail(503));
    renderDetail();
    fireEvent.change(await screen.findByLabelText('Epitaph'), { target: { value: 'new text' } });
    fireEvent.click(screen.getByRole('button', { name: /save edits/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/temporarily unavailable/i);
    expect(screen.queryByText(/earlier versions are kept/i)).not.toBeInTheDocument();
  });
});
