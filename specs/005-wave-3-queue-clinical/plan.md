# Implementation Plan: Wave 3 — Queue & Clinical Operations

**Branch**: `feature/wave-3-queue-and-clinical-operations` | **Date**: 2026-11-22 | **Spec**: [spec.md](spec.md)

**Input**: Current worktree `specs/005-wave-3-queue-clinical/spec.md`, including configured confirmation TTL and B-19's preserved local clock times and joint capacity-conflict exclusions. Reuse F-01 and established contracts; identify unverified November prerequisites without moving ownership or making production-only decisions planning blockers.

**Status**: Phase 1 planning refreshed; proposed engineering design, not implementation acceptance or execution authorization. Original research baseline: `79d6a2775d8b20a27b8df3e6ca4a64415c8eb8ec`; refresh source HEAD: `65838b0e373a8bf80b88ca22894c1e311bcccd45` plus the existing uncommitted feature specification. The **preceding** planning refresh changed six planning artifacts and preserved existing spec/checklist/task edits; the separate 2026-11-23 PO instruction permits this B-18 documentation-only propagation including the untracked tasks file, without overriding those historical approvals or granting implementation/release authority.

## Summary

Deliver B-11–B-20 through the existing FastAPI modular monolith and authenticated Next.js BFF: dated check-in and independent lanes, immediate intake, shared-capacity admission, Visit work, individual and atomic bulk changes, and optional dated reconciliation. Reuse D-04–09/D-11 persistence, booking/price/calendar/Follow-up helpers, D-09 receipts/audit/outbox and F-01's signed, database-verified identity boundary. No additional identity system, queue service, entitlement engine or delivery worker.

Existing Queue/Visit/DayReview schema and historical 0040 writers are substrate, not supported current-head product commands. Each new operation needs explicit protected routing, operation/resource-scoped database authority, domain validation and current executable-SHA positive/negative/concurrency evidence. See [research](research.md), [HTTP](contracts/http.md), [transactions](contracts/transactions.md), [data model](data-model.md) and [validation guide](quickstart.md).

## Technical Context

**Language/Version**: CPython 3.13.16; TypeScript 5.9.3, Node 24.22.1 and pnpm 10.34.7 under the existing pinned toolchain.

**Primary Dependencies**: Existing locked FastAPI, SQLAlchemy 2 Core, psycopg 3 and Alembic; Next.js 15.5.28, React 19.3.1, Better Auth 1.7.12. Reuse lockfiles; no new dependency selected.

**Storage**: PostgreSQL; tenant-qualified tables, FORCE RLS, immutable evidence and forward-only Alembic chain. Baseline head `0046_f01_audit_integrity`; reserve no migration identifier now. Recheck the actual immediate predecessor before an authorized migration.

**Testing**: Focused pytest units and real-PostgreSQL persistence/RLS/races plus BFF contract checks; ordinary full Foundation at final executable SHA. Applicable later UI owners retain Playwright/E2E. Protected T133 remains separate.

**Target Platform**: Existing Linux Compose topology in Docker-capable CI. Windows PowerShell workstation runs container-free document/static checks; no local Docker installation or requirement.

**Project Type**: Existing web application and private API modular monolith.

**Performance Goals**: Bounded tenant/date/lane queries, applicable pagination and short ordered transactions. Inspect representative PostgreSQL query plans under master §11.4; no invented latency, load, page-size or throughput target.

**Constraints**: One Doctor/Owner and branch per clinic; Cairo dates/UTC instants; EGP Decimal/NUMERIC; six Appointment states; live actual identity and least privilege; no network work under business locks. Missing required configuration denies its capability. Confirmation TTL is configured with labeled synthetic fixture values; no production number or user-facing control selected.

**Scale/Scope**: Ten existing Masters, W3-M1/W3-M2, within the unchanged 58-Master plan. Backend/API and minimal BFF transport only. UI, files/export, reporting, realtime/push delivery and durable cutoff scheduling retain their owners.

## Constitution Check

*Gate evaluated before Phase 0 and re-evaluated after Phase 1.*

Authority order: [PRD](../../docs/CarePulse_PRD_v1_APPROVED.md) → [Domain Model](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md) → [System Design](../../docs/CarePulse_System_Design_v1_APPROVED(1).md) → [master implementation plan](../../docs/implementation-plan.md). The [Constitution](../../.specify/memory/constitution.md) governs practice within these authorities.

| Gate | Before research | After design |
| --- | --- | --- |
| Authority, scope and dependencies | PASS: approved spec and master §§8/12 govern | PASS: all ten Masters, dependency edges and Waves preserved |
| I — minimal modular implementation | PASS: established stack/substrate | PASS: existing F-01 and evidence stores; no new service/dependency |
| II — identity, privacy, atomicity | PASS for planning; executable proof required | PASS for design: scoped authority, separate DTOs, ordered locks and durable evidence; enablement unverified |
| III — pragmatic performance/UI boundaries | PASS: no new numeric target | PASS: bounded projections/query-plan checks; later U owners retain Arabic/English and accessibility |
| IV — code-first focused evidence | PASS: document checks for this invocation | PASS: production code first, focused tests after; PostgreSQL/Compose in CI; no test-first mandate |
| V — focused scope/worktree safety | PASS: existing dirty feature documents and untracked tasks identified for preservation | PASS: preceding six-artifact planning refresh retained earlier inputs; the separately authorized 2026-11-23 B-18 documentation correction also updates spec and existing untracked task content in place. Upstream approved authorities, application code, checklists and Issues remain untouched |
| Approval/production boundaries | PASS: authorized synthetic policy variance | PASS: production TTL/numerics/legal/custody remain separate; no release claim |

These are documentary planning gates, not executable Security/Clinical approval. The specification records Product Owner approval of **CHK007 Option B, CHK009 Option A and CHK037 conservative Cairo DST/recovery policy on 2026-11-22**; their scope is recorded in [spec.md](spec.md#checklist-resolution--product-owner-approval-2026-11-22) and the B-18/B-20 contracts. This refresh grants no new approval. Their former PO-decision blockers are resolved for requirements review, subject to independent reviewer checkbox acceptance. No Constitution violation is identified. Unverified executable prerequisites below remain separate implementation/enablement gates.

## Ownership, dependencies and delivery order

Dependencies are copied from the spec and master §8; no edge is added. Work starts only after separate implementation authorization and the consumed-contract gate.

| Master | Dependencies, unchanged | Planned change and owner evidence |
| --- | --- | --- |
| B-12 | D-06, B-09 | Check-in/dates/projections; one entry, no Visit, archived existing-ID success |
| B-11 | B-06–09, B-12, D-06 | Separate Routine/Emergency/Urgent intake; atomic Waiting creation, price/calendar races |
| B-14 | D-06/09, B-01/12 | Pause, capacity, mode, configured one-case confirmation; role/expiry/revocation |
| B-15 | B-12/14, D-07/09 | Shared capacity → lane → admission/Visit; fresh old-day presence, justified exception |
| B-13 | B-12/15, D-04/06 | Routine placement/future policy; placement versus B-15 admission proof owned here |
| B-16 | B-15, D-07/08, B-10 | Doctor clinical work/corrections; actual-examination completion; In Examination exceptional cancel |
| B-17 | B-10–12/15 | Booked/Waiting cancel; typed-confirmed new-ID Routine→Urgent replacement; rollback/races |
| B-18 | B-07–10/12/17 | Same-ID reschedule; forbid same-day Routine Waiting in Open/Exact-Time (B-13 placement handles queue order); retain cross-day Waiting→Booked and eligible Booked moves. For **every same-day existing-ID Paid Urgent move**, require a fresh positive physical-presence attestation by the currently authorized acting Doctor/Receptionist, bind actual actor/Appointment/server operation time and validate inside the transaction, preserving onsite/day/work/schedule/revision guards, archived-ID continuation and frozen premium/lane. Distinct B-10 extension and Doctor precedence stay unchanged; prove positive/negative/authority-race outcomes in B-18's own acceptance |
| B-19 | B-18, D-06 | All-staff complete authorized original-day selection; destination must be a DIFFERENT Africa/Cairo service date. Reject the ENTIRE original-day bulk preview, even Booked-only, before per-row eligibility, with no successful preview, counts, receipt or business effects. Strict input rejects empty/duplicate IDs without deduplication. Valid different-day preview retains exact eligible/included and ineligible/excluded selected identities, matching counts and reasons (including an explanatory zero-eligible preview). Typed-confirmed commit atomically moves **all and only unchanged eligible included IDs**, not originally excluded rows; stable exclusions alone do not block, but stale inclusion/bound conditions reject all; zero-included commits cannot succeed as no-ops. B-18 individual lawful same-day Booked moves remain permitted; CHK007 separately forbids already-Waiting Routine same-day moves, including interval-only changes. No separate bulk grant |
| B-20 | B-12, B-15–18, D-11 | Strict Cairo prior-date manual overdue; separate selected-day cutoff at IANA fold=1/gap-first-valid instant; atomic Booked-only/retryable duplicate-safe transitions, dated review/close; B-25 owns invocation/retries |

B-19 carries each Exact-Time row's original Africa/Cairo start/end clock times onto the destination date; Open remains day-only. After individual destination guards, one half-open interval evaluation of existing bookings plus all remaining candidates excludes every candidate participating in any oversubscribed segment (`C=1` when overlap is off), with no priority, tie-breaking or iterative salvage. Independent candidates and existing bookings are preserved. The preview binds derived intervals and partitions; stale or jointly infeasible included rows reject the entire commit, without re-exclusion or partial moves. Reuse existing Exact-Time DST validation, without B-20 cutoff shifting/fold choice. B-18/B-19 also require narrow F-01 destination occupancy projection authority; its current SLOTS/booking scope is insufficient. See [research](research.md#carry-original-local-intervals-and-classify-bulk-capacity-conflicts-jointly).

B-19 commit sends only the `preview_id` and exact `confirmation:"RESCHEDULE"` JSON under the existing `Idempotency-Key` header. The authorized server retrieves the complete cohort, both included/excluded partitions, Cairo dates and bound revisions from the original immutable D-09 preview receipt; the client never echoes these potentially large fields. Keep both existing 1 MiB mutation-body checks. The bulk preview names the master's generic `eligible[]` subset `included`, without an extra `affected[]` alias. Commit persists exactly one batch command receipt, one history entry per moved included row and one Doctor-origin review event per moved row, never per-row receipts.

**B-11 Exact-Time intake wire:** In Open mode, immediate Urgent intake is day-only and supplies no invented interval. In Exact-Time mode the request identifies the intended current valid manual or unsuppressed generated slot by its canonical start/end instants from the existing B-08 `GET /api/slots` contract; the server re-resolves the interval and checks candidate membership, Cairo date, periods/breaks, occupancy/capacity and applicable policy revisions at commit. Missing/invalid timed endpoints fail rather than being auto-assigned. This neither changes current-instant onsite work-period eligibility nor the Receptionist-only new-Urgent premium policy.

**B-14/B-15 confirmation lifecycle and handoff:** Under Doctor-control, dated QueueDay and target Appointment serialization, the Doctor's same-key issuance retry returns its original confirmation ID without a second approval. A different issuance key for the same currently valid case is a conflict, not a second authority; only an expired, revoked or bound-revision-stale previous record permits a fresh issuance under a new key and fresh guards. The consumed-only uniqueness constraint is insufficient by itself. An admission-mode-authorized Receptionist in a **separate authenticated session** obtains the matching usable Doctor-issued ID through a narrow Appointment/day/lane-scoped read projection, never via Doctor session reuse or issuance/revocation permission. B-14 owns this read/issuance contract; B-15 consumes the ID under live one-use validation, with its own separate-session acceptance.

**B-18 occupancy and authorization:** Timed same-ID destination capacity validation models one replacement interval: omit only the locked target Appointment's old interval, include its proposed interval exactly once, and retain every other counted booking. Recheck inside the transaction against competing bookings and schedule/capacity publication under the existing F-01 signed, target/date-scoped occupancy read authority. The Manager grant's removal invalidates stale Manager credentials/permissions, not the ordinary reschedule permission of a newly authenticated, still-enabled Receptionist; actual disablement or invalid/stale credentials continue to deny. Every independently successful same-day Paid Urgent adjustment needs its own fresh attestation; Doctor-origin produces one review event, Manager/ordinary Receptionist produces zero Doctor-origin review events.

**B-18 onsite-evidence decision (PO 2026-11-23):** Every same-day existing-ID Paid Urgent reschedule now requires a **fresh positive physical-presence attestation** from its currently authorized acting staff member, submitted and validated with the reschedule, with immutable actual actor ID/role, tenant, Appointment and server operation time. Historical intake/active `Waiting` or QueueEntry alone is insufficient; missing/false or mismatched evidence, lost authority or stale eligibility denies atomically. Preserve Doctor existing-ID reschedule authority (without expanding Doctor Urgent creation), archived-Patient continuation, current Cairo day/work/break/holiday/slot/PRICE/SCHEDULE revisions, same lane and frozen premium. The **PO clarification blocker is removed**; onsite-specific T049/T052 executable acceptance remains **not yet demonstrated**, together with other pending technical/security prerequisites. No geolocation, freshness TTL or new role/requirement is added. See [research](research.md#b-18-approved-current-onsite-evidence-po-2026-11-23).

W3-M1 covers check-in/intake/lanes/admission and applicable queue-policy proof; W3-M2 covers Visit/individual/bulk/reconciliation. B-12 is independent of B-11; B-15 is independent of B-13. Ordering does not permit another Wave or transfer ownership.

## Consumed prerequisites and open obligations

The [research register](research.md#upstream-prerequisite-register) names source/evidence limitations, owners, affected consumers and closure checks. Before implementing an affected Master, independently establish its exact upstream contract at an executable revision; if evidence is absent, report **that Master BLOCKED for implementation**, naming the consumed missing contract. Do not blanket-block every Master, reopen checked tasks or implement upstream amendments inside Wave 3. B-16's new completion command remains B-16; D-07/D-08 retain storage/provenance ownership.

Live GitHub search on 2026-11-22 confirmed [#24](https://github.com/ahmedali5696/care-pulse-3/issues/24), [#25](https://github.com/ahmedali5696/care-pulse-3/issues/25), [#26](https://github.com/ahmedali5696/care-pulse-3/issues/26) and [#28](https://github.com/ahmedali5696/care-pulse-3/issues/28) OPEN. #24/#25 retain amendment engineering/downstream evidence; #26 independent Security/Clinical and PO disposition. Master §12 records later synthetic compatibility evidence addressing #28's historical observations; its open status does not require repeating accepted engineering. This run did not rerun that CI or dispose of an Issue.

Production/deployment-only [#2](https://github.com/ahmedali5696/care-pulse-3/issues/2), [#4](https://github.com/ahmedali5696/care-pulse-3/issues/4), [#5](https://github.com/ahmedali5696/care-pulse-3/issues/5), [#13](https://github.com/ahmedali5696/care-pulse-3/issues/13), [#18](https://github.com/ahmedali5696/care-pulse-3/issues/18), SECURITY-POLICY-NUMERICS and OPEN-OPS-01/02/03 / OPEN-FILES-01 do not block this planning or authorized synthetic design. Existing Engineering, Security/Operations, Clinical, legal/privacy and PO owners retain Issue-specific production triggers/closure criteria. Missing required policy still fails closed; synthetic values are not production defaults.

## Phase outputs and validation

Phase 0 resolves design unknowns using established contracts, recording evidence gaps separately. Phase 1 specifies entities, operation families, field permissions, transaction/recovery behavior and runnable future validation. New migrations, if needed, extend the actual current head, preserve published 0021/0022 and 0041–0046, and prove populated immediate-predecessor preservation and failed-migration atomicity without fabricating historical authorship.

The [quickstart](quickstart.md) distinguishes checks for these documents from future executable acceptance. No tasks are generated. B-23/B-24 own delivery/push; B-25 scheduler/retries; B-21/B-22 files/export; B-26 reports; D-12/B-27 broader integration; U tasks final UI/E2E. Later delivery cannot weaken Wave 3's committed event/evidence requirements.

## Project Structure

### Documentation (this feature)

```text
specs/005-wave-3-queue-clinical/
├── spec.md              # Existing input; preserved
├── checklists/          # Existing specification checks; preserved
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
└── contracts/
    ├── http.md
    └── transactions.md
```

The earlier six-artifact planning refresh preserved the then-untracked `tasks.md` byte-for-byte **at that earlier checkpoint**. This historical verification statement does not prohibit or negate later separately authorized edits to that same untracked file, including the 2026-11-23 B-18 evidence correction and this eight-finding documentation correction. The file remains untracked; no task-generation or implementation authority follows from this clarification.

### Source Code (repository root)

```text
apps/api/carepulse/
├── identity/            # Existing protected transaction and field boundary
├── appointments/        # Booking reuse; planned intake/change/reconciliation
├── queue/               # Existing metadata; planned check-in/admission/routes
├── visits/              # Preserve B-10; planned B-16 clinical/completion
├── policies/            # Existing policies; planned queue/admission operations
├── notifications/       # Existing audit/outbox append helpers
└── main.py              # Explicit operation/method/path registration
apps/web/auth/           # Existing session/proxy/signing
apps/web/app/api/        # Minimal protected BFF transport
apps/worker/             # Existing topology; B-25 invocation remains later
db/migrations/versions/  # Reviewed forward-only evolution if necessary
tests/unit/              # Small pure-rule/DTO checks
tests/integration/       # Real-login/signed PostgreSQL proof
ops/check.ps1            # Current-head selections; preserve predecessor evidence
.github/workflows/       # Existing Foundation acceptance
```

**Structure Decision**: Extend existing modules, typed routes and connection-taking SQLAlchemy Core primitives. Business policy stays in FastAPI. This maps later affected callers/configuration/test consumers; it does not authorize their modification now.

## Complexity Tracking

No Constitution violations or additional infrastructure proposed.
