import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Screenshot } from '../api/client';

const m = vi.hoisted(() => ({
  listStudyNotes: vi.fn(),
  getConsent: vi.fn(),
  setConsent: vi.fn(),
  generateStudyNote: vi.fn(),
  getOperation: vi.fn(),
  getStudyNote: vi.fn(),
  listStudyNoteVersions: vi.fn(),
  saveStudyNoteEdit: vi.fn(),
  getProviderMode: vi.fn(),
}));
vi.mock('../api/ai', async (orig) => ({ ...(await orig<typeof import('../api/ai')>()), ...m }));

import { StudyNotesPanel } from './StudyNotesPanel';

const mk = (n: number): Screenshot[] =>
  Array.from({ length: n }, (_, i) => ({
    screenshotId: i + 1, state: 'active' as const, mimeType: 'image/png', originalFilename: `s${i + 1}.png`,
    isDuplicate: false, duplicateOf: null, byteSize: 10, uploadedAt: '2026-11-07T00:00:00+00:00',
  }));
const ok = <T,>(data: T) => Promise.resolve({ ok: true as const, data });
const fail = (status: number, code = 'x', message = 'm') => Promise.resolve({ ok: false as const, status, code, message });
const accepted = (status = 'pending', warning: string | null = null) =>
  ok({ operationId: 9, kind: 'study_note', status, errorDetail: null, warning });
const op = (status: string, errorDetail: string | null = null, artifactId: number | null = null) =>
  ok({ operationId: 9, kind: 'study_note', status, errorDetail, artifactId });

const renderPanel = (n = 6, extra = {}) =>
  render(<StudyNotesPanel screenshots={mk(n)} pollIntervalMs={1} maxPolls={20} {...extra} />);
const select = (name: string) => fireEvent.click(screen.getByRole('button', { name: new RegExp(`Add ${name} to`) }));
const names = () => within(screen.getByRole('list', { name: /selected screenshots in order/i })).getAllByRole('listitem').map((li) => li.textContent ?? '');

beforeEach(() => {
  vi.clearAllMocks();
  m.listStudyNotes.mockImplementation(() => ok([]));
  m.getConsent.mockImplementation(() => ok(true));
  m.getProviderMode.mockImplementation(() => ok('fake'));
});

describe('StudyNotesPanel: selection', () => {
  it('does not invoke AI or write anything on page load', async () => {
    renderPanel();
    await waitFor(() => expect(m.listStudyNotes).toHaveBeenCalled());
    expect(m.generateStudyNote).not.toHaveBeenCalled();
    expect(m.setConsent).not.toHaveBeenCalled();
    expect(m.getOperation).not.toHaveBeenCalled();
  });

  it('shows the privacy warning and the 1-12 / 4-8 guidance before anything is sent', async () => {
    renderPanel();
    await waitFor(() => {
      expect(screen.getByRole('note', { name: /privacy notice/i })).toHaveTextContent(/fake adapter/i);
    });
    expect(screen.getByText(/1 to 12.*4 to 8/i)).toBeInTheDocument();
  });

  it('keeps the user\u2019s selection ORDER and lets them reorder', async () => {
    renderPanel();
    select('s3.png'); select('s1.png'); select('s2.png');
    await waitFor(() => expect(names()).toHaveLength(3));
    expect(names().map((t) => t.slice(0, 6))).toEqual(['s3.png', 's1.png', 's2.png']);
    fireEvent.click(screen.getByRole('button', { name: 'Move s2.png up' }));
    expect(names().map((t) => t.slice(0, 6))).toEqual(['s3.png', 's2.png', 's1.png']);
    expect(screen.getByTestId('selection-count')).toHaveTextContent('3 screenshots selected');
  });

  it('shows a NON-blocking warning exactly outside 4-8 (3 warns, 4 and 8 do not, 9 warns)', async () => {
    renderPanel(9);
    const warning = /work best with 4 to 8/i;
    const gen = () => screen.getByRole('button', { name: /generate study note/i });
    for (let i = 1; i <= 3; i++) select(`s${i}.png`);
    expect(await screen.findByText(warning)).toBeInTheDocument(); // 3 -> warns
    await waitFor(() => expect(gen()).toBeEnabled()); // ...but never blocks
    select('s4.png');
    await waitFor(() => expect(screen.queryByText(warning)).not.toBeInTheDocument()); // 4 -> none
    for (let i = 5; i <= 8; i++) select(`s${i}.png`);
    await waitFor(() => expect(screen.getByTestId('selection-count')).toHaveTextContent('8 screenshots'));
    expect(screen.queryByText(warning)).not.toBeInTheDocument(); // 8 -> none
    select('s9.png');
    expect(await screen.findByText(warning)).toBeInTheDocument(); // 9 -> warns
    await waitFor(() => expect(gen()).toBeEnabled());
  });

  it('refuses a 13th selection and keeps Generate disabled with no selection', async () => {
    renderPanel(13);
    expect(screen.getByRole('button', { name: /generate study note/i })).toBeDisabled();
    expect(screen.getByText(/select at least 1/i)).toBeInTheDocument();
    for (let i = 1; i <= 12; i++) select(`s${i}.png`);
    select('s13.png');
    expect(await screen.findByRole('alert')).toHaveTextContent(/at most 12/i);
    expect(screen.getByTestId('selection-count')).toHaveTextContent('12 screenshots selected');
  });
});

describe('StudyNotesPanel: consent review', () => {
  it('blocks generation until every selected screenshot is authorized, then asks once explicitly', async () => {
    m.getConsent.mockImplementation((id: number) => ok(id !== 2));
    m.setConsent.mockImplementation(() => ok(true));
    renderPanel(4);
    select('s1.png'); select('s2.png'); select('s3.png'); select('s4.png');
    expect(await screen.findByText(/s2\.png.*not authorized/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /generate study note/i })).toBeDisabled();
    expect(screen.getByText(/grant ai consent for every selected/i)).toBeInTheDocument();
    expect(m.setConsent).not.toHaveBeenCalled(); // nothing granted implicitly

    fireEvent.click(screen.getByRole('button', { name: /authorize simulated demo processing for 1 screenshot/i }));
    await waitFor(() => expect(m.setConsent).toHaveBeenCalledTimes(1));
    expect(m.setConsent).toHaveBeenCalledWith(2, true); // only the ungranted one
    await waitFor(() => expect(screen.getByRole('button', { name: /generate study note/i })).toBeEnabled());
  });
});

describe('StudyNotesPanel: generation + polling', () => {
  const note = {
    artifactId: 40, versionId: 1, versionNumber: 1, origin: 'ai_generate', sourceScreenshotIds: [3, 1, 2],
    content: { topic: 'Generated topic', main_concepts: ['c'], extracted_code: [], code_explanations: [], pitfalls: [], uncertain_content: [], references: [3, 1, 2] },
  };
  beforeEach(() => {
    m.getStudyNote.mockImplementation(() => ok(note));
    m.listStudyNoteVersions.mockImplementation(() => ok([{ versionId: 1, versionNumber: 1, origin: 'ai_generate', isCurrent: true }]));
  });

  const pick = async () => {
    renderPanel(4);
    select('s3.png'); select('s1.png'); select('s2.png');
    await waitFor(() => expect(screen.getByRole('button', { name: /generate study note/i })).toBeEnabled());
  };

  it('sends the ids in selection order, polls, then shows the new note', async () => {
    m.generateStudyNote.mockImplementation(() => accepted());
    m.getOperation.mockImplementationOnce(() => op('pending')).mockImplementation(() => op('successful', null, 40));
    m.listStudyNotes
      .mockImplementationOnce(() => ok([]))
      .mockImplementation(() => ok([
        { artifactId: 99, versionNumber: 1, origin: 'ai_generate', topic: 'Concurrent note', sourceScreenshotIds: [4] },
        { artifactId: 40, versionNumber: 1, origin: 'ai_generate', topic: 'Generated topic', sourceScreenshotIds: [3, 1, 2] },
      ]));
    await pick();
    const go = screen.getByRole('button', { name: /generate study note/i });
    fireEvent.click(go);
    fireEvent.click(go); // duplicate click
    expect(await screen.findByText(/Status: Successful/)).toBeInTheDocument();
    expect(m.generateStudyNote).toHaveBeenCalledTimes(1);
    expect(m.generateStudyNote).toHaveBeenCalledWith([3, 1, 2]);
    expect(await screen.findByRole('heading', { name: 'Generated topic' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Concurrent note' })).not.toBeInTheDocument();
  });

  it('shows the server\u2019s non-blocking warning and still proceeds', async () => {
    m.generateStudyNote.mockImplementation(() => accepted('pending', 'Study notes work best with 4 to 8 related screenshots; you can still continue with this selection.'));
    m.getOperation.mockImplementation(() => op('successful'));
    await pick();
    fireEvent.click(screen.getByRole('button', { name: /generate study note/i }));
    expect(await screen.findByText(/Status: Successful/)).toBeInTheDocument();
    expect(screen.getAllByText(/work best with 4 to 8/i).length).toBeGreaterThan(0);
  });

  it('explains a FAILED generation and does not open a note', async () => {
    m.generateStudyNote.mockImplementation(() => accepted());
    m.getOperation.mockImplementation(() => op('failed', 'AI output failed validation'));
    await pick();
    fireEvent.click(screen.getByRole('button', { name: /generate study note/i }));
    expect(await screen.findByText(/Status: Failed/)).toBeInTheDocument();
    expect(screen.getByText(/could not trust/i)).toBeInTheDocument();
    expect(m.getStudyNote).not.toHaveBeenCalled();
  });

  it('reports UNCERTAIN as its own state and tells the user to verify', async () => {
    m.generateStudyNote.mockImplementation(() => accepted());
    m.getOperation.mockImplementation(() => op('uncertain'));
    await pick();
    fireEvent.click(screen.getByRole('button', { name: /generate study note/i }));
    expect(await screen.findByText(/Status: Uncertain/)).toBeInTheDocument();
    expect(screen.getByText(/check\s+it against the original screenshots/i)).toBeInTheDocument();
  });

  it('handles server refusal (403) and quota (429) in plain words', async () => {
    m.generateStudyNote.mockImplementationOnce(() => fail(403, 'consent_required'));
    await pick();
    fireEvent.click(screen.getByRole('button', { name: /generate study note/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/consent is required first/i);
    expect(m.getOperation).not.toHaveBeenCalled();

    m.generateStudyNote.mockImplementationOnce(() => fail(429, 'quota_exceeded'));
    await waitFor(() => expect(screen.getByRole('button', { name: /generate study note/i })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /generate study note/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/allowance/i));
  });

  it('stops polling at the bound and on unmount', async () => {
    m.generateStudyNote.mockImplementation(() => accepted());
    m.getOperation.mockImplementation(() => op('pending'));
    renderPanel(4, { maxPolls: 3 });
    select('s1.png');
    await waitFor(() => expect(screen.getByRole('button', { name: /generate study note/i })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /generate study note/i }));
    expect(await screen.findByText(/Stopped checking/i)).toBeInTheDocument();
    expect(m.getOperation).toHaveBeenCalledTimes(3);
  });

  it('cancels polling when unmounted mid-operation', async () => {
    m.generateStudyNote.mockImplementation(() => accepted());
    m.getOperation.mockImplementation(() => op('pending'));
    const { unmount } = renderPanel(4, { maxPolls: 1000 });
    select('s1.png');
    await waitFor(() => expect(screen.getByRole('button', { name: /generate study note/i })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /generate study note/i }));
    await waitFor(() => expect(m.getOperation).toHaveBeenCalled());
    unmount();
    await new Promise((r) => setTimeout(r, 10));
    const n = m.getOperation.mock.calls.length;
    await new Promise((r) => setTimeout(r, 40));
    expect(m.getOperation.mock.calls.length).toBe(n);
  });
});
