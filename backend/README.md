# Screenshot Graveyard — Backend

FastAPI backend for Screenshot Graveyard.

## Setup

- Create a virtual environment under `backend/.venv`.
- Install pinned dependencies from `requirements.txt`.

## Run

From `backend/`, apply the Alembic schema before the first launch, then run the
ASGI app with a non-secure cookie for local HTTP development:

```sh
.venv/bin/alembic upgrade head
COOKIE_SECURE=0 .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The default cookie remains `Secure` for production HTTPS. Do not set
`COOKIE_SECURE=0` in production.

## Test

Run the test suite with `pytest` from the `backend/` directory.
