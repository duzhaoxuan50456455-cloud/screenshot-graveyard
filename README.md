# Screenshot Graveyard

**Where screenshots finish their unfinished business.**

A purpose-aware, reversible AI screenshot-management application built for
Kiroween 2026. It is for people with a backlog of lecture slides, reference
material, shopping ideas, and memories who need a safe way to keep, revisit,
and eventually make use of them.

The project treats screenshots as evidence, not disposable uploads: original
bytes are immutable; derived artifacts retain source references; cloud AI needs
explicit consent; and retained versions avoid silently replacing output. AI
interpretation is user-controlled, may express uncertainty, and never claims to
know why someone saved an image.

This project is explicitly **not** an OCR tool, generic organizer, to-do list,
or decorative gallery. Its Halloween language maps to real state: active
screenshots are **Ghosts**, archived screenshots are in **The Crypt**, **Safe
Burial** archives, and **Resurrection** restores.

## Status

### Implemented

- React/Vite Ghosts and Crypt UI for workspace bootstrap, multi-image upload,
  owner-authorized previews, duplicate indicators, Safe Burial, Resurrection,
  loading/error states, and refresh-backed state.
- Anonymous server-resolved Workspaces with HttpOnly cookies, workspace-scoped
  authorization, CSRF/Origin protections, and rate/size limits.
- Content-authoritative upload validation, SHA-256 checksums, per-workspace
  deduplication, private content-addressed write-once original storage, and
  reversible archive/restore verification.
- Backend Classification + Epitaph workflow with structured-output validation,
  explicit consent checks, atomic persistence, operation polling, timeout
  handling, stale-pending recovery, and per-workspace AI-call quotas.
- Backend Study Note workflow with manual ordered selection, all-source consent
  checks, source references, atomic generation, retained Study Note versions,
  manual editing, reopen-original links, and Markdown export.
- A deterministic, network-free `FakeAIAdapter`, plus an opt-in OpenAI vision
  adapter with mocked tests, configured request deadlines, zero automatic
  retries, bounded inputs/output tokens, and `store=False` requests.

### Not yet verified or incomplete

- No live OpenAI request has been run from this repository; model behavior,
  output quality, latency, and billing remain unverified.
- The current frontend does **not** expose consent, classification, Study Notes,
  editing/export, or AI operation polling (Task 2.10).
- Generalized AI Redo, version comparison, and version-conflict handling remain
  Phase 3 work. Retained versions currently cover the implemented Study Note
  edit path.
- The Ghost/Crypt interface is functional, but the full Halloween UX is not
  complete. There is no public production deployment or live demo URL.
- Local SQLite is tested; PostgreSQL integration, mounted-volume durability,
  and multi-instance reliability are not yet verified.

## Tech stack

- **Frontend:** React + TypeScript + Vite
- **Backend:** Python + FastAPI (SQLAlchemy 2.x + Alembic; SQLite locally;
  PostgreSQL is a future deployment target)
- **Original image bytes:** private, content-addressed file storage (never
  web-served)
- **AI:** a single replaceable multimodal adapter with validated structured output

## Repository structure

```
screenshot-graveyard/
├── .kiro/
│   ├── specs/screenshot-graveyard/   # requirements.md, design.md, tasks.md
│   └── steering/                     # product.md, architecture.md, safety.md
├── backend/                          # FastAPI app (app/), tests (tests/)
│   ├── app/main.py                   # app factory + /api/health + SPA serving
│   ├── tests/                        # pytest suite
│   ├── pyproject.toml                # pinned deps + pytest config
│   └── requirements.txt              # full pinned lock
├── frontend/                         # React + TS + Vite SPA
│   ├── src/                          # App, HealthStatus, api/client, tests
│   ├── package.json                  # scripts + pinned deps
│   └── vite.config.ts                # dev proxy + Vitest config
├── docs/kiro-development-log.md      # development log + decisions
├── LICENSE
└── README.md
```

## Prerequisites

- Node.js 20+ and npm
- Python 3.11+ (developed on 3.13)

## Local development

### Backend

From `backend/`:

1. Create a virtual environment: `python3 -m venv .venv`
2. Install pinned dependencies: `.venv/bin/pip install -r requirements.txt`
3. Apply the schema before the first launch: `.venv/bin/alembic upgrade head`
4. Launch the ASGI app with the network-free fake provider and a local cookie
   setting: `COOKIE_SECURE=0 AI_PROVIDER=fake .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`

`COOKIE_SECURE=0` is required only for local HTTP development. Production keeps
the default secure, HTTPS-only workspace cookie. The Vite `/api` proxy preserves
the browser's same-origin `localhost:5173` cookie contract.

### Frontend

From `frontend/`:

1. Install dependencies: `npm install`
2. Start the dev server: `npm run dev` (serves the SPA and proxies `/api` to the
   backend at `http://127.0.0.1:8000`)

With both running, the UI shows health plus the implemented upload/archive/
restore experience. The AI workflows are backend APIs today, not UI actions.

## Testing

Verified locally at commit
[`b631721`](https://github.com/duzhaoxuan50456455-cloud/screenshot-graveyard/commit/b6317210c7a54f3755fdf88f5ea85dfc4047fdb4):

| Command | Result |
| --- | --- |
| `cd backend && .venv/bin/pytest` | **465 passed, 2 skipped** |
| `cd frontend && npm test -- --run` | **33 passed** |
| `cd frontend && npm run build` | Passed |
| `cd frontend && npm run lint` | Passed |

The skipped backend tests are intentionally gated: PostgreSQL integration and
an opt-in live OpenAI vision smoke test. Mocked adapter tests validate request
and schema contracts; they do **not** validate live model quality. The backend
suite includes Hypothesis property tests for storage, lifecycle, and workspace
isolation.

## Build

- **Frontend production build:** `npm run build` from `frontend/` (outputs to
  `frontend/dist/`, which the backend serves same-origin in production).

## Architecture and engineering highlights

```text
React + TypeScript + Vite SPA
        │  /api (Vite proxy in development; same origin in production)
FastAPI middleware → routers → services → workspace-scoped repositories
                                      ├─ SQLAlchemy 2.x + Alembic / SQLite locally
                                      └─ private, content-addressed original-image store
```

- **Eight relational tables:** `workspace`, `content_blob`, `screenshot`,
  `artifact`, `artifact_version`, `source_reference`, `consent_record`, and
  `operation`.
- **Integrity:** original bytes are SHA-256 addressed and published write-once;
  restore recomputes and verifies the checksum. Archive state does not delete
  bytes.
- **Defensive image intake:** streaming size limits, signature/decode checks,
  and decompression-bomb dimension caps treat filenames and declared MIME types
  only as hints.
- **Private-by-default access:** foreign resources receive generic 404s;
  original images are never public/static files.
- **Traceable outputs:** transactional artifact persistence, Study Note
  versions, and source references keep derived work tied to its originals.
- **Failure-aware AI:** `AIAdapter` isolates Fake and OpenAI implementations;
  Pydantic validates output before persistence. `202` operations are pollable
  through `pending`, `successful`, `failed`, and `uncertain`; successful is set
  only after its persistence transaction commits. Work uses in-process FastAPI
  background tasks, not a durable distributed queue, so stale pending work is
  recovered conservatively.

## Optional real-provider configuration

`AI_PROVIDER=fake` is the safe default and makes no network calls. Real mode is
explicit and fails closed if configuration is absent or invalid:

```sh
export AI_PROVIDER=openai
export OPENAI_API_KEY='replace-with-your-server-side-key'
export OPENAI_MODEL='replace-with-a-supported-vision-model'
```

Keep keys only in local/deployment environment configuration—never source,
browser code, logs, or committed `.env` files. Obtain informed consent for each
selected screenshot before real processing. `store=False` does not mean zero
provider retention; review the provider's current data controls before a live
deployment. The live smoke test is opt-in and was not run here.

## Built with Kiro — Kiroween workflow

Kiro's spec-driven workflow is preserved in this repository:

- [Requirements](.kiro/specs/screenshot-graveyard/requirements.md),
  [design](.kiro/specs/screenshot-graveyard/design.md), and
  [implementation tasks](.kiro/specs/screenshot-graveyard/tasks.md) provide
  traceable requirements, acceptance criteria, and property-test plans.
- [Product](.kiro/steering/product.md),
  [architecture](.kiro/steering/architecture.md), and
  [safety](.kiro/steering/safety.md) guide immutable-source behavior,
  workspace isolation, consent, and scope.
- Kiro was used for meaningful application implementation, including the
  ClassificationService and StudyNoteService workflows, alongside the vault,
  operations, and UI foundation. Independent reviews and regression tests were
  used to find and correct defects.

See the [Kiro development log](docs/kiro-development-log.md) for decisions and
recorded development history. This repository does not document use of Kiro
Hooks, MCP, Powers, or custom agents, so none is claimed here.

## Roadmap and limitations

- **Task 2.10:** frontend consent, classification, Study Notes, editing/export,
  and operation status.
- **Phase 3:** generalized append-only AI Redo, conflict handling, and version
  comparison.
- **UX:** further Halloween presentation and accessibility polish.
- **Validation/deployment:** synthetic opt-in real-provider smoke validation,
  PostgreSQL/persistent-volume verification, then a public deployment.

## Links and competition notes

- Repository: <https://github.com/duzhaoxuan50456455-cloud/screenshot-graveyard>
  (public at the time of this update)
- License: [MIT](LICENSE)
- Kiro artifacts: [`.kiro/`](.kiro/), tracked in this repository

There is intentionally no live-demo URL because none has been verified. A
complete Kiroween/Devpost submission separately requires a functional
application URL, public YouTube demo, and Kiro-use write-up; this README is
repository evidence, not a substitute for those submission materials.
