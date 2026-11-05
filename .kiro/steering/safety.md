# Safety Steering — Screenshot Graveyard

> Source of truth: `.kiro/specs/screenshot-graveyard/requirements.md` and
> `design.md`. These invariants are copied from the approved specs and MUST NOT
> be weakened, reworded away, or removed. Enforce them in the service layer.

## The ten mandatory engineering invariants (INV-1 … INV-10)

- **INV-1 — AI never overwrites original screenshot bytes.** Originals are
  write-once, content-addressed, stored read-only; there is no UPDATE path to
  original bytes.
- **INV-2 — Archived images can be restored.** Safe Burial moves a screenshot to
  The Crypt while retaining its bytes; Resurrection returns it to active.
- **INV-3 — Restored image bytes match the original SHA-256.** Restore recomputes
  the checksum and verifies it equals the stored checksum before succeeding.
- **INV-4 — Failed processing cannot destroy source data.** On any AI/validation/
  delivery failure, sources are preserved unchanged; failed operations never
  mutate or delete originals or prior artifacts.
- **INV-5 — Redo creates a new artifact version.** An AI Redo inserts a new
  version; it never overwrites an existing version.
- **INV-6 — Previous artifact versions remain available.** All prior versions
  stay retrievable after any redo or manual edit.
- **INV-7 — Generated outputs retain references to their source screenshots.**
  `source_reference` is attached to the artifact so provenance survives every
  version and redo.
- **INV-8 — External writes are never reported successful without verified
  evidence.** (P1; governs the `Delivery_Service`. In P0 the service is an
  interface stub only.)
- **INV-9 — Uncertain retries never blindly create duplicate external
  artifacts.** (P1; enforced via an idempotency key when external delivery is
  enabled. Stub-only in P0.)
- **INV-10 — Archive and review actions never permanently delete originals.**
  There is no delete endpoint; the file store exposes no delete/overwrite;
  archiving retains bytes.

## Safety boundaries to enforce

### Workspace isolation (the only security boundary; no accounts)

- Each visitor gets an opaque, unguessable Workspace Identifier =
  `secrets.token_urlsafe(32)`. Store only `sha256(token)` server-side; the raw
  token lives only in an `HttpOnly; Secure; SameSite=Strict; Path=/` cookie.
- The client never supplies a workspace id; the server-resolved id is
  authoritative. Every query and every file read is scoped by `workspace_id`.
- A resource that exists in another workspace returns **404** (existence
  non-disclosure), never 403.
- CSRF defense on state-changing requests: SameSite=Strict + server-side
  Origin/Referer check + required custom header.
- Losing the cookie can make a workspace permanently inaccessible; there is
  intentionally no token-bearing recovery URL in P0.

### Immutable original bytes

Write to a unique temp file in a reserved `.tmp/` location, fsync, then publish
by exclusive create (`os.link` / `O_CREAT|O_EXCL`) to the content-addressed path;
if the destination exists, treat as success without replacing bytes. Published
files are `0o444`. try/finally cleanup covers in-process errors only; cleanup
sweeps must never touch published originals.

### Append-only versions

`artifact_version` is INSERT-only with `UNIQUE(artifact_id, version_number)`.
Manual edits and AI redos both create new versions; a later redo never discards a
manual edit. Concurrent redo/edit resolve via bounded retry-on-conflict so both
complete with distinct version numbers.

### Upload validation (content-authoritative)

Accept decision = file signature + successful Pillow decode + dimension/megapixel
cap (decompression-bomb guard) + 15 MB size gate (enforced before full buffering).
Declared extension/MIME are hints only, never the sole reject reason. Reject
malformed input safely with a descriptive, file-named error.

### Consent before cloud AI

Show a general privacy warning before analysis; obtain explicit informed consent
before transmitting image bytes to any cloud AI; no consent → the adapter is never
called (403). Users can still store, archive, and restore without consenting.
Sensitive-content indicators are advisory POST-analysis outputs only — never a
pre-consent detection claim and never a guarantee.

### Safe execution boundaries

Operation status is one of pending/successful/failed/uncertain; `successful` is
set only after the persistence transaction commits. Classification + Epitaph from
one AI call persist atomically (both or neither). Never report success without
evidence; present uncertainty rather than fake success. Do not log image contents
or credentials. Never expose private original files through public storage.
