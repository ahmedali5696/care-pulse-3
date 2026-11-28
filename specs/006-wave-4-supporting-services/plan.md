# Implementation Plan: Wave 4 — Supporting Services & Integration Proof

**Branch**: `feature/wave-4-Supporting-Services-&-Integration-Proof` | **Date**: 2026-11-28 | **Spec**: [spec.md](spec.md)

**Input**: `specs/006-wave-4-supporting-services/spec.md`  
**Source baseline**: `f52738dc0f4a33a1c8798df704134f690b9463c2`, with the existing untracked Wave 4 specification.  
**Historical planning disposition**: Phase 0 research and Phase 1 design only. That invocation added planning authority after the historical specification-only stage; it authorized no task generation, implementation, migration, CI dispatch, publication or production activation. Subsequent task generation and five-finding document remediation have separate explicit authorization; no implementation, CI, migration execution, publication, Issue changes, production activation or automatic Spec Kit progression is authorized by this plan.

## Summary

Plan the approved nine Masters: attachment/export persistence, private files and full one-patient export, committed in-app delivery/SSE, Doctor device push, scheduled consumers, precise metrics and integrated database/backend assurance. Reuse the Modular Monolith, F-01 identity boundary, D-09 evidence, shared email sender and B-20 cutoff. Delivery never repeats the originating clinical transition.

Authority descends from the [PRD](../../docs/CarePulse_PRD_v1_APPROVED.md), [Domain Model](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md), [System Design](../../docs/CarePulse_System_Design_v1_APPROVED(1).md), then [implementation plan](../../docs/implementation-plan.md) §§2/9/11–14. The [Constitution](../../.specify/memory/constitution.md) governs practice within those authorities. Existing requirement identifiers and Master dependencies remain unchanged.

## Technical Context

**Language/Version**: Existing standard-GIL CPython 3.13.16; TypeScript 5.9.3, Node 24.22.1, pnpm 10.34.7. Repository pins remain authoritative.

**Primary Dependencies**: FastAPI 0.158.1, SQLAlchemy Core 2.0.55, psycopg 3.3.6, Alembic 1.20.0, cryptography 50.0.2; Next.js exactly 15.5.28, React 19.3.1, Better Auth 1.7.12. Retain ClamAV, VAPID and SMTP/Mailpit. Planned additions: ReportLab with Arabic shaping/bidi and a licensed bundled font; pywebpush for native Web Push/VAPID. Select reviewed compatible exact releases and lock them during authorized implementation; no dependency change occurs here.

**Storage**: PostgreSQL 17.11, existing protected `private_files` volume; per-object AES-256-GCM with key version, fresh nonce and authenticated resource metadata. Forward-only successors after current head `0072_b20_worker`; assign actual revision IDs during implementation. No broker, Redis or new storage service.

**Testing**: Existing pytest 9.1.1; real PostgreSQL/Compose, signed real-actor boundaries, existing Web/TAP/Playwright tooling. Document checks for this change; future validation in [quickstart.md](quickstart.md).

**Target Platform**: Existing Linux Compose topology. Docker is unavailable and must not be installed on the PO workstation; authorized container checks run in Docker-capable CI.

**Project Type**: Modular Monolith web application: Next.js BFF, FastAPI domain modules and one Python worker.

**Performance Goals**: Configured bounded inputs/pages/batches and tenant-selective queries; scan/render/encryption/transport outside critical transactions. No new latency, throughput, volume or CI-duration target. Measure suite/lane/workflow cost after each implemented Master.

**Constraints**: Actual-login F-01 operation/resource authority, live session/account/tenant/class checks, FORCE RLS, immutable audit, private one-use retrieval, no unsafe or uncommitted disclosure, no automatic cutoff by default. Arabic/English export readability. Production policy remains default-deny.

**Scale/Scope**: Nine Masters; one Doctor/Owner per tenant; Receptionist with optional fixed `operational_manager`; two-tenant synthetic fixtures. Final UI/E2E belongs to Wave 5. No patient messaging/login, bulk export, billing, e-prescription authoring or commercial platform scope.

## Constitution Check

*Evaluate before Phase 0 and again after Phase 1. PASS below means document conformance, not runtime acceptance.*

| Gate | Before research | After design |
| --- | --- | --- |
| I — simplicity and ownership | PASS | Reuse current database, storage, crypto, sender, cutoff and identity boundary. PDF/Arabic and Web Push dependencies supply required missing capabilities. |
| II — security and integrity | PASS | Separate upload/read/export projections; narrow real-login grants; fenced finalization/publication; live retrieval/device checks; atomic audit/receipt and no retained raw grant bearer. |
| III — pragmatic performance | PASS | Short READ COMMITTED transactions, one-statement export/metric snapshots, bounded polling and external work outside locks; no speculative service/cache. |
| IV — code-first focused evidence | PASS | No implementation/tests here. Future owner evidence and integrated gaps, actual PostgreSQL races, exact-SHA Full acceptance; no imposed test-first cycle. |
| V — focused execution | PASS | Six Phase 0–1 artifacts; preserve existing spec/checklist/feature selection; no authority/task/Issue changes. |
| Scope and approvals | PASS | Exact dependencies below; missing future evidence gates affected execution/acceptance, while independent reviews and production gates stay open. |

No Constitution exception is required. CHK019's product clarification is resolved by the explicit PO decision of 2026-11-28: reporting weeks run Saturday through Friday using Cairo-local calendar intervals `[Saturday, next Saturday)`. US9-AS7 and the HTTP contract define DST-safe service-date and event-time attribution and unchanged monthly boundaries. The final independent reviewer PASS for CHK001–CHK040 is recorded in `checklists/review.md` on 2026-11-28 as reported by the PO; the nine Master dependencies stay unchanged. Exact dependency release pins are implementation validation, and production policy approvals are release gates; neither is invented here.

## Master Ownership and Dependencies

| Master | Approved dependencies | Design / owner proof |
| --- | --- | --- |
| D-10 | D-03, D-07, D-09 | Class-constrained Patient/Visit composite FK, private object/export/grant metadata, effective grants and predecessor preservation. |
| D-12 | D-01–11 | All applicable INV-01–38/database-mappable CON-01–45; owner evidence plus uncovered integrated interactions, migrations, indexes and real lock ordering. |
| B-21 | D-10, B-01/05/16 | Scan/encrypt/finalize, independent original reads, fail-closed scanner/key/type checks, cleanup and grant/revocation races. |
| B-22 | B-05/16/21, D-09 | All three staff variants' full PDF/CSV/manifest, active/archived patient, purpose/verified handoff/audit/formula defense and independent privacy denials. |
| B-23 | D-09, B-01/02/15/16/17/18 | Reusable committed-event delivery and narrow nonclinical tenant enumeration, recipient dedupe, commit-ordered cursor, reads/SSE/reconnect/revocation. |
| B-24 | B-23, B-04 | Doctor device opt-in, VAPID payload/send-time authorization and role-safe sound metadata. |
| B-25 | D-09, F-07, B-01/03/04/20/23 | Existing worker consumes B-23's enumeration/delivery capability, retains B-20 cutoff capabilities and invokes the shared sender; fresh tenant scope, fenced recovery, restart, transport uncertainty and disabled cutoff. T044–T048 own sender socket/configuration wiring in the existing worker/sender Compose services, ordinary durable handoff fencing, and bounded read-only cutoff discovery as specified in the transaction contract. |
| B-26 | D-11, B-16–20 | Dictionary/snapshot queries; replacement, reschedule, midnight, admission time and zero-price fixtures. |
| B-27 | B-01–26 | Applicable AC-01–43/critical CON-01–45; integrated identity/file/worker/role/race evidence, wire behavior and independent review. |

Retain **W4-M1** (attachment/export data, worker and notifications) and **W4-M2** (private access, metrics, combined assurance). Dependencies determine execution: B-25 waits for B-23; B-22 for B-21; B-27 for B-01–26. D-12 follows its own database prerequisites. Milestones add no new dependency edge or execution permission.

## Phase 0 — Research

[research.md](research.md) resolves storage/scan/crypto, composite keys, external-file atomicity, full export/Arabic rendering, dedicated binary/SSE BFF adapters, publication order, worker identities/recovery, push consent, consistent metrics and CI evidence. Product safeguards are authoritative; technical selections are this plan's design decisions.

## Phase 1 — Design and Contracts

Review corrections specify fresh signed per-transaction F-01 requests for file/export/download phases (one PRIMARY per registration, no reusable continuation), expiry-aware recovery and persisted stage/generation fences. Binary transfers use independently verified contiguous chunks within existing API/PostgreSQL 1 MiB request limits and an independently checked full-file assembly; JSON limits and configured total-file policy remain unchanged. See the [effective-source verification](research.md#review-finding-1--effective-f-01-lifecycle-verification) and [protocol](contracts/http.md#dedicated-binary-adapter). These are planning corrections, not implemented identity or upload changes. Canonical HTTP phase inventory defines exact method/path/purpose/target/phase, independently signed chunk stage/publication/status and BFF/API CP_POLICY_FILE compatibility before upload or SSE resource use.

Pre-tasks corrections preserve the same owners: D-10/B-21 fenced abandonment and configured disposal; B-22 preparatory verification plus actual handoff confirmation before grant and redemption recheck; B-23 terminal failure/reconciliation and configured fail-closed SSE rechecks; B-24 committed alert routing and separate explicit Doctor name consent; B-25 bounded configured retry/lease/cleanup; B-26 Cairo calendar boundaries and PO-approved all-staff own-tenant completed-value read; D-12/B-27 shared authority-first lock-order proof against privilege changes. See the corresponding spec scenarios and contracts. Missing required configuration refuses affected operations/readiness; no production values, new delivery channel, Master or dependency are selected. B-21 owns reusable candidate cleanup, B-22 proves failed export disposal and both READY publication/cleanup orders, B-23 owns recipient mark-read and protected operational telemetry, and B-24 owns the final consent/handoff boundary; no new Master dependency.

- [data-model.md](data-model.md): fields, relationships, validation, permissions and technical lifecycles.
- [contracts/http.md](contracts/http.md): supporting-service wire and role/field projections.
- [contracts/transactions.md](contracts/transactions.md): identity grants, locks, recovery, migrations and integrated evidence map.
- [quickstart.md](quickstart.md): validation prerequisites, runnable supported commands and expected outcomes, distinct from checks executed now.

## Project Structure

### Documentation (this feature)

```text
specs/006-wave-4-supporting-services/
├── spec.md                         # Existing input; preserved
├── checklists/requirements.md      # Existing checklist; preserved
├── checklists/review.md            # Existing independent review; preserved
├── plan.md
├── research.md
├── data-model.md
├── contracts/
│   ├── http.md
│   └── transactions.md
└── quickstart.md
```

`tasks.md` belongs to the separately authorized `$speckit-tasks` stage; it was not generated during the historical planning invocation. Its subsequent presence does not authorize implementation.

### Source Code (repository root)

```text
apps/api/carepulse/
├── files/               # Existing stub; planned D-10/B-21/B-22 modules
├── notifications/       # Existing evidence/email sender; planned B-23/B-24
├── reporting/           # Existing day-review data; planned B-26 queries/routes
├── identity/            # Existing boundary/receipts; narrow new scope registration
└── appointments/        # Existing cutoff and transition ownership retained
apps/web/auth/           # Existing BFF; dedicated binary/SSE adapters planned
apps/web/app/api/        # Supporting-service BFF routes planned
apps/web/mail/           # Shared existing email transport
apps/worker/             # Existing process; B-25 invokes handlers
db/migrations/versions/  # Forward-only successors to 0072
tests/integration/       # Existing test tree; focused owner/integrated proofs
tests/unit/              # Small deterministic validators/projections
ops/                     # Existing checks, reviewed inventory and evidence validators
```

**Structure Decision**: Extend existing modules; proposed additions above are not implemented functionality. FastAPI retains business ownership. Wave 5 retains final notification, files/export and report interfaces. Existing shared paths touched by planned tasks: `requirements.in`, `requirements.lock`, `apps/api/Dockerfile`, `apps/api/carepulse/config.py`, `apps/web/config.ts`, `apps/api/carepulse/main.py`, `.github/workflows/foundation.yml` and `docs/postgresql-ci.md` during implementation only; `docs/full-foundation-scalability.md` remains a governing reference. D-12/B-27 use the existing flat `tests/integration/test_w4_*.py` convention for the approved DB/API security areas. Use numeric `NNNN_<slug>.py` forward successors; schema and route registration remain Master-owned, extend unpublished revisions when safe, allocate further successors only as needed against actual head; no fixed migration count per Master. Serialize shared files and head allocation; use parallel markers only for independent work after prerequisites.

## Integration Evidence and Scalability

Follow [postgresql-ci.md](../../docs/postgresql-ci.md) and all six [permanent scalability rules](../../docs/full-foundation-scalability.md#permanent-scalability-rules-for-wave-4-wave-5-and-future-masters). Retain `ordinary-db-core`, `ordinary-db-clinical`, `ordinary-stack`, `historical`, `compat`, common checks and exact `Full Foundation acceptance`. Choose existing lanes by actual dependencies and measured cost: DB-only proofs in a suitable DB lane; scanner/storage/BFF/browser/SMTP/push transport proofs in Stack where required. Do not assign solely by case count or Master name.

Map valid owner evidence before adding D-12/B-27 tests; add distinct interactions/gaps without dropping mandatory executions, deliberate repetitions, case identities or report digests. Preserve immutable 0040 Historical and bounded six-successor Compatibility provenance; new current-head migration evidence does not broaden that exercise.

After each implemented Master, record suite times, workflow/common/lane/gate elapsed, critical path and aggregate unrounded runner-minutes from its already required acceptance run, including inventory/schema/pins/environment. No automatic repartition, added lane, extra measurement run, weakened assertion, retry/skip or timeout increase. Fast Feedback is routine feedback; diagnostics are non-acceptance. Full acceptance requires all seven jobs, five digest-verified post-cleanup receipts, same exact SHA/run/attempt, zero mandatory failures/skips and the aggregate marker. Required reruns run all jobs in one attempt.

## Open Issues and Execution Gates

A connected open-Issue search on 2026-11-28 confirmed #2/#4/#5/#13/#18/#24/#25/#26/#28 remain open. Underlying fixes/evidence were not independently revalidated. Existing Issues cover relevant obligations; no duplicate Issue is created.

| Issues | Responsible review / verifiable closure boundary | Scope effect |
| --- | --- | --- |
| [#24](https://github.com/ahmedali5696/care-pulse-3/issues/24), [#25](https://github.com/ahmedali5696/care-pulse-3/issues/25) | Engineering, Clinical/Security/PO: applicable November behavior, exact-SHA evidence, independent review and PO disposition. | Settled product decisions; preserve affected D-12/B-22/B-23/B-27 acceptance review. |
| [#26](https://github.com/ahmedali5696/care-pulse-3/issues/26) | DB/API Security, Security/Operations, Clinical/PO: effective real-login grants, forged-context denials, actual actor evidence and explicit risk disposition. | Reuse F-01; neither assert a current exploit nor infer independent security acceptance. |
| [#28](https://github.com/ahmedali5696/care-pulse-3/issues/28) | Compatibility engineering/Security/Operations/Clinical/PO: authentic source/candidate worker/sender/replay evidence and explicit disposition. | Preserve later engineering evidence in plan §12; do not reassert historical gaps as current failures. |
| [#13](https://github.com/ahmedali5696/care-pulse-3/issues/13) | Migration engineering/Security/Operations: representative populated intermediate upgrades, retained records/FKs/grants and atomic rejection at exact SHA. | D-12 planning review trigger retained; empty-install results cannot clear populated adoption. |
| [#18](https://github.com/ahmedali5696/care-pulse-3/issues/18) | PO/Security/Operations/Data Governance/legal adviser: approved numerics/file policy and backup/restore/recovery/Egypt disclosure evidence. | Production-only gates OPEN; synthetic labeled fixtures permitted. Medical production uploads disabled while OPEN-FILES-01 remains open. |
| [#2](https://github.com/ahmedali5696/care-pulse-3/issues/2), [#4](https://github.com/ahmedali5696/care-pulse-3/issues/4), [#5](https://github.com/ahmedali5696/care-pulse-3/issues/5) | PO/Engineering/Security/Operations: ingress controls, current dependency disposition and credential/key/mail-provider/retry policy evidence/signoffs. | Deferred production work, including B-25 mail policy review; no invented policy or remediation authority. |

The PO-approved CHK019 week-start decision resolves the B-26 product clarification; the correction has received final independent reviewer requirements-quality PASS as reported by the PO and recorded in `checklists/review.md`; this did not itself authorize task generation and does not establish B-26 runtime acceptance or authorize implementation. Missing consumed prerequisite evidence blocks affected implementation/acceptance. Independent Issue dispositions and SECURITY-POLICY-NUMERICS/OPEN-OPS-01/02/03/OPEN-FILES-01 remain separate from documentary completion.

## Complexity Tracking

No Constitution exception is requested. Separate record/manifest artifacts avoid an archive format; short signed reads behind BFF SSE avoid connection-wide database authority; PostgreSQL remains the durable event/job store.
