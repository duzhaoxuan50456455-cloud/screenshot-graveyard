# Screenshot Graveyard

**Where screenshots finish their unfinished business.**

A purpose-aware, reversible AI screenshot workflow web application built for
Kiroween 2026 (Costume Contest). Upload previously-captured screenshots; the app
classifies each one's likely purpose, writes a short honest "Epitaph" caption,
turns selected study screenshots into structured notes, and lets you redo AI work
non-destructively — all wrapped in a functional Halloween UX where every metaphor
maps to real system state.

This project is explicitly **not** an OCR tool, a generic organizer, a to-do
list, or a decorative gallery.

## Status

Phase 0 (project bootstrap) is in progress. The frontend and backend are
scaffolded, the health endpoint is live, and the SPA is served same-origin by the
API. Safe Vault, AI transformation, and the full Halloween UI are upcoming phases.

## Tech stack

- **Frontend:** React + TypeScript + Vite
- **Backend:** Python + FastAPI (SQLAlchemy 2.x + Alembic; SQLite in dev →
  PostgreSQL in prod)
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
4. Launch the ASGI app with a local-development cookie setting:
   `COOKIE_SECURE=0 .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`

`COOKIE_SECURE=0` is required only for local HTTP development. Production keeps
the default secure, HTTPS-only workspace cookie. The Vite `/api` proxy preserves
the browser's same-origin `localhost:5173` cookie contract.

### Frontend

From `frontend/`:

1. Install dependencies: `npm install`
2. Start the dev server: `npm run dev` (serves the SPA and proxies `/api` to the
   backend at `http://127.0.0.1:8000`)

With both running, the UI shows the live backend health status (Backend Online
when the API is up, Backend Offline / Error when it is not).

## Testing

- **Frontend:** `npm test -- --run` (Vitest, single run) from `frontend/`
- **Frontend lint:** `npm run lint` from `frontend/`
- **Backend:** `pytest` from `backend/` (using the project virtual environment)

## Build

- **Frontend production build:** `npm run build` from `frontend/` (outputs to
  `frontend/dist/`, which the backend serves same-origin in production).

## Specs and steering

- Requirements, design, and the task plan: `.kiro/specs/screenshot-graveyard/`
- Working steering guides: `.kiro/steering/` (product, architecture, safety)
- Development log: `docs/kiro-development-log.md`

## License

See [LICENSE](LICENSE).
