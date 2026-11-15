"""Validation of user-supplied AI Redo regeneration instructions (Task 3.3).

Instructions are UNTRUSTED text typed by the user (Req 7.7). They may only steer
emphasis, detail, or wording of a regeneration. They never change the required
output schema, the approved categories, source references, consent, or any safety
rule; those are enforced by code after the model answers, not by the prompt.

This module is pure (no I/O). Errors carry fixed, safe messages and NEVER echo the
submitted text, so instructions cannot leak into responses or logs.
"""

from __future__ import annotations

import unicodedata

from app import config

# Whitespace controls that are legitimate in multi-line guidance.
_ALLOWED_CONTROLS = frozenset("\n\t")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs"})  # other controls, lone surrogates


class RedoInstructionsError(ValueError):
    """The supplied regeneration instructions are not acceptable."""


def normalize_redo_instructions(raw: object, *, max_chars: int | None = None) -> str | None:
    """Return cleaned instructions, or None when the user supplied none.

    ``None`` and whitespace-only text mean "regenerate without extra guidance",
    which is a valid Redo. Anything else must be a string within the bound and
    free of control characters.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RedoInstructionsError("Instructions must be text")
    limit = config.AI_REDO_INSTRUCTIONS_MAX_CHARS if max_chars is None else max_chars
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    if len(text) > limit:
        raise RedoInstructionsError(
            f"Instructions are too long (maximum {limit} characters)"
        )
    for ch in text:
        if ch in _ALLOWED_CONTROLS:
            continue
        if unicodedata.category(ch) in _FORBIDDEN_CATEGORIES:
            raise RedoInstructionsError("Instructions contain unsupported characters")
    return text


def instructions_from_body(body: object) -> str | None:
    """Extract instructions from a parsed JSON request body.

    The body is optional (``None`` or ``{}`` = no extra guidance). The only
    accepted key is ``instructions``; unknown keys are rejected rather than
    ignored, so a client cannot smuggle fields such as a workspace id.
    """
    if body is None:
        return None
    if not isinstance(body, dict) or set(body) - {"instructions"}:
        raise RedoInstructionsError("Invalid request body")
    return normalize_redo_instructions(body.get("instructions"))
