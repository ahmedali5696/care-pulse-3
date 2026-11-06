# Implementation Plan: CarePulse 3 Wave 2 — Booking & Scheduling

**Git branch**: `feature/wave-2-booking-and-scheduling` | **Date**: 2026-10-28 | **Spec**: [spec.md](spec.md)

**Spec Kit identifier**: `003-wave-2-booking-scheduling` (setup's `BRANCH` result; not a Git branch rename).

**Status (final documentation consolidation, 2026-11-06):** The completed 13-decision review is historical; the [final proposed engineering baseline](m1-m3-technical-design-proposal.md#current-decision-baseline--final-proposed-engineering-disposition-2026-11-06) now records the preferred decisions and objective G1–G5 gates. Product rules are approved; independent Security/Clinical acceptance, verified physical DDL, implementation authorization, signed/PostgreSQL proof and populated adoption remain pending. **T121–T124 remain verified/checked; T125–T139 remain pending.** The recorded synthetic 0023→0024 preservation and T124 CI evidence in [quickstart](quickstart.md) retains its exact-SHA scope. #24/#25/#26 stay OPEN; no implementation, migration, commit, push or release is authorized.

**Historical pre-task requirements review (2026-10-28; not November acceptance)**: Twelve explicit [Product Owner clarifications](spec.md#clarifications) preserve the prior ten answers and add only CHK002/CHK011: no maximum booking horizon and no percentage fractional-digit cap, with existing guards retained. [Booking review](checklists/booking-review.md) records 43/43 booking criteria and 16/16 specification-quality criteria. Independent documentation review found no demonstrated BLOCKER or MAJOR finding; Issue #19 was closed as completed. This clears the requirements-review condition only. `/speckit.tasks`, implementation, commits, deployment and production release still require separate authorization; no tasks were generated in this scope. Preserve the nine masters, edges and Wave boundaries below.

## Summary

Plan exactly **D-06, D-07, D-08, D-11, B-06, B-07, B-08, B-09 and B-10** from [master plan §7](../../docs/implementation-plan.md). Extend the existing Modular Monolith with tenant-isolated queue/Visit/claim/day-review persistence, Owner/live operational_manager pricing/calendar APIs, valid Exact-Time candidates, ordinary Booked-only Routine creation and qualified preferential Follow-up. Preserve the existing policy, Appointment, numbering, frozen-price, patient lifecycle, authorization and audit/receipt contracts.

Authority, descending: [PRD](../../docs/CarePulse_PRD_v1_APPROVED.md), [Domain Model](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md), [System Design](../../docs/CarePulse_System_Design_v1_APPROVED%281%29.md), [master implementation plan](../../docs/implementation-plan.md). The [constitution v1.1.0](../../.specify/memory/constitution.md) governs engineering within them. Full master task text and the spec's authority/acceptance mapping remain binding; this document is not a replacement master inventory.

## Technical Context

**Language/Version**: Existing standard-GIL CPython 3.13.16; PostgreSQL SQL; TypeScript 5.9.3 on Node 24.22.1 for guarded BFF forwarding only.

**Primary Dependencies**: Existing FastAPI 0.158.1, SQLAlchemy Core 2.0.55, psycopg 3.3.6, Alembic 1.20.0; Python standard-library Decimal/zoneinfo. Preserve Next.js 15.5.28, React 19.3.1, Better Auth 1.7.12, pnpm 10.34.7 and committed locks. No new dependency, framework, service, broker or cache is needed.

**Storage**: Existing PostgreSQL 17.11; `0018` was the historical generation head. Published `0021`/`0022` remain immutable; `0023_m1_operational_manager` implements M1 capability and **`0024_b06_followup_window` is the current head** (T123). Any subsequent D-07/D-08 guard/storage changes require reviewed forward-only revisions **after `0024`**, retaining its current-policy/version, lock ordering and immutable historical-claim guarantees. Migration-only DDL; non-owner NOBYPASSRLS runtime, metadata-only operator, FORCE RLS and tenant-qualified relationships retained.

**Testing**: Existing pytest 9.1.1/Ruff, deterministic unit checks and real concurrent PostgreSQL transactions. Docker-capable CI owns Compose, migrations, effective privileges, RLS, pool reuse, races and affected BFF integration. Documentation-only planning receives document checks, not application acceptance tests.

**Target Platform**: Existing private Linux Compose topology; Windows workstation uses container-free checks only. Docker must not be installed or required locally.

**Project Type**: Modular web application. FastAPI owns business logic; Next BFF validates actual sessions/Origin/CSRF and signs explicitly allowlisted private requests; worker imports the same domain modules when its later owning tasks are authorized.

**Performance Goals**: No approved numeric latency, throughput or clinic-volume targets. Short ordered transactions, tenant-first bounded queries and representative PostgreSQL query-plan checks; optimize only demonstrated problems.

**Constraints**: One Doctor/Owner and one branch per clinic, fixed EGP/Africa/Cairo, exact six Appointment states; immutable confirmed money/entitlement/history. Open has no clock time or implicit day cap. No local Docker, production policy guesses, network work under business locks or frontend business-rule duplication.

**Scale/Scope**: Nine unchanged Wave 2 masters; at least two isolated synthetic clinics and both staff roles for verification. D-06/D-07/D-08/D-11 are foundations, not operational queue/clinical/reporting delivery.

**Research disposition**: [research](research.md) retains prior evidence; the [single current decision summary](m1-m3-technical-design-proposal.md#current-decision-baseline--final-proposed-engineering-disposition-2026-11-06) selects the conservative proposed engineering baseline. The 13 reviews are complete and are not repeated. Future work supplies the named proof gates for those choices, rather than competing speculative designs. Clinical adoption, Security/PO #26 disposition, physical/executable acceptance and production values remain open.

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

**2026-11-05 evidence qualification, updated 2026-11-06:** The preceding last sentence refers to **original T001–T120 historical checkmarks/CI**. Separately checked **T121–T123** prove their M1 prerequisite, live pricing authorization and configurable default-window behavior; subsequent [T123 predecessor preservation](quickstart.md) and [T124 acceptance](quickstart.md) prove their bounded synthetic migration and B-06 pricing/claim cases. **T121–T124 are now verified**, with T125–T139 pending. These proofs do not verify M2's no-Doctor-decision, actual-actor evidence/attestation, populated-clinic clinical provenance, or M3 wire evolution.

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

Historical Issue inventory reviewed on 2026-11-04, with the 2026-11-05 documented [Issue #26](https://github.com/ahmedali5696/care-pulse-3/issues/26) M1 security-sign-off boundary. Production/upgrade Issues #2/#4/#5/#13/#18 remain separate obligations. All Product Owner decisions tracked by #19 are recorded locally and #19 was closed as completed; neither closure nor this documentation reconciliation grants implementation or release approval.

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
| [#26](https://github.com/ahmedali5696/care-pulse-3/issues/26) — OPEN | DB/identity security reviewers and Product Owner; before M1 security sign-off | Assess arbitrary shared-runtime SQL and caller-set GUC impersonation threat versus signed live service boundary; approve an evidenced trust model and residual risk treatment. |

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

These are **MAJOR engineering review gates for affected implementation**, not unresolved Product Owner policy choices or blockers to this documentation handoff. Issues #24/#25 track broader November acceptance; #26 tracks M1's DB trust limitation. [The technical register](m1-m3-technical-design-proposal.md) distinguishes observed evidence and proposed schema/DTO/locks. Do not create duplicate Issues or close any here.

| Ref / risk | Concrete review boundary and verifiable closure |
| --- | --- |
| M1 — capability verified; security/Manager acceptance still OPEN (#25/#26) | Wave 1 `0023` delivers live Manager capability. T121 rechecked READY ([CI #41911450349](https://github.com/ahmedali5696/care-pulse-3/actions/runs/41911450349)); T122 live pricing authorization ([CI #41918795425](https://github.com/ahmedali5696/care-pulse-3/actions/runs/41918795425)), T123 configurable policy with `0024` ([CI #41923245126](https://github.com/ahmedali5696/care-pulse-3/actions/runs/41923245126)) and T124 B-06 signed Manager/policy, revocation/race and receipt acceptance ([commit `10ab13356`](https://github.com/ahmedali5696/care-pulse-3/commit/10ab13356b7d98d05655be3d7e5339542a057fb4), [CI #42098211721](https://github.com/ahmedali5696/care-pulse-3/actions/runs/42098211721), **299 core + 54 pricing PostgreSQL passed**) are verified. **T125–T127 pending**; #26 still requires security sign-off for caller-set GUCs. Do not implement `0023` again or claim Manager/B-07 fully accepted. |
| M2 — baseline documented; independent acceptance pending (#24/#25/#26) | Prefer 0024 → standalone D-07 → D-08, one attestation relation, Option A source/events with existing claim serialization, preserved Doctor ancestry and synthetic-only A. Prefer populated strict quarantine B without fabricated backfill. [G2/G3](m1-m3-technical-design-proposal.md#current-decision-baseline--final-proposed-engineering-disposition-2026-11-06) require exact successor SQL/ACL review, old-writer fencing and real PostgreSQL fresh/repeat/failure/synthetic-populated immediate-predecessor preservation/actor/ledger proof. G1 inventory and measured-impact Clinical/Security sign-off plus PO adoption confirmation apply **before populated adoption**, not synthetic design or D-06. Existing synthetic 0023→0024 proof remains complete in quickstart and does not prove successors. |
| M3 — baseline documented; signed/PostgreSQL acceptance pending (#24/#26) | [HTTP §§6.1–6.2](contracts/http.md#61-operation-specific-dto-and-projection-proposal--engineering-review-pending-m3) selects explicit operation-specific tokens, distinct v1 clinical/v2 operational semantics, strict dispatch/common source-free booking, original raw/typed digests, committed-only legacy replay, operation-specific result revisions and denial of overbroad cached results. [Transactions §1](contracts/transactions.md#1-shared-security-and-commit-boundary) retains stable existing-Appointment order and 0024 policy-before-source; **trigger/FK deadlock safety unverified**. G3/G4 name owners and exact signed/field/receipt/both-start-order PostgreSQL proof. No implementation approval. |

Downstream alignment is identified only: **Wave 1** D-02/B-01/B-04/B-05 capability/live authorization, role-safe projections and all-staff archive; **Wave 3** B-13–16/B-18–20 settings/control boundaries, actual completion and extension-aware reschedule, all-staff non-head/bulk; **Wave 4** D-10/D-12/B-21–23/B-26/B-27 export/files, actor-safe audit/events/metrics and integrated proof; **Wave 5** U-02–06/U-08–12 operational controls, delegated settings and bilingual/accessibility/E2E. No such artifact or implementation is changed here. Production Issues #2/#4/#5/#13/#18 remain separate release/populated-upgrade obligations.

**Resumption and pending acceptance (not authorized here):** **D-06/T094 is the first pending Phase 7 task**, dependency-ready on completed **D-04/D-05** for separate authorization; it does not wait for M2/M3, inventory or T125–T139. **D-11 still depends on D-04/D-05/D-06/D-07**, including applicable T128–T130 acceptance. The 13 reviews and current contract reconciliation are complete as documentation; do not rerun them as a pending stage. Apply the [final proposed baseline and G1–G5](m1-m3-technical-design-proposal.md#current-decision-baseline--final-proposed-engineering-disposition-2026-11-06): independent Security/Clinical acceptance, implementation authorization, SQL/effective grants/signed/PostgreSQL evidence remain pending, with G1 populated adoption and production gates separately open. Preserve all task markers and exit obligations; no automatic Spec Kit regeneration or implementation follows.
