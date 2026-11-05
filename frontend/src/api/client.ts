// Minimal, typed API client for the Screenshot Graveyard backend.
//
// Only the health endpoint is implemented at this stage (Task 0.4). The client
// validates the response shape and never fabricates a successful result: any
// network failure, non-2xx status, or unexpected body is reported as an error.

export interface HealthResponse {
  status: string;
}

export type HealthResult =
  | { ok: true; status: string }
  | { ok: false; error: string };

function isHealthResponse(value: unknown): value is HealthResponse {
  return (
    typeof value === 'object' &&
    value !== null &&
    'status' in value &&
    typeof (value as Record<string, unknown>).status === 'string'
  );
}

export async function getHealth(
  fetchImpl: typeof fetch = fetch,
): Promise<HealthResult> {
  let response: Response;
  try {
    response = await fetchImpl('/api/health', {
      headers: { Accept: 'application/json' },
    });
  } catch {
    // Network-level failure (backend down, DNS, CORS, offline, etc.).
    return { ok: false, error: 'Network request failed' };
  }

  if (!response.ok) {
    return { ok: false, error: `HTTP ${response.status}` };
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { ok: false, error: 'Invalid JSON response' };
  }

  if (!isHealthResponse(body)) {
    return { ok: false, error: 'Unexpected response shape' };
  }

  return { ok: true, status: body.status };
}
