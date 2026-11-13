// Typed client for the AI workflow endpoints (Task 2.10).
//
// Same-origin only. The workspace travels in the HttpOnly cookie; this module
// never sends or reads a workspace id. State-changing requests send the
// `X-Requested-With` header required by the server's CSRF defence. Failures keep
// the HTTP status and server error code so the UI can distinguish consent (403),
// quota (429), not-found (404) and outages (503) without guessing.

export interface ApiFailure {
  ok: false;
  /** HTTP status, or 0 for a network failure / unparseable response. */
  status: number;
  code: string;
  message: string;
}
export type ApiResult<T> = { ok: true; data: T } | ApiFailure;

export type OperationStatus = 'pending' | 'successful' | 'failed' | 'uncertain';
export type OperationKind = 'classify' | 'study_note' | 'redo';
export type ProviderMode = 'fake' | 'openai';

export const CATEGORIES = [
  'Study/Knowledge',
  'Reference/Document',
  'Shopping/Inspiration',
  'Memory/Entertainment',
  'Unknown/Needs Review',
  'Potentially Sensitive',
] as const;
export type Category = (typeof CATEGORIES)[number];

export interface Operation {
  operationId: number;
  kind: OperationKind;
  status: OperationStatus;
  errorDetail: string | null;
  artifactId: number | null;
}
export interface Accepted extends Operation {
  warning: string | null;
}
export interface Analysis {
  screenshotId: number;
  classification: {
    category: Category;
    confidence: number;
    uncertain: boolean;
    versionNumber: number;
    origin: string;
  };
  epitaph: {
    text: string;
    visibleFacts: string;
    inferredIntent: string | null;
    uncertain: boolean;
    versionNumber: number;
    origin: string;
  };
}
export interface StudyNoteContent {
  topic: string;
  main_concepts: string[];
  extracted_code: string[];
  code_explanations: string[];
  pitfalls: string[];
  uncertain_content: string[];
}
export interface StudyNoteVersion {
  artifactId: number;
  versionId: number;
  versionNumber: number;
  origin: string;
  content: StudyNoteContent & { references: number[] };
  sourceScreenshotIds: number[];
}
export interface StudyNoteSummary {
  artifactId: number;
  versionNumber: number;
  origin: string;
  topic: string;
  sourceScreenshotIds: number[];
}
export interface VersionInfo {
  versionId: number;
  versionNumber: number;
  origin: string;
  isCurrent: boolean;
}

type Fetch = typeof fetch;
type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null;
const isStrArr = (v: unknown): v is string[] =>
  Array.isArray(v) && v.every((x) => typeof x === 'string');
const isIntArr = (v: unknown): v is number[] =>
  Array.isArray(v) && v.every((x) => typeof x === 'number');
const STATUSES = ['pending', 'successful', 'failed', 'uncertain'];

async function call<T>(
  path: string,
  init: { method?: 'GET' | 'POST'; json?: unknown },
  parse: (body: unknown) => T | null,
  fetchImpl: Fetch,
): Promise<ApiResult<T>> {
  const method = init.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (method === 'POST') headers['X-Requested-With'] = 'fetch';
  let body: string | undefined;
  if (init.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(init.json);
  }
  let response: Response;
  try {
    response = await fetchImpl(path, {
      method,
      headers,
      credentials: 'same-origin',
      body,
    });
  } catch {
    return { ok: false, status: 0, code: 'network', message: 'Network request failed' };
  }
  let parsed: unknown = null;
  try {
    parsed = await response.json();
  } catch {
    parsed = null;
  }
  if (!response.ok) {
    const err = isObj(parsed) && isObj(parsed.error) ? parsed.error : {};
    return {
      ok: false,
      status: response.status,
      code: typeof err.code === 'string' ? err.code : `http_${response.status}`,
      message: typeof err.message === 'string' ? err.message : `HTTP ${response.status}`,
    };
  }
  const data = parse(parsed);
  if (data === null) {
    return { ok: false, status: response.status, code: 'bad_shape', message: 'Unexpected response shape' };
  }
  return { ok: true, data };
}

function parseOperation(b: unknown): Operation | null {
  if (!isObj(b)) return null;
  if (typeof b.operationId !== 'number' || typeof b.status !== 'string') return null;
  if (!STATUSES.includes(b.status)) return null;
  if (b.kind !== 'classify' && b.kind !== 'study_note' && b.kind !== 'redo') return null;
  return {
    operationId: b.operationId,
    kind: b.kind,
    status: b.status as OperationStatus,
    errorDetail: typeof b.errorDetail === 'string' ? b.errorDetail : null,
    artifactId: typeof b.artifactId === 'number' ? b.artifactId : null,
  };
}
function parseAccepted(b: unknown): Accepted | null {
  const op = parseOperation(b);
  if (!op || !isObj(b)) return null;
  return { ...op, warning: typeof b.warning === 'string' ? b.warning : null };
}
function parseAnalysis(b: unknown): Analysis | null {
  if (!isObj(b) || typeof b.screenshotId !== 'number') return null;
  const c = b.classification;
  const e = b.epitaph;
  if (!isObj(c) || !isObj(e)) return null;
  if (!CATEGORIES.includes(c.category as Category)) return null;
  if (typeof c.confidence !== 'number' || typeof c.uncertain !== 'boolean') return null;
  if (typeof c.versionNumber !== 'number' || typeof c.origin !== 'string') return null;
  if (typeof e.text !== 'string' || typeof e.visibleFacts !== 'string') return null;
  if (typeof e.uncertain !== 'boolean') return null;
  if (typeof e.versionNumber !== 'number' || typeof e.origin !== 'string') return null;
  if (e.inferredIntent !== null && typeof e.inferredIntent !== 'string') return null;
  return {
    screenshotId: b.screenshotId,
    classification: {
      category: c.category as Category,
      confidence: c.confidence,
      uncertain: c.uncertain,
      versionNumber: c.versionNumber,
      origin: c.origin,
    },
    epitaph: {
      text: e.text,
      visibleFacts: e.visibleFacts,
      inferredIntent: e.inferredIntent as string | null,
      uncertain: e.uncertain,
      versionNumber: e.versionNumber,
      origin: e.origin,
    },
  };
}
function parseContent(c: unknown): (StudyNoteContent & { references: number[] }) | null {
  if (!isObj(c) || typeof c.topic !== 'string') return null;
  if (
    !isStrArr(c.main_concepts) ||
    !isStrArr(c.extracted_code) ||
    !isStrArr(c.code_explanations) ||
    !isStrArr(c.pitfalls) ||
    !isStrArr(c.uncertain_content) ||
    !isIntArr(c.references)
  ) {
    return null;
  }
  return {
    topic: c.topic,
    main_concepts: c.main_concepts,
    extracted_code: c.extracted_code,
    code_explanations: c.code_explanations,
    pitfalls: c.pitfalls,
    uncertain_content: c.uncertain_content,
    references: c.references,
  };
}
function parseNoteVersion(b: unknown): StudyNoteVersion | null {
  if (!isObj(b)) return null;
  const content = parseContent(b.content);
  if (
    !content ||
    typeof b.artifactId !== 'number' ||
    typeof b.versionId !== 'number' ||
    typeof b.versionNumber !== 'number' ||
    typeof b.origin !== 'string' ||
    !isIntArr(b.sourceScreenshotIds)
  ) {
    return null;
  }
  return {
    artifactId: b.artifactId,
    versionId: b.versionId,
    versionNumber: b.versionNumber,
    origin: b.origin,
    content,
    sourceScreenshotIds: b.sourceScreenshotIds,
  };
}

// ------------------------------------------------------------------ consent

export function getConsent(id: number, f: Fetch = fetch) {
  return call(
    `/api/screenshots/${id}/consent`,
    {},
    (b) => (isObj(b) && typeof b.granted === 'boolean' ? b.granted : null),
    f,
  );
}
export function getProviderMode(f: Fetch = fetch) {
  return call('/api/ai/provider-mode', {}, (b): ProviderMode | null =>
    isObj(b) && (b.mode === 'fake' || b.mode === 'openai') ? b.mode : null, f);
}
export function setConsent(id: number, granted: boolean, f: Fetch = fetch) {
  return call(
    `/api/screenshots/${id}/consent`,
    { method: 'POST', json: { granted } },
    (b) => (isObj(b) && typeof b.granted === 'boolean' ? b.granted : null),
    f,
  );
}

// ------------------------------------------------------- classification (Revive)

export function classifyScreenshot(id: number, f: Fetch = fetch) {
  return call(`/api/screenshots/${id}/classify`, { method: 'POST' }, parseAccepted, f);
}
export function getOperation(operationId: number, f: Fetch = fetch) {
  return call(`/api/operations/${operationId}`, {}, parseOperation, f);
}
export function getAnalysis(id: number, f: Fetch = fetch) {
  return call(`/api/screenshots/${id}/analysis`, {}, parseAnalysis, f);
}
export function editAnalysis(
  id: number,
  edit: { category?: Category; epitaphText?: string },
  f: Fetch = fetch,
) {
  return call(`/api/screenshots/${id}/analysis/edits`, { method: 'POST', json: edit }, parseAnalysis, f);
}

// -------------------------------------------------------------- study notes

export function generateStudyNote(screenshotIds: number[], f: Fetch = fetch) {
  return call('/api/study-notes', { method: 'POST', json: { screenshotIds } }, parseAccepted, f);
}
export function listStudyNotes(f: Fetch = fetch) {
  return call(
    '/api/study-notes',
    {},
    (b): StudyNoteSummary[] | null => {
      if (!isObj(b) || !Array.isArray(b.notes)) return null;
      const out: StudyNoteSummary[] = [];
      for (const n of b.notes) {
        if (
          !isObj(n) ||
          typeof n.artifactId !== 'number' ||
          typeof n.versionNumber !== 'number' ||
          typeof n.origin !== 'string' ||
          typeof n.topic !== 'string' ||
          !isIntArr(n.sourceScreenshotIds)
        ) {
          return null;
        }
        out.push({
          artifactId: n.artifactId,
          versionNumber: n.versionNumber,
          origin: n.origin,
          topic: n.topic,
          sourceScreenshotIds: n.sourceScreenshotIds,
        });
      }
      return out;
    },
    f,
  );
}
export function getStudyNote(artifactId: number, versionId?: number, f: Fetch = fetch) {
  const q = versionId === undefined ? '' : `?versionId=${versionId}`;
  return call(`/api/study-notes/${artifactId}${q}`, {}, parseNoteVersion, f);
}
export function listStudyNoteVersions(artifactId: number, f: Fetch = fetch) {
  return call(
    `/api/study-notes/${artifactId}/versions`,
    {},
    (b): VersionInfo[] | null => {
      if (!isObj(b) || !Array.isArray(b.versions)) return null;
      const out: VersionInfo[] = [];
      for (const v of b.versions) {
        if (
          !isObj(v) ||
          typeof v.versionId !== 'number' ||
          typeof v.versionNumber !== 'number' ||
          typeof v.origin !== 'string' ||
          typeof v.isCurrent !== 'boolean'
        ) {
          return null;
        }
        out.push({
          versionId: v.versionId,
          versionNumber: v.versionNumber,
          origin: v.origin,
          isCurrent: v.isCurrent,
        });
      }
      return out;
    },
    f,
  );
}
export function saveStudyNoteEdit(artifactId: number, content: StudyNoteContent, f: Fetch = fetch) {
  return call(
    `/api/study-notes/${artifactId}/edits`,
    { method: 'POST', json: { content } },
    parseNoteVersion,
    f,
  );
}
/** Server-generated Markdown download for a retained version (GET; cookie auth). */
export function studyNoteExportUrl(artifactId: number, versionId?: number): string {
  const q = versionId === undefined ? '' : `?versionId=${versionId}`;
  return `/api/study-notes/${artifactId}/export${q}`;
}
