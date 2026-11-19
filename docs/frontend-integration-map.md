# Visual UI redesign: component to backend map

The redesign runs in a frontend-only **demo mode** (`npm run dev:demo`, or `/?demo=1`). It imports no API client and makes no requests. The existing live app (`Graveyard.tsx` and `src/ai/*`) and every FastAPI endpoint are unchanged, including consent protections and operation reconciliation.

Layers:
- `src/ui/`: design tokens, primitives (Button, Card, Badge, Checkbox, SelectField, MenuButton, Stepper), ghost and graveyard art, synthetic screenshot art.
- `src/shell/`: presentational components that take view models from `shell/types.ts`. No data access.
- `src/demo/`: `DemoApp` (state and interactions) and `fixtures.ts` (invented sample data, labeled "Demo data" / "Sample" in the UI).

| Component | Demo source | Real source to wire later |
|---|---|---|
| `UploadDropZone` | Local object URLs, never uploaded | `uploadScreenshots` (`POST` upload in `api/client.ts`) |
| `ScreenshotGallery` / `ScreenshotCard` | `DEMO_SHOTS` | `listScreenshots`, `imageUrl`; partition via `graveyardState.ts` (active vs archived, analysed vs not) |
| `FilterBar` | Client-side filter/sort | Client-side on `Screenshot` + `analysis.category`; no endpoint needed |
| Card suggestion label | `ShotVM.suggestedAction` fixture | `deriveSuggestions` (`assistant/suggestions.ts`), rules and AI-sourced, after consent |
| `SelectionBar` | Local selection | Existing selection state in `Graveyard.tsx` (`MAX_STUDY_NOTE_SELECTION` applies) |
| Detail `Drawer` | Fixture details | `ScreenshotDetail` (`GET` analysis, `editAnalysis`, `classifyScreenshot` via `ai/reviveCoordinator.ts`) |
| `AssistantPanel` suggestion, stepper | `buildArtifact` fixtures | `QuietAssistant` logic and `deriveSuggestions`; Prepare = `generateStudyNote` operation, Review = `getStudyNote` / versions, Approve = future export/write step |
| Assistant "Approve" | Always disabled | Not available: no external write endpoint exists. `studyNoteExportUrl` is the only export |
| Recent Activity | `DEMO_ACTIVITY` | Operation polling (`getOperation`, `ai/useOperationPolling.ts`) |
| Safe Burial / Resurrection, The Crypt | Local state | `archiveScreenshot`, `restoreScreenshot` |
| Creations | `DEMO_CREATIONS` | `listStudyNotes`, `getStudyNote` |
| Connections | Static "Not connected" / "Coming later" | None yet (OneNote/Calendar OAuth out of scope) |
| Header "Demo AI mode" | Constant | `getProviderMode`, `HealthStatus` |

## Remaining integration tasks
1. Replace `DemoApp` state with a data adapter that maps `Screenshot` / `Analysis` / study notes onto the `shell/types.ts` view models.
2. Offer the new shell as the default app, and move consent (`useConsent`, `PrivacyNotice`) into it. The live app has not been restyled; only demo mode has the new look.
3. **Safety blocker, unchanged:** the backend consent-dispatch race (see `docs/` proposal) must be fixed before real AI provider acceptance.
4. Approve step and external connections need backend work.
5. Header bell and account buttons are inert placeholders.
