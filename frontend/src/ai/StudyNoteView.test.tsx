import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

const m = vi.hoisted(() => ({
  getStudyNote: vi.fn(),
  listStudyNoteVersions: vi.fn(),
  saveStudyNoteEdit: vi.fn(),
}));
vi.mock('../api/ai', async (orig) => ({ ...(await orig<typeof import('../api/ai')>()), ...m }));

import { StudyNoteView } from './StudyNoteView';

const CODE = 'def f(x):\n\treturn  x  # \u03bb\n    pass   ';
const ok = <T,>(data: T) => Promise.resolve({ ok: true as const, data });
const version = (n: number, origin: string, over = {}) => ({
  artifactId: 40, versionId: 100 + n, versionNumber: n, origin, sourceScreenshotIds: [5, 2, 9],
  content: {
    topic: 'C++ functions', main_concepts: ['parameters'], extracted_code: [CODE], code_explanations: ['Returns x.'],
    pitfalls: ['Missing type.'], uncertain_content: ['Could not read line 4'], references: [5, 2, 9], ...over,
  },
});
const versions = [
  { versionId: 101, versionNumber: 1, origin: 'ai_generate', isCurrent: false },
  { versionId: 102, versionNumber: 2, origin: 'manual_edit', isCurrent: true },
];

beforeEach(() => {
  vi.clearAllMocks();
  m.listStudyNoteVersions.mockImplementation(() => ok(versions));
  m.getStudyNote.mockImplementation((_id: number, vid?: number) => ok(vid === 101 ? version(1, 'ai_generate') : version(2, 'manual_edit')));
});

describe('StudyNoteView', () => {
  it('shows every required section, with code exactly as stored', async () => {
    render(<StudyNoteView artifactId={40} />);
    expect(await screen.findByRole('heading', { name: 'C++ functions' })).toBeInTheDocument();
    for (const h of ['Main concepts', 'Extracted code', 'Code explanations', 'Important pitfalls', 'Uncertain/ambiguous content', /Sources/]) {
      expect(screen.getByRole('heading', { name: h })).toBeInTheDocument();
    }
    const code = document.querySelector('pre code') as HTMLElement;
    expect(code.textContent).toBe(CODE); // character-for-character
    expect(screen.getByText(/Could not read line 4/)).toBeInTheDocument(); // uncertainty explicit
  });

  it('lets the user reopen each referenced original, in source order', async () => {
    render(<StudyNoteView artifactId={40} />);
    await screen.findByRole('heading', { name: /Sources/ });
    const links = screen.getAllByRole('link', { name: /open original screenshot/i });
    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      '/api/screenshots/5/image', '/api/screenshots/2/image', '/api/screenshots/9/image',
    ]);
    for (const a of links) {
      expect(a).toHaveAttribute('target', '_blank');
      expect(a).toHaveAttribute('rel', expect.stringContaining('noopener'));
    }
  });

  it('lists retained versions and reads a historical one as read-only', async () => {
    render(<StudyNoteView artifactId={40} />);
    await screen.findByRole('heading', { name: 'C++ functions' });
    expect(screen.getByText(/Version 2 \(edited by you\) — current/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^v1 \(AI-generated\)/ }));
    await waitFor(() => expect(m.getStudyNote).toHaveBeenCalledWith(40, 101));
    expect(await screen.findByText(/Version 1 \(AI-generated\) — earlier version, read only/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /edit from version 1/i })).toBeInTheDocument();
  });

  it('offers Markdown export for the SHOWN version via the server endpoint', async () => {
    render(<StudyNoteView artifactId={40} />);
    const link = await screen.findByRole('link', { name: /export version 2 as markdown/i });
    expect(link).toHaveAttribute('href', '/api/study-notes/40/export?versionId=102');
    expect(link).toHaveAttribute('download');
    fireEvent.click(screen.getByRole('button', { name: /^v1/ }));
    expect(await screen.findByRole('link', { name: /export version 1 as markdown/i })).toHaveAttribute('href', '/api/study-notes/40/export?versionId=101');
  });

  it('edits through the existing endpoint and keeps code exactly as typed', async () => {
    m.saveStudyNoteEdit.mockImplementation(() => ok(version(3, 'manual_edit', { topic: 'New topic' })));
    render(<StudyNoteView artifactId={40} />);
    fireEvent.click(await screen.findByRole('button', { name: /edit this note/i }));
    fireEvent.change(screen.getByLabelText('Topic'), { target: { value: 'New topic' } });
    fireEvent.change(screen.getByLabelText('Main concepts (one per line)'), { target: { value: 'a\n\nb\n' } });
    const code = screen.getByLabelText('Code block 1') as HTMLTextAreaElement;
    expect(code.value).toBe(CODE); // not normalised when loaded into the form
    const submit = screen.getByRole('button', { name: /save as new version/i });
    fireEvent.click(submit);
    fireEvent.click(submit); // duplicate click must not double-save
    await waitFor(() => expect(m.saveStudyNoteEdit).toHaveBeenCalledTimes(1));
    expect(m.saveStudyNoteEdit).toHaveBeenCalledWith(40, {
      topic: 'New topic', main_concepts: ['a', 'b'], extracted_code: [CODE],
      code_explanations: ['Returns x.'], pitfalls: ['Missing type.'], uncertain_content: ['Could not read line 4'],
    });
    expect(await screen.findByText(/Saved as version 3.*Earlier versions are kept/i)).toBeInTheDocument();
  });

  it('shows a save failure honestly and stays in the editor', async () => {
    m.saveStudyNoteEdit.mockImplementation(() => Promise.resolve({ ok: false as const, status: 400, code: 'invalid_content', message: 'm' }));
    render(<StudyNoteView artifactId={40} />);
    fireEvent.click(await screen.findByRole('button', { name: /edit this note/i }));
    fireEvent.click(screen.getByRole('button', { name: /save as new version/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/not valid/i);
    expect(screen.getByLabelText('Topic')).toBeInTheDocument();
  });

  it('shows an error state when the note cannot be loaded', async () => {
    m.getStudyNote.mockImplementation(() => Promise.resolve({ ok: false as const, status: 404, code: 'not_found', message: 'm' }));
    render(<StudyNoteView artifactId={40} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not load this study note/i);
  });
});
