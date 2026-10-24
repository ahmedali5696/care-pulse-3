# Issue #7: isolated B-04 adapter feasibility spike

Date: 2026-10-24. Scope: the Product Owner's [Issue #7 approval](https://github.com/ahmedali5696/care-pulse-3/issues/7#issuecomment-6388736299).
This is a throwaway experiment, not an approved architecture or B-04 implementation.
Issue #7 remains OPEN/BLOCKED and all existing B-04 task statuses remain unchanged.

## Evidence status

The three requested spike proofs PASSED in real disposable PostgreSQL in both runs.
The corrected run also PASSED both Foundation jobs. This verifies the tested
transaction pattern only; it does not establish B-04 acceptance or architecture approval.

- Initial executable SHA: `8f2f5620ed9189ed122995fc4ecd01494bc80dd0`.
- [Initial run 40128418500](https://github.com/ahmedali5696/care-pulse-3/actions/runs/40128418500), dispatched with `b04_spike=true`: `inputs-and-build` PASS; all spike proofs PASS; `compose-integration` FAIL after the spike, because its committed active Owner contaminated two subsequent empty-active-tenant assertions (97 passed, 2 failed). Cleanup succeeded.
- One evidence-directed CI isolation correction: run the spike after all existing integration assertions. No prototype or existing assertion changed; no schema/grant change or data deletion was used to make the tests pass.
- Corrected executable SHA: `696a5969d12b3e7f3801eb86591fe65113ac9eef`.
- [Corrected run 40129291343](https://github.com/ahmedali5696/care-pulse-3/actions/runs/40129291343): `inputs-and-build` PASS and `compose-integration` PASS at the same executable SHA. The initial 99-test PostgreSQL suite passed, the remaining existing integration/browser/sender checks completed, all spike assertions passed at the end, and disposable volumes were removed successfully.
- The subsequent report-only commit changes no executable file from that tested SHA.

Corrected run's actual connection/transaction observations (PID 139 throughout):

| Scenario | PostgreSQL transaction ID | Result |
| --- | --- | --- |
| Issuance failure after job | 1373 | PASS: exact callback error propagated, all three row counts zero |
| Issuance failure after material | 1374 | PASS: exact callback error propagated, all three row counts zero |
| Issuance success | 1377 | PASS: all three committed; ciphertext decrypts to original token |
| Reset with invalid completion | 1381 | PASS: proof, password, two sessions and fence rolled back |
| Reset success | 1382 | PASS: native new password, session/proof removal and completion committed |

Missing/foreign (unowned UUID) context and clean reuse, FORCE RLS, non-bypass runtime
and denied ciphertext SELECT/RETURNING also reported PASS in the corrected run.

## Prototype and public extension boundary

`apps/web/auth/b04-spike.test.ts` uses `better-auth`, `better-auth/adapters`
(`createAdapterFactory`, its CRUD methods, `consumeOne`, and transaction callback),
and `better-auth/crypto`. There are no internal Better Auth imports or patched packages.
See the official [custom adapter guide](https://better-auth.com/docs/guides/create-a-db-adapter)
and [native password APIs and callback](https://better-auth.com/docs/authentication/email-password).
The installed dependency and lockfile stay at Better Auth 1.7.12.

An application-owned `AsyncLocalStorage` binds every prototype adapter and domain query
to one leased `pg` connection. The outer owner starts/ends the real transaction; the
public adapter transaction callback joins it when native reset nests its transaction.
No Better Auth async context or internal transaction helper is imported.
This prototype has no production callers and starts no HTTP listener.

Important qualification: 1.7.12's `runInBackgroundOrAwait` catches errors from
`sendResetPassword`, even without a background task handler. The prototype callback
therefore records its original error in the application's transaction context and
rethrows it; the outer owner checks that error before COMMIT. A successful native
response alone is insufficient evidence of successful issuance. No background handler,
SMTP, secondary storage or post-commit database hook is configured in this experiment.

## Requested proof assertions

| Guarantee | Real PostgreSQL observations required by the harness |
| --- | --- |
| Atomic reset rollback | Native API returns success inside the still-open outer transaction; that connection sees the proof consumed, two sessions deleted and the new password hash. An independent observer still sees the original committed hash. A deliberately invalid completion insert fails SQLSTATE `23514`; an independent observer then sees the original hash, both sessions, unconsumed proof, `RESERVED`/fence 0 and no completion. |
| Atomic reset commit control | Retrying without the injection commits the native new password, rejects the old password, removes both sessions and the proof, and leaves one completion. |
| Atomic issuance | Failure after the job insert and after the encrypted-material insert rethrows the exact injected callback error from the owner and leaves zero verification/job/material rows. The success control commits all three and decrypts to the exact original native token using a separate test-only observer. |
| One connection/transaction | Every actual adapter/domain query samples `pg_backend_pid()`, `pg_current_xact_id()` and tenant GUC from its executing connection. Required writes, including nested native reset effects, must have one backend PID and one transaction ID. |
| Tenant context and reuse | Missing and foreign contexts fail delivery insertion with SQLSTATE `42501` and leave no partial native proof. After commit, callback rollback, RLS error and completion SQL error, the single-connection pool reuses the same PID with empty tenant context and no visibility of the fixture's staff row. |
| Existing least privilege | Runtime is neither superuser nor BYPASSRLS; all five inspected identity/recovery/delivery tables retain ENABLE/FORCE RLS. Runtime material insertion has no SELECT or RETURNING. Separate ciphertext SELECT and INSERT RETURNING attempts fail `42501`. Only the existing migration-role test observer reads ciphertext for verification. |

## Reproduction

```text
gh workflow run foundation.yml --ref codex/b04-adapter-feasibility-spike -f b04_spike=true
gh run view 40129291343 --log
```

The existing Foundation workflow creates a uniquely named disposable Compose project,
boots pinned PostgreSQL 17.11, applies unchanged roles and all migrations, and cleans
up its volumes. `tests/integration/b04_spike_seed.py` explicitly exports the existing
`test_sessions._seed_owner` synthetic fixture into the one-shot Node process over stdin.
It is not collected by normal pytest filename discovery. The migration credential
is mounted only into that disposable Node test invocation; ordinary web services
receive no additional credentials. The spike is opt-in and fail-fast.

Local focused TypeScript, ESLint, Ruff format/lint and scoped diff checks passed.
The initial whole-project local typecheck encountered stale `.next` references to
an absent route; no unrelated file was changed to address that cache. Clean CI's
whole-project typecheck passed. Docker was not installed or required locally.
Test Guard self-review was applied; this is not independent B-04 acceptance review.

## Limits and estimated full-adapter impact

The prototype intentionally supports only the native CRUD/predicates needed by these
calls. Domain SQL binds one trusted synthetic fixture. It is not a general adapter,
tenant resolver, password policy, fingerprint implementation or recovery coordinator.
Recovery/delivery records use SQL on the owned connection; general public mapped-model
CRUD for those tables is not implemented or proven by this experiment.
It does not establish multi-process competitor/replay behavior, strict expiry,
in-flight old-password login fencing, revocation/finalization races, crash recovery,
post-commit delivery, callback background execution, hook ordering or production readiness.
No schema, grants, dependencies, authorities, production routes or B-04 task statuses
were changed. The pre-existing uncommitted `quickstart.md` remains outside the commits;
its preserved SHA-256 is `56700CC56A09E9BE1FA8348400F3F4788F306B265977A904459A20FB957E8CC1`.

Preliminary engineering estimate: **1-2 engineer-weeks for a production adapter and
its parity/upgrade tests**, excluding the B-04 coordinator, independent security review
and full acceptance matrix. Complexity and continuing maintenance are moderate to high:
complete eight CRUD methods, atomic consume, all supported predicates/null behavior,
mapping/selection/pagination and accurate affected-row counts; enforce owned connection
and callback failure propagation; preserve existing login/session gates and bounded
pool behavior. `auth.ts`, `db.ts` and session-authority integration would require review:
existing `inTenant` leases a separate connection and cannot simply be assumed to join
this owner. Better Auth upgrades need real-PostgreSQL regression checks for these
boundaries and database-hook commit ordering. This estimate is not delivery authorization.

## Relevant open Issues

- [#7](https://github.com/ahmedali5696/care-pulse-3/issues/7): immediate B-04 implementation gate. Product Owner and Security/Backend must review the observed evidence, limitations and adapter cost before a separate architecture decision. Full existing closure criteria remain unsatisfied.
- [#2](https://github.com/ahmedali5696/care-pulse-3/issues/2), [#4](https://github.com/ahmedali5696/care-pulse-3/issues/4), [#5](https://github.com/ahmedali5696/care-pulse-3/issues/5): existing release/production obligations for login-source throttling, dependency advisories, and credentials/keys/SMTP/retry policy. These are outside this synthetic spike and are not resolved by its tests.

Stop after the spike. No B-04 implementation, architecture approval or Issue closure is implied.
