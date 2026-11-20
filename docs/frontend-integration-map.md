# Visual UI redesign: component to backend map

The Gothic Workspace is the default. It uses the existing same-origin clients for workspace bootstrap, screenshot upload and listing, and owner-authorized previews. The Crypt is read-only in the new shell. The **Open classic workspace** link (`/?view=classic`) keeps consent, Revive, Epitaph editing, archive/restore, and Study Notes available with the same workspace cookie. FastAPI endpoints are unchanged.

The frontend-only **demo mode** (`/?demo=1` or `npm run dev:demo`) still uses synthetic fixtures and makes no backend requests.

Layers:
- `src/ui/`: design tokens, primitives (Button, Card, Badge, Checkbox, SelectField, MenuButton, Stepper), ghost and graveyard art, synthetic screenshot art.
- `src/shell/`: presentational components that take view models from `shell/types.ts`. No data access.
- `src/demo/`: `DemoApp` (state and interactions) and `fixtures.ts` (invented sample data, labeled "Demo data" / "Sample" in the UI).
- `src/shell/LiveWorkspace.tsx`: live entry point and mapping from server screenshots to shell view models.

| Component | Demo source | Real source to wire later |
|---|---|---|
| `UploadDropZone` | Local object URLs, never uploaded | Connected to `uploadScreenshots` (`POST /api/screenshots`); the server validates file bytes |
| `ScreenshotGallery` / `ScreenshotCard` | `DEMO_SHOTS` | Connected to `listScreenshots` and `imageUrl`; Workspace shows active screenshots and The Crypt shows archived screenshots |
| `FilterBar` | Client-side filter/sort | Client-side on `Screenshot` + `analysis.category`; no endpoint needed |
| Card suggestion label | `ShotVM.suggestedAction` fixture | `deriveSuggestions` (`assistant/suggestions.ts`), rules and AI-sourced, after consent |
| `SelectionBar` | Local selection | Existing selection state in `Graveyard.tsx` (`MAX_STUDY_NOTE_SELECTION` applies) |
| Detail `Drawer` | Fixture details | Authorized image and real metadata are connected; consent, analysis and edits still use the classic `ScreenshotDetail` |
| `AssistantPanel` suggestion, stepper | `buildArtifact` fixtures | `QuietAssistant` logic and `deriveSuggestions`; Prepare = `generateStudyNote` operation, Review = `getStudyNote` / versions, Approve = future export/write step |
| Assistant "Approve" | Always disabled | Not available: no external write endpoint exists. `studyNoteExportUrl` is the only export |
| Recent Activity | `DEMO_ACTIVITY` | Operation polling (`getOperation`, `ai/useOperationPolling.ts`) |
| Safe Burial / Resurrection, The Crypt | Local state | `archiveScreenshot`, `restoreScreenshot` |
| Creations | `DEMO_CREATIONS` | `listStudyNotes`, `getStudyNote` |
| Connections | Static "Not connected" / "Coming later" | None yet (OneNote/Calendar OAuth out of scope) |
| Header "Demo AI mode" | Constant | `getProviderMode`, `HealthStatus` |

## Remaining integration tasks
1. Add Safe Burial and restore in the live drawer using the classic lifecycle clients and server-confirmed state.
2. Move the existing consent and AI workflow UI into the live shell in a later batch.
3. **Safety blocker, unchanged:** the backend consent-dispatch race (see `docs/` proposal) must be fixed before real AI provider acceptance.
4. Approve step and external connections need backend work.
5. Header bell and account buttons are inert placeholders.

## Safe Burial / Resurrection: outcome limits of the current API

`POST /api/screenshots/{id}/archive` and `/restore` return no operation id, and there is no lifecycle status endpoint. The only evidence the client can get about a request it lost track of (timeout, dropped connection, 5xx, unreadable or mismatched body) is the screenshot's current `state` from `GET /api/screenshots`.

What the Gothic Workspace does with that:
- A matching, explicit reply is the only thing treated as success or failure. A 4xx, or a reply for the right screenshot with `status: failed`, is a definite failure. Anything else is **unresolved**.
- An unresolved action is never reported as failed and is never replayed. The screenshot's Safe Burial / Resurrection control stays blocked.
- "Check status" re-reads the list (bounded to 10 s). Only a read that is still authoritative (not superseded by a newer read or a confirmed change) and shows the expected state resolves it, as success.
- An unchanged state does **not** resolve it. The earlier request may still commit after the read, and the client cannot tell "failed" from "not finished yet". No wait period is treated as proof.
- Recovery: check again, or reload the page, which starts from the server's current state. Both actions are idempotent on the server today, so a late commit cannot corrupt data, but the UI does not rely on that.

Definitive resolution needs backend support: an idempotency key or operation id returned by (or sent with) the lifecycle POST plus a status read, or a state precondition on the transition. That is a separate backend task.
