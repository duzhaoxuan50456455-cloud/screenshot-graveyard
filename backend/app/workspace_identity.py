"""Workspace identity: token minting, hashing, and server-side resolution.

This is the trusted source of a visitor's `workspace_id` (consumed by the
Task 1.2 WorkspaceRepository). Design (CORRECTION 1):

* Token = `secrets.token_urlsafe(32)` — 256 bits of entropy, unguessable.
* Only `sha256(token)` is stored server-side (`workspace.token_hash`); the raw
  token lives ONLY in the httpOnly cookie, never in the DB, logs, or responses.
* Resolution is by hash: the server hashes the presented cookie token and looks
  up the workspace. A client-supplied `workspace_id` in a path/body is never
  trusted — only the cookie-resolved id is authoritative.
* There is NO token-bearing recovery URL; losing the cookie can permanently
  orphan a workspace (accepted P0 tradeoff).

This module does no HTTP/cookie parsing itself (that is the middleware's job);
it provides the primitives and the DB resolve/mint logic so both are testable.
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models

# 32 bytes of entropy -> url-safe base64 token (~43 chars). "32-byte" per spec.
_TOKEN_NBYTES = 32


def mint_token() -> str:
    """Return a fresh, cryptographically secure, unguessable Workspace token."""
    return secrets.token_urlsafe(_TOKEN_NBYTES)


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of a token. Only this is ever stored server-side."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_workspace_id(session: Session, token: str | None) -> int | None:
    """Resolve a raw cookie token to a workspace id by hash lookup, or None.

    Returns None for a missing, malformed, unknown, or tampered token. The caller
    treats None as "no current workspace" and never distinguishes the reasons to
    avoid leaking which tokens exist.
    """
    if not token:
        return None
    # Defensive: reject obviously malformed tokens without a DB hit. A valid
    # url-safe token of 32 bytes is ~43 chars; cap length to avoid abuse.
    if len(token) > 256:
        return None
    token_hash = hash_token(token)
    stmt = select(models.Workspace.id).where(
        models.Workspace.token_hash == token_hash
    )
    return session.scalars(stmt).one_or_none()


def create_workspace(session: Session) -> tuple[int, str]:
    """Mint a new workspace. Returns (workspace_id, raw_token).

    Only the hash is persisted; the raw token is returned ONCE so the caller can
    set it in the cookie. It is never stored or logged.
    """
    token = mint_token()
    ws = models.Workspace(token_hash=hash_token(token))
    session.add(ws)
    session.flush()
    return ws.id, token
