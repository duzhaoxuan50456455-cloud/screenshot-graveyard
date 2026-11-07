// Minimal, typed API client for the Screenshot Graveyard backend.
//
// All requests are same-origin; the workspace identity travels in an HttpOnly
// cookie the browser attaches automatically (the client NEVER sends or reads a
// workspace id). State-changing requests (POST) send the `X-Requested-With`
// header required by the server's CSRF defense. The client never fabricates a
// successful result: any network failure, non-2xx status, or unexpected body is
// reported as an error.

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

// --------------------------------------------------------------- workspace

export type WorkspaceResult =
  | { ok: true; active: boolean }
  | { ok: false; error: string };

export async function getWorkspace(
  fetchImpl: typeof fetch = fetch,
): Promise<WorkspaceResult> {
  let response: Response;
  try {
    // A GET that the WorkspaceMiddleware uses to mint/resume the cookie.
    response = await fetchImpl('/api/workspace', {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
  } catch {
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
  if (
    typeof body !== 'object' ||
    body === null ||
    typeof (body as Record<string, unknown>).workspaceActive !== 'boolean'
  ) {
    return { ok: false, error: 'Unexpected response shape' };
  }
  return { ok: true, active: (body as { workspaceActive: boolean }).workspaceActive };
}

// --------------------------------------------------------------- screenshots

export type ScreenshotState = 'active' | 'archived';

export interface Screenshot {
  screenshotId: number;
  state: ScreenshotState;
  mimeType: string;
  originalFilename: string;
  isDuplicate: boolean;
  duplicateOf: number | null;
  byteSize: number;
  uploadedAt: string;
}

export type ListResult =
  | { ok: true; screenshots: Screenshot[] }
  | { ok: false; error: string };

function isScreenshot(value: unknown): value is Screenshot {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.screenshotId === 'number' &&
    (v.state === 'active' || v.state === 'archived') &&
    typeof v.mimeType === 'string' &&
    typeof v.originalFilename === 'string' &&
    typeof v.isDuplicate === 'boolean' &&
    (v.duplicateOf === null || typeof v.duplicateOf === 'number') &&
    typeof v.byteSize === 'number' &&
    typeof v.uploadedAt === 'string'
  );
}

export async function listScreenshots(
  fetchImpl: typeof fetch = fetch,
): Promise<ListResult> {
  let response: Response;
  try {
    response = await fetchImpl('/api/screenshots', {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
  } catch {
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
  if (
    typeof body !== 'object' ||
    body === null ||
    !Array.isArray((body as Record<string, unknown>).screenshots)
  ) {
    return { ok: false, error: 'Unexpected response shape' };
  }
  const raw = (body as { screenshots: unknown[] }).screenshots;
  if (!raw.every(isScreenshot)) {
    return { ok: false, error: 'Unexpected response shape' };
  }
  return { ok: true, screenshots: raw as Screenshot[] };
}

// The authorized bytes endpoint. Returned as a URL for an <img src>; the browser
// sends the workspace cookie automatically. Never a static/public path.
export function imageUrl(screenshotId: number): string {
  return `/api/screenshots/${screenshotId}/image`;
}

// Per-file upload outcome mirrored from the backend 201/207 response body.
export interface UploadFileResult {
  filename: string;
  status: 'accepted' | 'rejected';
  screenshotId: number | null;
  isDuplicate: boolean | null;
  error: string | null;
}

export type UploadResult =
  | { ok: true; accepted: number; rejected: number; results: UploadFileResult[] }
  | { ok: false; error: string };

function toFileResult(value: unknown): UploadFileResult | null {
  if (typeof value !== 'object' || value === null) return null;
  const v = value as Record<string, unknown>;
  if (typeof v.filename !== 'string') return null;
  if (v.status !== 'accepted' && v.status !== 'rejected') return null;
  return {
    filename: v.filename,
    status: v.status,
    screenshotId: typeof v.screenshotId === 'number' ? v.screenshotId : null,
    isDuplicate: typeof v.isDuplicate === 'boolean' ? v.isDuplicate : null,
    error: typeof v.error === 'string' ? v.error : null,
  };
}

export async function uploadScreenshots(
  files: File[],
  fetchImpl: typeof fetch = fetch,
): Promise<UploadResult> {
  const form = new FormData();
  for (const file of files) {
    form.append('files', file, file.name);
  }
  let response: Response;
  try {
    response = await fetchImpl('/api/screenshots', {
      method: 'POST',
      // CSRF: custom header required on state-changing requests.
      headers: { 'X-Requested-With': 'fetch', Accept: 'application/json' },
      credentials: 'same-origin',
      body: form,
    });
  } catch {
    return { ok: false, error: 'Network request failed' };
  }
  if (response.status !== 201 && response.status !== 207 && response.status !== 400) {
    return { ok: false, error: `HTTP ${response.status}` };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { ok: false, error: 'Invalid JSON response' };
  }
  // 400 with an error envelope (e.g. no files) — surface its message.
  if (
    typeof body === 'object' &&
    body !== null &&
    'error' in body &&
    typeof (body as { error?: { message?: unknown } }).error?.message === 'string'
  ) {
    return {
      ok: false,
      error: (body as { error: { message: string } }).error.message,
    };
  }
  if (
    typeof body !== 'object' ||
    body === null ||
    !Array.isArray((body as Record<string, unknown>).results)
  ) {
    return { ok: false, error: `HTTP ${response.status}` };
  }
  const parsed = (body as { results: unknown[] }).results.map(toFileResult);
  if (parsed.some((r) => r === null)) {
    return { ok: false, error: 'Unexpected response shape' };
  }
  const results = parsed as UploadFileResult[];
  const accepted = results.filter((r) => r.status === 'accepted').length;
  const rejected = results.length - accepted;
  return { ok: true, accepted, rejected, results };
}

// --------------------------------------------------------------- lifecycle

export type LifecycleStatus = 'successful' | 'failed';

export type LifecycleResult =
  | { ok: true; screenshotId: number; status: LifecycleStatus; state: ScreenshotState }
  | { ok: false; error: string };

async function postLifecycle(
  path: string,
  fetchImpl: typeof fetch,
): Promise<LifecycleResult> {
  let response: Response;
  try {
    response = await fetchImpl(path, {
      method: 'POST',
      headers: { 'X-Requested-With': 'fetch', Accept: 'application/json' },
      credentials: 'same-origin',
    });
  } catch {
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
  if (typeof body !== 'object' || body === null) {
    return { ok: false, error: 'Unexpected response shape' };
  }
  const v = body as Record<string, unknown>;
  if (
    typeof v.screenshotId !== 'number' ||
    (v.status !== 'successful' && v.status !== 'failed') ||
    (v.state !== 'active' && v.state !== 'archived')
  ) {
    return { ok: false, error: 'Unexpected response shape' };
  }
  return {
    ok: true,
    screenshotId: v.screenshotId,
    status: v.status,
    state: v.state,
  };
}

export async function archiveScreenshot(
  screenshotId: number,
  fetchImpl: typeof fetch = fetch,
): Promise<LifecycleResult> {
  return postLifecycle(`/api/screenshots/${screenshotId}/archive`, fetchImpl);
}

export async function restoreScreenshot(
  screenshotId: number,
  fetchImpl: typeof fetch = fetch,
): Promise<LifecycleResult> {
  return postLifecycle(`/api/screenshots/${screenshotId}/restore`, fetchImpl);
}
