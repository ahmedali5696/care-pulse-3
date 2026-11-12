# PostgreSQL CI diagnostics

Dispatch **Foundation** on the candidate branch with `postgres_diagnostic` set to one of these reviewed selections. For example, run `gh workflow run foundation.yml --ref <branch> -f postgres_diagnostic=authorization-lock`.

| Selection | Test target |
| --- | --- |
| `authorization-lock` | `test_request_boundary.py::test_suspension_serializes_with_protected_mutation` |
| `booking-races` | `test_booking_races.py` |
| `followup-races` | `test_followups_races.py` |

The diagnostic check uses the same pinned PostgreSQL, migrations, disposable Compose project and synthetic secret lifecycle as Foundation. Use a separate dispatch for each independent database. Check the run's SHA and project ID, pytest counts, and volume and secret cleanup. The log reports the failing phase and test location, plus SQLSTATE or expected and observed blocker PIDs when available. It omits exception text, query text, parameters, test output, and parameter IDs.

For acceptance, dispatch with the default `full` selection and verify both `inputs-and-build` and `compose-integration` at the executable SHA. Focused runs report `postgres-diagnostic` and use a separate cancellation group. Push and pull request checks use full Foundation. The B-04 and T133 modes remain separate.

## Concurrency test inventory

Reviewed the PostgreSQL integration suites, their shared fixtures and the native PostgreSQL adapter test for parallel calls, synchronization primitives, transaction barriers, and lock probes. Parameterizations use the pattern listed for their parent test.

| File under `tests/integration` | Race cases and synchronization |
| --- | --- |
| `test_appointment_races.py` | Three barrier-started outcome races for same patient/day, archive versus new ID, and Main/Emergency numbering. |
| `test_booking_races.py` | Barrier-started outcome races for same patient/key, signed same-key conflict, distinct keys, distinct patients and timed capacity. Events and backend lock observations for direct and signed archive/replay, archive/booking commit orders, pricing/booking and schedule/booking. |
| `test_day_review_races.py` | Four ordered lock tests for waiting transition, first QueueDay insert, and direct queue entry update/insert versus the close barrier. Events and PostgreSQL blocker/query observations. |
| `test_evidence_persistence.py` | Four barrier-started outcome races for claim/reclaim, duplicate nonce and operator receipt. The exact-instant lease expiry test uses a controlled database timestamp. |
| `test_followup_decisions.py` | Barrier-started signed same-key conflict. Receipt checkpoint and backend lock observations for both staff-revocation orders. |
| `test_followup_races.py` | Barrier-started outcome races for two claims, extension/claim and deadline boundaries. Events plus PostgreSQL blockers for revision, source, extension and terminal ordering. Explicit SQLSTATE lock-timeout witnesses for terminal-first and staged-claim contention. The attempted-claim-first case uses events to hold the attempt until terminal commit. |
| `test_followups_races.py` | Barrier-started competing claims. Events and backend lock observations for edit/confirm order, terminal and extension races, source lock order, timed capacity rescan and policy publication. |
| `test_identity_persistence.py` | Shared two-transaction barrier for first Owner, distinct recovery proof and native completion outcomes. Local barrier for activation versus last-Owner disable. |
| `test_identity_races.py` | Held Organization/session locks and PostgreSQL waiter observations for parallel proof, suspension, stale finalization, duplicate finalization, proof replacement and native reset expiry. The deadline probes wait for an actual stored expiry. |
| `test_patient_persistence.py` | Event and backend lock observation for archive versus new-ID guard or demographic edit. |
| `test_patient_races.py` | Barrier-started outcome races for competing commands and archive/patch. Held transaction and observed blocker for the new-ID guard and revoked authorization. |
| `test_policy_persistence.py` | Barrier-started same-revision outcome race. |
| `test_pricing.py`, `test_pricing_races.py` | Held transaction and observed blocker for receipt replay, grant/revoke, identical signed retry and Owner/manager publication. Shared two-transaction barrier for same-revision and mixed-price constraints. |
| `test_provisioning.py` | Parallel same-key creation, barrier-started Owner activation versus operator mutation, and barrier-started competing invitations. Final outcome and durable state assertions permit either order. |
| `test_queue_races.py` | Barrier-started Waiting/Cancelled outcome race. Held transactions and explicit PostgreSQL lock-timeout witnesses for cross-lane Doctor control, QueueDay upsert, provisional admission and provisional confirmation. |
| `test_request_boundary.py` | Parallel nonce outcome race. Suspension versus an authorized operation now uses transaction checkpoints and an exact waiter-to-holder lock observation. |
| `test_schedule.py`, `test_schedule_races.py` | Held transaction and PostgreSQL lock observations or lock-timeout witnesses for manager revocation and both publication/booking orders. Barrier-started signed stage/preview receipt race. |
| `test_sessions.py` | Session INSERT lock observations for simultaneous sign-in and stale-password login. Parallel activity checks the committed session deletion. The locked-session failure case intentionally tests bounded failure. |
| `test_slot_races.py` | In-transaction barrier before the day allocator for overlapping/adjacent slots and capacity-two contention. |
| `test_staff.py` | Barrier and relation-lock observation for same-key creation. Barrier-started competing receptionist acceptances. |
| `test_t133_runtime.py` | Events and backend lock observations for Waiting cancel/admission, cancellation/completion and extension/terminal races in the restricted opt-in T133 lane. |
| `test_tenant_isolation.py`, `test_visit_migration.py` | Shared two-transaction barrier for tenant metadata; local in-transaction barriers for completion/completion and completion/clinical correction. |

Outside these files, the native PostgreSQL adapter test in `apps/web/auth/postgres-adapter.integration.test.ts` races four one-use proof consumers with `Promise.all`. `test_booking.py` and `test_health.py` use held relation locks to exercise bounded rollback or readiness failure. Startup and topology polling observes service readiness.

The [two B-09 failures](https://github.com/ahmedali5696/care-pulse-3/actions/runs/43246319720) [at different checkpoints](https://github.com/ahmedali5696/care-pulse-3/actions/runs/43248126126) had the same earlier Organization lock. The existing [verified booking correction](https://github.com/ahmedali5696/care-pulse-3/actions/runs/43256764231) gates the signed receipt after observing that lock. B-10 also had a [cross-role observer failure](https://github.com/ahmedali5696/care-pulse-3/commit/2524914e0a5b441694a01ad97a09ccd95bc2029d). The shared test helper now uses `pg_blocking_pids` directly so it can observe known backends across roles without access to activity details. The authorization test previously accepted a scheduled but unfinished worker as evidence that suspension was blocked. It now observes the expected database blocker before releasing the authorized transaction.
