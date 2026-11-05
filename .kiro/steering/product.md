# Product Steering — Screenshot Graveyard

> Source of truth: `.kiro/specs/screenshot-graveyard/requirements.md`. This file
> summarizes product intent for day-to-day work; it never overrides the specs.

## Purpose

Screenshot Graveyard is a purpose-aware, reversible AI screenshot workflow web
application. Tagline: "Where screenshots finish their unfinished business."

Users upload previously-captured screenshots. The app helps them understand each
screenshot's likely purpose, transform appropriate content into genuinely useful
outputs (notably structured study notes), preserve recoverable originals, and
redo transformations when the AI is inaccurate.

The application is explicitly **NOT** an OCR tool, a generic organizer, a to-do
list, or a decorative haunted gallery.

## Target users

A solo/anonymous visitor (initially the solo undergraduate developer and
Kiroween 2026 judges) who has a backlog of screenshots saved for different
reasons — lecture slides, reference docs, shopping ideas, memories — and wants
help making sense of and acting on them. No accounts or login: each visitor gets
an isolated anonymous Workspace.

## Three core pillars

1. **Purpose-aware transformation** — classification into one of six fixed
   categories plus a short, honest purpose caption called an **Epitaph**,
   produced together in a single AI call. The system may express uncertainty and
   never pretends to know why the user originally saved an image.
2. **Reversible AI execution** — immutable originals, append-only versioned
   artifacts, and non-destructive redo. Redo and manual edits create new
   versions; prior versions and originals always remain available.
3. **Functional Halloween UX** — a cozy haunted digital archive where each
   metaphor maps to a real system state (Ghost = pending, The Crypt = recoverable
   archive, Revive = begin transformation, Resurrection = restore or redo, Safe
   Burial = archive). The theme improves usability; it is never merely decorative.

## Scope boundaries

### P0 — must-have (build these)

- Safe Screenshot Vault: multi-upload, content-authoritative validation, stable
  IDs, immutable byte storage, SHA-256 checksums, persistence, per-workspace
  deduplication, archive/restore.
- Purpose-aware AI classification (six categories) + Epitaph in one AI call;
  structured, validated, editable, uncertainty-aware output.
- Study-note transformation from a manually selected set of screenshots, with
  faithful code preservation, uncertainty flags, source references, and
  **mandatory Markdown export**.
- Reversible AI execution: append-only versions, manual edit vs AI redo,
  v1/v2 comparison, failure/status handling.
- Privacy & consent, accessible + responsive functional Halloween UX, persistent
  data model, replaceable AI adapter, automated + property-based tests, public
  deployment and submission readiness.

### P1 — optional, only after P0 is stable (do not let these block P0)

Shopping wishlist cards; memories gallery; one genuine external destination
(OneNote preferred, Google Docs fallback); Quick Ritual review; persistent,
undoable review actions; subtle themed animations tied to real state. Markdown
export remains mandatory regardless of any external integration.

### P2 — explicitly out of scope (do NOT build)

Automatic screenshot clustering; advanced reminders/scheduling; price tracking;
persistent personalization from corrections; screenshot-region annotation;
multiple external integrations; phone photo-library background syncing;
permanent deletion of originals or device images; a password/credential vault;
automated purchases; a large autonomous-agent framework; full multi-user
account authentication (per-visitor Workspace isolation is used instead).

## Working rules

- Do not expand scope, invent completed features, or rewrite the approved specs.
- When in doubt, defer to `requirements.md` and `design.md`.
