import { describe, it, expect, vi } from 'vitest';
import {
  archiveScreenshot,
  getWorkspace,
  listScreenshots,
  restoreScreenshot,
  uploadScreenshots,
} from './client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('getWorkspace', () => {
  it('returns active=true on a valid response', async () => {
    const f = vi.fn(() => Promise.resolve(jsonResponse({ workspaceActive: true }))) as unknown as typeof fetch;
    expect(await getWorkspace(f)).toEqual({ ok: true, active: true });
  });

  it('reports an error on an unexpected shape', async () => {
    const f = vi.fn(() => Promise.resolve(jsonResponse({ nope: 1 }))) as unknown as typeof fetch;
    expect(await getWorkspace(f)).toEqual({ ok: false, error: 'Unexpected response shape' });
  });
});

describe('listScreenshots', () => {
  it('parses a valid screenshot list', async () => {
    const payload = {
      screenshots: [
        {
          screenshotId: 1,
          state: 'active',
          mimeType: 'image/png',
          originalFilename: 'a.png',
          isDuplicate: false,
          duplicateOf: null,
          byteSize: 10,
          uploadedAt: '2026-11-07T00:00:00+00:00',
        },
      ],
    };
    const f = vi.fn(() => Promise.resolve(jsonResponse(payload))) as unknown as typeof fetch;
    const result = await listScreenshots(f);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.screenshots).toHaveLength(1);
  });

  it('reports an error on a bad item shape', async () => {
    const f = vi.fn(() =>
      Promise.resolve(jsonResponse({ screenshots: [{ screenshotId: 'x' }] })),
    ) as unknown as typeof fetch;
    expect(await listScreenshots(f)).toEqual({ ok: false, error: 'Unexpected response shape' });
  });
});

describe('uploadScreenshots', () => {
  it('sends the CSRF header and parses a 207 mixed result', async () => {
    const f = vi.fn((_url: string, init?: RequestInit) => {
      // CSRF: custom header must be present on the POST.
      const headers = new Headers(init?.headers);
      expect(headers.get('X-Requested-With')).toBe('fetch');
      expect(init?.method).toBe('POST');
      return Promise.resolve(
        jsonResponse(
          {
            results: [
              { filename: 'ok.png', status: 'accepted', screenshotId: 1, isDuplicate: false },
              { filename: 'bad', status: 'rejected', error: 'nope' },
            ],
          },
          207,
        ),
      );
    }) as unknown as typeof fetch;
    const result = await uploadScreenshots([new File(['x'], 'ok.png')], f);
    expect(result).toMatchObject({ ok: true, accepted: 1, rejected: 1 });
  });

  it('surfaces a 400 error-envelope message', async () => {
    const f = vi.fn(() =>
      Promise.resolve(jsonResponse({ error: { code: 'no_files', message: 'No files provided' } }, 400)),
    ) as unknown as typeof fetch;
    expect(await uploadScreenshots([], f)).toEqual({ ok: false, error: 'No files provided' });
  });

  it('does not treat a results-shaped 500 response as an upload success', async () => {
    const f = vi.fn(() =>
      Promise.resolve(jsonResponse({ results: [] }, 500)),
    ) as unknown as typeof fetch;
    expect(await uploadScreenshots([new File(['x'], 'ok.png')], f)).toEqual({
      ok: false,
      error: 'HTTP 500',
    });
  });
});

describe('archive/restore', () => {
  it('archive sends CSRF header and parses the result', async () => {
    const f = vi.fn((_url: string, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      expect(headers.get('X-Requested-With')).toBe('fetch');
      return Promise.resolve(
        jsonResponse({ screenshotId: 2, status: 'successful', state: 'archived' }),
      );
    }) as unknown as typeof fetch;
    expect(await archiveScreenshot(2, f)).toEqual({
      ok: true,
      screenshotId: 2,
      status: 'successful',
      state: 'archived',
    });
  });

  it('restore surfaces an honest failed/archived result without error', async () => {
    const f = vi.fn(() =>
      Promise.resolve(jsonResponse({ screenshotId: 3, status: 'failed', state: 'archived' })),
    ) as unknown as typeof fetch;
    expect(await restoreScreenshot(3, f)).toEqual({
      ok: true,
      screenshotId: 3,
      status: 'failed',
      state: 'archived',
    });
  });
});
