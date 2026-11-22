# Wave 3 Validation Guide

Phase 1 guide, 2026-11-22. These are future executable scenarios, not completed feature tests. Use [HTTP](contracts/http.md), [transactions](contracts/transactions.md), [model](data-model.md) and [prerequisite register](research.md#upstream-prerequisite-register). Stop this invocation after planning; no tasks, implementation, migrations, CI dispatch, Issue disposition or release is authorized here.

## Prerequisites and evidence boundaries

- Separate authorization for the exact Master being implemented; retain master §8 dependency sets. Independently verify its exact consumed upstream November contract at an executable revision. Missing evidence means report that affected Master **BLOCKED for implementation**, naming the prerequisite and upstream owner. New Wave 3 command absence is its owner's implementation work, not a transferred upstream defect.
- Before enabling an operation, require its own F-01 real-login/signed positive/negative/critical concurrency evidence at current executable SHA. Preserve source baseline `79d6a2775d8b20a27b8df3e6ca4a64415c8eb8ec`, actual migration predecessor and changed source hashes in the later evidence record.
- Disposable synthetic clinics/Patients and actual Doctor, Manager and ordinary Receptionist sessions plus API/Web/worker/gate credentials and separately protected signer/key/nonce authority. Include a second synthetic tenant, revoked sessions/grants and wrong-scope/resource cases. Never use production data or log secrets/clinical text.
- Existing pinned toolchain/lockfiles and isolated PostgreSQL/Compose in approved Docker-capable CI. Docker is unavailable locally and must not be installed. Configure explicit labeled test-only security freshness/TTL and confirmation TTL; no production default. Missing TTL must separately prove confirmation issue/use denial.
- Existing migration/maintenance fixtures may seed/inspect state. They do not substitute for actual runtime command success. In particular `appointment_storage` uses migration authority despite a local variable named runtime, while F-01 queue-denial tests intentionally reject unrelated booking scope. Preserve predecessor-only 0040 tests and historical checked evidence; do not relabel them current-head coverage.

## Container-free checks

From repository root in PowerShell, resolve this feature without persisting selection:

```powershell
$env:SPECIFY_FEATURE_NO_PERSIST = '1'
$env:SPECIFY_FEATURE_DIRECTORY = 'D:\care-pulse-3\specs\005-wave-3-queue-clinical'
$env:SPECIFY_FEATURE = 'feature/wave-3-queue-and-clinical-operations'
& 'D:\care-pulse-3\.specify\scripts\powershell\check-prerequisites.ps1' -Json -RequireSpec
git diff --check
```

Expected: selected feature path plus research/model/contracts/quickstart, no missing required plan/spec and no whitespace errors. Also inspect untracked artifact whitespace/local links and compare all ten dependency sets with spec/master; git diff alone does not inspect untracked content. These are document checks, not executable acceptance.

After separately authorized implementation, use the existing supported checks as applicable:

```powershell
$env:PATH = 'D:\care-pulse-3\.venv-wave0\Scripts;' + $env:PATH
& 'D:\care-pulse-3\.venv-wave0\Scripts\python.exe' --version
& 'D:\care-pulse-3\ops\check.ps1' python-quality
& 'D:\care-pulse-3\ops\check.ps1' imports
& 'D:\care-pulse-3\ops\check.ps1' unit
pnpm typecheck
pnpm lint
```

Expected pinned Python 3.13.16 and passing applicable static/unit/BFF checks. These commands need the existing pinned toolchain available on PATH as required by the runner. Code-first production implementation precedes focused tests; no test-first cycle is imposed. Collection/static checks do not establish PostgreSQL behavior. Existing queue/Visit/day-review files are substrate evidence until owning runtime route tests are added and explicitly selected at current head; no nonexistent test name is presented as runnable now.

## Runnable end-to-end acceptance scenarios

Use authenticated BFF `/api` operations from the HTTP contract against the CI disposable application. Observe committed state/evidence separately with the maintenance verifier after each request; never authorize product calls with that verifier. Each scenario must be covered by an owning focused integration test and the full current-head selection added during implementation.

| Scenario / owner | Execution | Required observation |
| --- | --- | --- |
| Check-in and ordering / B-12 | Book through existing B-09; check in twice, including a later-archived prebooking; select dated MAIN and EMERGENCY views; vary booked-order/FIFO and late arrivals | One Waiting entry/alert, no Visit, server arrival; Urgent ahead of Routine and independent Emergency FIFO; number differs from position; old day retained |
| Immediate intake / B-11 | Routine with toggle OFF/ON; Doctor and both Receptionist variants Emergency; Doctor denied Urgent; valid Receptionist Urgent then break/holiday/missing-hours/premium failures | Distinct predicates; standard Emergency price/permitted free, strict Urgent premium; atomic snapshot/number/Waiting/entry, no fused walk-in bypass or failed-command effects |
| Controls and confirmation / B-14 | Owner/Manager/ordinary settings attempts; Doctor Pause/resume; issue exact-case confirmation at configured TTL, consume at boundary, revoke/race/mismatch attempts | Manager mode only, Owner capacity, Doctor-only Pause/issue/revoke; no eviction; expiry at or after cutoff denies; missing/invalid TTL denies issue/use; no reuse |
| Admission / B-15 | Prepare E1/E2 and MAIN U1/R1; each mode-authorized actor selects either head or explicitly reasoned non-head; exercise old-day presence and cross-lane/date capacity competition | Exactly one Visit/status/queue close/audit/event per success; no global lane priority, only ordinary MAIN Routine cursor progression; each extra active case needs its own Doctor confirmation |
| Placement/policy / B-13 | Receptionist reorders Routine with reason/revision; Owner/Manager changes future policy; compete placement with old-head admission | Only Routine positions change, no cursor/Urgent/Emergency effect; stale ordinary admission denies; current snapshot unchanged, next operating day uses new policy |
| Visit/completion / B-16 | Doctor clinical PATCH/correction versus source-family PATCH; all staff complete with true assertion; false/missing assertion and Receptionist clinical/nested-read attacks; Doctor exceptional cancel | Closed family dispatch, no mixed source/clinical grant; one actual-actor attestation and claim consumption; retained Visit; reasoned Doctor cancel releases claim and records one review event; no fabricated Doctor decision/text |
| Cancel/replacement / B-17 | Cancel Booked/Waiting as each actor; valid typed-confirmed Routine→new Urgent; inject failure after old mutation/new allocation and race admission/reschedule | Frozen old history; Doctor-origin event exactly once, Receptionist not impersonated; linked new ID/current price/number/Waiting or all old committed state preserved |
| Reschedule / B-18 | Attempt already-Waiting Routine same-day date/interval change in Open and Exact-Time; use B-13 ManualRoutinePlacement to reorder; test cross-day Routine Waiting, eligible same-day/cross-day Booked, archived same-ID, valid/invalid same-day Urgent and cross-day Urgent; confirmed Follow-up outside frozen deadline with/without distinct extension | Same-day Routine Waiting rejects without success state/queue/history/event/receipt effects; B-13 reorders with audit. Lawful cross-day Waiting preserves ID/frozen price, closes old queue, assigns new-day number/Booked/fresh attendance; eligible Booked and same-day Urgent retained; no cross-day Urgent; reasoned extension/Doctor precedence/revisions enforced; one Doctor individual-move review event |
| Bulk / B-19 | Each staff actor previews same-original-day Routine selection, commits exact preview + RESCHEDULE to another valid day; attempt same-day Waiting target, change selection, admit one row, introduce one destination/claim conflict, try foreign/revoked preview access; replay Doctor-origin success | No Manager/bulk flag; same-day Waiting Routine explicitly ineligible/excluded; exact actor-bound preview UUID recoverable; original preview only readable by narrow matching commit; all moves/evidence or none; one Doctor-origin Receptionist-review event per moved Appointment, zero duplicate on replay/rollback |
| Reconciliation / B-20 | Cross Cairo midnight with previous-day Booked/Waiting and same-day elapsed Exact-Time Booked; manual absence without/with enabled cutoff and competing check-in; selected-day cutoff at ambiguous fall-back time (before first/after first/before second/at second) and nonexistent spring-forward time (before/at first valid instant); wrong selected date, disabled/null cutoff; two concurrent invocations, lost response and partial-processing failure then authorized retry; staff initial review/close, Owner amend/reopen | `overdue` iff service_date < today's IANA Cairo date; same-day Exact-Time slot expiry is not overdue/manual No-show. Automatic cutoff uses fold=1 or first valid gap instant, never a fixed offset; before cutoff/no enabled policy/wrong day yields no success. Each eligible Booked→No-show/claim release/audit/outbox/receipt happens once under check-in race; repeated/concurrent calls do not duplicate any effects, and a partial run resumes remaining Booked safely. Existing-review changes Owner-only; zero-Waiting close, Booked unchanged by close and active Visit retains original date |
| Receipt target binding / each owning mutation | Reuse same actor/operation/key and identical body/revisions against two visible targets/dates/preview IDs; repeat with foreign/hidden target and revoked access | Different authorized command target conflicts, not replay of first result; no hidden-resource oracle; original same command returns safe target-bound result without duplicate effects |
| Cross-cutting / owning Master | Same key/same and changed body, lost response after commit, rolled-back SQL error, wrong tenant/resource/operation/login, revocation-first and operation-first, mixed/oversized input | Fresh-authorized original result/no duplicate effects; unknown reconciled using original key; safe error/pool cleanup; zero forbidden field disclosure and successful domain/audit/outbox/receipt effects on denial/rollback |

For every preferential path, include genuine Doctor OPTED_OUT/DEADLINE versus later Receptionist decisions/extensions/moves; non-DEFAULT reason/valid date, source/clinical revision split, release/rebook under current policy/price and no inherited extension. Use B-10 as upstream authorization, not a duplicated Wave 3 policy. For new migrations exercise populated immediate-predecessor preservation, failed-migration atomicity, actual effective grants/RLS/functions and unchanged authentic historical receipt replay.

Races need transaction checkpoints and PostgreSQL waiter/holder observations before releasing the blocker, plus final committed-state/effect counts. Cover both relevant authority/terminal start orders. No arbitrary sleep, retry, skipped assertion or timeout increase replaces evidence. Inspect actual current-head selection/deselection; green aggregate counts are insufficient.

## CI acceptance (later authorization only)

Wire owning focused tests into the current-head runner, preserving mandatory selections, predecessor isolation and protected T133 controls. For a failure, record test/step/run/exact SHA, classify application/test/environment and establish cause before correction. Run the smallest relevant listed `postgres_diagnostic` selection from [PostgreSQL CI diagnostics](../../docs/postgresql-ci.md) / Foundation first: examples include `authorization-lock`, `booking-races`, `followup-races`, `f01-visit-lock` and `f01-followup-context`. Existing selections are not a dedicated Wave 3 end-to-end suite; add a necessary scoped selection under its owning implementation before relying on it. After two speculative corrections, require new root-cause evidence.

Under separate implementation/CI authorization, after the final executable commit is published on its authorized branch:

```powershell
$candidateBranch = git branch --show-current
$candidateSha = git rev-parse HEAD
gh workflow run foundation.yml --ref $candidateBranch -f postgres_diagnostic=full
gh run list --workflow foundation.yml --branch $candidateBranch --commit $candidateSha --event workflow_dispatch --limit 5 --json databaseId,url,headSha,status,conclusion
gh run view <run-id> --json headSha,status,conclusion,jobs,url
```

Replace `<run-id>` with the observed matching ID and inspect its jobs/logs and selected cases. Require headSha equal to candidateSha, both `inputs-and-build` and `compose-integration` PASS, correct migration head/actual logins, all required current-head scenarios/counts with zero mandatory skips, preserved predecessor isolation, and disposable volume/synthetic-secret cleanup. Diagnostic-only, stale, pre-existing, dispatched, superseded or locally unavailable results are not acceptance. These commands do not enable protected T133; no CI is dispatched during documentation finalization.

Record URL/SHA, selected cases, failures and each owner as IMPLEMENTED, TESTED, INTEGRATION VERIFIED or BLOCKED with evidence. Independent Security/Clinical/PO review remains separate; synthetic Foundation does not establish production readiness.

## Planning-run verification record

This invocation runs only native plan setup/prerequisite resolution, document scope/dependency/link/placeholder/whitespace checks and read-only source/Issue inspection. The completion report records their observed results. It does not execute the application scenarios or dispatch CI.

Observed document checks: native setup and `check-prerequisites.ps1 -Json -RequireSpec` succeeded for this feature; six planning artifacts and their local links validated; all ten dependency sets matched both spec and master; no template placeholders or whitespace errors. Spec/checklist, authorities and tracked source remained unchanged. Parsed before/after plan hooks were empty; no tasks.md was generated. The consistency review's receipt-target and DayReview authority corrections are incorporated in the contracts and scenarios above.

Open #24/#25 retain amendment/upstream/downstream proof; #26 independent Security/Clinical and PO risk disposition; #28 existing compatibility disposition, with later synthetic PASS recorded by master §12. Missing exact consumed evidence blocks only affected implementation; new-operation proof gates enablement. Production #2/#4/#5/#13/#18, confirmation TTL approval and canonical OPS/file/numerical gates retain owners and do not block this planning. No Issue was changed or closed.
