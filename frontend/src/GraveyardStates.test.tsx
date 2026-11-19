import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Analysis } from './api/ai';
import type { Screenshot, ScreenshotAnalysis } from './api/client';

// Task 4.1: the Halloween metaphors are bound to REAL state.
//   Ghost   = active and not analysed yet (Pending)       (Req 10.1)
//   Revived = active and analysed
//   Crypt   = exactly the archived screenshots             (Req 10.2, 10.6)
//   Epitaph = the stored caption, editable                 (Req 5.5)

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
import { partitionScreenshots } from './graveyardState';

const ok = <T,>(data: T) => Promise.resolve({ ok: true as const, data });
const fail = (status: number, code = 'x') => Promise.resolve({ ok: false as const, status, code, message: 'm' });

const ANALYSIS: ScreenshotAnalysis = {
  category: 'Reference/Document', uncertain: false,
  epitaph: 'Synthetic reference fixture.', epitaphOrigin: 'ai_generate',
};
function shot(id: number, over: Partial<Screenshot> = {}): Screenshot {
  return {
    screenshotId: id, state: 'active', mimeType: 'image/png', originalFilename: `s${id}.png`,
    isDuplicate: false, duplicateOf: null, byteSize: 1024, uploadedAt: '2026-11-07T00:00:00+00:00',
    analysis: null, ...over,
  };
}
const analysed = (over: Partial<ScreenshotAnalysis> = {}) => ({ ...ANALYSIS, ...over });
function serverAnalysis(id: number, epitaph: string, origin = 'manual_edit'): Analysis {
  return {
    screenshotId: id,
    classification: { category: 'Reference/Document', confidence: 0.75, uncertain: false, versionNumber: 1, origin: 'ai_generate' },
    epitaph: { text: epitaph, visibleFacts: 'f', inferredIntent: null, uncertain: false, versionNumber: 2, origin },
  };
}

const region = (name: RegExp) => screen.getByRole('region', { name });
const cardsIn = (name: RegExp) =>
  within(region(name)).queryAllByRole('listitem').map((li) => li.querySelector('.gy-card__name')?.textContent);

async function renderWith(list: Screenshot[]) {
  c.listScreenshots.mockResolvedValue({ ok: true, screenshots: list });
  render(<Graveyard />);
  await screen.findByRole('region', { name: /ghosts/i });
}

const openCrypt = () => fireEvent.click(screen.getByRole('button', { name: /the crypt/i }));
const openWorkspace = () => fireEvent.click(screen.getByRole('button', { name: /workspace/i }));

beforeEach(() => {
  vi.clearAllMocks();
  c.getWorkspace.mockResolvedValue({ ok: true, active: true });
  a.getProviderMode.mockImplementation(() => ok('fake'));
  a.getConsent.mockImplementation(() => ok(false));
  a.getAnalysis.mockImplementation(() => fail(404, 'not_found'));
  a.listStudyNotes.mockImplementation(() => ok([]));
});

// ------------------------------------------------------------------ the mapping

describe('partitionScreenshots: metaphors derive only from real state', () => {
  it('puts every screenshot in exactly one view, for every combination of state', () => {
    const all: Screenshot[] = [];
    let id = 1;
    for (const state of ['active', 'archived'] as const) {
      for (const analysis of [null, analysed()]) {
        for (let copy = 0; copy < 3; copy++) all.push(shot(id++, { state, analysis }));
      }
    }
    const { ghosts, revived, crypt } = partitionScreenshots(all);
    const seen = [...ghosts, ...revived, ...crypt].map((s) => s.screenshotId).sort((x, y) => x - y);
    expect(seen).toEqual(all.map((s) => s.screenshotId)); // none lost, none duplicated
    expect(ghosts.every((s) => s.state === 'active' && s.analysis === null)).toBe(true);
    expect(revived.every((s) => s.state === 'active' && s.analysis !== null)).toBe(true);
    expect(crypt.map((s) => s.screenshotId)).toEqual(all.filter((s) => s.state === 'archived').map((s) => s.screenshotId));
    expect([ghosts.length, revived.length, crypt.length]).toEqual([3, 3, 6]);
  });

  it('is empty-safe', () => {
    expect(partitionScreenshots([])).toEqual({ ghosts: [], revived: [], crypt: [] });
  });
});

describe('Graveyard views', () => {
  it('shows Ghosts = pending only, Revived = analysed active, Crypt = exactly the archived', async () => {
    await renderWith([
      shot(1),                                                    // ghost
      shot(2, { analysis: analysed() }),                          // revived
      shot(3, { state: 'archived' }),                             // crypt, never analysed
      shot(4, { state: 'archived', analysis: analysed() }),       // crypt, analysed
      shot(5),                                                    // ghost
    ]);
    expect(cardsIn(/^ghosts/i)).toEqual(['s1.png', 's5.png']);
    expect(cardsIn(/^revived/i)).toEqual(['s2.png']);
    expect(screen.getByRole('heading', { name: /ghosts \(2\)/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /revived \(1\)/i })).toBeInTheDocument();
    openCrypt();
    expect(cardsIn(/^the crypt/i)).toEqual(['s3.png', 's4.png']);       // exactly the archived ones
    expect(screen.getByRole('heading', { name: /the crypt \(2\)/i })).toBeInTheDocument();
  });

  it('labels every card with its real state in TEXT, not just color', async () => {
    await renderWith([shot(1), shot(2, { analysis: analysed() }), shot(3, { state: 'archived' })]);
    expect(within(region(/^ghosts/i)).getByText(/Ghost: Pending, not analyzed yet/)).toBeInTheDocument();
    expect(within(region(/^revived/i)).getByText(/Revived: analyzed/)).toBeInTheDocument();
    openCrypt();
    expect(within(region(/^the crypt/i)).getByText(/In The Crypt: archived, original kept/)).toBeInTheDocument();
  });

  it('an archived screenshot is never shown as a Ghost or Revived, even when analysed', async () => {
    await renderWith([shot(9, { state: 'archived', analysis: analysed() })]);
    expect(cardsIn(/^ghosts/i)).toEqual([]);
    expect(cardsIn(/^revived/i)).toEqual([]);
    expect(screen.getByText(/no ghosts: nothing is waiting for analysis/i)).toBeInTheDocument();
    openCrypt();
    expect(cardsIn(/^the crypt/i)).toEqual(['s9.png']);
  });

  it('keeps an analysed screenshot\u2019s category and Epitaph visible while it rests in the Crypt', async () => {
    await renderWith([shot(4, { state: 'archived', analysis: analysed({ category: 'Study/Knowledge', epitaph: 'Buried caption' }) })]);
    openCrypt();
    const crypt = region(/^the crypt/i);
    expect(within(crypt).getByText('Study/Knowledge')).toBeInTheDocument();
    expect(within(crypt).getByTestId('epitaph-4')).toHaveTextContent('Buried caption');
    expect(within(crypt).getByRole('button', { name: /resurrection: restore s4\.png/i })).toBeInTheDocument();
    expect(within(crypt).queryByRole('button', { name: /safe burial/i })).not.toBeInTheDocument();
  });

  it('empty states are explicit for each view', async () => {
    await renderWith([]);
    expect(screen.getByText(/no ghosts yet/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing revived yet/i)).toBeInTheDocument();
    openCrypt();
    expect(screen.getByText(/the crypt is empty/i)).toBeInTheDocument();
  });

  it('exposes keyboard-reachable lists and named regions', async () => {
    await renderWith([shot(1), shot(2, { analysis: analysed() }), shot(3, { state: 'archived' })]);
    expect(screen.getByRole('list', { name: 'Ghosts' })).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Revived screenshots' })).toBeInTheDocument();
    openCrypt();
    expect(screen.getByRole('list', { name: 'The Crypt' })).toBeInTheDocument();
    for (const b of screen.getAllByRole('button')) expect(b).toHaveAccessibleName();
  });

  it('moves a Revived card into The Crypt on Safe Burial, keeping its Epitaph', async () => {
    c.archiveScreenshot.mockResolvedValue({ ok: true, screenshotId: 2, status: 'successful', state: 'archived' });
    await renderWith([shot(2, { analysis: analysed({ epitaph: 'Keep me' }) })]);
    c.listScreenshots.mockResolvedValue({ ok: true, screenshots: [shot(2, { state: 'archived', analysis: analysed({ epitaph: 'Keep me' }) })] });
    fireEvent.click(screen.getByRole('button', { name: /safe burial: archive s2\.png/i }));
    await screen.findByText(/moved to the crypt/i);
    await waitFor(() => expect(cardsIn(/^revived/i)).toEqual([]));
    expect(screen.queryByTestId('epitaph-2')).not.toBeInTheDocument();
    openCrypt();
    await waitFor(() => expect(cardsIn(/^the crypt/i)).toEqual(['s2.png']));
    expect(screen.getByTestId('epitaph-2')).toHaveTextContent('Keep me');
    openWorkspace();
    expect(cardsIn(/^ghosts/i)).toEqual([]);
  });
});

// -------------------------------------------------------------- Ghost interactions

describe('Ghost interactions', () => {
  it('a Ghost offers Revive (not Epitaph editing) and shows Pending in words', async () => {
    await renderWith([shot(1)]);
    const ghosts = region(/^ghosts/i);
    expect(within(ghosts).getByText(/No Epitaph yet/i)).toBeInTheDocument();
    expect(within(ghosts).queryByRole('button', { name: /edit epitaph/i })).not.toBeInTheDocument();
    expect(within(ghosts).getByRole('button', { name: /revive \(start ai analysis\): open s1\.png to review consent first/i })).toBeInTheDocument();
  });

  it('opening Revive shows the privacy notice and consent state but starts NO AI work', async () => {
    await renderWith([shot(1)]);
    fireEvent.click(screen.getByRole('button', { name: /revive \(start ai analysis\)/i }));
    const panel = await screen.findByRole('dialog');
    expect(within(panel).getByRole('note', { name: /privacy notice/i })).toBeInTheDocument();
    expect(await within(panel).findByText(/NOT authorized/i)).toBeInTheDocument();
    expect(a.classifyScreenshot).not.toHaveBeenCalled();
    expect(a.setConsent).not.toHaveBeenCalled();
    expect(a.getOperation).not.toHaveBeenCalled();
  });

  it('loading the page performs no AI action and no write', async () => {
    await renderWith([shot(1), shot(2, { analysis: analysed() })]);
    for (const fn of [a.classifyScreenshot, a.setConsent, a.editAnalysis, a.generateStudyNote, c.archiveScreenshot, c.restoreScreenshot]) {
      expect(fn).not.toHaveBeenCalled();
    }
  });

  it('a Ghost becomes Revived when its analysis really exists, without a reload', async () => {
    a.getConsent.mockImplementation(() => ok(true));
    a.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    a.getOperation.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'successful', errorDetail: null }));
    await renderWith([shot(1)]);
    expect(cardsIn(/^ghosts/i)).toEqual(['s1.png']);
    fireEvent.click(screen.getByRole('button', { name: /revive \(start ai analysis\)/i }));
    const go = await screen.findByRole('button', { name: /revive: analyze with ai/i });
    await waitFor(() => expect(go).toBeEnabled());
    a.getAnalysis.mockImplementation(() => ok(serverAnalysis(1, 'Fresh epitaph', 'ai_generate')));   // now it exists
    fireEvent.click(go);
    await waitFor(() => expect(cardsIn(/^revived/i)).toEqual(['s1.png']));
    expect(cardsIn(/^ghosts/i)).toEqual([]);
    expect(screen.getByTestId('epitaph-1')).toHaveTextContent('Fresh epitaph');
    expect(a.classifyScreenshot).toHaveBeenCalledTimes(1);
  });

  it('a failed Revive leaves the screenshot a Ghost (the state did not change)', async () => {
    a.getConsent.mockImplementation(() => ok(true));
    a.classifyScreenshot.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'pending', errorDetail: null, warning: null }));
    a.getOperation.mockImplementation(() => ok({ operationId: 5, kind: 'classify', status: 'failed', errorDetail: 'AI analysis failed' }));
    await renderWith([shot(1)]);
    fireEvent.click(screen.getByRole('button', { name: /revive \(start ai analysis\)/i }));
    const go = await screen.findByRole('button', { name: /revive: analyze with ai/i });
    await waitFor(() => expect(go).toBeEnabled());
    fireEvent.click(go);
    expect(await screen.findByText(/Status: Failed/)).toBeInTheDocument();
    expect(cardsIn(/^ghosts/i)).toEqual(['s1.png']);
    expect(cardsIn(/^revived/i)).toEqual([]);
  });
});

// ------------------------------------------------------------ Epitaph interactions

describe('Epitaph interactions (Req 5.5)', () => {
  it('shows the stored Epitaph with honest provenance for demo, real and manual values', async () => {
    await renderWith([
      shot(1, { analysis: analysed({ epitaph: 'A', epitaphOrigin: 'ai_generate' }) }),
      shot(2, { analysis: analysed({ epitaph: 'B', epitaphOrigin: 'ai_redo' }) }),
      shot(3, { analysis: analysed({ epitaph: 'C', epitaphOrigin: 'manual_edit' }) }),
    ]);
    const card = (id: number) => screen.getByTestId(`epitaph-${id}`).closest('li') as HTMLElement;
    expect(within(card(1)).getByText(/generated by the demo AI \(placeholder text, not real image understanding\)/)).toBeInTheDocument();
    expect(within(card(2)).getByText(/regenerated by the demo AI/)).toBeInTheDocument();
    expect(within(card(3)).getByText(/edited by you/)).toBeInTheDocument();
  });

  it('never calls demo output genuine understanding, and labels real mode as a fallible suggestion', async () => {
    a.getProviderMode.mockImplementation(() => ok('openai'));
    await renderWith([shot(1, { analysis: analysed() })]);
    expect(await screen.findByTestId('ai-provider-note')).toHaveTextContent(/Real AI mode.*sent to an external AI service.*can be wrong/i);
    expect(screen.getByText(/generated by AI \(a suggestion; it can be wrong\)/)).toBeInTheDocument();
    expect(screen.queryByText(/demo AI/i)).not.toBeInTheDocument();
  });

  it('labels demo mode and shows no banner when the mode is unknown', async () => {
    await renderWith([shot(1)]);
    expect(await screen.findByTestId('ai-provider-note')).toHaveTextContent(/Demo AI mode.*do not read your images.*Nothing is sent/i);
  });

  it('shows no provider banner (and no guess) when the mode cannot be read', async () => {
    a.getProviderMode.mockImplementation(() => fail(503));
    await renderWith([shot(1, { analysis: analysed() })]);
    expect(screen.queryByTestId('ai-provider-note')).not.toBeInTheDocument();
    expect(screen.getByText(/Epitaph generated by AI\./)).toBeInTheDocument();
  });

  it('edits the REAL stored value through the existing endpoint and shows what the server returns', async () => {
    a.editAnalysis.mockImplementation(() => ok(serverAnalysis(2, 'Server-normalised caption')));
    await renderWith([shot(2, { analysis: analysed({ epitaph: 'Old caption' }) })]);
    fireEvent.click(screen.getByRole('button', { name: /edit epitaph of s2\.png/i }));
    const box = screen.getByLabelText('Epitaph for s2.png') as HTMLTextAreaElement;
    expect(box.value).toBe('Old caption');                       // starts from the stored value
    fireEvent.change(box, { target: { value: '   My own words  ' } });
    fireEvent.click(screen.getByRole('button', { name: /save epitaph/i }));
    await waitFor(() => expect(a.editAnalysis).toHaveBeenCalledWith(2, { epitaphText: 'My own words' }));
    // The card shows the server's response, not the typed text.
    await waitFor(() => expect(screen.getByTestId('epitaph-2')).toHaveTextContent('Server-normalised caption'));
    expect(screen.getByText(/edited by you/)).toBeInTheDocument();
    expect(screen.getByText(/saved as a new version\. Earlier versions are kept/i)).toBeInTheDocument();
    expect(screen.queryByLabelText('Epitaph for s2.png')).not.toBeInTheDocument();    // editor closed
  });

  it('editing the Epitaph never starts AI work or touches the screenshot lifecycle', async () => {
    a.editAnalysis.mockImplementation(() => ok(serverAnalysis(2, 'x')));
    await renderWith([shot(2, { analysis: analysed() })]);
    fireEvent.click(screen.getByRole('button', { name: /edit epitaph/i }));
    fireEvent.change(screen.getByLabelText(/epitaph for/i), { target: { value: 'changed' } });
    fireEvent.click(screen.getByRole('button', { name: /save epitaph/i }));
    await waitFor(() => expect(a.editAnalysis).toHaveBeenCalled());
    for (const fn of [a.classifyScreenshot, a.generateStudyNote, a.setConsent, c.archiveScreenshot, c.restoreScreenshot]) {
      expect(fn).not.toHaveBeenCalled();
    }
    expect(Object.keys(a.editAnalysis.mock.calls[0][1])).toEqual(['epitaphText']);   // category untouched
  });

  it('a failed save keeps the stored text, stays in the editor, and says so honestly', async () => {
    a.editAnalysis.mockImplementation(() => fail(503));
    await renderWith([shot(2, { analysis: analysed({ epitaph: 'Stored' }) })]);
    fireEvent.click(screen.getByRole('button', { name: /edit epitaph/i }));
    fireEvent.change(screen.getByLabelText(/epitaph for/i), { target: { value: 'Unsaved' } });
    fireEvent.click(screen.getByRole('button', { name: /save epitaph/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/temporarily unavailable/i);
    expect(screen.queryByText(/saved as a new version/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(screen.getByTestId('epitaph-2')).toHaveTextContent('Stored');
  });

  it('skips the request when nothing changed and blocks an empty Epitaph', async () => {
    await renderWith([shot(2, { analysis: analysed({ epitaph: 'Same' }) })]);
    fireEvent.click(screen.getByRole('button', { name: /edit epitaph/i }));
    fireEvent.click(screen.getByRole('button', { name: /save epitaph/i }));
    expect(await screen.findByText(/nothing changed/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/epitaph for/i), { target: { value: '   ' } });
    expect(screen.getByRole('button', { name: /save epitaph/i })).toBeDisabled();
    expect(a.editAnalysis).not.toHaveBeenCalled();
  });

  it('prevents a duplicate save from a double click', async () => {
    let release: (v: unknown) => void = () => {};
    a.editAnalysis.mockImplementation(() => new Promise((r) => { release = r; }));
    await renderWith([shot(2, { analysis: analysed() })]);
    fireEvent.click(screen.getByRole('button', { name: /edit epitaph/i }));
    fireEvent.change(screen.getByLabelText(/epitaph for/i), { target: { value: 'new text' } });
    const save = screen.getByRole('button', { name: /save epitaph/i });
    fireEvent.click(save);
    fireEvent.click(save);
    expect(a.editAnalysis).toHaveBeenCalledTimes(1);
    release({ ok: true, data: serverAnalysis(2, 'new text') });
    await waitFor(() => expect(screen.getByTestId('epitaph-2')).toHaveTextContent('new text'));
  });

  it('opening the editor again starts from the latest stored value, not an old draft', async () => {
    await renderWith([shot(2, { analysis: analysed({ epitaph: 'Stored' }) })]);
    fireEvent.click(screen.getByRole('button', { name: /edit epitaph/i }));
    fireEvent.change(screen.getByLabelText(/epitaph for/i), { target: { value: 'abandoned draft' } });
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    fireEvent.click(screen.getByRole('button', { name: /edit epitaph/i }));
    expect((screen.getByLabelText(/epitaph for/i) as HTMLTextAreaElement).value).toBe('Stored');
  });

  it('is available for a screenshot resting in The Crypt (originals are untouched)', async () => {
    a.editAnalysis.mockImplementation(() => ok(serverAnalysis(4, 'Edited in crypt')));
    await renderWith([shot(4, { state: 'archived', analysis: analysed() })]);
    openCrypt();
    fireEvent.click(within(region(/^the crypt/i)).getByRole('button', { name: /edit epitaph/i }));
    fireEvent.change(screen.getByLabelText(/epitaph for/i), { target: { value: 'Edited in crypt' } });
    fireEvent.click(screen.getByRole('button', { name: /save epitaph/i }));
    await waitFor(() => expect(screen.getByTestId('epitaph-4')).toHaveTextContent('Edited in crypt'));
    expect(cardsIn(/^the crypt/i)).toEqual(['s4.png']);          // still archived
    expect(c.restoreScreenshot).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------- Navigation

describe('Top-level navigation', () => {
  const navBtn = (name: RegExp) => within(screen.getByRole('navigation', { name: /main/i })).getByRole('button', { name });

  it('offers Workspace, Creations and The Crypt, with Workspace current first', async () => {
    await renderWith([shot(1)]);
    expect(navBtn(/workspace/i)).toHaveAttribute('aria-current', 'page');
    expect(navBtn(/creations/i)).not.toHaveAttribute('aria-current');
    expect(navBtn(/the crypt/i)).not.toHaveAttribute('aria-current');
  });

  it('switches views by click and shows only that view', async () => {
    await renderWith([shot(1), shot(3, { state: 'archived' })]);
    expect(screen.getByLabelText(/add screenshots/i)).toBeInTheDocument();
    fireEvent.click(navBtn(/creations/i));
    expect(navBtn(/creations/i)).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('heading', { name: /^study notes$/i, level: 2 })).toBeVisible();
    expect(screen.queryByLabelText(/add screenshots/i)).not.toBeInTheDocument();
    fireEvent.click(navBtn(/the crypt/i));
    expect(navBtn(/the crypt/i)).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('region', { name: /^ghosts/i })).not.toBeInTheDocument();
    expect(cardsIn(/^the crypt/i)).toEqual(['s3.png']);
  });

  it('is operable with the keyboard (arrow keys, Home and End move focus and view)', async () => {
    await renderWith([]);
    const ws = navBtn(/workspace/i);
    ws.focus();
    fireEvent.keyDown(ws, { key: 'ArrowRight' });
    expect(navBtn(/creations/i)).toHaveAttribute('aria-current', 'page');
    expect(navBtn(/creations/i)).toHaveFocus();
    fireEvent.keyDown(navBtn(/creations/i), { key: 'End' });
    expect(navBtn(/the crypt/i)).toHaveAttribute('aria-current', 'page');
    fireEvent.keyDown(navBtn(/the crypt/i), { key: 'ArrowRight' });
    expect(navBtn(/workspace/i)).toHaveAttribute('aria-current', 'page');
    fireEvent.keyDown(navBtn(/workspace/i), { key: 'ArrowLeft' });
    expect(navBtn(/the crypt/i)).toHaveFocus();
  });

  it('shows the Crypt count from server data and a useful empty state', async () => {
    await renderWith([shot(3, { state: 'archived' })]);
    expect(navBtn(/the crypt/i)).toHaveTextContent('(1)');
  });

  it('collapses the long privacy details behind an expander', async () => {
    await renderWith([]);
    fireEvent.click(navBtn(/creations/i));
    const note = screen.getByRole('note', { name: /privacy notice/i });
    const details = note.querySelector('details') as HTMLDetailsElement;
    expect(details).not.toBeNull();
    expect(details.open).toBe(false);
    expect(within(note).getByText(/read the full privacy details/i)).toBeInTheDocument();
  });
});
