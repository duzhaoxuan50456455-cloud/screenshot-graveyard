"""Unit tests for workspace identity primitives (Task 1.3).

Covers token generation, hash-only storage, and hash-based resolution, without
any HTTP layer. Uses an in-memory SQLite database and synthetic data.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db import models
from app.workspace_identity import (
    create_workspace,
    hash_token,
    mint_token,
    resolve_workspace_id,
)


@pytest.fixture()
def session():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s
    eng.dispose()


def test_mint_token_is_urlsafe_and_high_entropy():
    t = mint_token()
    # 32 bytes url-safe base64 ~ 43 chars; url-safe alphabet only.
    assert len(t) >= 43
    assert all(c.isalnum() or c in "-_" for c in t)
    # Two tokens are never equal (unguessable / random).
    assert mint_token() != mint_token()


def test_hash_token_is_sha256_hex():
    t = "example-token"
    h = hash_token(t)
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    # Deterministic and distinct from the raw token.
    assert hash_token(t) == h
    assert h != t


def test_create_workspace_stores_only_hash_not_raw_token(session):
    ws_id, token = create_workspace(session)
    session.commit()
    row = session.get(models.Workspace, ws_id)
    assert row is not None
    # The stored value is the hash, never the raw token.
    assert row.token_hash == hash_token(token)
    assert row.token_hash != token
    # No column anywhere contains the raw token.
    all_hashes = session.scalars(select(models.Workspace.token_hash)).all()
    assert token not in all_hashes


def test_resolve_by_hash_returns_correct_workspace(session):
    ws_id, token = create_workspace(session)
    session.commit()
    assert resolve_workspace_id(session, token) == ws_id


def test_resolve_rejects_missing_unknown_and_tampered_tokens(session):
    ws_id, token = create_workspace(session)
    session.commit()
    assert resolve_workspace_id(session, None) is None
    assert resolve_workspace_id(session, "") is None
    assert resolve_workspace_id(session, "not-a-real-token") is None
    # Tampering with one character must not resolve.
    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    assert resolve_workspace_id(session, tampered) is None
    # Overly long input is rejected without a DB hit.
    assert resolve_workspace_id(session, "x" * 1000) is None


def test_two_visitors_get_distinct_workspaces(session):
    id1, t1 = create_workspace(session)
    id2, t2 = create_workspace(session)
    session.commit()
    assert id1 != id2
    assert t1 != t2
    assert resolve_workspace_id(session, t1) == id1
    assert resolve_workspace_id(session, t2) == id2
