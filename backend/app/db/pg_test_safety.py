"""Safety guards for destructive PostgreSQL integration tests.

These helpers decide whether it is safe to run DDL against a PostgreSQL target.
The PRIMARY guarantee is an explicit operator opt-in designating a disposable
test database (`PG_TEST_DB_IS_DISPOSABLE=1`). A URL keyword heuristic is only an
ADDITIONAL, secondary defense — it is never relied upon alone.

Kept in the app package (not the tests package) so it can be imported and unit
tested directly; it contains no database logic itself.
"""

from __future__ import annotations

from collections.abc import Mapping

# Substrings that suggest a managed/shared/production database. Heuristic only.
RISKY_URL_TOKENS = (
    "prod",
    "production",
    "live",
    "rds.amazonaws",
    "neon",
    "supabase",
)


def is_disposable_opt_in(env: Mapping[str, str]) -> bool:
    """Return True only if the operator explicitly designated a disposable DB.

    This is the primary safety gate for destructive DDL. It requires the exact
    value "1" for PG_TEST_DB_IS_DISPOSABLE; anything else (absent, "0", "true",
    "yes") is treated as NOT opted in, so the default is always safe.
    """
    return env.get("PG_TEST_DB_IS_DISPOSABLE") == "1"


def looks_like_production(url: str) -> bool:
    """Heuristic: does the URL resemble a production/shared database?

    Secondary defense only; a False result is NOT a safety guarantee.
    """
    low = url.lower()
    return any(token in low for token in RISKY_URL_TOKENS)
