import { describe, it, expect, vi } from 'vitest';
import {
  classifyScreenshot,
  editAnalysis,
  generateStudyNote,
  getAnalysis,
  getConsent,
  getProviderMode,
  getOperation,
  saveStudyNoteEdit,
  setConsent,
  studyNoteExportUrl,
} from './ai';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const fetchOf = (r: () => Response | Promise<Response>) =>
  vi.fn(() => Promise.resolve(r())) as unknown as typeof fetch & ReturnType<typeof vi.fn>;

describe('AI API client', () => {
  it('sends the CSRF header + same-origin credentials on every POST, none on GET', async () => {
    const f = fetchOf(() => json({ granted: true }));
    await setConsent(3, true, f);
    await getConsent(3, f);
    const [, post] = (f as ReturnType<typeof vi.fn>).mock.calls[0];
    const [, get] = (f as ReturnType<typeof vi.fn>).mock.calls[1];
    expect(post.method).toBe('POST');
    expect(post.headers['X-Requested-With']).toBe('fetch');
    expect(post.credentials).toBe('same-origin');
    expect(JSON.parse(post.body)).toEqual({ granted: true });
    expect(get.method).toBe('GET');
    expect(get.headers['X-Requested-With']).toBeUndefined();
  });

  it('never puts a workspace id in a request', async () => {
    const f = fetchOf(() => json({ operationId: 1, kind: 'study_note', status: 'pending', warning: null }, 202));
    await generateStudyNote([5, 2, 9], f);
    const [url, init] = (f as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('/api/study-notes');
    expect(JSON.parse(init.body)).toEqual({ screenshotIds: [5, 2, 9] }); // order preserved
    expect(JSON.stringify(init)).not.toMatch(/workspace/i);
  });

  it('keeps status + code for consent (403) and quota (429) failures', async () => {
    const r403 = await classifyScreenshot(1, fetchOf(() => json({ error: { code: 'consent_required', message: 'x' } }, 403)));
    const r429 = await classifyScreenshot(1, fetchOf(() => json({ error: { code: 'quota_exceeded', message: 'y' } }, 429)));
    expect(r403).toMatchObject({ ok: false, status: 403, code: 'consent_required' });
    expect(r429).toMatchObject({ ok: false, status: 429, code: 'quota_exceeded' });
  });

  it('reports a network failure as status 0, never a fake success', async () => {
    const f = vi.fn(() => Promise.reject(new Error('down'))) as unknown as typeof fetch;
    expect(await getOperation(1, f)).toMatchObject({ ok: false, status: 0, code: 'network' });
  });

  it('rejects malformed success bodies instead of trusting them', async () => {
    expect(await getOperation(1, fetchOf(() => json({ operationId: 1, kind: 'classify', status: 'weird' })))).toMatchObject({ ok: false, code: 'bad_shape' });
    expect(await getAnalysis(1, fetchOf(() => json({ screenshotId: 1 })))).toMatchObject({ ok: false, code: 'bad_shape' });
    expect(await getConsent(1, fetchOf(() => json({ granted: 'yes' })))).toMatchObject({ ok: false, code: 'bad_shape' });
  });

  it('accepts only the public fake/openai provider-mode contract', async () => {
    expect(await getProviderMode(fetchOf(() => json({ mode: 'fake' })))).toMatchObject({ ok: true, data: 'fake' });
    expect(await getProviderMode(fetchOf(() => json({ mode: 'openai', apiKey: 'never exposed' })))).toMatchObject({ ok: true, data: 'openai' });
    expect(await getProviderMode(fetchOf(() => json({ mode: 'other' })))).toMatchObject({ ok: false, code: 'bad_shape' });
  });

  it('preserves an operation-linked artifact ID only when it is a number', async () => {
    const result = await getOperation(1, fetchOf(() => json({
      operationId: 1, kind: 'study_note', status: 'successful', artifactId: 44,
    })));
    expect(result).toMatchObject({ ok: true, data: { artifactId: 44 } });
  });

  it('parses a persisted analysis including visible facts and uncertainty', async () => {
    const body = {
      screenshotId: 4,
      classification: { category: 'Study/Knowledge', confidence: 0.8, uncertain: false, versionNumber: 1, origin: 'ai_generate' },
      epitaph: { text: 'T', visibleFacts: 'F', inferredIntent: null, uncertain: true, versionNumber: 1, origin: 'ai_generate' },
    };
    const r = await getAnalysis(4, fetchOf(() => json(body)));
    expect(r.ok && r.data.epitaph.visibleFacts).toBe('F');
    expect(r.ok && r.data.epitaph.uncertain).toBe(true);
  });

  it('sends analysis edits in the wire format the server expects', async () => {
    const f = fetchOf(() => json({}, 400));
    await editAnalysis(2, { category: 'Study/Knowledge', epitaphText: 'mine' }, f);
    const [url, init] = (f as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('/api/screenshots/2/analysis/edits');
    expect(JSON.parse(init.body)).toEqual({ category: 'Study/Knowledge', epitaphText: 'mine' });
  });

  it('sends study-note edits as JSON content and builds export URLs', async () => {
    const f = fetchOf(() => json({}, 400));
    const content = { topic: 'T', main_concepts: [], extracted_code: ['a\n\tb'], code_explanations: [], pitfalls: [], uncertain_content: [] };
    await saveStudyNoteEdit(7, content, f);
    expect(JSON.parse((f as ReturnType<typeof vi.fn>).mock.calls[0][1].body)).toEqual({ content });
    expect(studyNoteExportUrl(7)).toBe('/api/study-notes/7/export');
    expect(studyNoteExportUrl(7, 12)).toBe('/api/study-notes/7/export?versionId=12');
  });
});
