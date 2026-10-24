# Implementation Plan: Wave 1 — Security & Core Data

**Git branch**: `feature/wave-1-security-&-core-data` | **Date**: 2026-10-23 | **Spec**: [spec.md](spec.md)

**Spec Kit feature identifier**: `002-wave-1-security-core` (the setup script's `BRANCH` value; not a Git branch rename).

**Status**: Phase 0 research and Phase 1 design for synthetic data; U1/I1/U2/U3 task reconciliation is complete. These artifacts grant no implementation authorization. No master task is implemented, tested or integration-verified by them.

## Summary

Deliver exactly D-01–D-05, D-09 and B-01–B-05 from [the approved implementation plan §6](../../docs/implementation-plan.md): tenant-isolated persistence, real invited authentication, signed BFF requests, immediate revocation, restricted operator provisioning, Receptionist lifecycle/recovery, and patient registration/search/archive. Policy and Appointment work is persistence only. Preserve completed Wave 0, the Modular Monolith, pinned dependencies and private topology.

Authority, descending: [PRD v1](../../docs/CarePulse_PRD_v1_APPROVED.md), [Domain v1.2](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md), [System Design v1.0](../../docs/CarePulse_System_Design_v1_APPROVED%281%29.md), [master plan](../../docs/implementation-plan.md). [Constitution v1.1.0](../../.specify/memory/constitution.md) governs execution. These feature artifacts elaborate implementation choices, not new product approval or a second master inventory. The full task text and references in master §6 remain binding.

The 2026-10-21 SECURITY-POLICY-TIMING-VARIANCE permits labeled synthetic-data development/testing only. SECURITY-POLICY-NUMERICS and all applicable release gates remain OPEN. Missing required production policy fails closed; neither framework defaults nor test fixtures authorize real-user/patient operation.

## Technical Context

**Language/Version**: Standard-GIL CPython 3.13.16; TypeScript 5.9.3 on Node 24.22.1; PostgreSQL SQL.

**Primary Dependencies**: Existing Next.js 15.5.28, React 19.3.1, Better Auth 1.7.12, pg 8.23.1, Nodemailer 10.1.7; FastAPI 0.158.1, SQLAlchemy Core 2.0.55, psycopg 3.3.6, Alembic 1.20.0. pnpm 10.34.7 and committed locks remain authoritative. No existing pin is upgraded. U3 selects one deliberate addition, PyCA `cryptography`, to avoid Node/sealing subprocesses in Python issuer images; exact compatible pin, binary-wheel/hash-locked closure and advisory review are implementation gates, not edits in this pass. The approved narrow Playwright journey likewise requires an exact compatible pin and reviewed lock update during implementation; neither addition is installed here.

**Storage**: One PostgreSQL 17.11 database. Migration-owned DDL, non-owner NOBYPASSRLS runtime, metadata-only operator. Better Auth owns its supported session/credential persistence; application metadata augments authorization, not session authentication. Clinical tenant tables use FORCE RLS, non-null tenant identity and composite tenant foreign keys.

**Testing**: Existing pytest/Ruff and TypeScript/ESLint/Next build; real PostgreSQL isolation/parallel-transaction checks and Better Auth+BFF integration in Docker-capable CI; one focused first-Owner invite/login Playwright smoke. Deterministic clocks and synthetic identities. No local Docker.

**Target Platform / Project Type**: Existing private Compose web/API/Python-worker modular web application, plus restricted one-shot operator CLI. Mailpit for synthetic delivery. No new long-running service, public operator endpoint, broker or cache.

**Performance Goals**: No numeric target or clinic-volume commitment is authorized. Tenant-indexed, paginated queries; short, consistently ordered transactions; no network work under business locks; measure before optimizing.

**Constraints**: FastAPI owns product authorization/business commands; BFF owns Better Auth, cookie/Origin/CSRF, authentication integration and signing. Supported adapter extensions may coordinate nonclinical auth metadata; they do not duplicate patient/provisioning business rules. Existing production denial and secret hygiene remain in force.

**Scale/Scope**: Exactly 11 master tasks, one Doctor/Owner and one physical branch per clinic; Egypt, `Africa/Cairo` and EGP fixed. Synthetic tests use at least two clinics and both staff roles. No final UI, booking commands, queue/Visit/Follow-up consumers, files, reports, general worker scheduling or realtime delivery.

**Research disposition**: Planning questions and source interpretations are resolved in [research.md](research.md). Runtime proof obligations are acceptance gates, not claims of already-working code. If the selected supported auth integration cannot satisfy its transaction contract, affected work stops with ERROR/BLOCKED; it must not silently substitute a weaker flow or unsupported library patch.

**Refinement scope**: U1 covers reset reservation/auth completion/finalization, competing/failure outcomes, and the revocation-invalidated `AUTH_DONE` → reactivated eligible Owner → distinct-proof replacement deadlock; I1 separates B-05 backend delivery from deferred browser integration; U2 adds abandoned-claim recovery/fencing to the existing one-shot path; U3 evaluates the complete encryption/issuance/delivery arrangement rather than the cipher alone. The completed task reconciliation reflects these decisions without changing approved product requirements, architecture, master scope, direct dependencies, authorities, locks or implementation files.

## Constitution Check

| Gate | Before research | After design | Evidence / restriction |
| --- | --- | --- | --- |
| I — smallest approved architecture | PASS | PASS | Reuse modules/DB/adapter/transport; U3 compares whole-path alternatives and selects direct token OAEP plus one crypto dependency over hybrid envelopes and Node issuer subprocesses. No duplicate engine/service. |
| II — security and integrity | PASS (design scope) | PASS (design scope) | RLS/nonce/live revision/projections/lock order/history retained; U1 binds/fences reset retries/finalizers and U2 fences abandoned claims. Public-only issuance and sender-only private custody remain. Executable proof required. |
| III — pragmatic performance | PASS | PASS | Indexed tenant access, bounded pages, no network work inside critical locks; no invented targets. |
| IV — code-first focused evidence | PASS | PASS | Implementation then focused tests; CI owns PostgreSQL/Compose/E2E. This documentation change receives document checks only. |
| V — scope and execution | PASS | PASS | Same 11 master IDs/direct edges and 80 reconciled subtasks; authorities and Wave 0 untouched. No implementation. |
| Synthetic-policy authorization | PASS | PASS | Authorized variance used only for labeled synthetic fixtures; no production numbers selected. |
| Production readiness | BLOCKED / OPEN | BLOCKED / OPEN | SECURITY-POLICY-NUMERICS, OPEN-OPS-01/02/03, OPEN-FILES-01 and master §14 evidence remain unresolved. |

PASS here means a planning/design gate, never implementation acceptance. Any unjustified authority violation or material unresolved synthetic-design clarification is an **ERROR**: stop the affected phase and obtain the relevant decision. Production gates are explicitly deferred by existing authorization, not waived or relabeled passed.

## Exact Scope, Dependencies and Delivery Boundaries

The following are the existing master tasks, not new task generation. Coverage labels refer to [spec.md](spec.md), which retains their FR/BR/INV/AC references.

| Task | Exact direct dependencies | Planned change and focused proof |
| --- | --- | --- |
| D-01 | F-04 | Organization/operator metadata and isolation substrate; two-tenant/no-context/privilege evidence. Activation closed until D-02 and B-03. W1-FR-01, SEC-01. |
| D-02 | D-01, F-07 | Supported Better Auth schema, staff/revisions/invites/recovery metadata; partial Owner uniqueness plus deferred cross-table guard under tenant lock; replay/Owner race proof. W1-FR-02, RACE-02–04. |
| D-03 | D-01 | Patient demographics/intake/archive/lifecycle and tenant search indexes; family duplicates, valid age alternatives, tenant FK and archive lock proof. W1-FR-03, RACE-06. |
| D-04 | D-01 | Versioned policy/schedule/price and revision-bound preview substrate; exact PRD defaults, pinned versions and revision collisions. W1-FR-04, RACE-07. |
| D-09 | D-01, D-02 | Immutable audit/outbox, separate delivery jobs, nonces and the single shared receipt store; rollback, forbidden mutation and duplicate claims. W1-FR-05, RACE-08/09. |
| D-05 | D-03, D-04, D-09 | Appointment/snapshot/counter/number/history persistence; counted uniqueness, cross-lane numbering, terminal and monetary constraints. No booking endpoints. W1-FR-06, RACE-05/08. |
| B-01 | F-03, F-04, D-01, D-02, D-09 | Signed request verification, durable replay rejection, live transaction authz, projections/errors/idempotency; positive and bypass/stale/replay/rollback cases. W1-FR-07, SEC-01–05. |
| B-02 | F-07, D-02, B-01 | Real invited sessions, expiry overlay, secure BFF, logout and closed SSE boundary; CSRF/session/signature/revocation evidence. W1-FR-08. |
| B-03 | D-01, D-02, D-09, B-01, B-02, F-07 | Restricted operator CLI, verified Owner activation and single shared post-commit sender; documented operating procedure, Mailpit and invite/login E2E. W1-FR-09. |
| B-04 | D-02, D-09, B-01, B-02, B-03, F-07 | Owner-managed Receptionists, sole bulk grant and coordinated recovery; exact expiry, old-session, reset/revocation races and committed recovery email. W1-FR-10. |
| B-05 | D-03, B-01 | Patient registry/family search/role projections/archive; own-tenant positives, forbidden fields/foreign IDs and lifecycle races. W1-FR-11. |

One valid sequential order is D-01 → D-02 → D-03 → D-04 → D-09 → D-05 → B-01 → B-02 → B-03 → B-04 → B-05. This is a convenient migration-owner order, **not additional dependency edges**: D-03/D-04 may be ready after D-01; B-01/auth does not wait for D-05; B-05 does not gain a B-02/B-03/B-04 dependency. B-05's backend routes, projections and focused evidence are independently deliverable with D-03+B-01 using legitimately persisted auth fixtures through the genuine private security boundary. Browser forwarding waits for B-02's guarded proxy; invited Owner session checks wait for B-03, and invited Receptionist session checks wait for B-04/T049 (Receptionist invitation acceptance). Both are required at Wave exit. An absent BFF helper cannot gate B-05 backend completion; no browser auth bypass or new master edge is permitted. See [delivery staging](contracts/http-and-operator.md#b-05--patient-registry). Keep one writer per migration chain/shared boundary.

**W1-M1**: tenant-isolated records and audit/nonce substrate. **W1-M2**: invited identity, immediate revocation and patient operations. Neither milestone replaces any of the 11 task exit checks.

## Design and Integration Approach

1. Add incremental Alembic revisions after `0001_foundation`; never edit its historical contents. Use the installed Better Auth migration tooling to derive/review its schema and include it in migration-role application. Restore adapter schema validation only with the compatible schema. Database guards cover tenant/Owner, immutable evidence, counted records and historical numbers.
2. Extend `carepulse.db.transaction` with the shared B-01 security boundary, using READ COMMITTED and applicable master §2 lock order. Serialize sensitive operations and revocations on tenant/live-permission state, then patient lifecycle, policy/date, later capacity/lane, Appointment and evidence. No cross-process auth/SMTP call joins this transaction by assumption.
3. Keep nonclinical auth tables separate from clinical RLS paths. Add server-controlled session revision/absolute-origin/activity metadata through Better Auth's supported schema extension. Verify actual server sessions each request; compare live revisions in both BFF and API. Capture revision before credential verification so a login racing reset cannot bind old credentials to new authority.
4. Owner acceptance prepares an unusable auth identity, then the signed single-purpose activation command locks tenant/invitation and atomically consumes proof, installs verified Owner and activates the clinic. Prepared identities never authorize ordinary operations. Suspension wins according to the serialized outcome.
5. Recovery uniquely reserves each proof and at most one unresolved operation per native auth identity, with a keyed exact-request fingerprint and one security-revision bump/owned block. A guarded operation-row fence encloses supported atomic proof consumption/password/session effects and durable completion. Domain revocations invalidate that gate; idempotent finalization requires exact evidence, block ownership and unchanged relevant revisions. The existing guarded `POST /api/auth/reset-complete` BFF action invokes the existing signed private recovery-purpose coordination for both matching retries and fresh valid-proof attempts; under the normal Organization → Staff → RecoveryOperation barrier, an expired incomplete predecessor may be fenced/closed before replacement. A separate recovery branch applies to `AUTH_DONE` only when durable authorization revocation has explicitly invalidated it: after reactivation, require a distinct valid proof and current verified Owner/ACTIVE clinic, lock and recheck both completion and revocation evidence against any in-flight auth handler, then atomically close invalidated A as `ABORTED`, retain its immutable completion without success audit, reserve B and transfer reset-block ownership A→B without an unblocked interval. The replacement never repeats or acknowledges A's credential effect; B performs its own fenced credential/session invalidation and finalization with exactly one B success audit. A non-invalidated `AUTH_DONE` continues through completion-only finalization, even after proof expiry. Concurrent revocation, stale A finalizers, invalid proof or unverifiable evidence deny without clearing the block. Crash/expiry/competing-password/two-proof outcomes and this explicit reconciliation path are in [security lifecycle §5](contracts/security-lifecycle.md#5-recovery-issuance-and-durable-reset-coordination). Never use a post-reset callback or timeout as proof of success.
6. Commit invitation/recovery digests and delivery intent together. Encrypt only the bounded token using direct RSA-OAEP/SHA-256 with context label: Python crypto library for Python issuance, native Node for recovery/decryption, public keys at issuers and private keys only in the trusted sender invocation. Keep API/operator images Node-free; package the existing Node/Nodemailer modules only with the Python-owned worker/sender target, with mail-only configuration and private SMTP reachability. The shared one-shot claim/reclaim path uses expiring leases and increasing generations; stale owners cannot finish/reschedule/dispose material. It runs post-commit outside business locks; B-25 later schedules that exact path. No scheduler, new mail service or request-time SMTP. See [research §7](research.md#7-committed-mail-and-delivery-secrets) for full alternatives/cost.
7. B-05 exposes only approved patient fields; all clinical/provisioning authority remains server-owned. Own-clinic Owner may read/PATCH allowed demographics/intake on archived patients; Receptionist general archived-detail/PATCH is denied. Deliver the private backend independently, deferring browser forwarding until B-02, real invited Owner checks until B-03 and real invited Receptionist checks until B-04/T049. These are Wave-exit integration evidence obligations, not new B-05 master prerequisites. D-04/D-05 fixtures demonstrate persistence rules without making later commands or clinical relations operational.
8. D-09's **single** receipt table supports an explicitly separated pre-tenant operator-create scope: verified operator + operation + idempotency key, with a durable result pointing to the newly committed clinic. B-03 checks this scope before allocating a new tenant; clinic, invitation, job, audit and receipt commit together, including against competing creates/lost responses. Ordinary tenant/staff receipts retain their tenant-isolated scope; operators cannot browse clinical receipts.
9. D-04 owns transactional initialization of approved policy defaults regardless of delivery order: backfill clinics already present when its migration lands and install a narrowly privileged Organization-insert trigger for future clinics in that same migration. The trigger runs without direct operator policy-table grants or unrestricted RLS bypass, writing only the approved initial versions for the inserted clinic. Repeated initialization never overwrites historical/current versions; neither B-03 nor D-01 gains a D-04 dependency.

## Wave 0 Preservation and Affected Consumers

| Existing anchor | Planned adaptation; regression to retain |
| --- | --- |
| `apps/web/auth.ts`, `apps/web/db.ts`, `apps/web/app/api/backend/[...path]/route.ts` | Replace deny-only/missing-schema assumptions with legitimate invited auth; retain uncached lookup, disabled signup, private credentials, bounded failure and no bearer forwarding. |
| `apps/api/carepulse/identity/boundary.py`, `db.py`, `main.py` | Evolve service-only denial into signed live authorization; retain missing/duplicate/untrusted denial and context cleanup. Add domain routes only for B-03–05's approved surfaces. |
| `db/roles.sql`, `db/migrations/versions/0001_foundation.py` | Preserve role separation/baseline. Add narrowly scoped metadata grants and reviewed migrations; no operator clinical privileges. |
| `apps/api/carepulse/health.py`, `apps/web/health.ts` | Replace exact baseline-head expectation with explicit compatible current migration revision; health never creates schema. Worker shares Python readiness. |
| `apps/web/mail/{transport,templates,smoke}.ts`, `apps/web/config.ts` | Extend shared templates/transport for committed invitation/recovery; extract mail-only config so sender needs no BFF auth/service secrets. Retain redaction, recipient restriction, timeout/error hygiene. One sender only. |
| `tests/integration/test_database.py`, `test_health.py` | Evolve empty-schema/head assertions and failure-migration parent; retain rollback/ownership/DDL/pool-error/cancellation checks. |
| `tests/integration/test_auth_mail.py`, `tests/unit/test_boundary.py` | Replace blanket missing-login denial with allowed verified flow plus all original unauthorized denials; inspect committed/rolled-back email jobs. |
| `apps/api/Dockerfile`, `compose.yaml`, `tests/integration/test_topology.py` | Keep API/operator Python-only; package existing pinned Node/mail closure with worker/sender target and wire explicit private SMTP reachability/secret-scoped one-shot execution. Operator credentials and sender private keys never share issuer execution. No new listener/service or host-published API/DB. |
| `requirements.in`, `requirements.lock` | During authorized implementation only, review/add exact compatible Python crypto dependency and hashed binary-wheel closure without changing existing pins. No dependency changes here. |
| `ops/check.ps1`, `.github/workflows/foundation.yml`, `tests/unit/test_inputs.py` | Wire new suites into actual CI execution, update health-only import assumptions and reviewed executable fingerprints. Preserve frozen locks, exact pins, missing-suite failure and private input checks. PR/manual CI is available; current push filter does not cover this branch. |

No fixture, guard, lock fingerprint or input-integrity assertion may simply be removed to make Wave 1 green. Record scoped replacement evidence under its existing master task.

## Project Structure

### Documentation (this feature)

```text
specs/002-wave-1-security-core/
├── spec.md                         # existing; preserved
├── checklists/                     # existing; preserved
├── plan.md                         # this plan
├── research.md                     # authority/technology decisions
├── data-model.md                   # persistence design
├── contracts/
│   ├── http-and-operator.md         # approved external surfaces/projections
│   └── security-lifecycle.md        # internal trust, auth and delivery protocols
└── quickstart.md                    # validation/run guide
```

The U1/I1/U2/U3 reconciliation of `tasks.md` is complete, retaining 80 subtasks, 11 master tasks and their direct dependencies. Its B-05 subtasks distinguish independent backend acceptance from later B-02 browser forwarding, and its delivery subtasks use U3's direct-token RSA-OAEP choice in place of the superseded hybrid-envelope/Node-issuance approach. Task reconciliation does not authorize implementation.

### Source Code (existing structure; planned changes)

```text
apps/api/carepulse/
  db.py, identity/, provisioning/, patients/, policies/, appointments/, notifications/
apps/web/
  auth.ts, db.ts, config.ts, mail/, app/api/
apps/worker/main.py                 # existing runner; B-25 scheduling stays later
db/roles.sql
db/migrations/versions/             # incremental task-owned migrations
ops/carepulse-admin                 # planned B-03 one-shot entrypoint
ops/check.ps1
tests/unit/
tests/integration/
.github/workflows/foundation.yml
```

**Structure Decision**: Extend existing domain modules and check entrypoints. Keep business rules in FastAPI; use narrow auth integration in Next and reusable one-shot delivery. No generic repository or parallel subsystem.

## Verification and Handoff

[quickstart.md](quickstart.md) maps the exact task evidence to runnable existing check entrypoints and required CI additions. Real PostgreSQL races check both winners and zero partial effects. Signed nonce consumption survives domain failure, while domain changes/success audit/outbox/receipt roll back together. E2E is limited to first-Owner invitation/acceptance/login; no future UI is claimed.

Wave acceptance requires master §§6/11.2/13.3 and spec SEC-01–06/RACE-01–09/SC-001–007 for this slice. Record actual commands, commit SHA, run URL and artifacts. Missing CI proof is BLOCKED/UNVERIFIED. Completed Wave 0 is the accepted starting point; its historical evidence is not rerun or recertified by planning.

## Material Decisions and Release Gates

| Item | Disposition / responsible owner |
| --- | --- |
| Patient wording | Preserve spec authority interpretation: PRD §2 explicitly makes phone optional; FR-006 requires DOB or age+reference date. Lower-priority B-05 shorthand cannot make both age alternatives absent. No extra clinical age rule. |
| Runtime privilege wording | Read master §2's operator paragraph consistently with its explicit RLS architecture: runtime has tenant-constrained clinical grants; operator has none. No unrestricted runtime clinical access. |
| Supported auth integration | Design selected in research/contracts; D-02/B-02/B-04 must prove supported schema, transactions, epoch binding and failure reconciliation. Unsupported API dependence or inability to meet atomicity is ERROR/BLOCKED, not permission to patch Better Auth or add a session service. Escalate any material architectural change to Product Owner. |
| SECURITY-POLICY-NUMERICS | OPEN — Product Owner + Security/Operations: durations, throttles/reset conditions, freshness/skew, production email provider and evidence. No production numbers selected. |
| OPEN-OPS-01 | OPEN — Product Owner + Operations/Security: backup/retention/key custody and actual DB/file restore evidence. |
| OPEN-OPS-02 | OPEN — Product Owner + Operations: numeric RPO/RTO and demonstrated recovery. |
| OPEN-OPS-03 | OPEN — Product Owner + Egyptian legal/privacy adviser + Security/Operations: legal basis, residency, retention/access, hosting and written authorization. |
| OPEN-FILES-01 | OPEN — Product Owner + Security/Data Governance: file limits/classes/scanning/download/key policy and proof. Files remain outside Wave 1. |

These are not inferred approvals. Test-only configuration remains visibly synthetic; non-billed pilots do not bypass release gates. A new material conflict not resolved by the authority chain must be reported before implementing the affected decision.

## Complexity Tracking

No Constitution exception requested. Session authorization metadata, uniquely fenced recovery coordination, expiring job ownership and protected delivery material are necessary to meet existing security/atomicity requirements. U3 removes hybrid-envelope and issuance-process machinery; one reviewed Python crypto dependency is justified by standard secure public-key encryption without Node in issuer images. Cross-language proof and a Node child at delivery remain explicit costs of reusing F-07 transport, not speculative flexibility. No new master task, infrastructure service or product capability is authorized.
