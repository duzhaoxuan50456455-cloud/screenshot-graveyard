# PRD v2.0 Gap Analysis

Branch: `overnight/ux-foundation-01`, base `e375ca9`.

## Source limitation (read first)

The PRD v2.0 file (`.docx`) was **not available as readable text** during this
analysis. Nothing here quotes or paraphrases the PRD, and no PRD requirement
numbers are cited. The analysis is based on:

- the product vision supplied for this task: a quiet screenshot-to-action
  assistant; web-first and minimalist; proactive but not intrusive;
  agent-driven but user-controlled; privacy-conscious; reversible; Playful
  Gothic identity; progressive disclosure;
- `.kiro/specs/screenshot-graveyard/{requirements,design,tasks}.md`;
- `.kiro/steering/{product,architecture,safety}.md`;
- the frontend in `frontend/src` and the routers in `backend/app/routers`.

**Action required:** re-supply the PRD as text or Markdown, then re-run this
comparison. Any row marked "target" below is derived from the vision statement
only and may differ from the PRD.

Out of scope for this batch: real OneNote or Calendar authentication, writes to
external apps, autonomous deletion, live OpenAI calls, long-term memory,
external undo.

## Status legend

- **Implemented**: present on this branch and covered by tests.
- **Partial**: some behavior exists, the target is not met.
- **Planned**: not built. Proposed in
  `.kiro/specs/screenshot-graveyard/prd-v2-additions.md`.
- **Not planned**: excluded by the out-of-scope list or the P2 list.

## Current implementation inventory

### API endpoints (all under `/api`, same origin)

| Method and path | Purpose | Notes |
| --- | --- | --- |
| `GET /health` | Liveness | No auth, no DB |
| `GET /workspace` | Mint or confirm anonymous workspace | Never returns token or id |
| `POST /screenshots` | Multi-upload | 201 all ok, 207 mixed, 400 all rejected; content-authoritative validation, dedup |
| `GET /screenshots?state=active\|archived` | List safe metadata plus analysis summary | 400 `bad_state` on other values |
| `GET /screenshots/{id}/image` | Authorized image read | Never a public URL |
| `POST /screenshots/{id}/archive` | Safe Burial | Bytes retained (INV-2, INV-10) |
| `POST /screenshots/{id}/restore` | Resurrection | SHA-256 verified (INV-3) |
| `GET, POST /screenshots/{id}/consent` | Per-screenshot AI consent | Gate before any adapter call |
| `POST /screenshots/{id}/classify` | Revive: classification plus Epitaph | 202 + operation id; 403 `consent_required`; 429 rate limit |
| `GET /screenshots/{id}/analysis` | Read analysis | |
| `POST /screenshots/{id}/analysis/edits` | Edit category or Epitaph | 400 `invalid_edit` |
| `POST /screenshots/{id}/analysis/redo` | AI redo of analysis | 202 |
| `POST /operations`, `GET /operations/{id}` | Operation lifecycle | pending, successful, failed, uncertain |
| `POST /study-notes/selection` | Validate manual selection and count policy | |
| `POST /study-notes` | Generate study note | 202 |
| `GET /study-notes`, `GET /study-notes/{id}` | List and read | optional version |
| `GET /study-notes/{id}/versions` | Version list | Append-only (INV-5, INV-6) |
| `POST /study-notes/{id}/edits` | Manual edit | New version |
| `POST /study-notes/{id}/redo` | AI redo | New version, edits retained |
| `GET /study-notes/{id}/sources` | Source references | INV-7 |
| `GET /study-notes/{id}/export` | Markdown export | Mandatory P0 |
| `GET /artifacts/{id}/compare` | Version comparison | |

Error shape is `{"error": {"code", "message"}}`. Cross-workspace resources
return 404. State-changing requests need the `X-Requested-With: fetch` header
and pass the Origin check. `GET /ai/provider-mode` (defined in `backend/app/main.py`) returns
`{mode: fake|openai}` and drives the provider note in the UI.

### Frontend structure

- `App.tsx`: one page. Header (title, tagline, `HealthStatus`) then
  `Graveyard`. No router, no navigation.
- `Graveyard.tsx`: upload form, provider note, then three always-visible
  sections: Ghosts (analysis pending or absent), Revived (analyzed), The Crypt
  (archived). Each card shows image, state, size, duplicate hint, category and
  Epitaph, and actions (Revive or Open, Safe Burial, Resurrection).
- `ai/ScreenshotDetail.tsx`: consent, classify, polling, Epitaph editing.
- `ai/StudyNotesPanel.tsx`, `ai/StudyNoteView.tsx`: select, generate, edit,
  redo, compare, export.
- `ai/PrivacyNotice.tsx`, `ai/useOperationPolling.ts`, `ai/StatusBadge.tsx`.
- Responsive CSS exists as two media queries in `App.css`
  (`min-width: 720px`, `max-width: 560px`). No `prefers-reduced-motion` rule
  exists yet.

## Current vs target

| Area | Current | Target (from vision) | Status | Gap |
| --- | --- | --- | --- | --- |
| Navigation | Single scrolling page; three sections stacked; detail and study notes open inline | Few clear destinations (for example Inbox, Crypt, Notes) with one primary task per view | Planned | No router or view switcher. Task 4.3 covers layout only, not navigation |
| Screenshot-first cards | Card shows image plus metadata, file name, size, state line, category, Epitaph and several buttons together | The image leads. Text is minimal. Actions appear on focus, hover or open | Partial | Card density is high. Image is small relative to text. Labels are long |
| Progressive disclosure | Consent, analysis, edits, versions and compare are exposed in detail panels, but each card always lists every action and AI hint text | Default view shows image and one suggested next step. Detail on demand | Partial | Provider hint, origin hints and duplicate notes always rendered |
| Quiet Assistant (proactive, not intrusive) | None. Revive is user-initiated per screenshot | A single, dismissible suggestion per screenshot, for example "Looks like lecture notes, make a study note?". User accepts or ignores | Planned | No suggestion surface. No dismissal state |
| Agent-driven, user-controlled | AI runs only after explicit consent and an explicit click | Suggestions are agent-made but never executed without confirmation | Implemented (control) / Planned (suggestion) | Control model is sound. Suggestions are missing |
| Intent inference and tool planning | Classification into six fixed categories plus Epitaph. No plan of actions | Backend infers intent and proposes a typed action plan | Planned (follow-up, not built) | Needs a new adapter capability and schema. See follow-up task in additions doc |
| Privacy-conscious | Per-screenshot consent gate, privacy notice, workspace isolation, no logging of image content | Same, with plain-language controls | Implemented | Wording and discoverability can improve |
| Reversible | Archive and restore, append-only versions, redo, compare | Same | Implemented | Task 4.2 (Resurrection experience) still open |
| Playful Gothic identity | Halloween metaphors tied to real states (Ghost, Revived, Crypt) | Playful Gothic, restrained | Partial | Visual language is serviceable; typography and palette not systematized; no design tokens |
| Accessibility and motion | Accessible names on buttons, roles on status | Keyboard operable, non-color cues, reduced motion toggle | Partial | Tasks 4.4 open. No reduced-motion handling |
| Responsive | Two breakpoints | Usable at mobile, tablet, desktop | Partial | Task 4.3 open |
| External destinations (OneNote, Calendar) | None; Markdown export only | Out of scope this batch | Not planned (this batch) | Delivery_Service is a stub by design (INV-8, INV-9) |
| Long-term memory, personalization | None | Out of scope | Not planned | Excluded by P2 list |
| Autonomous deletion | No delete path exists | Out of scope | Not planned | INV-10 forbids it |

## Gaps that matter most

1. **Navigation and density.** The single page shows everything at once. This
   conflicts most directly with "minimalist" and "progressive disclosure".
2. **No quiet suggestion surface.** The Quiet Assistant has nowhere to appear.
   A first version can be frontend-only, deriving suggestions from existing
   classification output (category and Epitaph) with no new backend capability.
3. **Real intent inference does not exist.** Classification is a label, not a
   plan. A frontend-derived suggestion must be labelled as derived from the
   category, not as inferred intent.
4. **Open P0 UX tasks** (4.2, 4.3, 4.4) overlap with this work and should be
   sequenced with it rather than duplicated.

## Safety constraints carried forward

INV-1 to INV-10 stay unchanged. Specifically for any new UX or assistant work:
no suggestion may call the AI adapter without existing consent; accepting a
suggestion only triggers existing endpoints; dismissing a suggestion changes no
stored screenshot or artifact; nothing reports success before server
confirmation.

## Recommended next steps

1. Re-supply the PRD as text and repeat this comparison.
2. Implement the additive tasks in
   `.kiro/specs/screenshot-graveyard/prd-v2-additions.md` in the listed order.
3. Keep intent inference and tool planning as a separate, later task.

## Addendum: review against the real PRD v2.0 (Markdown digest)

The PRD is now available, so the earlier statement "the PRD could not be read"
is superseded. Review result for the Draft PR at 7742649 plus this follow-up:

| PRD item | Status |
| --- | --- |
| UX-03 one suggestion, dismissible, no nagging | Met. Assistant shows one suggestion, Dismiss is session-only, "Something else" cycles alternatives. |
| UX-02 actions only after selection, Something else | Met for the assistant (idle hint before selection). |
| AG-02 unselected images never included | Met. Batches use only `selected` ids. |
| Consent freshness (PRD 8) | Fixed here. Consent is re-read on window focus and immediately before each image of a Revive batch. |
| Honest status (PRD 2, PR-01) | Fixed here. Provider loading now reads "Checking the AI provider", distinct from "unconfirmed" failure. Revive stays blocked in both. |
| Fake AI labelling | Met in assistant label and Graveyard provider note. |
| Privacy scope not hidden behind a collapse | Met. The provider scope sentence and the "withdrawing consent cannot take back anything sent" caveat are outside the `<details>`. Only the long list is collapsed. |
| Glossary (Revived = active with valid analysis) | Met in Graveyard labels. |
| UX-01 one primary CTA per view | **Gap.** Every Ghost card shows a `gy-btn--revive` button, plus Safe Burial, so a grid has many equal CTAs. Needs human design review. |
| CR-01 Crypt must not imply phone gallery deletion | **Partial.** Copy says "archived, original kept" but no explicit sentence that the phone gallery is untouched. |
| Floating batch Action Bar ("5 selected - Make Notes - Something else") | Gap. Suggestion lives in the sidebar only. |
| Narrow screens: assistant as bottom drawer | Needs real browser check. |
| Not-yet-covered: Creations Review/Approve flow, Activity states, Connections and Privacy page, OneNote/Calendar, undo | Not implemented, out of scope for this UX PR. |
| PrivacyNotice shows "unconfirmed" text while loading | Minor remaining gap (assistant is fixed; the shared notice still uses one string). |
