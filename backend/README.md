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

## Optional real vision provider

The default `AI_PROVIDER=fake` is deterministic and network-free. To explicitly
enable the OpenAI-backed adapter, set `AI_PROVIDER=openai`, `OPENAI_API_KEY`, and
an explicit supported `OPENAI_MODEL`; startup fails closed if that configuration
is invalid. The adapter uses bounded direct Base64 image inputs, never provider
Files or public Vault URLs, and sets `store=false` on Responses API requests.

`store=false` does **not** mean zero provider retention: OpenAI documents abuse
monitoring retention and image-safety exceptions separately. Obtain explicit
per-screenshot consent before using real mode, and do not place API keys in
source control, browser code, or logs. The optional live smoke test is skipped
unless `RUN_LIVE_VISION_SMOKE=1` and sends only its generated synthetic image.
