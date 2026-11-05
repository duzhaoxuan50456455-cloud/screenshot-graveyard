# Kiro Development Log — Screenshot Graveyard

A running account of how this project was built with Kiro: planning decisions,
architecture choices, implemented tasks, testing evidence, and notable incidents.

## Planning (spec-driven)

The project was developed with Kiro's spec workflow. Artifacts live under
`.kiro/specs/screenshot-graveyard/`:

- **requirements.md** — EARS-style, testable requirements organized by capability,
  privacy, data integrity, accessibility, failure handling, and submission
  readiness. P0/P1/P2 are clearly separated. Ten mandatory engineering invariants
  (INV-1…INV-10) are threaded through the acceptance criteria.
- **design.md** — the smallest maintainable architecture satisfying all P0
  capabilities and invariants, with per-decision rationale (why / simpler
  alternative / invariants supported / how tested), data model, API contracts,
  correctness properties, and a requirements-to-design traceability matrix.
- **tasks.md** — an incremental, dependency-ordered plan across seven dated
  phases (Phase 0–6), with grouped property-based test modules and a parallel
  execution wave summary.

### Requirements refinements

- Public access without exposing data: resolved to **isolated per-visitor
  anonymous Workspaces** (opaque unguessable token) rather than full accounts.
- Manual edits vs AI redo: both create retained append-only versions; a redo
  never discards a manual edit.
- External integration (OneNote/Google Docs) is **P1, not P0**; Markdown export
  is the mandatory P0 output path; P0 never depends on OAuth.
- Consent ordering corrected: general privacy warning → explicit consent before
  transmitting bytes → advisory sensitive-content indicators only post-analysis.

### Design refinements

- Workspace token stored as `sha256(token)` only; `HttpOnly; Secure;
  SameSite=Strict` cookie; no token-bearing recovery URL; three-layer CSRF.
- Truly immutable file creation via exclusive create (`os.link`/`O_CREAT|O_EXCL`);
  crash-tolerant temp cleanup that never touches published originals.
- Content-authoritative upload validation (signature + decode + pixel cap + size),
  extension/MIME as hints only.
- In-process background tasks declared non-durable; stale-pending startup sweep.
- Classification + Epitaph persisted atomically from one AI call.

## Architecture decisions (summary)

- React + TypeScript + Vite SPA; Python + FastAPI backend; SQLAlchemy 2.x +
  Alembic; SQLite (dev) → PostgreSQL (prod); private content-addressed file
  volume for original bytes; single replaceable `AIAdapter` with a `fake_adapter`.
- Strict layering: middleware → routers → services → repositories + file store.
  Services own all invariant enforcement; only `Vault_Service` touches the file
  store; every repository method is workspace-scoped.
- Same-origin deployment: FastAPI serves the built SPA and routes `/api/*`;
  same-origin makes SameSite cookies effective.

See `.kiro/steering/architecture.md` and `.kiro/steering/safety.md`.

## Implemented tasks

### Phase 0

- **0.1 — Frontend scaffold.** React + TS + Vite with ESLint, Prettier, Vitest,
  React Testing Library. Real smoke test renders the App and asserts visible
  title + tagline. `npm run build` and `npm test -- --run` pass; `npm run lint`
  clean.
- **0.2 — Backend scaffold.** FastAPI app factory with pinned deps (FastAPI,
  uvicorn, Pydantic v2, SQLAlchemy 2.x, Alembic, Pillow, pytest, Hypothesis).
  Real app-factory smoke tests pass under `pytest`.
- **0.3 — `GET /api/health`.** Returns `200 {"status":"ok"}`, no auth/DB. Verified
  with a FastAPI `TestClient` test.
- **0.4 — Same-origin frontend↔backend.** Vite dev proxy for `/api`; a typed
  frontend API client that never fabricates success; an accessible HealthStatus
  component (Checking / Backend Online / Backend Offline / Error) driven by the
  live call; FastAPI serves the built SPA same-origin with `/api` precedence and
  JSON 404s for unknown `/api` routes; guarded so a missing `frontend/dist` does
  not crash backend-only dev or pytest. Verified with unit/component tests and a
  real over-the-wire integration smoke test against a running uvicorn server.

## Testing evidence (latest Phase 0 runs)

- Frontend: `npm run build` ✓; `npm test -- --run` → 3 files, 12 tests passed;
  `npm run lint` → exit 0.
- Backend: `pytest` → 11 passed (app-factory, health, and SPA-serving tests).
- Integration smoke (live uvicorn + built SPA): `GET /` returns React HTML;
  `GET /api/health` → 200 `{"status":"ok"}`; SPA asset served same-origin; invalid
  `/api` route → 404 JSON (not the SPA).

## Incident: source-file recovery

During early Phase 0, the backend/frontend source files were written and the
build/test commands ran successfully (artifacts `frontend/dist`, `node_modules`,
`backend/.venv`, and compiled `__pycache__`/pytest cache were present on disk),
but the loose **source files were subsequently removed** from the working tree
for an unknown reason while the artifacts remained. Investigation found the
source directories' modification times all updated at a single instant after the
successful runs, and nothing had been committed to git yet, so the files were not
recoverable from history.

Resolution: the source was recreated deterministically from the verified prior
outputs, a root `.gitignore` was established first (excluding `node_modules`,
`dist`, `.venv`, caches, local DBs, local image storage, and secrets), and the
work was committed to git to create a durable recovery point:

- `873dc81` chore: bootstrap Screenshot Graveyard frontend and backend
- `ecf41a8` feat: connect React frontend to FastAPI backend

Verification after each recreation confirmed the files persisted on disk and all
builds/tests passed. Going forward, each completed task is committed promptly so
the git index is always a recovery point.

## Conventions

- Steering files: `.kiro/steering/{product,architecture,safety}.md`.
- Build artifacts, dependencies, caches, local DBs, local image storage, and
  secrets are never committed (see root `.gitignore`).
- Kiro-generated transient data (`tasks.meta.json`, any `.kiro/sessions/`) is
  gitignored.
