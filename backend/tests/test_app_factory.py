"""Smoke tests for the backend scaffold (Task 0.2).

These tests assert only that the app factory is importable and produces a real
FastAPI application instance. They require no network and no database.
"""

from fastapi import FastAPI

from app.main import app, create_app


def test_create_app_returns_fastapi_instance() -> None:
    """The app factory returns a real FastAPI application instance."""
    application = create_app()
    assert isinstance(application, FastAPI)


def test_create_app_returns_fresh_instances() -> None:
    """Each call builds its own application instance (true app factory)."""
    first = create_app()
    second = create_app()
    assert first is not second


def test_module_level_app_is_fastapi_instance() -> None:
    """The module-level `app` used by the ASGI server is a FastAPI app."""
    assert isinstance(app, FastAPI)


def test_app_has_expected_metadata() -> None:
    """The scaffolded app carries the configured title and version."""
    application = create_app()
    assert application.title == "Screenshot Graveyard API"
    assert application.version == "0.1.0"
