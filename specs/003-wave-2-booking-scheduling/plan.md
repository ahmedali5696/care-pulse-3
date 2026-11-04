# Implementation Plan: CarePulse 3 Wave 2 — Booking & Scheduling

**Git branch**: `feature/wave-2-booking-and-scheduling` | **Date**: 2026-10-28 | **Spec**: [spec.md](spec.md)

**Spec Kit identifier**: `003-wave-2-booking-scheduling` (setup's `BRANCH` result; not a Git branch rename).

**Status**: Existing planning baseline aligned in place on 2026-11-04; independent review pending. Current working-tree authorities and latest approved Issue #24/#25 decisions govern. Historical task/CI acceptance is preserved only for its original scope; amended requirements are UNVERIFIED. No implementation, migration execution, commit, push or release authorized here.

**Historical pre-task requirements review (2026-10-28; not November acceptance)**: Twelve explicit [Product Owner clarifications](spec.md#clarifications) preserve the prior ten answers and add only CHK002/CHK011: no maximum booking horizon and no percentage fractional-digit cap, with existing guards retained. [Booking review](checklists/booking-review.md) records 43/43 booking criteria and 16/16 specification-quality criteria. Independent documentation review found no demonstrated BLOCKER or MAJOR finding; Issue #19 was closed as completed. This clears the requirements-review condition only. `/speckit.tasks`, implementation, commits, deployment and production release still require separate authorization; no tasks were generated in this scope. Preserve the nine masters, edges and Wave boundaries below.

## Summary

Plan exactly **D-06, D-07, D-08, D-11, B-06, B-07, B-08, B-09 and B-10** from [master plan §7](../../docs/implementation-plan.md). Extend the existing Modular Monolith with tenant-isolated queue/Visit/claim/day-review persistence, Owner/live operational_manager pricing/calendar APIs, valid Exact-Time candidates, ordinary Booked-only Routine creation and qualified preferential Follow-up. Preserve the existing policy, Appointment, numbering, frozen-price, patient lifecycle, authorization and audit/receipt contracts.

Authority, descending: [PRD](../../docs/CarePulse_PRD_v1_APPROVED.md), [Domain Model](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md), [System Design](../../docs/CarePulse_System_Design_v1_APPROVED%281%29.md), [master implementation plan](../../docs/implementation-plan.md). The [constitution v1.1.0](../../.specify/memory/constitution.md) governs engineering within them. Full master task text and the spec's authority/acceptance mapping remain binding; this document is not a replacement master inventory.

## Technical Context

**Language/Version**: Existing standard-GIL CPython 3.13.16; PostgreSQL SQL; TypeScript 5.9.3 on Node 24.22.1 for guarded BFF forwarding only.

**Primary Dependencies**: Existing FastAPI 0.158.1, SQLAlchemy Core 2.0.55, psycopg 3.3.6, Alembic 1.20.0; Python standard-library Decimal/zoneinfo. Preserve Next.js 15.5.28, React 19.3.1, Better Auth 1.7.12, pnpm 10.34.7 and committed locks. No new dependency, framework, service, broker or cache is needed.

**Storage**: Existing PostgreSQL 17.11; 0018 was the historical generation head and published D-07/D-08 revisions 0021/0022 are immutable. Any amended guard/storage changes require reviewed forward-only revisions after the then-current integrated head. Migration-only DDL; non-owner NOBYPASSRLS runtime, metadata-only operator, FORCE RLS and tenant-qualified relationships retained.

**Testing**: Existing pytest 9.1.1/Ruff, deterministic unit checks and real concurrent PostgreSQL transactions. Docker-capable CI owns Compose, migrations, effective privileges, RLS, pool reuse, races and affected BFF integration. Documentation-only planning receives document checks, not application acceptance tests.

**Target Platform**: Existing private Linux Compose topology; Windows workstation uses container-free checks only. Docker must not be installed or required locally.

**Project Type**: Modular web application. FastAPI owns business logic; Next BFF validates actual sessions/Origin/CSRF and signs explicitly allowlisted private requests; worker imports the same domain modules when its later owning tasks are authorized.

**Performance Goals**: No approved numeric latency, throughput or clinic-volume targets. Short ordered transactions, tenant-first bounded queries and representative PostgreSQL query-plan checks; optimize only demonstrated problems.

**Constraints**: One Doctor/Owner and one branch per clinic, fixed EGP/Africa/Cairo, exact six Appointment states; immutable confirmed money/entitlement/history. Open has no clock time or implicit day cap. No local Docker, production policy guesses, network work under business locks or frontend business-rule duplication.

**Scale/Scope**: Nine unchanged Wave 2 masters; at least two isolated synthetic clinics and both staff roles for verification. D-06/D-07/D-08/D-11 are foundations, not operational queue/clinical/reporting delivery.

**Research disposition**: [research.md](research.md) retains prior research and the November target design. Product manager/reason/date/attestation decisions are settled; the bounded technical review register below remains open before implementation. Production values remain separately deferred, never defaults.

## Constitution Check

| Gate | Before Phase 0 | After Phase 1 | Evidence / restriction |
| --- | --- | --- | --- |
| I — simplicity/module ownership | PASS | PASS | Existing PostgreSQL/Core/Decimal/zoneinfo, shared contracts and domain modules; no generic repository or new service. |
| II — security/data integrity | PASS (design) | PASS (design) | Live auth before receipts, forced RLS/composite FKs, ordered serialization, immutable evidence and atomic receipts/outbox; executable proofs remain required. |
| III — pragmatic performance | PASS | PASS | Bounded pages, targeted indexes/query plans, interval sweep and short locks; no invented targets. |
| IV — code-first/evidence | PASS | PASS | Future production functionality first, then focused tests; each owner owes real-PostgreSQL checks and populated preceding-revision upgrades, without waiting for D-12/B-27. |
| V — focused authorization | PASS | PASS | Exact master IDs/edges and owning Waves preserved; only native Phase 0/1 documents produced. |
| Synthetic policy boundary | PASS | PASS | 2026-10-21 timing variance permits clearly labeled synthetic fixture values only; production fails closed. |
| Production readiness | OPEN / BLOCKED | OPEN / BLOCKED | SECURITY-POLICY-NUMERICS, OPEN-OPS-01/02/03, OPEN-FILES-01 and applicable Issues remain unresolved. |

The pre-research gate was evaluated from the spec, constitution and approved authorities. The post-design gate concerns documents only. Any unjustified authority violation or unresolved material synthetic-design clarification is **ERROR** and stops the affected work; production gates are not waived by planning PASS.

## Exact Master Scope and Dependencies

This table restates existing masters, not generated implementation tasks. Expanded ranges retain exactly the direct edges in master §7. The spec's matching table carries the full PRD/BR/INV/CON/AC anchors.

| Master Task | Exact direct dependencies | Design and owner evidence | Spec coverage |
| --- | --- | --- | --- |
| D-06 | D-04, D-05 | Dated QueueDay/Entry, shared Doctor controls, scoped single-use confirmations; cross-date/lane capacity simulation, no ghost membership, approval denials. | W2-FR-23–25; US5.1–3 |
| D-07 | D-05 | Unique admission-linked Visit, Doctor clinical revisions and separate actual-actor positive completion attestation/source evidence; no Receptionist clinical authorship, retained terminal history. | W2-FR-26; US4.7, US5.4/8 |
| D-08 | D-07, D-05 | Actual-actor source decisions, separate NEW-date/confirmed-extension evidence and single-use ledger; Doctor precedence, policy/source/claim/Visit revision and lifecycle proofs. | W2-FR-17–22; US4 |
| D-11 | D-04, D-05, D-06, D-07 | Dated DayReview revisions and date/status/lane/family/report query foundations; midnight preservation, no-Waiting close constraint and query plans. No schedule-preview ownership. | W2-FR-27–28; US5.5–7 |
| B-06 | D-04, D-05, D-09, B-01 | Owner/live operational_manager pricing/Follow-up policy, reasoned discounts/free non-Urgent, final-only half-up EGP and valid Urgent base/net; policy race and frozen snapshot proof. | W2-FR-03–07; US2 |
| B-07 | D-04, D-05, D-09, B-01 | Owner/live operational_manager calendar/overrides/breaks/holidays and explicit cutoff configuration; affected Open/timed preview, fresh activation recheck and booking race. | W2-FR-08–11; US3.1–3/7–9 |
| B-08 | B-07, D-05 | Valid manual/generated half-open candidates, Cairo gap/fold handling and true simultaneous capacity; commit-time rescan. | W2-FR-12–14; US3.4–6/8 |
| B-09 | D-05, D-09, B-05, B-06, B-07, B-08, B-01 | Active patient → Routine New Examination, Booked only; unique patient/day and number, snapshot/history/receipt/outbox together; retry/archive/slot races. | W2-FR-01–02/15–16/29; US1 |
| B-10 | D-07, D-08, B-06, B-09 | All-staff actual-actor decisions/extensions and qualification without a Doctor decision; mandatory non-default/extension reasons, Cairo dates, Doctor precedence on all Receptionist paths, frozen claims and safe fallback. | W2-FR-17–22/29; US4 |

W2-FR-01–02/28–29 apply across relevant owners. **W2-M1** remains date, queue, Visit and claim persistence constraints; **W2-M2** remains policy/slot/number/Follow-up booking behavior. These are not extra dependencies. D-06 must be verifiable without D-07; D-07 without D-06; B-06/B-07 without D-11; B-09 without D-08/B-10. One writer owns the migration chain, but serialization of editing is not a new master dependency.

W2-FR-17–22 explicitly share D-08 persistence and B-10 behavior ownership, not duplicate command ownership. Pre-task corrections apply within existing owners: B-06 both-role operational quotes, zero-base/free semantics and stale-price denial; B-07/B-08 mode/slot/duration/overlap previews preserving prior intervals; B-09 current-candidate/date guards; B-10 acknowledged fallback with reaffirmed discount. Shared receipts cover every exposed Wave 2 mutation. No added route, UI, queue-policy/Doctor-capacity command or operational fixture surface is authorized.

CHK006 clarification: B-07/B-08 use saved examination capacity directly for enabled overlap; enabling requires configured Exact-Time slots and explicit confirmation by the live authorized settings actor of its current integer value >=2 and revision. No separate booking limit/additional ceiling or CAPACITY write is introduced. B-14 remains the capacity-changing owner; later admission still needs its own live count and per-case Doctor confirmations.

CHK007 clarification: B-08 generates full duration-sized intervals on a grid anchored to each applicable work-period start; breaks suppress intersecting slots without restarting the grid, and trailing remainders are discarded. Manual precedence and all existing interval/commit-time guards remain unchanged; no new generation option is introduced.

CHK008 clarification: B-07/B-08 reuse weekly/date-keyed schedule configuration for recurring manual slots plus explicit date-specific replacement sets. Presence replaces the weekly manual set for that date; absence inherits it, with no union, generated-grid reset or implicit opening. Existing guards and affected-booking review apply to both kinds of edit.

CHK013 retained lower bound/range: D-08/B-10 accept a specific justified source deadline on/after source Cairo completion, including below day seven or beyond it without a maximum duration. Source edits cannot reduce frozen confirmed entitlement; confirmed-target extensions remain separate prior non-shortening authorizations.

**Active approved amendments (2026-11-04, Issues #24/#25):** PRD BR-036 fixes the operational_manager settings allowlist, with Owner-only all-Receptionist accounts/capacity and Doctor-only Pause/case approvals/clinical content. B-06/B-07 apply their existing subset with live grant/revocation checks. Fresh claims use current clinic policy, initially seven inclusive Cairo days, without requiring a Doctor decision. All staff may record DEFAULT/OPTED_OUT/DEADLINE (non-default reason mandatory, DEADLINE date ≥ completion) and revise Receptionist-origin decisions with immutable before/after evidence. Genuine Doctor restrictions are revised only by authenticated Doctor and checked on every Receptionist decision/NEW-date/confirmed-extension path. NEW-date reason remains optional for Receptionist, mandatory for Doctor, audit mandatory for both. Confirmed extensions require mandatory reason, specific later/non-shortening Cairo date and append-only actual-actor evidence, preserving original claim/price and no inheritance after release. Completion support requires positive actual-exam attestation without Doctor impersonation; B-16 owns the command. Prior checked tasks/CI prove none of these amendments.

CHK014 clarification: D-08/B-10 qualify intended dates from Cairo completion day zero through the authorized deadline inclusive, never earlier. The source must already be Completed; all ordinary guards remain, including counted uniqueness by Appointment service date. A source finishing after midnight retains its older original service date, which does not itself occupy the completion day's allowance. No source date rewrite, clinical disclosure or later-wave workflow is added.

CHK002 clarification: B-08/B-09/B-10 impose no maximum future booking horizon in date availability or new Open/Exact-Time confirmation. Resolve the requested date through existing calendar/slot guards; no horizon setting or advance materialization of all future slots is added. Preferential qualification still enforces its authorized deadline, and nonqualifying fallback still requires ordinary-type/price acknowledgment and discount reaffirmation.

CHK011 clarification: B-06 accepts finite percentage decimal strings in [0,100] without an additional fractional-digit cap, subject to existing bounded-request safeguards. Preserve the full accepted value through quote, calculation and immutable snapshot; do not apply two-digit EGP scale to percentages or rely on a fixed arithmetic precision that loses accepted digits. Retain exact intermediates and round only final net. Reuse D-05's unconstrained NUMERIC discount field, with focused compatibility/round-trip and high-precision boundary proof, not a new storage engine or monetary policy.

## Reuse and Delivery Boundaries

| Existing contract / evidence | Wave 2 use and affected consumers |
| --- | --- |
| D-04 policy kinds/heads/versions, service-day revisions, immutable previews; [Wave 1 data model](../002-wave-1-security-core/data-model.md#d-04--policy-versions-and-schedule-substrate) | B-06/B-07 extend valid policy publication; B-08/B-09 share calendar/date serialization. Preserve initialization for existing and future tenants, pending queue effectiveness and history. Do not recreate policy storage. |
| D-05 Appointment/snapshot/counter/assignments/history | Reuse existing metadata and atomic number allocation; retain counted unique index, immutable numbers/snapshot and tenant/patient/Doctor associations. Fixture-oriented `persist_appointment` is not a public booking validator. |
| B-01/D-09; [Wave 1 common protocol](../002-wave-1-security-core/contracts/http-and-operator.md#common-protocol) | Reuse signed purpose allowlists, live transaction authorization, receipt store, errors and post-commit intent. Never serve receipts before reauthorization or confuse a fresh transport nonce with a booking idempotency key. |
| B-05 lifecycle lock and [archive contract](../002-wave-1-security-core/contracts/http-and-operator.md#b-05--patient-registry) | New ordinary/preferential IDs require non-archived patient; archive cannot cancel existing IDs/history or grant Receptionist general archived/clinical access. Family identity stays ID-based. |
| [Wave 1 task evidence](../002-wave-1-security-core/tasks.md) and [verification records](../002-wave-1-security-core/quickstart.md) | D-04 CI #40374511116 (`5ffb3e9`), D-05 #40388474059 (`34b8f72`), B-05 #40353921541 (`95f8fa2`) and B-01/B-02/B-04 records are prerequisite evidence, not proof of new Wave 2 code. Verify assigned dependencies and executable ancestry before future implementation. |

**Wave 3 remains B-11–20**: immediate intake, check-in, queue projections/placement, queue-policy mutation, Pause/capacity/confirmation commands, admission, general clinical authoring/completion/exceptional cancellation, individual cancel/replacement/reschedule, bulk and dated reconciliation/No-show. Wave 2 uses valid synthetic states/internal persistence checks, not shipped shortcuts. B-10 exposes only the approved eligibility/extension slice on a valid source Visit. D-08 foundation release/consumption must be proved in this Wave; B-16/B-17/B-18/B-20 later integrate their actual transitions. A conflicting schedule remains blocked if its resolution needs an unavailable Wave 3 command.

**Wave 4 remains D-10/D-12/B-21–27**: files, combined assurance, notification delivery/SSE/push, worker scheduling and reports. Persist existing D-09 commit-time intent now; do not dispatch it. D-11 adds query foundations, not metrics/report routes. **Wave 5 remains U-01–12**; no UI is planned here. U-03 still needs U-01/U-02/B-07–10 and explicit authorization, with no added Wave 2 master.

## Design and Verification Handoff

- [research.md](research.md): decisions, rationale and rejected alternatives; no production policy selection.
- [data-model.md](data-model.md): reused/new entities, validation, lifecycle and non-destructive upgrade design.
- [HTTP contracts](contracts/http.md): only B-06–10 surfaces, role-safe DTOs and errors.
- [transaction contracts](contracts/transactions.md): serialization, receipts, denial/unknown outcomes and foundation-only proof boundaries.
- [quickstart.md](quickstart.md): runnable existing entrypoints plus explicit future suite-wiring and CI evidence requirements; each master has positive, denial and applicable race/upgrade checks.

All new migrations must be exercised on representative rows at their **immediately preceding revision**, as well as empty install/repeated upgrade. Verify retained IDs/FKs/history, forced RLS, actual runtime/operator grants and failure rollback/version state. Do not defer these Wave 2 checks to Issue #13 or D-12. Update affected metadata, compatible-head readiness in API/Web/worker, import/route assertions, fixtures and CI suite registration together; never weaken a guard to get green.

## Material Deferred Obligations

Latest open-Issue inventory reviewed on 2026-11-04; historical #19 disposition retained. Production/upgrade Issues #2/#4/#5/#13/#18 remain separate open obligations. All Product Owner decisions tracked by #19 are recorded locally; the independent requirements review is complete and #19 was closed as completed. That closure is not implementation authorization and does not resolve any production gate.

| Issue | Responsible role / review trigger | Required closure boundary |
| --- | --- | --- |
| [#2](https://github.com/ahmedali5696/care-pulse-3/issues/2) | Product Owner + Security/Operations; production ingress review | Approve trusted login-source/NAT/proxy policy and verify spoof-resistant deployment throttling. |
| [#4](https://github.com/ahmedali5696/care-pulse-3/issues/4) | Engineering/Security + Product Owner; dependency review/before release | Current audit/exposure evidence and approved remediation or risk treatment; do not silently change Next pin. |
| [#5](https://github.com/ahmedali5696/care-pulse-3/issues/5) | Product Owner + Security/Operations + Engineering; before production | Approved operator/key custody, SMTP/retry/retention policy and executable operational evidence. |
| [#13](https://github.com/ahmedali5696/care-pulse-3/issues/13) | Migration lead + Product Owner/Security/Operations; populated upgrade and D-12 | Historical intermediate populated-upgrade evidence, retained records/privileges and safe failure. Separate from mandatory new Wave 2 predecessor-upgrade proofs. |
| [#18](https://github.com/ahmedali5696/care-pulse-3/issues/18) | Canonical gate owners in master §§2/14; D-06 expiry planning and before production | Approved security numerics including confirmation expiry; signed OPS/file decisions and recovery/security/legal evidence. No durations or legal conclusions chosen here. |
| [#19](https://github.com/ahmedali5696/care-pulse-3/issues/19) — CLOSED, completed requirements gate | Independent reviewer with Product Owner/Engineering; reviewed 2026-10-28 | All seven originally tracked decisions are resolved; independent review verified 43/43 booking and 16/16 quality criteria, 12 answers, nine masters/30 edges and document consistency. Closure completes this historical requirements gate only; it does not certify November requirements. |
| [#24](https://github.com/ahmedali5696/care-pulse-3/issues/24) — OPEN | Domain/database/API engineering + PO; before amended D-07/D-08/B-10 implementation and B-18 integration | Reviewed actual-actor DTO/evidence/locks and forward-only upgrade, authorized implementation, exact-SHA PostgreSQL positive/denial/race evidence, independent review and PO acceptance. Product reason/date/precedence choices are settled. |
| [#25](https://github.com/ahmedali5696/care-pulse-3/issues/25) — OPEN | Identity/security/domain engineering + PO; before delegated settings or administrative completion integration | Live capability/revision/receipt authorization, fixed settings versus Owner/Doctor denials, positive actual-exam evidence and cross-wave verification; no identity/clinical impersonation. |

Immediate implementation blockers would be a failed assigned prerequisite, a violated invariant, missing focused CI evidence or an unresolved material technical conflict. Production-only open obligations remain release/capability restrictions, not new master edges. Explicit task assignment is still required even when dependencies are ready.

## Project Structure

### Documentation (this feature)

```text
specs/003-wave-2-booking-scheduling/
  spec.md                       # existing, preserved
  checklists/                   # existing, preserved
  plan.md
  research.md
  data-model.md
  quickstart.md
  contracts/
    http.md
    transactions.md
```

`tasks.md` is the existing T001–T120 baseline. This alignment preserves its IDs, master ownership, 30 edges and all status markers; historical checkmarks do not certify amended wording.

### Source Code (existing layout; future affected areas only)

```text
apps/api/carepulse/
  db.py, health.py, main.py
  identity/, patients/, policies/, appointments/, queue/, visits/, notifications/
apps/web/auth/proxy.ts
apps/web/app/api/                  # explicit forwarding only
apps/web/health.ts
apps/worker/                      # shared readiness/imports, not workflow scheduling
db/migrations/versions/            # additive master-owned revisions
db/roles.sql                      # only if actual narrow grant changes are needed
tests/unit/
tests/integration/
ops/check.ps1
.github/workflows/foundation.yml
```

**Structure Decision**: Extend existing domain modules and checks. Keep Follow-up logic with its source Visit/booking owners in the existing Python application; no standalone service, duplicate receipt engine or new framework. This planning pass changes only the native artifacts above.

## Complexity Tracking

No Constitution exception requested. Deferred relational checks, source serialization, a shared Doctor control row and a schedule publication barrier implement already-required integrity, not speculative flexibility. Reuse installed standard-library/database capabilities and existing substrate; no policy or architecture amendment is needed.

## Historical Native Workflow Completion Record — 2026-10-28

Completed Phase 0 research and Phase 1 design on 2026-10-28. Standard-library document checks passed for all six generated artifacts: existence, local links/anchors, fenced blocks, no unresolved placeholders and whitespace. Independently parsed master §7 and verified the plan/spec contain exactly its nine IDs and direct dependency sets. `git diff --check` passed; new untracked artifacts were checked directly as well. Initial document-check range parsing was corrected for Windows shell Unicode encoding; no master dependency or authority was changed.

`.specify/extensions.yml` was absent at both pre-plan and mandatory post-plan checks, so no hooks were registered for dispatch. Existing spec/checklist and all approved authorities, implementation files and Wave 1 evidence were preserved. No `tasks.md` was generated. Application tests, migrations and Compose/PostgreSQL CI were intentionally not executed for this documentation-only scope; new Wave 2 behavior remains unimplemented/unverified. Relevant open Issues and production restrictions are recorded above; none is a blocker to this planning completion.

## November alignment traceability and pending engineering review

| Approved contract | Canonical anchors | Wave 2 allocation / existing tasks |
| --- | --- | --- |
| Fixed manager scope, live revocation, retained Owner/Doctor boundaries | PRD §3, FR-005/071/091, BR-036, AC-02; Domain INV-01/19/20/26; System Design §2; master §7 B-06/B-07 | W2-FR-01/03/08/10/29; B-06 T009–T018, B-07 T023–T034. Capability persistence/security integration remains Wave 1 D-02/B-01/B-04. |
| No-Doctor-decision eligibility; actual-actor source decisions and Receptionist-origin revisions | FR-009a/c/067, BR-002a, AC-06/27; Domain §8/INV-10 and November CON-10/11/16/22 obligations; master D-07/D-08/B-10 | W2-FR-17–20/26; D-07 T057–T066, D-08 T067–T077, B-10 T078–T093. Clinical content/corrections remain Doctor-only. |
| Separate NEW-date exception and confirmed extension | FR-009c, BR-002a/c/025, AC-06/27/28; Domain §8/CON-16/22; master B-10/§11 | W2-FR-18/20/21; D-08 T067–T077 and B-10 T079–T093; B-18 later moves with fresh slot/day/claim checks. |
| Positive completion provenance | FR-034, BR-001/007/030, AC-04/05; Domain §§7/10, INV-05/15/23; master D-07/D-08 | W2-FR-26; US5.8; D-07 T057–T066 and D-08 T068/T072–T075 foundation support. B-16 retains actual completion. |
| Safe forward-only evidence evolution | Domain §8/12.3; System Design §3; master §§11.4/12 | W2-FR-28/29; migration/privilege/upgrade tasks within existing masters; never edit published 0021/0022. |

These are **MAJOR engineering review gates for affected implementation**, not unresolved Product Owner permission choices or blockers to this documentation handoff. Existing Issues #24/#25 already track them; do not create duplicate Issues or close either here.

| Ref / risk | Concrete review boundary and verifiable closure |
| --- | --- |
| M1 — live delegation/security integration (#25) | Wave 1 D-02/B-01/B-04 owners must review capability storage and grant/revoke security-revision invalidation against the existing authorization barrier, signed identity, previews and receipt recovery. Closure: ordinary/revoked/disabled/foreign/stale-grant settings denials, both serialized grant races and preserved Owner-only accounts/capacity/Doctor-only controls. No new master edge is silently introduced; review any necessary task correction before implementation. |
| M2 — legacy evidence and forward-only guard evolution (#24/#25) | D-07/D-08 database/security owners must review the proposed separate operational evidence/source revision and upgrade from 0021/0022: genuine legacy Doctor restriction mapping, existing Completed sources without new attestation, retained clinical history and frozen claims. Do not infer positive attestation/Doctor authorship from a staff role or fabricate backfill. Preserve already confirmed evidence; incompatible or unverifiable new qualification fails closed pending reviewed remediation. Closure: approved migration design and representative populated immediate-predecessor, fresh/repeat/failure, RLS/grant and provenance tests at exact SHA. |
| M3 — wire/projection/revision compatibility (#24) | B-10/API/security owners must review the operation-specific proposal in HTTP §6.1: replace clinical_revision as operational source token, permitted decision-control projection, actual actor derivation, mandatory reasons/dates, distinct extension chain and old signed caller/receipt compatibility. Closure: concrete DTO/storage/lock review with no clinical disclosure, source/policy/claim/Visit and restriction/extension/release races, safe stale/conflicting/unknown-outcome behavior and exact-SHA PostgreSQL evidence. |

Downstream alignment is identified only: **Wave 1** D-02/B-01/B-04/B-05 capability/live authorization, role-safe projections and all-staff archive; **Wave 3** B-13–16/B-18–20 settings/control boundaries, actual completion and extension-aware reschedule, all-staff non-head/bulk; **Wave 4** D-10/D-12/B-21–23/B-26/B-27 export/files, actor-safe audit/events/metrics and integrated proof; **Wave 5** U-02–06/U-08–12 operational controls, delegated settings and bilingual/accessibility/E2E. No such artifact or implementation is changed here. Production Issues #2/#4/#5/#13/#18 remain separate release/populated-upgrade obligations.
