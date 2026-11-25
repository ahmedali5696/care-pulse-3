# PostgreSQL CI diagnostics

## Choosing Fast Feedback or Full Foundation

**Fast Feedback** is advisory development feedback. It runs automatically on branch pushes except `development` and `main`, and can be dispatched manually with `gh workflow run fast-feedback.yml --ref <branch>` once the workflow exists on the default branch. Tags and pull request events do not trigger it; there are no path filters. Superseded pushes to the same branch cancel older Fast Feedback runs. Manual runs are grouped separately by ref and SHA; Foundation has its own concurrency namespace.

The single `Fast Feedback` job verifies the checkout SHA and runs the same pinned common bootstrap and sequential `inputs`, `python-quality`, `unit`, `imports` and `web` checks as Foundation, including zero-skip unit validation. Only dependency downloads are cached; no installed environments, build outputs or test results are reused. The workflow has read-only repository permissions and does not run Compose, publish Full receipts or upload reports. Its summary identifies the actual checkout SHA, event/ref, run and attempt. A successful Fast Feedback run provides no PostgreSQL integration, migration, cleanup or Full acceptance evidence.

Use Fast Feedback for routine feature checkpoints instead of optional manual Full runs. Foundation still runs its common checks independently and retains all existing triggers, lanes, diagnostics and B-04/T133 behavior. Use Full Foundation for integration acceptance at the exact candidate SHA; do not automatically dispatch another Full when an applicable complete run already exists. PR head, PR test-merge SHA and merged-development SHA are distinct acceptance identities even if their trees match. An existing complete run may be referenced only for its actual tested SHA while its evidence and provenance remain available; no automatic reuse, cross-SHA substitution, mixed-attempt receipts or proxy success checks.

Branch protection is a separate, deferred administrative requirement. CI success alone does not enforce fail-closed merging. Under separate authorization, require PR-based changes and up-to-date branches on `development`, with the exact `Full Foundation acceptance` check bound to the verified `github-actions` app (ID `15368`), administrator enforcement without routine bypass, and force pushes/deletion disabled. Keep Fast Feedback optional; do not require generic common, lane or diagnostic check names. Verify missing, red and stale acceptance blocks merging and current eligible success permits it before claiming enforcement. Workflow fingerprints and CI changes still require trusted review; cancellation is incomplete evidence and never proves cleanup. Manual Fast Feedback dispatch must be verified after merge if the workflow is absent from the default branch during implementation review.

## PostgreSQL diagnostics and acceptance

Dispatch **Foundation** on the candidate branch with `postgres_diagnostic` set to one of these reviewed selections. For example, run `gh workflow run foundation.yml --ref <branch> -f postgres_diagnostic=authorization-lock`.

| Selection | Test target |
| --- | --- |
| `authorization-lock` | `test_request_boundary.py::test_suspension_serializes_with_protected_mutation` |
| `booking-races` | `test_booking_races.py` |
| `followup-races` | `test_followups_races.py` |
| `f01-cleanup` | `test_f01_runtime_identity.py::test_session_cleanup_requires_committed_matching_revocation[committed]` |
| `f01-followup-lock` | `test_followup_persistence.py::test_v2_creation_exception_cannot_authorize_same_date_interval_change` |
| `f01-visit-lock` | `test_visit_persistence.py::test_invalid_admission_shell_rejected_without_a_visit[forged_actor]` |
| `f01-followup-context` | `test_followup_persistence.py::test_v2_writer_rejects_invalid_context_without_evidence[missing]` |
| `f01-pricing` | `test_pricing.py` |
| `f01-pricing-races` | `test_pricing_races.py` |
| `w3-checkin` | `test_w3_checkin.py` |
| `w3-checkin-races` | `test_w3_checkin_races.py` |
| `w3-receipt-boundary` | `test_w3_receipt_boundary.py` |
| `w3-queue-scope` | `test_w3_queue_scope.py` |
| `w3-intake` | `test_w3_intake.py` |
| `w3-intake-races` | `test_w3_intake_races.py` |
| `w3-migration` | `test_f01_grants_migration.py` |

The diagnostic check uses the same pinned PostgreSQL, migrations, disposable Compose project and synthetic secret lifecycle as Foundation. Use a separate dispatch for each independent database. Check the run's SHA and project ID, pytest counts, and volume and secret cleanup. The log reports the failing phase and test location, plus SQLSTATE or expected and observed blocker PIDs when available. It omits exception text, query text, parameters, test output, and parameter IDs.

For **Full Foundation acceptance**, dispatch with the default `full` selection and no special flags, or use the unchanged `development` push / `pull_request` triggers. The workflow validates the request before common checks, then executes the `ordinary`, `historical` (the immutable 0040 selection of 163 cases) and `compat` (44 mandatory Python and 11 Web cases) lanes independently with `fail-fast: false`. Each lane must pass all required checks with zero mandatory skips and verify its own Compose resources and synthetic secret/source cleanup before sealing a schema-2 `FOUNDATION_FULL_LANE` receipt.

The unconditional **`Full Foundation acceptance`** job succeeds only after common checks, all three lanes, same-repository/workflow/tested SHA/run ID/attempt receipts and artifact digest verification succeed. Artifacts are restricted to sanitized post-cleanup receipts named `foundation-full-<run_id>-<attempt>-<sha>-<lane>`; no private test reports or secrets are uploaded. The final job verifies exactly one evidence file for each lane. A full acceptance marker printed in isolation is not accepted evidence: inspect the completed successful workflow run and the final gate's conclusion. Failed-jobs-only reruns cannot combine receipts from different attempts; rerun **all jobs** to acquire a complete fresh set. After artifact expiration, unavailable evidence cannot be reconstructed.

On the Wave 3 branch, the ordinary lane must prove current head `0052_b11_routine_scope` and execute all three B-12 integration suites (`test_w3_receipt_boundary.py`, `test_w3_checkin.py`, `test_w3_checkin_races.py`) plus the queue scope and authenticated BFF suites (`test_w3_queue_scope.py`, `test_w3_bff.py`) and both B-11 suites (`test_w3_intake.py`, `test_w3_intake_races.py`) with separate mandatory zero-skip JUnit reports. The BFF suite runs after real web/API startup. The compatibility lane continues to prove the explicitly bounded 0040-to-0046 transition; it does not establish 0047–0052 cutover compatibility. The historical selection and protected special modes remain unchanged.

PostgreSQL diagnostic selections retain lightweight execution and report `postgres-diagnostic result (NOT Full Foundation acceptance)`; the three compatibility diagnostics run their selected legacy checks and report `compat-diagnostic result (NOT Full Foundation acceptance)`. B-04 and protected T133 keep their single-runner behavior, common checks, guards and distinct non-acceptance job names. Invalid booleans, unknown selections, conflicting modes, orphaned or wrong T133 SHA fail before integration. Each mode uses the original concurrency grouping and no diagnostics or special execution emits Full receipts or acceptance. For a PR, record the tested `github.sha` merge commit and its distinct head/base SHAs; never substitute the PR head as the tested commit. Diagnose failed PostgreSQL checks with the smallest reviewed selection before rerunning complete Full Foundation at a corrected final executable SHA.

## Concurrency test inventory

Reviewed the PostgreSQL integration suites, their shared fixtures and the native PostgreSQL adapter test for parallel calls, synchronization primitives, transaction barriers, and lock probes. Parameterizations use the pattern listed for their parent test.

| File under `tests/integration` | Race cases and synchronization |
| --- | --- |
| `test_appointment_races.py` | Three barrier-started outcome races for same patient/day, archive versus new ID, and Main/Emergency numbering. |
| `test_booking_races.py` | Barrier-started outcome races for same patient/key, signed same-key conflict, distinct keys, distinct patients and timed capacity. Events and backend lock observations for direct and signed archive/replay, archive/booking commit orders, pricing/booking and schedule/booking. |
| `test_day_review_races.py` | Four ordered lock tests for waiting transition, first QueueDay insert, and direct queue entry update/insert versus the close barrier. Events and PostgreSQL blocker/query observations. |
| `test_evidence_persistence.py` | Four barrier-started outcome races for claim/reclaim, duplicate nonce and operator receipt. The exact-instant lease expiry test uses a controlled database timestamp. |
| `test_followup_decisions.py` | Barrier-started signed same-key conflict. Receipt checkpoint and backend lock observations for both staff-revocation orders. |
| `test_followup_races.py` | Barrier-started outcome races for two claims, extension/claim and deadline boundaries. Events plus PostgreSQL blockers for revision, source, extension, current-head clinical correction vs signed extension, and terminal ordering. Explicit SQLSTATE lock-timeout witnesses for terminal-first and staged-claim contention. The attempted-claim-first case uses events to hold the attempt until terminal commit. |
| `test_followups_races.py` | Barrier-started competing claims. Events and backend lock observations for edit/confirm order, terminal and extension races, source lock order, timed capacity rescan and policy publication. |
| `test_identity_persistence.py` | Shared two-transaction barrier for first Owner, distinct recovery proof and native completion outcomes. Local barrier for activation versus last-Owner disable. |
| `test_identity_races.py` | Held Organization/session locks and PostgreSQL waiter observations for parallel proof, suspension, stale finalization, duplicate finalization, proof replacement and native reset expiry. The deadline probes wait for an actual stored expiry. |
| `test_patient_persistence.py` | Event and backend lock observation for archive versus new-ID guard or demographic edit. |
| `test_patient_races.py` | Barrier-started outcome races for competing commands and archive/patch. Held transaction and observed blocker for the new-ID guard and revoked authorization. |
| `test_policy_persistence.py` | Barrier-started same-revision outcome race. |
| `test_pricing.py`, `test_pricing_races.py` | Held transaction and observed PostgreSQL lock blockers for receipt replay, grant/revoke, identical signed retry (both superseding `f01_activate()` authority lock and schema `clinic_policy_heads` lock) and Owner/manager publication. Shared two-transaction barrier for same-revision and mixed-price constraints. |
| `test_provisioning.py` | Parallel same-key creation, barrier-started Owner activation versus operator mutation, and barrier-started competing invitations. Final outcome and durable state assertions permit either order. |
| `test_queue_races.py` | Barrier-started Waiting/Cancelled outcome race. Held transactions and explicit PostgreSQL lock-timeout witnesses for cross-lane Doctor control, QueueDay upsert, provisional admission and provisional confirmation. |
| `test_request_boundary.py` | Parallel nonce outcome race. Suspension versus an authorized operation now uses transaction checkpoints and an exact waiter-to-holder lock observation. |
| `test_schedule.py`, `test_schedule_races.py` | Held transaction and PostgreSQL lock observations or lock-timeout witnesses for manager revocation, identical stage/preview/activation receipt retries (superseding `f01_activate()` authority lock, inner `clinic_policy_heads` schedule lock, and unique receipt index collision), and both publication/booking orders. |
| `test_sessions.py` | Session INSERT lock observations for simultaneous sign-in and stale-password login. Parallel activity checks the committed session deletion. The locked-session failure case intentionally tests bounded failure. |
| `test_slot_races.py` | In-transaction barrier before the day allocator for overlapping/adjacent slots and capacity-two contention. |
| `test_staff.py` | Barrier and relation-lock observation for same-key creation. Barrier-started competing receptionist acceptances. |
| `test_t133_runtime.py` | Events and backend lock observations for Waiting cancel/admission, cancellation/completion and extension/terminal races in the restricted opt-in T133 lane. |
| `test_tenant_isolation.py`, `test_visit_migration.py` | Shared two-transaction barrier for tenant metadata; local in-transaction barriers for completion/completion and completion/clinical correction. |

Outside these files, the native PostgreSQL adapter test in `apps/web/auth/postgres-adapter.integration.test.ts` races four one-use proof consumers with `Promise.all`. `test_booking.py` and `test_health.py` use held relation locks to exercise bounded rollback or readiness failure. Startup and topology polling observes service readiness.

The [two B-09 failures](https://github.com/ahmedali5696/care-pulse-3/actions/runs/43246319720) [at different checkpoints](https://github.com/ahmedali5696/care-pulse-3/actions/runs/43248126126) had the same earlier Organization lock. The existing [verified booking correction](https://github.com/ahmedali5696/care-pulse-3/actions/runs/43256764231) gates the signed receipt after observing that lock. B-10 also had a [cross-role observer failure](https://github.com/ahmedali5696/care-pulse-3/commit/2524914e0a5b441694a01ad97a09ccd95bc2029d). The shared test helper now uses `pg_blocking_pids` directly so it can observe known backends across roles without access to activity details. The authorization test previously accepted a scheduled but unfinished worker as evidence that suspension was blocked. It now observes the expected database blocker before releasing the authorized transaction.
