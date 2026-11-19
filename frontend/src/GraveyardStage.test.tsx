import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Screenshot } from './api/client';

// One primary action per stage: Prepare (select), Review, Approve (Confirm).
// All screenshots here are synthetic fixtures and the AI provider is the demo adapter.

const c = vi.hoisted(() => ({
  getWorkspace: vi.fn(),
  listScreenshots: vi.fn(),
  archiveScreenshot: vi.fn(),
  restoreScreenshot: vi.fn(),
  uploadScreenshots: vi.fn(),
}));
const a = vi.hoisted(() => ({
  getProviderMode: vi.fn(),
  getConsent: vi.fn(),
  setConsent: vi.fn(),
  classifyScreenshot: vi.fn(),
  getOperation: vi.fn(),
  getAnalysis: vi.fn(),
  editAnalysis: vi.fn(),
  listStudyNotes: vi.fn(),
  generateStudyNote: vi.fn(),
}));
vi.mock('./api/client', () => ({ ...c, imageUrl: (id: number) => `/api/screenshots/${id}/image` }));
vi.mock('./api/ai', async (orig) => ({ ...(await orig<typeof import('./api/ai')>()), ...a }));

import Graveyard from './Graveyard';

const ok = <T,>(data: T) => Promise.resolve({ ok: true as const, data });
const fail404 = () => Promise.resolve({ ok: false as const, status: 404, code: 'not_found', message: 'm' });

const shot = (id: number, over: Partial<Screenshot> = {}): Screenshot => ({
  screenshotId: id, state: 'active', mimeType: 'image/png', originalFilename: `s${id}.png`,
  isDuplicate: false, duplicateOf: null, byteSize: 1024, uploadedAt: '2026-11-07T00:00:00+00:00',
  analysis: null, ...over,
});

async function renderWith(list: Screenshot[]) {
  c.listScreenshots.mockResolvedValue({ ok: true, screenshots: list });
  render(<Graveyard />);
  await screen.findByRole('region', { name: /ghosts/i });
}

beforeEach(() => {
  vi.clearAllMocks();
  c.getWorkspace.mockResolvedValue({ ok: true, active: true });
  a.getProviderMode.mockImplementation(() => ok('fake'));
  a.getConsent.mockImplementation(() => ok(true));
  a.getAnalysis.mockImplementation(() => fail404());
  a.listStudyNotes.mockImplementation(() => ok([]));
});

describe('cards stay quiet', () => {
  it('has no Revive, Safe Burial or Resurrection button on any card', async () => {
    await renderWith([shot(1), shot(2), shot(3, { state: 'archived' })]);
    const buttons = screen.getAllByRole('button').map((b) => b.getAttribute('aria-label') ?? b.textContent ?? '');
    expect(buttons.filter((n) => /^open /i.test(n))).toHaveLength(2);
    expect(buttons.filter((n) => /revive|safe burial|resurrection/i.test(n) && !/confirm/i.test(n))).toEqual([]);
  });
});

describe('detail drawer focus and keyboard', () => {
  it('Escape closes the drawer and returns focus to the opener', async () => {
    await renderWith([shot(1)]);
    const opener = screen.getByRole('button', { name: /^open s1\.png/i });
    opener.focus();
    fireEvent.click(opener);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(opener).toHaveFocus();
  });

  it('Close button also restores focus to the opener', async () => {
    await renderWith([shot(1)]);
    const opener = screen.getByRole('button', { name: /^open s1\.png/i });
    opener.focus();
    fireEvent.click(opener);
    fireEvent.click(await screen.findByRole('button', { name: /^close$/i }));
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it('falls back to the view navigation when the opener card is gone', async () => {
    c.archiveScreenshot.mockResolvedValue({ ok: true, screenshotId: 1, status: 'successful', state: 'archived' });
    await renderWith([shot(1)]);
    const opener = screen.getByRole('button', { name: /^open s1\.png/i });
    opener.focus();
    fireEvent.click(opener);
    c.listScreenshots.mockResolvedValue({ ok: true, screenshots: [shot(1, { state: 'archived' })] });
    fireEvent.click(await screen.findByRole('button', { name: /safe burial: move to the crypt/i }));
    await screen.findByText(/moved to the crypt/i);
    await waitFor(() => expect(opener.isConnected).toBe(false));
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.getByRole('button', { name: /^workspace/i })).toHaveFocus());
  });

  it('states the gallery is untouched next to Safe Burial', async () => {
    await renderWith([shot(1)]);
    fireEvent.click(screen.getByRole('button', { name: /^open s1\.png/i }));
    expect(await screen.findByTestId('detail-crypt-note')).toHaveTextContent(
      /does not remove the photo from your phone gallery/i,
    );
  });

  it('Revive is the only primary button in the detail when consent is granted', async () => {
    await renderWith([shot(1)]);
    fireEvent.click(screen.getByRole('button', { name: /^open s1\.png/i }));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/is authorized/i);
    const primaries = Array.from(dialog.querySelectorAll('.gy-btn--revive'));
    expect(primaries).toHaveLength(1);
    expect(primaries[0]).toHaveTextContent(/revive/i);
  });

  it('consent grant is the sole primary when consent is the required next step', async () => {
    a.getConsent.mockImplementation(() => ok(false));
    await renderWith([shot(1)]);
    fireEvent.click(screen.getByRole('button', { name: /^open s1\.png/i }));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText(/NOT authorized/i);
    const primaries = Array.from(dialog.querySelectorAll('.gy-btn--revive'));
    expect(primaries).toHaveLength(1);
    expect(primaries[0]).toHaveTextContent(/I understand and agree/i);
  });
});

describe('selection, review and approval', () => {
  const select = (name: RegExp) => fireEvent.click(screen.getByRole('checkbox', { name }));

  it('shows the selected count and a Clear selection action', async () => {
    await renderWith([shot(1), shot(2)]);
    expect(screen.queryByTestId('selected-count')).toBeNull();
    select(/select s1\.png/i);
    select(/select s2\.png/i);
    expect(screen.getByTestId('selected-count')).toHaveTextContent('2 selected');
    fireEvent.click(screen.getByRole('button', { name: /clear selection/i }));
    expect(screen.queryByTestId('selected-count')).toBeNull();
  });

  it('makes no classify request before Confirm, and Cancel submits nothing', async () => {
    await renderWith([shot(1), shot(2)]);
    select(/select s1\.png/i);
    select(/select s2\.png/i);
    fireEvent.click(await screen.findByRole('button', { name: /Review 2 ghosts to Revive/ }));
    const review = await screen.findByTestId('qa-review');
    expect(within(review).getByText('s1.png', { exact: false })).toBeInTheDocument();
    expect(within(review).getByText('s2.png', { exact: false })).toBeInTheDocument();
    expect(review).toHaveTextContent(/nothing is sent/i);
    expect(a.classifyScreenshot).not.toHaveBeenCalled();
    // Only one primary action while reviewing.
    const visible = Array.from(document.querySelectorAll('.gy-btn--revive')).filter((el) => !el.closest('[hidden]'));
    expect(visible).toHaveLength(1);
    expect(visible[0]).toHaveTextContent(/Confirm and Revive 2/);
    fireEvent.click(within(review).getByRole('button', { name: /^cancel$/i }));
    expect(screen.queryByTestId('qa-review')).toBeNull();
    expect(a.classifyScreenshot).not.toHaveBeenCalled();
  });

  it('locks the batch selection while processing and reports progress', async () => {
    let finish: (v: unknown) => void = () => {};
    a.classifyScreenshot.mockImplementation(() => new Promise((r) => { finish = r; }));
    await renderWith([shot(1), shot(2)]);
    select(/select s1\.png/i);
    select(/select s2\.png/i);
    fireEvent.click(await screen.findByRole('button', { name: /Review 2 ghosts to Revive/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm and Revive 2/ }));
    const status = await screen.findByTestId('batch-status');
    expect(status).toHaveTextContent(/Processing 1 of 2/);
    expect(status).toHaveTextContent(/s1\.png, s2\.png/);
    expect(screen.getByRole('checkbox', { name: /select s1\.png/i })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /select s2\.png/i })).toBeDisabled();
    expect(screen.queryByRole('button', { name: /clear selection/i })).toBeNull();
    expect(a.classifyScreenshot).toHaveBeenCalledTimes(1);
    finish({ ok: false, status: 403, code: 'consent_required', message: 'Consent is required.' });
    await waitFor(() => expect(screen.queryByTestId('batch-status')).toBeNull());
    expect(screen.getByRole('checkbox', { name: /select s1\.png/i })).toBeEnabled();
  });
});

describe('provider loading language', () => {
  it('says Checking while the provider status is loading, never "unconfirmed"', async () => {
    a.getProviderMode.mockImplementation(() => new Promise(() => {}));
    await renderWith([shot(1)]);
    fireEvent.click(screen.getByRole('button', { name: /^open s1\.png/i }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('note', { name: /privacy notice/i })).toHaveTextContent(/checking the ai provider/i);
    expect(within(dialog).getByRole('button', { name: /revive: analyze with ai \(unavailable: checking the ai provider/i })).toBeDisabled();
    expect(document.body).not.toHaveTextContent(/unconfirmed/i);
    // Study Notes (Creations) uses the same wording.
    fireEvent.click(screen.getByRole('button', { name: /^creations/i }));
    expect(document.body).not.toHaveTextContent(/unconfirmed/i);
    expect(screen.getAllByText(/checking the ai provider/i).length).toBeGreaterThan(1);
  });

  it('says could not be confirmed only after the status request failed', async () => {
    a.getProviderMode.mockImplementation(() => Promise.resolve({ ok: false, status: 503, code: 'x', message: 'm' }));
    await renderWith([shot(1)]);
    fireEvent.click(screen.getByRole('button', { name: /^open s1\.png/i }));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() =>
      expect(within(dialog).getByRole('note', { name: /privacy notice/i })).toHaveTextContent(/could not be confirmed/i),
    );
    expect(document.body).not.toHaveTextContent(/checking the ai provider/i);
  });
});

describe('Crypt copy', () => {
  it('explains the phone gallery is untouched in the Crypt view', async () => {
    await renderWith([shot(1, { state: 'archived' })]);
    fireEvent.click(screen.getByRole('button', { name: /^the crypt/i }));
    expect(screen.getByTestId('crypt-gallery-note')).toHaveTextContent(
      'Archiving in The Crypt does not remove the photo from your phone gallery. It only moves it inside Screenshot Graveyard.',
    );
  });
});
