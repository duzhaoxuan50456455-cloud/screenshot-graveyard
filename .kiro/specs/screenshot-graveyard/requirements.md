# Requirements Document

## Introduction

Screenshot Graveyard is a purpose-aware, reversible AI screenshot workflow web application built for Kiroween 2026 (Costume Contest category). Users upload previously-captured screenshots, and the application helps them understand each screenshot's possible purpose, transform appropriate content into useful outputs (notably structured study notes), preserve recoverable originals, and redo transformations when the AI is inaccurate.

The product's differentiation rests on three pillars: purpose-aware transformation (classification plus a short purpose caption called an Epitaph), reversible AI execution (immutable originals, versioned artifacts, non-destructive redo), and a functional Halloween user experience where each metaphor communicates real system state. The application is explicitly NOT an OCR tool, a generic organizer, a to-do list, or a decorative gallery.

This document defines P0 (must-have), P1 (optional, only after P0 is stable), and P2 (explicitly out of scope / non-goal) requirements. The architecture is intentionally constrained to be realistic for one undergraduate developer and a December deadline: React + TypeScript + Vite frontend, Python + FastAPI backend, a single persistent relational database plus private persistent file storage for original image bytes, and a replaceable multimodal AI model adapter that produces validated structured output. Microservices, distributed queues, and complex infrastructure are avoided.

Ten mandatory engineering invariants (INV-1 through INV-10) are threaded through the acceptance criteria below; each criterion that enforces an invariant references it by number so the invariant is traceable to at least one criterion.

### Submission and Timeline Constraints (context, not system behavior)

- Submission deadline: December 4, 2026, 2:00 PM Pacific; internal deadline December 2, 2026.
- The application must be newly developed starting from October 30, 2026.
- The deployed application must remain live through January 29, 2027.
- Deliverables: automated tests including property-based tests for critical invariants, public deployment, real Kiro implementation evidence, a public GitHub repository with a root `.kiro` directory, an English README, an open-source LICENSE, a public YouTube demo under 3 minutes, a complete Devpost submission, and development documentation.

### Priority Legend

- **P0** — Must-have for submission. Required acceptance criteria.
- **P1** — Optional. Attempted only after all P0 capabilities are stable. Required acceptance criteria are written conditionally (WHERE the P1 feature is enabled).
- **P2** — Explicitly out of scope. Captured as non-goals only (Requirement 17). No implementable criteria are derived from P2.

### Inline Assumptions and Recommended Decisions

The following ambiguities were identified in the source vision. Each is resolved with the smallest practical decision so the requirements remain testable. These are assumptions, not new scope.

- **ASSUMPTION A1 (Public access and per-visitor data isolation):** The application must be publicly reachable for Kiroween judging, but a shared unauthenticated deployment must never expose one visitor's private screenshots to another visitor. Rather than a bare "single-user, no auth" model, this document assumes isolated per-visitor workspaces as the recommended MVP strategy: on first visit the System issues an opaque, unguessable workspace identifier (an unpredictable session/workspace token) that scopes all persisted data and all private file access, so screenshots, classifications, Epitaphs, study notes, artifacts, and versions belonging to one workspace are never readable from another workspace. A full multi-user authentication system (accounts, login, password management) is NOT introduced. The isolation model is compatible with persistence, archive/restore, and AI processing: every data and file operation is scoped to the requesting workspace and survives page refreshes and backend restarts. If isolated per-visitor workspaces cannot be delivered in time, the documented fallback is a read-only public demo populated only with synthetic content, combined with a separately protected (non-public) upload environment. Private original image files remain unreachable through any public storage URL regardless of which option is chosen (see Requirement 9 and Requirement 15). Multi-tenant account auth is out of scope.
- **ASSUMPTION A2 (Permanent deletion):** The vision states Safe Burial is "never permanent deletion." This document assumes there is NO user-facing permanent-delete action for originals in P0. Archive (Safe Burial) and restore (Resurrection) are the only lifecycle transitions for originals.
- **ASSUMPTION A3 (Duplicate handling):** "Handle duplicates reasonably" is resolved as: duplicates are detected by SHA-256 checksum, stored once as bytes, and surfaced to the user as distinct upload records that reference the shared content, rather than silently rejected. Duplicate detection and shared-content storage are scoped within a single workspace (see ASSUMPTION A1).
- **ASSUMPTION A4 (Study-note screenshot count):** "~4–8 related study screenshots" is treated as a soft guideline. The system accepts 1 to 12 screenshots for a study note and warns (does not block) outside the 4–8 range.
- **ASSUMPTION A5 (Supported formats):** Supported upload formats are PNG, JPEG, and WebP. The maximum accepted file size is 15 MB per file. These are concrete, testable defaults and may be tuned in design.
- **ASSUMPTION A6 (External destination for P1):** The single genuine external integration, if attempted, is OneNote; Google Docs is the fallback. The external integration is P1, not P0: no OAuth flow and no third-party destination availability are required for P0 to be complete. Markdown export is mandatory regardless and does not depend on any external integration.

## Glossary

- **System**: The Screenshot Graveyard application as a whole (frontend plus backend) unless a more specific component is named.
- **Workspace**: An isolated per-visitor data boundary identified by an opaque, unguessable workspace identifier. All of a visitor's screenshots, artifacts, versions, and private files are scoped to a single Workspace and are not readable from any other Workspace. (See ASSUMPTION A1.)
- **Workspace Identifier**: An opaque, unpredictable token assigned on first visit that scopes all persisted data and private file access for one visitor. It is not a user account and carries no authentication credentials.
- **Vault_Service**: The backend component responsible for upload validation, immutable storage of original image bytes, checksums, metadata persistence, and archive/restore lifecycle.
- **AI_Adapter**: The replaceable multimodal model adapter that accepts images and prompts and returns structured, validated output. Swappable without changing calling code.
- **Classification_Service**: The backend component that invokes the AI_Adapter to classify a screenshot and produce its Epitaph in a single AI call.
- **Study_Note_Service**: The backend component that generates structured study notes from a user-selected set of screenshots.
- **Artifact_Service**: The backend component that records source-to-artifact relationships, maintains artifact version history, and manages redo/regeneration.
- **Delivery_Service**: The backend component that performs external writes (e.g., OneNote/Google Docs) and verifies delivery evidence. (P1)
- **Screenshot**: An uploaded image record with a stable Screenshot ID, immutable original bytes, a SHA-256 checksum, and metadata.
- **Screenshot ID**: A stable, unique identifier assigned to a screenshot at upload that never changes for the life of the record.
- **Artifact**: A generated output (Epitaph, Study_Note, or external-delivery record) that references one or more source screenshots and carries a version history.
- **Artifact Version**: A single retained, retrievable snapshot of an artifact's content. Versions are produced either by an AI Redo or by a saved manual edit, and a prior saved version is never mutated in place. (See Requirement 7.)
- **Manual Edit**: A user-authored change to an artifact's content (for example, editing a study note). A saved Manual Edit produces a new retained version rather than overwriting a prior saved version.
- **AI Redo**: A user-initiated regeneration of an artifact by the AI_Adapter. An AI Redo always creates a new version and never discards a user's manually edited version.
- **Epitaph**: A short, editable purpose caption for a single analyzed screenshot, generated together with classification. It distinguishes visible facts from inferred intent and may express uncertainty. See Requirement 3.
- **Classification Category**: One of six fixed categories: Study/Knowledge, Reference/Document, Shopping/Inspiration, Memory/Entertainment, Unknown/Needs Review, Potentially Sensitive.
- **Study_Note**: A structured, editable artifact generated from selected study screenshots, containing Topic, Main concepts, Extracted code, Code explanations, Important pitfalls, Uncertain/ambiguous content, and References to source screenshots.
- **Ghost**: UI metaphor for a screenshot that has been uploaded but not yet analyzed (pending state).
- **The Crypt**: UI metaphor for the recoverable archive — the collection of archived (Safe Buried) screenshots that can be restored.
- **Revive**: UI metaphor for the user action that begins an AI transformation on a screenshot or set of screenshots.
- **Resurrection**: UI metaphor for restoring an archived original or redoing a transformation to produce a new version.
- **Safe Burial**: UI metaphor for archiving a screenshot. Archiving never permanently deletes the original and never affects the user's device.
- **Reduced-motion**: An accessibility mode that disables or minimizes animation, honoring the user's operating-system or in-app preference.
- **External Write**: A write of generated content to a third-party destination outside the System (e.g., OneNote, Google Docs). (P1)

---

## Requirements

### Requirement 1: Safe Screenshot Vault — Upload and Validation (P0)

**User Story:** As a solo user, I want to upload multiple previously-captured screenshots from my browser and have them validated, so that only genuine, supported images enter my vault without risk to my originals.

#### Acceptance Criteria

1. WHEN a user selects one or more files for upload, THE Vault_Service SHALL accept multiple files in a single upload operation.
2. WHEN a file is uploaded, THE Vault_Service SHALL validate that the file is a supported image type (PNG, JPEG, or WebP) determined by its actual content (file signature plus successful decode), treating any declared filename extension or declared MIME type as a non-authoritative hint only.
3. WHEN a file is uploaded, THE Vault_Service SHALL validate the actual file content against its declared format by inspecting the file signature rather than trusting the file extension alone.
4. IF an uploaded file's actual content does not match a supported image format, THEN THE Vault_Service SHALL reject the file and return a descriptive error that names the file.
5. IF an uploaded file exceeds 15 MB, THEN THE Vault_Service SHALL reject the file and return a descriptive error that names the file and the size limit.
6. WHEN a batch upload contains both valid and invalid files, THE Vault_Service SHALL store the valid files and report per-file results for the invalid files, without aborting the entire batch.
7. WHEN a file passes validation, THE Vault_Service SHALL assign a stable Screenshot ID that does not change for the life of the record.

### Requirement 2: Safe Screenshot Vault — Immutable Storage, Checksums, Persistence, Duplicates (P0)

**User Story:** As a solo user, I want my original screenshots stored immutably with verifiable integrity and persisted across restarts, so that I never lose or corrupt an original. (Enforces INV-1, INV-3, INV-10.)

#### Acceptance Criteria

1. WHEN a screenshot passes validation, THE Vault_Service SHALL store the original image bytes immutably such that no later operation modifies those bytes. (INV-1)
2. WHEN a screenshot is stored, THE Vault_Service SHALL compute and persist a SHA-256 checksum of the original image bytes.
3. THE Vault_Service SHALL persist stored image bytes and screenshot metadata such that they survive page refreshes and backend restarts.
4. WHEN a newly uploaded file's SHA-256 checksum matches that of an already-stored screenshot within the same Workspace, THE Vault_Service SHALL store the identical bytes only once and create a distinct upload record that references the shared content. (See ASSUMPTION A3.)
5. WHEN the user views a duplicate upload record, THE System SHALL indicate that the content is identical to an existing screenshot.
6. THE Vault_Service SHALL NOT provide any user-facing action that permanently deletes stored original bytes. (INV-10; see ASSUMPTION A2.)

### Requirement 3: Safe Screenshot Vault — Safe Burial and Resurrection (Archive and Restore) (P0)

**User Story:** As a solo user, I want to archive (Safe Burial) screenshots and later restore (Resurrection) them with byte-for-byte integrity, so that my originals are always recoverable. (Enforces INV-2, INV-3, INV-10.)

#### Acceptance Criteria

1. WHEN a user performs Safe Burial on a screenshot, THE Vault_Service SHALL move the screenshot into The Crypt (archived state) while retaining its original bytes and checksum. (INV-2, INV-10)
2. WHEN a user performs Resurrection (restore) on an archived screenshot, THE Vault_Service SHALL return the screenshot to the active state. (INV-2)
3. WHEN an archived screenshot is restored, THE Vault_Service SHALL verify that the restored image bytes produce a SHA-256 checksum identical to the originally stored checksum. (INV-3)
4. IF a restore verification detects a checksum mismatch, THEN THE Vault_Service SHALL report the restore as failed and SHALL retain the archived original bytes unchanged. (INV-3, INV-4)
5. WHILE a screenshot is archived in The Crypt, THE System SHALL keep the screenshot's original bytes available for restoration and SHALL NOT permanently delete them. (INV-10)
6. WHEN any archive or restore operation fails, THE Vault_Service SHALL leave the original source bytes intact. (INV-4)

### Requirement 4: Purpose-Aware AI Classification (P0)

**User Story:** As a solo user, I want each screenshot classified into a clear purpose category with structured, editable, uncertainty-aware output, so that I understand what a screenshot is likely for without the system over-committing to a guess.

#### Acceptance Criteria

1. WHEN a user Revives a screenshot for analysis, THE Classification_Service SHALL classify the screenshot into exactly one of: Study/Knowledge, Reference/Document, Shopping/Inspiration, Memory/Entertainment, Unknown/Needs Review, Potentially Sensitive.
2. WHEN the AI_Adapter returns a classification, THE Classification_Service SHALL validate the output against a defined structured schema before persisting it.
3. IF the AI_Adapter returns output that fails schema validation, THEN THE Classification_Service SHALL mark the classification as failed and SHALL preserve the source screenshot unchanged. (INV-1, INV-4)
4. WHERE the AI expresses low confidence, THE Classification_Service SHALL represent the uncertainty in the structured output (for example, by assigning Unknown/Needs Review or recording a confidence indicator) rather than forcing a definitive category.
5. WHEN a classification is displayed, THE System SHALL allow the user to edit and override the category, and SHALL persist the edited category.
6. THE Classification_Service SHALL NOT automatically convert a screenshot into a task, reminder, or scheduled item as a result of classification.
7. WHEN the Classification_Service assigns the Potentially Sensitive category, THE System SHALL treat that category as a post-analysis advisory signal and SHALL NOT present it as a guaranteed detection of sensitive content. (See Requirement 9.)

### Requirement 5: Epitaph Generation and Editing (P0)

**User Story:** As a solo user, I want every analyzed screenshot to receive a short, honest purpose caption that I can edit, so that I get a human-readable reminder of a screenshot's likely purpose without the system pretending to know my original intent. (Enforces INV-1.)

#### Acceptance Criteria

1. WHEN a screenshot is classified, THE Classification_Service SHALL generate its Epitaph in the same AI call as the classification, without a separate AI request.
2. THE Epitaph SHALL distinguish visible facts from inferred intentions in its wording (for example, "C++ lecture: function parameters and return values." for a visible fact versus "Possibly saved to compare sweater styles." for an inferred intention).
3. WHERE the purpose cannot be reasonably inferred, THE Classification_Service SHALL produce an Epitaph that expresses uncertainty (for example, "Purpose unknown.").
4. THE System SHALL NOT produce an Epitaph that asserts the user's original reason for saving an image as a known fact.
5. WHEN an Epitaph is displayed, THE System SHALL allow the user to edit the Epitaph text and SHALL persist the edited text.
6. WHEN a user edits an Epitaph, THE System SHALL NOT modify the underlying screenshot's original image bytes. (INV-1)

### Requirement 6: Study-Note Transformation (P0)

**User Story:** As a solo user, I want to manually select a set of related study screenshots and generate one editable, structured study note that faithfully preserves code and flags uncertainty, so that I can turn scattered lecture screenshots into usable notes. (Enforces INV-1, INV-5, INV-6, INV-7.)

#### Acceptance Criteria

1. WHEN a user creates a study note, THE Study_Note_Service SHALL require the user to manually select the source screenshots and SHALL NOT auto-cluster screenshots on the user's behalf.
2. WHERE the user selects a number of screenshots outside the range of 4 to 8, THE System SHALL display a non-blocking warning while still allowing generation. (See ASSUMPTION A4.)
3. IF the user selects fewer than 1 or more than 12 screenshots, THEN THE Study_Note_Service SHALL reject the request with a descriptive error. (See ASSUMPTION A4.)
4. WHEN a study note is generated, THE Study_Note_Service SHALL produce a structured note containing: Topic, Main concepts, Extracted code, Code explanations, Important pitfalls, Uncertain/ambiguous content, and References to the source screenshots.
5. THE Study_Note_Service SHALL preserve extracted code syntax faithfully, including indentation and symbols, as read from the source screenshots.
6. WHERE an OCR or vision reading is uncertain, THE Study_Note_Service SHALL flag the uncertain content in the Uncertain/ambiguous content section rather than presenting it as certain.
7. WHEN a study note is generated, THE Study_Note_Service SHALL record references from the note back to each source Screenshot ID. (INV-7)
8. WHEN viewing a study note, THE System SHALL allow the user to reopen each referenced original screenshot to verify extracted information.
9. WHEN a user saves a Manual Edit to a study note, THE Study_Note_Service SHALL persist the edited content as a new retained Artifact Version and SHALL NOT overwrite any previously saved version in place. (INV-5, INV-6)
10. WHEN a user performs an AI Redo after a Manual Edit has been saved, THE Study_Note_Service SHALL create a new version and SHALL retain the user's manually edited version as separately retrievable, so that a Manual Edit is never lost on a later AI Redo. (INV-5, INV-6)
11. THE System SHALL export a study note as valid Markdown that reflects a selected retained version of the note content, defaulting to the current version.
12. WHEN a study note is generated, edited, or regenerated, THE Study_Note_Service SHALL NOT modify the original image bytes of any source screenshot. (INV-1)

### Requirement 7: Reversible AI Execution — Versioning and Redo (P0)

**User Story:** As a solo user, I want AI transformations and my own manual edits to be reversible and versioned so that an AI Redo or a saved manual edit creates a new version while all prior versions and originals remain available. (Enforces INV-1, INV-5, INV-6, INV-7.)

#### Acceptance Criteria

1. THE Artifact_Service SHALL preserve input screenshots as immutable throughout any AI execution. (INV-1)
2. WHEN an artifact is generated from one or more screenshots, THE Artifact_Service SHALL record the relationship between each source screenshot and the generated artifact. (INV-7)
3. WHEN a user performs an AI Redo (Resurrection of a transformation), THE Artifact_Service SHALL create a NEW artifact version rather than overwriting any existing version. (INV-5)
4. WHEN a user saves a Manual Edit to an artifact, THE Artifact_Service SHALL create a NEW retained artifact version rather than mutating a previously saved version in place. (INV-5, INV-6)
5. WHEN a new artifact version is created by either an AI Redo or a saved Manual Edit, THE Artifact_Service SHALL retain all previous versions as retrievable. (INV-6)
6. WHEN a user performs an AI Redo after a Manual Edit has been saved, THE Artifact_Service SHALL NOT discard the manually edited version and SHALL keep it separately retrievable. (INV-5, INV-6)
7. WHEN a user initiates an AI Redo, THE System SHALL allow the user to modify the regeneration instructions before the new version is generated.
8. WHEN at least two versions of an artifact exist, THE System SHALL allow the user to retrieve and compare any version against another (for example, v1 versus v2).
9. THE System SHALL allow the user to restore the original image(s) associated with an artifact at any time. (INV-2)

### Requirement 8: Reversible AI Execution — Failure, Status, and External-Write Safety (P0)

**User Story:** As a solo user, I want clear operation status and strict safeguards around external writes, so that failures never destroy my sources and the system never falsely claims an external delivery succeeded. (Enforces INV-4, INV-8, INV-9.)

**Note:** Criteria 4 and 5 are external-write safety constraints that govern the P1 Delivery_Service and apply only WHERE/WHEN an External Write is performed. They are mandatory design constraints on external-write behavior, but P0 completion does NOT depend on any external-delivery infrastructure existing. No OAuth flow or third-party destination needs to be built for P0. Markdown export (Requirement 6) remains the mandatory P0 output path.

#### Acceptance Criteria

1. WHEN an AI call fails during a transformation, THE Artifact_Service SHALL preserve all source material unchanged. (INV-4)
2. THE System SHALL represent every transformation operation with a distinct status of successful, failed, pending, or uncertain, and SHALL display that status to the user.
3. IF a transformation is in an uncertain state, THEN THE System SHALL present the uncertainty to the user rather than reporting success.
4. WHERE an External Write is performed, WHEN the write is attempted, THE Delivery_Service SHALL NOT report the write as successful unless it has verified evidence that the external destination accepted the content. (INV-8)
5. WHERE an External Write is performed, IF the write returns an uncertain result and the user retries, THEN THE Delivery_Service SHALL check for and prevent creation of a blind duplicate external artifact. (INV-9)
6. WHEN any transformation or delivery operation fails, THE System SHALL leave previously stored artifacts and source screenshots intact and retrievable. (INV-4, INV-6)

### Requirement 9: Privacy and Consent (P0)

**User Story:** As a solo user, I want informed control over what leaves my device for AI processing and honest handling of sensitive content, so that I can use the app without exposing credentials or private images. (Enforces INV-1 for source safety where processing is skipped.)

#### Acceptance Criteria

1. BEFORE any AI analysis of a screenshot, THE System SHALL display a general privacy warning that AI analysis transmits image content to a cloud AI service.
2. IF a screenshot has not yet received explicit informed consent for AI processing, THEN THE System SHALL obtain that explicit consent from the user before transmitting the screenshot's image content to any cloud AI service.
3. WHEN requesting consent, THE System SHALL clearly explain what data is transmitted to the AI service.
4. THE System SHALL allow the user to skip AI processing for any screenshot and still store, archive, and restore that screenshot, scoped to the user's Workspace. (INV-1)
5. THE System SHALL NOT state or imply that it can detect sensitive content using cloud AI before consent for cloud processing is obtained.
6. WHEN consented AI analysis has completed, THE System MAY display advisory sensitive-content indicators as a post-analysis output, and THE System SHALL treat the Potentially Sensitive category as an advisory signal rather than a guarantee of detection.
7. THE System SHALL NOT promise perfect detection of sensitive screenshots and SHALL NOT claim to automatically prevent sensitive uploads.
8. THE System SHALL NOT store screenshot image contents or credentials in application logs.
9. THE System SHALL scope all persisted data and private file access to the requesting Workspace such that one visitor cannot read another visitor's screenshots or artifacts. (See ASSUMPTION A1; INV-1 for source safety.)
10. THE System SHALL NOT expose private original image files through publicly reachable storage, regardless of Workspace.
11. THE System SHALL NOT create a plaintext repository of credentials extracted from screenshots.
12. THE System SHALL NOT state or imply that archiving a screenshot removes the corresponding image from the user's phone or device.
13. WHERE consented AI analysis has produced a reading that resembles a government ID, student credential, password, token, or financial document, THE System SHALL surface a cautionary advisory indication to the user as a post-analysis output, without claiming the detection is certain and without having inspected the content before consent.

### Requirement 10: Functional Halloween UX (P0)

**User Story:** As a solo user, I want a cozy haunted archive aesthetic where each Halloween metaphor communicates real system state, so that the theme improves usability instead of being merely decorative.

#### Acceptance Criteria

1. THE System SHALL represent a pending (unanalyzed) screenshot as a Ghost, and the Ghost state SHALL correspond to the actual pending status.
2. THE System SHALL represent the recoverable archive as The Crypt, and The Crypt SHALL contain exactly the archived (Safe Buried) screenshots.
3. WHEN a user chooses to begin a transformation, THE System SHALL present the action as Revive and SHALL start the corresponding transformation.
4. WHEN a user restores an archived original or redoes a transformation, THE System SHALL present the action as Resurrection and SHALL provide a memorable Resurrection experience in the UI.
5. WHEN a user archives a screenshot, THE System SHALL present the action as Safe Burial and SHALL NOT permanently delete the original. (INV-10)
6. THE System SHALL ensure that each Halloween metaphor maps to and accurately reflects the real system state it represents.

### Requirement 11: Accessibility and Responsive Design (P0)

**User Story:** As a solo user on varied devices and with varied needs, I want an accessible, responsive, readable interface with reduced-motion support, so that the haunted theme never compromises usability.

#### Acceptance Criteria

1. THE System SHALL provide responsive layouts that remain usable across common mobile, tablet, and desktop viewport widths.
2. THE System SHALL use readable typography with sufficient color contrast for primary text and controls.
3. WHERE the user has enabled a reduced-motion preference (via operating system or in-app setting), THE System SHALL disable or minimize non-essential animations.
4. THE System SHALL provide keyboard operability and accessible names for interactive controls.
5. WHEN conveying operation status, THE System SHALL communicate the status through text or non-color cues in addition to color.

### Requirement 12: Data Model Persistence and Integrity (P0)

**User Story:** As a solo user, I want all my data stored in a reliable persistent store with clear source-to-artifact relationships, so that nothing is lost between sessions and provenance is always traceable. (Enforces INV-3, INV-6, INV-7.)

#### Acceptance Criteria

1. THE System SHALL persist screenshots, classifications, Epitaphs, study notes, artifacts, versions, and status in a persistent relational database.
2. THE System SHALL store original image bytes in private persistent file storage separate from the relational database.
3. THE System SHALL maintain referential links between each artifact version and its source Screenshot IDs. (INV-7)
4. WHEN the backend restarts, THE System SHALL make all previously stored screenshots, artifacts, and versions available again, scoped to each Workspace. (INV-6)
5. THE System SHALL store the SHA-256 checksum of each screenshot alongside its metadata for restore verification. (INV-3)
6. THE System SHALL associate each persisted record and private file with the Workspace Identifier that owns it, such that all queries and file reads are scoped to a single Workspace. (See ASSUMPTION A1.)

### Requirement 13: Replaceable AI Adapter with Validated Structured Output (P0)

**User Story:** As a solo developer, I want the AI integration isolated behind a replaceable adapter that returns validated structured output, so that I can swap models and keep outputs trustworthy.

#### Acceptance Criteria

1. THE System SHALL access all multimodal AI functionality through a single AI_Adapter interface.
2. THE AI_Adapter SHALL return structured output conforming to a defined schema for classification, Epitaph, and study-note generation.
3. IF the AI_Adapter returns output that does not conform to the defined schema, THEN THE System SHALL reject the output, mark the operation as failed or uncertain, and preserve source material. (INV-4)
4. THE System SHALL allow the underlying AI model to be replaced without changing the calling services' code.
5. WHERE the AI_Adapter output includes sensitive-content indicators, THE System SHALL treat those indicators as advisory outputs of consented analysis rather than as guaranteed detections. (See Requirement 9.)

### Requirement 14: Automated Testing and Property-Based Invariant Tests (P0)

**User Story:** As a solo developer, I want automated tests including property-based tests for the critical invariants, so that the reversibility and integrity guarantees are verified rather than assumed.

#### Acceptance Criteria

1. THE System SHALL include an automated test suite that can be executed with a single command.
2. THE System SHALL include property-based tests that verify the round-trip integrity invariant: for stored screenshots, archiving then restoring SHALL yield bytes whose SHA-256 matches the original. (INV-2, INV-3)
3. THE System SHALL include property-based tests verifying that an AI Redo or a saved Manual Edit creates a new artifact version while all prior versions remain retrievable. (INV-5, INV-6)
4. THE System SHALL include property-based tests verifying that generated artifacts always retain references to their source Screenshot IDs. (INV-7)
5. THE System SHALL include tests verifying that failed AI or delivery operations never modify or destroy source screenshot bytes. (INV-1, INV-4)
6. WHERE the P1 external integration is enabled, THE System SHALL include tests verifying that an External Write is never reported successful without verified evidence and that uncertain retries do not create duplicate external artifacts. (INV-8, INV-9)
7. IF the P1 external integration is NOT enabled, THEN the external-write tests in criterion 6 SHALL NOT be required for P0 completion, while the property, round-trip, and reversibility tests in criteria 2 through 5 remain mandatory.

### Requirement 15: Deployment and Submission Readiness (P0)

**User Story:** As a solo developer entering Kiroween 2026, I want the project publicly deployed and submission-complete, so that it satisfies contest requirements and stays live through the judging window without exposing any visitor's private screenshots.

#### Acceptance Criteria

1. THE System SHALL be publicly deployed and reachable at a public URL.
2. THE deployed System SHALL remain available from submission through January 29, 2027.
3. WHEN a visitor accesses the public deployment, THE System SHALL scope that visitor's data to an isolated Workspace identified by an opaque, unguessable Workspace Identifier, such that one visitor cannot read another visitor's screenshots or artifacts. (See ASSUMPTION A1; Requirement 9.9.)
4. THE System SHALL ensure that Workspace isolation remains in effect across persistence, archive/restore, and AI processing, and SHALL make a Workspace's data available again after backend restarts. (See Requirement 12.)
5. IF isolated per-visitor Workspaces are not delivered, THEN THE System SHALL instead present a read-only public demo populated only with synthetic content and SHALL keep any upload-capable environment separately protected and non-public. (Fallback per ASSUMPTION A1.)
6. THE project SHALL be hosted in a public GitHub repository that contains a root `.kiro` directory.
7. THE repository SHALL include an English README and an open-source LICENSE file.
8. THE project SHALL include real Kiro implementation evidence and development documentation.
9. THE submission SHALL include a public YouTube demo video under 3 minutes and a complete Devpost submission.
10. THE System SHALL use only synthetic or appropriately authorized demo screenshots in publicly accessible demonstration material. (See Requirement 9.)

### Requirement 16: P1 Optional Enhancements (P1 — only after P0 is stable)

**User Story:** As a solo user, I want optional conveniences (wishlist cards, a memories gallery, one genuine external destination, a quick review ritual, and subtle themed animations) once the core is stable, so that the product feels richer without risking P0.

**Note:** Every criterion below is conditional on the corresponding P1 feature being enabled. The OneNote/Google Docs external integration is P1 and is not required for P0 completion; P0 MUST NOT depend on OAuth or third-party destination availability. Markdown export (Requirement 6.11) remains mandatory regardless of whether any P1 external integration is delivered. No external-delivery infrastructure should be built before the integration is enabled.

#### Acceptance Criteria

1. WHERE the shopping wishlist feature is enabled, THE System SHALL allow the user to create simple wishlist cards from Shopping/Inspiration screenshots.
2. WHERE the memories gallery feature is enabled, THE System SHALL present Memory/Entertainment screenshots in a gallery view.
3. WHERE a single external destination integration is enabled, THE Delivery_Service SHALL deliver a study note to one genuine external destination (OneNote preferred; Google Docs fallback) subject to the external-write safety criteria in Requirement 8. (INV-8, INV-9; see ASSUMPTION A6.)
4. WHERE the Quick Ritual feature is enabled, THE System SHALL offer a time-boxed review (2-minute, 10-minute, or all-items) presenting Revive, Let Rest, and Later actions.
5. WHERE the Quick Ritual feature is enabled, THE System SHALL persist review actions and SHALL allow the user to undo a review action.
6. WHERE subtle Halloween animations are enabled, THE System SHALL connect each animation to a real workflow state and SHALL honor the reduced-motion preference. (See Requirement 11.3.)

### Requirement 17: Non-Goals (P2 — Explicitly Out of Scope)

**User Story:** As a solo developer with a fixed deadline, I want out-of-scope items recorded as non-goals, so that scope creep is prevented and reviewers understand deliberate exclusions.

**Note:** This requirement defines boundaries only. No implementable acceptance criteria are derived from these items; they must NOT appear as required tasks.

#### Acceptance Criteria

1. THE System SHALL NOT provide automatic screenshot clustering.
2. THE System SHALL NOT provide advanced reminders or scheduling.
3. THE System SHALL NOT provide price tracking.
4. THE System SHALL NOT provide persistent personalization based on user corrections.
5. THE System SHALL NOT provide screenshot-region annotation.
6. THE System SHALL NOT provide multiple external integrations (only one genuine destination is considered, and only as P1).
7. THE System SHALL NOT provide phone photo-library background syncing.
8. THE System SHALL NOT permanently delete device images (and SHALL NOT permanently delete stored originals; see ASSUMPTION A2).
9. THE System SHALL NOT provide a password vault or plaintext credential repository.
10. THE System SHALL NOT provide automated purchases.
11. THE System SHALL NOT provide a large autonomous-agent framework.
12. THE System SHALL NOT provide a full multi-user authentication system with accounts and login (per-visitor Workspace isolation is used instead; see ASSUMPTION A1).

---

## Invariant Traceability Matrix

Each mandatory engineering invariant maps to at least one acceptance criterion above.

| Invariant | Description | Traced To |
|-----------|-------------|-----------|
| INV-1 | AI never overwrites original screenshot bytes | 2.1, 4.3, 5.6, 6.12, 7.1, 9.4, 13.3, 14.5 |
| INV-2 | Archived images can be restored | 3.1, 3.2, 7.9, 14.2 |
| INV-3 | Restored bytes match original SHA-256 | 2.2, 3.3, 3.4, 12.5, 14.2 |
| INV-4 | Failed processing cannot destroy source data | 3.4, 3.6, 4.3, 8.1, 8.6, 13.3, 14.5 |
| INV-5 | Redo creates a new artifact version | 6.9, 6.10, 7.3, 7.4, 7.6, 14.3 |
| INV-6 | Previous artifact versions remain available | 6.9, 6.10, 7.5, 7.6, 8.6, 12.4, 14.3 |
| INV-7 | Generated outputs retain references to sources | 6.7, 7.2, 12.3, 14.4 |
| INV-8 | External writes never reported successful without evidence (exercised/verified only when the P1 external integration is enabled) | 8.4, 14.6, 16.3 |
| INV-9 | Uncertain retries never blindly duplicate external artifacts (exercised/verified only when the P1 external integration is enabled) | 8.5, 14.6, 16.3 |
| INV-10 | Archive/review actions never permanently delete originals | 2.6, 3.1, 3.5, 10.5 |
