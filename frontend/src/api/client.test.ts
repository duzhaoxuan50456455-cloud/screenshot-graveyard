import { describe, it, expect, vi } from 'vitest';
import { getHealth } from './client';

function mockFetch(resolver: () => Promise<Response> | Response) {
  return vi.fn(() => Promise.resolve(resolver())) as unknown as typeof fetch;
}

describe('getHealth', () => {
  it('returns ok with status on a valid 200 response', async () => {
    const fetchImpl = mockFetch(
      () =>
        new Response(JSON.stringify({ status: 'ok' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const result = await getHealth(fetchImpl);
    expect(result).toEqual({ ok: true, status: 'ok' });
  });

  it('reports an error on a non-2xx response', async () => {
    const fetchImpl = mockFetch(() => new Response('nope', { status: 500 }));
    const result = await getHealth(fetchImpl);
    expect(result).toEqual({ ok: false, error: 'HTTP 500' });
  });

  it('reports an error when the network request throws', async () => {
    const fetchImpl = vi.fn(() =>
      Promise.reject(new Error('down')),
    ) as unknown as typeof fetch;
    const result = await getHealth(fetchImpl);
    expect(result).toEqual({ ok: false, error: 'Network request failed' });
  });

  it('reports an error on invalid JSON', async () => {
    const fetchImpl = mockFetch(
      () =>
        new Response('not json', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const result = await getHealth(fetchImpl);
    expect(result).toEqual({ ok: false, error: 'Invalid JSON response' });
  });

  it('reports an error on an unexpected response shape', async () => {
    const fetchImpl = mockFetch(
      () =>
        new Response(JSON.stringify({ unexpected: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const result = await getHealth(fetchImpl);
    expect(result).toEqual({ ok: false, error: 'Unexpected response shape' });
  });
});
