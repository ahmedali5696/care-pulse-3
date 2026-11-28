# Quickstart: Wave 4 Validation

**Status**: Phase 1 validation guide, 2026-11-28. The historical planning invocation ran document checks only; the current remediation is likewise document-only and grants no implementation, CI, migration execution, publication, Issue changes or automatic Spec Kit progression. Runtime scenarios below are future acceptance obligations, not executed results. [Plan](plan.md), [data model](data-model.md), [HTTP](contracts/http.md), [transactions/evidence map](contracts/transactions.md).

## Prerequisites and Scope

Before implementing a Master, obtain its explicit assignment and verify its exact prerequisites from [plan.md](plan.md#master-ownership-and-dependencies), consumed owner contracts/evidence, actual candidate SHA/head and dirty tree. Ready dependencies alone grant no execution authority. Preserve independent #24/#25/#26/#28 dispositions and D-12's Issue #13 populated-upgrade review. Use existing approved synthetic timing variance with visibly labeled file/security/mail test policy; no real patient data or production defaults.

Use repository-pinned Python 3.13.16, pip 26.2.2, Node 24.22.1 and pnpm 10.34.7 with committed locks. ReportLab/Arabic/font and pywebpush dependency additions must first receive compatible exact pins, reproducible lock/build checks and focused validation during authorized implementation. Do not silently upgrade approved Next.js 15.5.28.

Docker is unavailable on the PO workstation and must not be installed. Container-dependent PostgreSQL, scanner, private storage, signed BFF, worker/mail/push and races require authorized Docker-capable CI. Use actual invited/login identities and real service DB logins; no test trust bypass, owner impersonation, fake RLS/concurrency or live/shared probing.

## Document Checks Now

From PowerShell, use the existing active feature without modifying its persisted selection:

```powershell
$repo = 'D:\care-pulse-3'
Set-Location -LiteralPath $repo
$env:SPECIFY_FEATURE_DIRECTORY = Join-Path $repo 'specs/006-wave-4-supporting-services'
$env:SPECIFY_FEATURE_NO_PERSIST = '1'
& (Join-Path $repo '.specify/scripts/powershell/check-prerequisites.ps1') -Json -RequireSpec
git diff --check
git status --short
```

Expected: native prerequisites discover spec, plan, research, data-model, contracts and quickstart; no tasks are required/generated. Validate all local Markdown links/anchors, table structure, required plan sections, nine exact dependency sets, complete INV/CON allocations and absence of unresolved template markers. Compare original spec/checklist/feature-selection hashes; all remain unchanged. Since the feature directory is untracked, tracked `git diff --check` alone does not inspect its files: explicitly check the six artifacts' UTF-8, whitespace and content as well.

### Planning Validation Record — 2026-11-28

Native `check-prerequisites.ps1 -Json -RequireSpec` passed and discovered all Phase 0–1 outputs. Scoped document validation passed for six artifacts, 34 local links/anchors, required plan sections, table/UTF-8/whitespace/template checks, nine exact Master dependency sets and allocation of all 38 INV/45 CON identifiers. Hash comparisons confirmed the original spec, specification checklist and `.specify/feature.json` unchanged; tracked `git diff --check` passed and no tracked change was introduced. Parsed pre/post planning hook lists were empty. File/export and delivery/worker research reviews were reconciled, including explicit signed private-query cursor transport. These results establish documentary consistency only; no application/Compose/CI check, implementation acceptance, independent Issue disposition or production approval is supplied.

## Container-free Implementation Checkpoints — Future Authorized Work

### Review Correction Validation Obligations

**Document correction record — 2026-11-28:** Source-only reconstruction verified the effective bind replacement and all later bind rewrites across the 32 migrations `0041–0072`; registration wrapper/body-limit tracing retained API/PostgreSQL exact-byte verification and existing JSON bounds. Native prerequisites and scoped document checks passed: six artifacts, 41 local links/anchors, nine unchanged dependency sets, all 38 INV/45 CON allocations, table/encoding/whitespace/template checks. Hash checks preserve the approved spec, existing specification checklist and feature selection. The earlier planning record above is historical; these correction checks do not constitute runtime or independent security acceptance. Only the six affected planning artifacts changed; no implementation, task generation, CI, commit or Issue mutation occurred. Existing review/adoption Issues #13/#24/#25/#26/#28 and production Issues #2/#4/#5/#18 retain their recorded gates; no new planning blocker is identified.

**Finding 1 — authorization lifecycle (future runtime proof):** Exercise reserve/extract/publish and preflight/redeem with different request IDs/nonces/PID/xids; reject same-request PRIMARY reuse, arbitrary stages, wrong/expired proof and unsupported RECEIPT_RECOVERY. Expire proof during lock wait and between scan/render/decrypt and publication/redemption; require newly authenticated proof, preserve grant expiry, and deny revoked/reset/suspended access. Cover wrong phase/actor/session/target/generation, stale candidate/cleanup, unknown/in-progress/aborted/committed DB outcomes, crash recovery and consumed-grant lost acknowledgment. Only document/source checks run for this correction; these tests remain future implementation evidence.

**Finding 2 — binary limits (future runtime proof):** Check exact 1,048,576-byte chunk acceptance and one-byte-over denial at API and actual PostgreSQL registration, including absent/false Content-Length. With labeled synthetic file policy permitting a larger file, prove multi-chunk transfer/assembly; with policy denying it, prove cumulative denial despite valid chunks. Cover truncation, extra/gapped/overlapping/reordered/conflicting chunks, digest/target/generation mismatch, duplicate acknowledgment, expiry/recovery and quota races. Tampering must fail independent API/DB raw-byte verification; final assembly mismatch must fail before scan/readability. Existing JSON/envelope bounds must remain unchanged. No production limit is inferred from these transport-boundary scenarios. Prove independently signed raw stage, metadata publication and read-only status with exact tenant-qualified attempt identity/target/phase/generation/offset/digest; after BFF interruption resubmit the complete file and verify file identity, every committed prefix chunk, current generation/revision/offset and unknown-commit reconciliation before resuming, with stale selectors/conflicting file identity denied and no duplicate progress; missing/invalid/mismatched server-only CP_POLICY_FILE must deny before spool or stream resources are consumed.

**Pre-tasks checklist correction — future acceptance scenarios (not executed):**

| Finding | Required proof |
| --- | --- |
| CHK005 | Exhaust configured publication attempts with definitive failures; terminal outcome, no auto-retry or fabricated notification, safe diagnostics via existing protected operational access and sanitized telemetry only; deny ordinary clinic and unrelated runtime access, source unchanged. Lose commit response after successful publication; reconcile and retain exactly one notification. |
| CHK007/017 | Exercise each committed attendance, admission and Doctor-origin review alert and other status/Pause updates against the HTTP routing table; rollback/replay/snapshot cannot create new alerts. Doctor push opt-in without separate name consent is generic, explicit consent allows name only, name withdrawal before send restores generic payload without granting another device consent. Prove both consent/handoff orders, durable revocation and honest unknown transport without recall; sound requires role authorization and user interaction. |
| CHK008/009 | Independently omit/invalidate required file, quota, key/scanner, grant, retry/lease/disposal, SSE and push policies; affected operation/readiness fails closed. Prove expired lease versus active owner, failed/unreferenced generation versus uncertain commit, both cleanup/finalize orders and stale cleaner versus replacement; no CLEAN or READY referenced object deletion; B-22 proves failed candidate cleanup and both READY/cleanup orders. |
| CHK019 | PO-approved Saturday-through-Friday weeks use Cairo-local `[Saturday, next Saturday)`. Future proof: include all seven service dates (Saturday and Friday included, next Saturday excluded), event instants on both sides of the local boundary, adjacent weeks without gaps or double counting, and a week crossing month/year boundaries and a Cairo DST transition. Compare service dates directly and convert actual event instants to Africa/Cairo dates; verify that DST does not force a 168-hour week or UTC-date attribution. Retain monthly first-day/next-month boundaries, year rollover, leap day and DST cases. |
| CHK024 | Valid creation verification alone does not authorize disclosure. Missing/failed/mismatched handoff confirmation denies grant and redemption; positive same-recipient handoff evidence binds actual verifier/time to each grant; verify live revocation at redemption and independent record/manifest grants. Audit server disclosure without inferring external delivery. |
| CHK035 | Each active Doctor/Owner, Manager and ordinary Receptionist reads same own-tenant completed-value aggregate/trend; deny operator, foreign and revoked reads; ordinary Receptionist pricing/settings mutation remains denied. |
| CHK036 | Absent/invalid/mismatched Web/API CP_POLICY_FILE poll/batch/recheck policy denies SSE; configured maximum interval applies even when idle, and read/pre-emission checks still run. Revoke or fail recheck before emission; close and discard buffered protected frames. Immediate push pre-send recheck remains independent. |
| CHK038 | Observe exact PostgreSQL blocker chains for both orders of file/export publication/grant redemption and recipient notification publication versus disable/grant revoke/tenant suspension. Authority precedes resource and claim/stream locks; prior revocation yields zero protected success, prior committed effect retains evidence but later access reauthorizes. |

No new production values, task IDs or Master dependencies are introduced. CHK019's product decision is recorded as explicitly PO-approved on 2026-11-28; the final independent reviewer PASS for all CHK001–CHK040 (including CHK019) is recorded in `checklists/review.md` on 2026-11-28 as reported by the PO. All 40 reviewer-owned markers are checked to record that verdict; no runtime evidence or task-generation authority is established here.

After production code and focused tests exist, use the supported commands with the pinned local toolchain. These were not run for the document-only change:

```powershell
& (Join-Path $repo 'ops/check.ps1') inputs
& (Join-Path $repo 'ops/check.ps1') python-quality
& (Join-Path $repo 'ops/check.ps1') unit
& (Join-Path $repo 'ops/check.ps1') imports
& (Join-Path $repo 'ops/check.ps1') web
```

Use Fast Feedback for routine development checkpoints under [CI workflow selection](../../docs/postgresql-ci.md#choosing-fast-feedback-or-full-foundation). No local check or Fast Feedback pass supplies PostgreSQL/migration/Compose/Full acceptance. Load clean-code-guard/test-guard before implementation and perform required self-review. Retain current fixtures, assertions and case identities; add only focused proof that establishes missing behavior.

## Runtime Scenario Matrix — Future Authorized Work

Implement/wire the exact owner selectors into the existing controller/reviewed inventory before relying on these scenarios. The supported CI controller command `pwsh -File ./ops/check.ps1 integration` runs within Foundation's prepared isolated environment; it is not a supported workaround for missing local Docker. Do not publish speculative pytest paths or claim an unwritten test ran.

| Owner | Setup and action | Required observation |
| --- | --- | --- |
| D-10 | Two synthetic clinics, two patients each; initialize preliminary null-Visit and clinical/prescription same-patient Visit attachments; try foreign/wrong-patient associations and direct actual-role SQL. | Allowed class association persists; invalid composite FK/class/context/role denies with no readable object; effective grants/RLS verified. |
| B-21 | Through real BFF initiate/upload/finalize known-safe bytes under labeled policy; repeat identical command; submit boundary-size/count/quota, spoofed MIME, EICAR, scanner-off, key/tag/finalize failures. | Only validated clean encrypted committed object is readable; all failures remain private/non-readable; no duplicate logical finalize or plaintext finalized object. |
| B-21 | Race finalization and cleanup with held transaction checkpoints/observed PostgreSQL blockers; race two grant redemptions, then revoke before redemption; test traversal/direct storage/range/replay. | Cleaner never deletes committed CLEAN object; stale generation denied; at most one authorized redemption and zero bytes after ordered prior revocation; interrupted transfer consumes grant. |
| B-22 | Doctor, Manager and ordinary Receptionist each create PDF/CSV for an active and archived patient with full Doctor clinical history, purpose and verified recipient handoff. Download record and manifest independently. | Full history readable by exporter; no raw originals embedded; independent one-use artifacts, correct immutable minimal audit and no added ordinary narrative/edit/original-file access. |
| B-22 | Inspect mixed Arabic/English/digits and long paginated narrative; decode all CSV fields including formula/control/whitespace cases. Attempt missing purpose/verification, bulk/foreign/operator/revoked/replayed retrieval and changed-body key reuse. | Readable complete PDF; CSV text cannot execute formulas; correct conflict/denial and zero disclosure/duplicate success. Full record never becomes log/audit copy. |
| B-23 | Commit and roll back each Doctor Booked/Waiting cancellation, exceptional In Examination cancellation and individual reschedule; also act as Receptionist. Dispatch repeatedly. | Each committed Doctor source yields one logical permitted Receptionist review notification; rollback zero; actual origin preserved; no Visit narrative or repeated business transition. |
| B-23 | Delay publication commits, reconnect using cursor, remove history availability and request snapshot; revoke recipient while connected/unread. | No lost late commit; recipient/tenant/version-bound cursor; own mark-read succeeds once, same-key replay unchanged, same-tenant other-recipient and foreign denial; snapshot and watermark consistent; no protected send/read after access-loss check; no retained DB transaction during client wait. |
| B-24 | Doctor opt-in on two devices; inspect transport payload; withdraw one, logout/reset/disable/suspend before a pending send; try Manager/Receptionist/operator subscriptions and malicious endpoint destinations. | Only explicitly consented current Doctor devices send; one-device withdrawal is scoped; name is maximum patient detail; no internal destination bypass, later revoked delivery or unauthorized subscription. Sound needs user interaction. |
| B-25 | Stop/restart around claim/send/ack/finish, compete reclaim and stale completion, simulate known transport failure/unknown acknowledgment/unknown DB commit. Exercise consumption of B-23's nonclinical tenant-enumeration capability for notification orchestration, unchanged B-20 cutoff capabilities and wrong clinic capability. | One logical business effect, generation fencing, actual socket-scheduled mail and persisted cutoff discovery, ordinary restart beyond lease expiry with zero second handoffs, correct readback/ambiguity hold and configured bounded retries; no clinical enumeration or secrets/tokens/PHI in logs. Shared sender role/path retained. |
| B-25 | Disabled cutoff across Cairo midnight/restart, then explicitly enabled due selected-day cutoff including repeated/missing local time and check-in race. | Disabled produces no automatic No-show; existing B-20 DST/day/policy rules and Booked-only CAS/claim release hold; Waiting/In Examination unchanged. |
| B-26 | Small fixture with Cancelled Routine original plus Completed Urgent replacement, D→D+1 reschedule, old Waiting, midnight completion, zero-net completion and two clinics; call dictionary/dashboard/trends/throughput. | Exact named service/event populations and frozen value; both IDs in gross same-day total, completed replacement once; old/current assignments distinct; admission flow uses event time; one response snapshot; no cash/foreign data. |
| D-12 | Real-login effective grants/RLS, pooled context after success/failure, empty/repeated/clean redeploy and representative populated predecessor upgrades; exercise indexed constraints and ordered cross-operation races. | All mapped applicable INV/CON outcomes, retained records/history/identities and atomic rejection; no partial successful domain/outbox effects; Issue #13 adoption gap explicitly disposed or unverified. |
| B-27 | Map every applicable AC-01–43/critical CON-01–45 to exact owner evidence; run distinct integrated identity/session/field/file/worker/notification and November role/Follow-up interactions. | No silent gaps or weakened mandatory execution; five universal staff operations and fixed Owner/Doctor boundaries preserved; independent review/production status separate. |

## Full Foundation Acceptance — Future Authorized Work

Follow [PostgreSQL CI diagnostics](../../docs/postgresql-ci.md) and all [permanent scalability rules](../../docs/full-foundation-scalability.md#permanent-scalability-rules-for-wave-4-wave-5-and-future-masters). Before dispatch/push, obtain the applicable publication/CI authority; this guide does not grant it. Reuse an available applicable complete run only for its actual tested SHA, with evidence available and valid.

After an authorized implementation is published at its final executable SHA, use a normal Full run, without special modes:

```powershell
$branch = 'feature/wave-4-Supporting-Services-&-Integration-Proof'
git rev-parse HEAD
gh workflow run foundation.yml --repo ahmedali5696/care-pulse-3 --ref $branch -f postgres_diagnostic=full
# Set this from the actual dispatch result; never select merely the newest unrelated run.
gh run view $runId --repo ahmedali5696/care-pulse-3 --json headSha,event,headBranch,attempt,conclusion,jobs
```

Require common checks, `ordinary-db-core`, `ordinary-db-clinical`, `ordinary-stack`, `historical`, `compat` and final exact `Full Foundation acceptance` to succeed in the same complete attempt. Verify actual mandatory reports/counts/case-identity digests with zero failures/skips, current candidate schema, immutable source/compat provenance, isolated environment and scoped volume/secret/source cleanup. Five schema-3 lane receipts must match repository/workflow/exact tested SHA/run/attempt and reviewed inventory/coverage; strict artifact ZIP digest validation and aggregate acceptance must succeed. Do not upload private reports/PHI/secrets to substitute for sanitized evidence. A marker line alone does not prove the workflow passed; expired/unavailable evidence is not PASS.

Rerun **all jobs** when another Full attempt is required; never combine receipts across attempts/runs/SHAs or substitute diagnostic/Fast Feedback/profiling success. PR test-merge, branch head and merged-development SHA are distinct identities. Do not treat predecessor Wave 3/Phase 2 engineering results as Wave 4 runtime proof.

After each implemented Master record per-suite timing, full/common/lane/gate durations, critical path and summed unrounded runner-minutes from the required run, with schema/inventory/pins/environment. Compare against applicable preceding baseline while identifying coverage/environment changes. Choose suite lanes by actual dependencies and measured cost; preserve all five lanes and mandatory selectors/repeated cases. Reuse owner proof traceability, add distinct integrated interactions, retain minimal approved Wave 5 E2E. No automatic repartition/extra lane/run or invented duration gate.

## Failure Diagnosis and Completion Record

Record exact failing step/test/run/SHA and classify application/test/environment using logs, reproduction or observed transaction state before correction. Use the smallest **listed** `postgres_diagnostic` in the current CI document when it covers the failure; no Wave 4-specific diagnostic exists merely because this guide names a scenario. A new diagnostic needs reviewed wiring under authorized implementation. After two speculative corrections require new root-cause evidence. Preserve assertions, timeout policy and actual lock observations.

Later completion records must distinguish IMPLEMENTED, TESTED, INTEGRATION VERIFIED, BLOCKED/UNVERIFIED and PRODUCTION READY using real evidence. Include Master ID, changes/checks/results, exact SHA/run/attempt and relevant open Issues with immediate blockers vs deferred production work. #24/#25/#26/#28 retain independent review; #13 populated-adoption proof and #2/#4/#5/#18/canonical production gates are not cleared by synthetic Full success. This planning phase stops after its six artifacts and document checks.
