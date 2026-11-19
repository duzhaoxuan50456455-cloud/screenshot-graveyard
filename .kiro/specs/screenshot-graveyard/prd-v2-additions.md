# PRD v2.0 Additions (additive spec)

This file adds requirements and tasks. It does not edit, reorder or reopen any
item in `requirements.md`, `design.md` or `tasks.md`. Completed history stays
as written. Where an addition touches an existing open task, that task is named
and left as is.

## Source limitation

The PRD v2.0 `.docx` was unavailable as readable text. These additions come from
the stated product vision (quiet assistant, minimalist, progressive disclosure,
privacy, reversibility, Playful Gothic) and the existing specs and code. They
are proposals. Re-supply the PRD as text or Markdown and reconcile before
treating any item as approved. See `docs/prd-v2-gap-analysis.md`.

## Safety invariants preserved

INV-1 to INV-10 apply unchanged (see `.kiro/steering/safety.md`). In addition:

- No suggestion, assistant surface or view change may call the AI adapter
  without the existing per-screenshot consent.
- Accepting a suggestion only invokes existing endpoints.
- Dismissing or hiding a suggestion never modifies a screenshot, artifact or
  version.
- No delete path is added. No external write is added.
- The UI reflects server-confirmed results only.

## Out of scope for these additions

Real OneNote or Calendar auth, external app writes, autonomous deletion, live
OpenAI calls, long-term memory, external undo, and everything on the P2 list.

## Additional requirements

### Requirement 18: Calm navigation and progressive disclosure (P0 UX)

**User Story:** As a solo user, I want a small number of clear views and a
card that shows the screenshot first, so that the app feels quiet and I see
detail only when I ask.

1. THE System SHALL present separate views for active screenshots, The Crypt,
   and study notes, reachable by keyboard and with accessible names.
2. THE System SHALL show each screenshot card with the image as the dominant
   element and no more than one primary action visible by default.
3. THE System SHALL reveal secondary actions, consent, version and provenance
   detail only on explicit user request.
4. THE System SHALL keep Ghost, Revived and Crypt labels mapped to real states.
5. WHERE the user prefers reduced motion, THE System SHALL minimize animation
   (see open task 4.4).

### Requirement 19: Quiet Assistant suggestions (P0 UX, frontend-derived)

**User Story:** As a solo user, I want at most one unobtrusive suggestion per
analyzed screenshot that I can accept or dismiss, so that I get help without
being pushed.

1. WHEN a screenshot has a successful analysis, THE System MAY show at most one
   suggestion derived from its confirmed category.
2. THE System SHALL label the suggestion as derived from the category, and
   SHALL NOT present it as inferred user intent.
3. THE System SHALL NOT start any AI operation from a suggestion until the user
   accepts it and consent is already granted or is granted in the normal flow.
4. WHEN the user dismisses a suggestion, THE System SHALL hide it and SHALL NOT
   modify any screenshot or artifact.
5. THE System SHALL NOT show suggestions for screenshots without a successful
   analysis, and SHALL NOT show suggestions that rely on uncertain output as if
   it were certain.

### Requirement 20: Intent inference and tool planning (P1, deferred)

1. WHERE intent inference is enabled, THE AI_Adapter SHALL return a validated
   structured plan of proposed actions, each with a confidence and a reason.
2. THE System SHALL treat every planned action as a proposal that needs user
   confirmation before it runs.
3. THE System SHALL preserve INV-1 to INV-10 for every planned action.

## Additional tasks

Numbering uses an `A` prefix so no existing task number changes. Dependencies
refer to existing task numbers where relevant.

- [ ] A.1 Design tokens and Playful Gothic styling baseline
  - Centralize color, type scale, spacing and focus styles as CSS variables in `App.css`. No behavior change.
  - **Dependencies:** 4.1
  - **Requirements:** 11.4, 18.5
  - **Acceptance test:** existing Vitest suite passes unchanged. Contrast of text and focus ring is checked in tests or documented.
  - **Priority:** P0

- [ ] A.2 View navigation (Inbox, Crypt, Notes)
  - Add a small keyboard-accessible view switcher without adding a router dependency unless justified. Counts shown match real state.
  - **Dependencies:** A.1, 4.1
  - **Requirements:** 18.1, 18.4
  - **Acceptance test:** Testing Library tests assert each view lists exactly its items, and the switcher has accessible names and keyboard operation.
  - **Priority:** P0

- [ ] A.3 Screenshot-first card with progressive disclosure
  - Image-dominant card, one primary action, secondary actions behind a disclosure control. Safe Burial copy still states bytes are retained.
  - **Dependencies:** A.2
  - **Requirements:** 18.2, 18.3, 10.5
  - **Acceptance test:** tests assert the default card shows one primary action and the disclosure reveals the rest. Existing archive, restore and revive tests still pass.
  - **Priority:** P0

- [ ] A.4 Quiet Assistant suggestion (frontend-derived)
  - Single dismissible suggestion per analyzed screenshot from confirmed category. Accept routes into the existing consent and classify or study-note flow. Dismissal is client-side and non-destructive.
  - **Dependencies:** A.3
  - **Requirements:** 19.1 to 19.5
  - **Acceptance test:** tests assert no AI call on render or dismiss, suggestion absent without successful analysis, label says derived from category.
  - **Priority:** P0

- [ ] A.5 Reconcile with open UX tasks
  - Re-check 4.2, 4.3 and 4.4 against A.1 to A.4 and adjust implementation order. Do not edit those tasks' text.
  - **Dependencies:** A.4
  - **Priority:** P0

- [ ] A.7 `[P1, deferred, NOT implemented]` Intent inference and tool-planning backend
  - Follow-up task. The Quiet Assistant foundation (frontend/src/assistant) is in place and uses deterministic app rules only, labelled "Suggested by app rules". It does not infer intent. This item replaces those rules, or adds to them, with backend proposals labelled "AI suggestion". Add an adapter capability that returns a Pydantic-validated action plan (action type, target screenshot ids, confidence, reason). Add a service that stores plans as proposals only, an endpoint to read them, and UI that shows them through the A.4 surface. Execution of any action stays behind explicit user confirmation and existing endpoints. Fake adapter first, tests with the fake adapter only, no live OpenAI. No long-term memory, no external writes.
  - **Dependencies:** A.4, 2.1, 2.2, 2.3
  - **Requirements:** 20.1 to 20.3, INV-1 to INV-10
  - **Acceptance test:** plan output fails closed on schema violation, no action runs without confirmation, consent gate still blocks adapter calls, property tests keep INV-1 and INV-4.
  - **Priority:** P1, not started

## Dependency-aware plan

```
A.1 -> A.2 -> A.3 -> A.4 -> A.5
                       \-> A.7 (deferred)
```

Suggested waves: Wave 1 (A.1), Wave 2 (A.2), Wave 3 (A.3), Wave 4 (A.4),
Wave 5 (A.5). A.7 starts only after the PRD is re-supplied and A.4 is stable.

## Implemented vs planned

| Item | Status |
| --- | --- |
| Consent gate, reversible versions, Crypt, Markdown export | Implemented (earlier tasks) |
| A.1 to A.5 | Planned |
| A.7 intent inference and tool planning | Planned, deferred, not implemented |
