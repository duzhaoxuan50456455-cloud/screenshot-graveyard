# Proposal: AI dispatch and in-flight consent boundary

Status: proposal only. Nothing in this document is implemented. No file under
`backend/` or `frontend/` is changed by the commit that adds it.

Scope: `ClassificationService` (initial classify and AI Redo) and
`StudyNoteService` (initial generation and AI Redo).

Tracked as a separate, separately reviewable backend safety task in
`tasks.md` (task 7.1). It is deliberately not part of Draft PR #1.

## 1. Statement about PR #1

**Draft PR #1 does not eliminate this race.** Consent can still be revoked after
the worker's last consent check and before the adapter is invoked. PR #1 and the
earlier hardening commits narrowed the window by adding a second consent re-check
after the original bytes are read. They did not close it, and no document or UI
copy should claim they did. This race exists in code that predates PR #1.

An independent reviewer demonstrated it. The proposal below is the plan to
address it properly, with an honest statement of what the fix can and cannot
promise (section 7).

## 2. The current check-to-call sequence

Line numbers are for the tree at HEAD 5a9f22e.

### Classification (`backend/app/services/classification_service.py`)

| Step | Lines | What happens |
| --- | --- | --- |
| 1. First consent check | 195-206 | `self._consent.require_consent(screenshot_id)`; revoked or missing sets the operation `failed`. |
| 2. Read original bytes | 209-215 | `self._vault.read_original(screenshot_id)`. |
| 3. Second consent check | 220-231 | Same call, added to cover the read window. **Last check.** |
| 4. Heartbeat | 236-239 | `self._operations.heartbeat_pending(operation_id)`. |
| 5. Adapter call | 240-246 | `self._adapter.classify_and_epitaph(...)`. **Dispatch.** |

Redo reaches the same function through `run_redo` (270-286) and
`_run_redo_exclusively` (288-349, call at 322).

### Study Notes (`backend/app/services/study_note_service.py`)

| Step | Lines | What happens |
| --- | --- | --- |
| 1. First consent check (all sources) | 167-176 | Loop over `ids`, `require_consent(sid)`. |
| 2. Read originals | 178-196 | Loop over up to twelve originals, with size limits. |
| 3. Second consent check (all sources) | 198-210 | Loop again. **Last check.** |
| 4. Heartbeat | 212-219 | `heartbeat_pending(operation_id)`. |
| 5. Adapter call | 220-224 | `self._adapter.study_note(images[, instructions=...])`. **Dispatch.** |

Redo reaches it through `run_redo` (246-262), `_run_redo_exclusively`
(264-320, call at 299).

### Where the revoke happens

`backend/app/routers/consent.py` (`record_consent`, about line 91) calls
`ConsentService.record_consent` (`backend/app/services/consent_service.py`
88-106), which calls `WorkspaceRepository.set_consent`
(`backend/app/db/repository.py` 381-402) and then `session.commit()`. The worker
reads with `ConsentService.require_consent` (consent_service.py 138-153), which
calls `WorkspaceRepository.get_consent` (repository.py 404-410): a plain
`SELECT` with no lock and no version.

The worker and the revoke request use different `Session` objects
(`routers/classification.py` 59-67 builds its own session for the background
job). Nothing orders the revoke commit against the worker's check or the call.

### Why the window is not zero

1. The final check (classification 221, study notes 204) is a point-in-time read.
   The result is a Python value. Consent can change one millisecond later.
2. Between that read and the adapter call there is a heartbeat
   (`operation_service.py` 204-231). It calls `lock_workspace()`, performs an
   `UPDATE`, and commits. On SQLite `lock_workspace` takes the database write
   lock (`repository.py` 121-136), so the worker can block behind another
   writer. That makes the window longer exactly when the system is busy. The
   heartbeat sits after the last check, which is the wrong order for narrowing.
3. Inside the adapter, `OpenAIVisionAdapter` validates the image and builds a
   base64 payload (`openai_vision_adapter.py` 123-135, 190-213) before the
   request leaves the process.
4. `ConsentService.run_with_consent` (consent_service.py 155-167) is an
   unused-by-workers guarded boundary with the same shape: check, then call.
   It has the identical gap.
5. `exclusive_execution` (`operation_service.py` 144-164) is process-local and
   Redo-only. It prevents duplicate dispatch, not revoke-versus-dispatch.

Adjacent observation, not the subject of this proposal:
`ClassificationService` catches `ConsentRequiredError` and
`ConsentScreenshotNotFoundError` at 196-206 and 221-231 but not
`ConsentServiceUnavailableError`, which `StudyNoteService` does handle
(175-176, 209-210). A database error during the check escapes to the router job
wrapper (`routers/classification.py` 70+), which fails the operation. The
outcome is safe (no dispatch) but inconsistent. The follow-up task should align
them.

## 3. Defined boundaries

**AI dispatch boundary.** The instant the worker hands image bytes to
`AIAdapter.classify_and_epitaph` or `AIAdapter.study_note`. After this point the
application cannot recall bytes. For the OpenAI adapter this is the call to
`client.responses.parse` in `_request` (openai_vision_adapter.py 200-213),
which is the first moment bytes can leave the process.

**In-flight consent boundary.** The period from a successful dispatch
authorization to the adapter returning. Consent is a property of "may we start a
send", not "may a send continue". A revoke that commits during this period
cannot stop a request that is already on the wire. It can only prevent
persistence of the result (see 7).

**Authorization point.** The single moment at which the service decides "this
dispatch is permitted". Today that is the second `require_consent` call. The
proposal makes this an explicit, durable, ordered event instead of an implicit
Python local.

**Definition of "after revoke".** A revoke is effective at its commit. A
dispatch is "after revoke" if its authorization point is ordered after that
commit. A dispatch whose authorization point is ordered before the revoke commit
is "in flight" and is reported honestly (section 7).

## 4. Transaction and concurrency implications

- **Isolation.** `db/session.py` 17-26 sets no isolation level. On SQLite, the
  default deferred transaction reads a snapshot at first read; with
  `autoflush=False, expire_on_commit=False` a long-lived worker session can hold
  stale ORM state. `require_consent` issues a fresh `SELECT`, so it sees
  committed data, but only as of that instant.
- **No lock spans check and call.** Neither SQLite nor PostgreSQL ordering
  extends past the `SELECT`. Fixing this needs either a lock held to the call
  or a durable marker (designs C and D below).
- **Do not hold a database transaction across a network call.** The adapter
  timeout is configurable and bounded (`default_timeout_seconds`,
  `AI_ADAPTER_TIMEOUT_SECONDS`), but a transaction held for that long blocks
  writers. On SQLite a held write lock blocks every writer in the database, so
  uploads, consent changes, and other workspaces stall. Not acceptable.
- **Row locking.** On PostgreSQL, `SELECT ... FOR SHARE` on the consent row
  prevents a concurrent revoke `UPDATE` until the share lock is released.
  Holding it through the call makes revoke wait up to a full provider timeout.
  A revoke endpoint that hangs for the provider timeout is worse UX and still
  does not stop bytes already sent. On SQLite `FOR SHARE` is a no-op.
- **Consent version (epoch).** Adding `consent_record.version` (incremented on
  every grant or revoke) lets the worker record the version it authorized
  against and compare at persistence. This detects a revoke that happened during
  the call (useful for discarding the result) but does not prevent the send.
- **Cancellation tokens.** An in-process registry mapping screenshot id to a
  `threading.Event`, set by the revoke path, lets a worker abort between the
  last check and the HTTP send, and lets the adapter check the token right
  before `responses.parse`. It is process-local, like `exclusive_execution`, so
  it does not cover multiple workers or processes. It cannot interrupt a
  request already sent (the SDK call is synchronous, `max_retries=0`).
- **Heartbeat ordering.** The heartbeat commit must move before the final
  consent check, not after it, so no database write happens between the last
  check and dispatch.
- **Operation row as the serialization point.** `lock_workspace()` already
  serializes workspace critical sections (`repository.py` 121-136). A dispatch
  ledger written under that lock gives a total order between revoke and
  authorization within a workspace on both backends.
- **Multi-screenshot batches.** Study Notes need all twelve consents at one
  ordered instant. Checking in a loop (204-205) is not atomic; one query for all
  ids inside the authorization transaction is.
- **Failure semantics.** On any failure to record authorization the worker must
  fail closed with no dispatch (INV-4), consistent with
  `ConsentServiceUnavailableError` behavior in consent_service.py 62-67.

## 5. Candidate designs

### A. Re-check and reorder only (narrow, not close)

Move the heartbeat before the final consent check; keep the final check as the
last statement before the adapter call. Optionally run one more cheap check
inside the adapter entry via an injected callback.

- Pro: tiny change, no schema change.
- Con: a revoke can still commit after the last read. It shrinks the window
  from "heartbeat plus lock wait" to "microseconds". It does not close it.

### B. Consent version compare

Add a version column. Record the version at authorization and compare at
persistence; discard the result and fail the operation if it changed.

- Pro: guarantees a revoked screenshot never gets a new artifact from an
  in-flight call (persistence guard).
- Con: does nothing about the send. Needs a migration.

### C. Hold a consent lock through the call

`SELECT ... FOR SHARE` on PostgreSQL for the duration of the call.

- Pro: on PostgreSQL, a revoke commit is strictly ordered before or after the
  whole call.
- Con: blocks revoke up to the provider timeout; useless on SQLite (the dev and
  test default); holds a connection and a transaction across a network call.
  Rejected.

### D. Durable dispatch ledger plus in-process cancellation (recommended)

1. New `ai_dispatch` table: `id`, `workspace_id`, `operation_id`,
   `screenshot_id`, `consent_version`, `authorized_at`, `state`
   (`authorized`, `sent`, `completed`, `failed`, `aborted`).
2. A single short transaction does, under `lock_workspace()`: heartbeat, read all
   required consents together, and insert one `authorized` row per screenshot.
   Revoke (`record_consent`) takes the same lock, so the two are totally ordered
   per workspace on SQLite and PostgreSQL.
3. Revoke, in its own transaction, sets consent false, bumps the version, and
   marks every `authorized` (not yet `sent`) row for that screenshot `aborted`.
   It returns the count of rows already `sent` so callers know what is in
   flight.
4. The worker, immediately before the adapter call, calls a tiny
   `mark_sent()` that flips `authorized` to `sent` only if the row is still
   `authorized` (compare-and-set). If it lost, it does not call the adapter.
   This is a second short transaction and holds no lock across the call.
5. An optional process-local cancellation token (set by revoke) is checked
   inside the adapter immediately before the HTTP request, to reduce the
   authorized-but-not-sent gap further.
6. At persistence, `complete_after_persistence` verifies the dispatch rows are
   not `aborted` and the consent version still matches; otherwise the result is
   discarded and the operation fails with "AI processing consent required".

Residual race under D: between step 4 (`mark_sent` commit) and the actual socket
write there is still a gap of in-process code (base64 building, SDK setup). It
cannot be reduced to zero without the provider cooperating. D makes the gap
small, explicit, auditable, and covered by tests. It does not make it
nonexistent.

- Pro: works on SQLite and PostgreSQL, no transaction across the network call,
  revoke never waits for the provider, gives an accurate in-flight count.
- Con: schema migration, new table, more states. More to review, hence its own
  task.

### Recommendation

Implement D, with A's heartbeat reordering folded in and B's version compare
used for the persistence guard. Do not implement C.

## 6. Deterministic tests for revocation at the adapter boundary

No sleeps, no threads racing on wall clock. Use a test-only subclass or wrapper
of `FakeAIAdapter` (`backend/app/ai/fake_adapter.py`; it is not modified by this
proposal) with an `on_enter` hook that runs at the top of
`classify_and_epitaph` and `study_note`, before the fake returns. The hook
revokes consent through a separate `ConsentService` bound to a separate session,
exactly as the HTTP route would. Each test records adapter call count and bytes
received.

Classification:

1. `test_revoke_inside_adapter_entry_is_reported_in_flight`: hook revokes on
   entry. Assert the adapter was called once (the bytes were already handed
   over), the operation is `failed` with "AI processing consent required", no
   classification or epitaph artifact exists, and no new version rows exist.
   This pins the honest in-flight behavior (persistence guard from B).
2. `test_revoke_between_last_check_and_dispatch_blocks_adapter`: inject the
   revoke through a seam placed between authorization and `mark_sent` (for
   example a patched `OperationService.heartbeat_pending` or a hook on
   `mark_sent`). Assert adapter call count is 0 and the dispatch row is
   `aborted`. Fails today by design and is the acceptance test for D.
3. `test_revoke_before_first_check`: already blocked today; keep as a regression
   guard with call count 0.
4. `test_redo_revoke_inside_adapter_entry`: same as 1 for `run_redo`; assert
   prior versions and current-version pointers are unchanged (INV-5, INV-6).
5. `test_regrant_after_revoke_does_not_resurrect_aborted_dispatch`: revoke, then
   grant again before the worker proceeds. Assert the earlier `aborted` row is
   not reused and the worker needs a new authorization.

Study Notes:

6. `test_revoke_one_of_many_inside_adapter_entry`: three screenshots, hook
   revokes the middle one. Assert one adapter call, operation `failed`, no
   artifact, no source references, no version rows.
7. `test_revoke_one_of_many_between_check_and_dispatch_blocks_adapter`: as test
   2 across a batch; adapter call count 0; all dispatch rows `aborted`
   (all-or-nothing, matching the module contract in study_note_service.py
   11-16).
8. `test_study_redo_revoke_inside_adapter_entry`: as test 4.

Both services:

9. `test_no_database_transaction_open_during_adapter_call`: assert in the hook
   that the worker session is not in a transaction and no write lock is held
   (the revoke succeeding from the hook proves it; on SQLite a held write lock
   would raise "database is locked").
10. `test_heartbeat_not_between_final_check_and_dispatch`: spy on call order and
    assert no database write is issued between the authorization transaction and
    the adapter call other than `mark_sent`.
11. `test_consent_unavailable_fails_closed_in_both_services`: make
    `get_consent` raise `SQLAlchemyError`; assert adapter call count 0 and a
    consistent `failed` result (also covers the Classification gap noted in
    section 2).

Run the same tests on SQLite and, where CI provides it, PostgreSQL.

## 7. What revocation can and cannot guarantee

Can guarantee, if design D is implemented and tested:

- After a revoke has committed, no new dispatch is authorized for that
  screenshot.
- A dispatch authorized but not yet marked `sent` when the revoke commits is
  aborted and the adapter is not called.
- A result produced by a call that overlapped a revoke is discarded. No
  artifact, version, or source reference is persisted from it, and the operation
  is `failed` (INV-4; originals untouched, INV-1).
- The revoke response can report accurately how many calls were already in
  flight.

Cannot guarantee:

- Recall of content already sent. Once `mark_sent` has committed and the request
  is on the wire or processed, bytes cannot be unsent.
- Stopping a call already in flight. The adapter call is synchronous with
  `max_retries=0` (openai_vision_adapter.py 114-115) and cannot be cancelled
  mid-request by the app.
- Zero gap between `mark_sent` and the socket write. It can be made small and
  tested, not removed.
- Provider-side handling. The adapter sends `store=False`
  (openai_vision_adapter.py 210), but retention, abuse monitoring, and logging
  on the provider are outside this app's control and must not be described as
  deleted by revocation.
- Cross-process cancellation tokens. The token registry is process-local.
- Anything for content sent before consent was revoked in earlier operations.

User-facing copy must follow this: "Revoking stops new AI requests for this
screenshot. A request already started may complete, and its result will not be
saved." It must not say that revocation deletes data already sent or guarantees
no request was made.

## 8. Rollout

- Separate backend safety task (tasks.md 7.1), separate PR, separate review. It
  is not mixed into Draft PR #1, which stays UX and docs focused.
- Step 1 (small, can land first): reorder heartbeat before the final consent
  check and align `ConsentServiceUnavailableError` handling in
  `ClassificationService`. Tests 3, 10, 11. Does not close the race and must be
  described as narrowing.
- Step 2: migration for `ai_dispatch` and `consent_record.version`; the
  authorization transaction; `mark_sent`; revoke aborting rows; persistence
  guard. Tests 1, 2, 4 to 9.
- Step 3: optional process-local cancellation token inside the adapter.
- Step 4: update the in-product consent and revoke copy to the wording in
  section 7, and the README and `docs/prd-v2-gap-analysis.md` statements.
- Migration safety: additive table and nullable column only; back up the SQLite
  file first; no change to original bytes or existing versions.
- Reviewers should require: the new tests failing on the current code, a
  statement that the race is narrowed to the `mark_sent`-to-socket gap and not
  eliminated, and no database transaction held across the adapter call.
