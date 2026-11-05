# Architecture Steering — Screenshot Graveyard

> Source of truth: `.kiro/specs/screenshot-graveyard/design.md`. This file is a
> working summary; the design document governs any conflict.

## Stack

- **Frontend:** React + TypeScript + Vite single-page app (SPA).
- **Backend:** Python + FastAPI.
- **ORM / migrations:** SQLAlchemy 2.x + Alembic.
- **Database:** SQLite in local development → PostgreSQL in production (same
  models/migrations via SQLAlchemy + Alembic). Postgres gives durable writes and
  constraint enforcement under concurrency for the public deployment.
- **Original image bytes:** a private, content-addressed file volume, never
  web-served and never on ephemeral container storage in production. Stored
  separately from the relational database.
- **AI:** a single replaceable `AIAdapter` interface returning Pydantic-validated
  structured output, with a deterministic `fake_adapter` test double.

## Layered backend (strict, one responsibility per layer)

```
Client (SPA)
  -> Middleware   (resolve/mint workspace cookie, CSRF/Origin check, rate limit)
  -> Routers      (HTTP <-> service, request validation, status mapping)
  -> Services     (ALL business logic + invariant enforcement)
  -> Repositories (workspace-scoped DB access)  +  File store (write-once)
```

Rules:

- Routers never import repositories or the file store directly — only services.
- Services are the only place invariants (INV-1…INV-10) are enforced.
- Only `Vault_Service` imports the file store.
- Every repository method takes a required `workspace_id`; there is no unscoped
  query path.

## Same-origin deployment

The SPA and the API are served from the **same origin**: in development via the
Vite dev proxy (`/api` → FastAPI), and in production by FastAPI serving the built
SPA from `frontend/dist` while routing `/api/*` to the API. Same-origin makes
`HttpOnly; Secure; SameSite=Strict` workspace cookies effective. Unknown `/api`
paths return JSON API errors (404), never `index.html`. If `frontend/dist` is
absent (backend-only dev, CI, pytest), the app still starts and the API still
works; only SPA routes are skipped.

## Data model (overview)

Tables: `workspace`, `content_blob` (per-workspace dedup by SHA-256), `screenshot`
(stable ID, `state` active/archived), `artifact` (kind + `current_version_id`),
`artifact_version` (INSERT-only, `UNIQUE(artifact_id, version_number)`),
`source_reference` (provenance attached to the artifact), `consent_record`,
`operation` (status: pending/successful/failed/uncertain). Every non-`workspace`
table carries `workspace_id` FK + index.

## Async model

AI endpoints (classify, study-note, redo) return `202 + operationId` and run work
in an in-process FastAPI `BackgroundTask`; the client polls operation status.
**BackgroundTasks are not durable across process restarts** — a startup sweep
marks stale `pending` operations as `failed` (sources untouched). No Celery,
Redis, or distributed queue.

## Deliberately avoided

Microservices, distributed queues, ephemeral production storage, public URLs to
original bytes, full account authentication, and any P2 feature.
