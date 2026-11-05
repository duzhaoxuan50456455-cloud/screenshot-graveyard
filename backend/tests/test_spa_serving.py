"""Tests for same-origin SPA serving and API routing precedence (Task 0.4).

These tests assert that:
- unknown /api routes return a JSON API error (never the SPA),
- the health endpoint keeps working,
- the app starts and serves the API even when frontend/dist is absent,
- when a dist directory exists, GET / and client-side routes return index.html
  while /api routes still return API errors.

The SPA tests build an app against a temporary dist directory so they do not
depend on a real frontend build being present in the workspace.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _reload_main_with_dist(monkeypatch, dist_dir: Path | None) -> FastAPI:
    """Import app.main and rebuild create_app() pointed at ``dist_dir``.

    If ``dist_dir`` is None, points at a guaranteed-nonexistent path to
    simulate a missing build.
    """
    import app.main as main

    importlib.reload(main)
    target = dist_dir if dist_dir is not None else (Path("/nonexistent") / "dist")
    monkeypatch.setattr(main, "_FRONTEND_DIST", target)
    return main.create_app()


def test_health_still_works(monkeypatch) -> None:
    app = _reload_main_with_dist(monkeypatch, None)
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_unknown_api_route_returns_json_not_spa(monkeypatch, tmp_path) -> None:
    # Even with a valid dist present, /api/* must never serve index.html.
    (tmp_path / "index.html").write_text("<!doctype html><html>SPA</html>")
    app = _reload_main_with_dist(monkeypatch, tmp_path)
    client = TestClient(app)
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {
        "error": {"code": "not_found", "message": "Not Found"}
    }
    assert "SPA" not in resp.text


def test_app_starts_without_dist(monkeypatch) -> None:
    # Missing dist must not crash the app; only SPA routes are skipped.
    app = _reload_main_with_dist(monkeypatch, None)
    client = TestClient(app)
    # Root has no handler without dist -> 404 (API still fine).
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 404


def test_root_serves_index_html_when_dist_present(monkeypatch, tmp_path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><html>SPA ROOT</html>")
    app = _reload_main_with_dist(monkeypatch, tmp_path)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "SPA ROOT" in resp.text


def test_client_side_route_serves_index_html(monkeypatch, tmp_path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><html>SPA ROUTE</html>")
    app = _reload_main_with_dist(monkeypatch, tmp_path)
    client = TestClient(app)
    resp = client.get("/crypt/some-id")
    assert resp.status_code == 200
    assert "SPA ROUTE" in resp.text


def test_cleanup_reload(monkeypatch) -> None:
    # Reload the module once more at the end so the real module-level `app`
    # (bound to the true _FRONTEND_DIST) is restored for other test modules.
    import app.main as main

    importlib.reload(main)
    assert isinstance(main.app, FastAPI)
