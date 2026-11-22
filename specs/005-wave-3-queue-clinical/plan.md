# Implementation Plan: Wave 3 — Queue & Clinical Operations

**Branch**: `feature/wave-3-queue-and-clinical-operations` | **Date**: 2026-11-22 | **Spec**: [spec.md](spec.md)

**Input**: `specs/005-wave-3-queue-clinical/spec.md`, including configured confirmation TTL. Reuse F-01 and established contracts; identify unverified November prerequisites without moving ownership or making production-only decisions planning blockers.

**Status**: Phase 1 planning complete; proposed engineering design, not implementation acceptance or execution authorization. Source baseline: `79d6a2775d8b20a27b8df3e6ca4a64415c8eb8ec`. Only this feature's planning artifacts change. Spec/checklist authorization statements remain historical specification-stage records.

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
| V — focused scope/worktree safety | PASS: initial worktree clean | PASS: feature planning artifacts only; no upstream/task/code/Issue changes |
| Approval/production boundaries | PASS: authorized synthetic policy variance | PASS: production TTL/numerics/legal/custody remain separate; no release claim |

These are documentary planning gates, not executable Security/Clinical approval. No unresolved product clarification or Constitution violation remains. Unverified prerequisites below are implementation/enablement gates, not questions to answer by inventing requirements.

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
| B-18 | B-07–10/12/17 | Same-ID reschedule; frozen evidence/price; distinct B-10 extension and Doctor precedence |
| B-19 | B-18, D-06 | All-staff same-original-day eligible Routine preview/atomic move; no separate grant |
| B-20 | B-12, B-15–18, D-11 | Booked-only cutoff/manual No-show, dated review/close; B-25 owns invocation/retries |

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

`tasks.md` belongs to a separately authorized later stage.

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
