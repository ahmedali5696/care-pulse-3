# Implementation Plan: CarePulse 3 — Wave 0: Foundation

**Branch:** `feature/wave-0-foundation` | **Date:** 2026-10-21 | **Spec:** [spec.md](spec.md)

**Feature directory / setup identifier:** `specs/001-wave-0-foundation` / `001-wave-0-foundation`. Native `setup-plan.ps1 -Json` resolved that identifier from feature metadata; the actual Git branch remains `feature/wave-0-foundation`. No branch rename or new workflow occurred.

**Status:** Phase 0 research and Phase 1 design artifacts produced. **Product Owner approved the exact R1–R4 input proposals and conditional synthetic-development recommendations on 2026-10-21.** Implementation/test evidence remains UNVERIFIED and each task still requires separate execution authorization; production gates remain OPEN. No application implementation, test execution, integration verification or deployment is claimed.

## Summary

Plan only **F-01–F-07** from [approved implementation plan](../../docs/implementation-plan.md) §5: the smallest reproducible private Next.js/FastAPI/worker foundation, fail-closed configuration, empty PostgreSQL migration/role baseline, truthful health, lean checks and a closed Better Auth/BFF/mail skeleton. Preserve the approved modular monolith and exact task dependencies. FastAPI owns domain logic and the worker imports it; Next.js owns BFF/UI/auth adaptation, not clinical rules.

Authority order remains [PRD v1](../../docs/CarePulse_PRD_v1_APPROVED.md) → [Domain Model v1.2](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md) → [System Design v1.0](../../docs/CarePulse_System_Design_v1_APPROVED%281%29.md) → [implementation plan](../../docs/implementation-plan.md), with [Constitution v1.1.0](../../.specify/memory/constitution.md) governing practice. §5 requirements/evidence remain incorporated **in full**, not replaced by this design's summaries. Applicable §§2–4/11/13.3/14 remain binding.

## Technical Context

**Language/Version:** approved Next.js **15.5.28**, React 19 and TypeScript. Product Owner-approved exact baseline: Node **24.22.1**, pnpm **10.34.7**, React/DOM **19.3.1**, TypeScript **5.9.3**, CPython **3.13.16** standard GIL with mandatory securely bootstrapped pip **26.2.2**. All version/lock compatibility and security evidence must still be demonstrated; details/constraints in [research R1–R3](research.md).

**Primary Dependencies:** Product Owner-approved direct pins: bare FastAPI **0.158.1**, Uvicorn **0.54.0**, SQLAlchemy **2.0.55 Core**, synchronous psycopg[binary] **3.3.6**, Alembic **1.20.0**; Better Auth **1.7.12** with pg **8.23.1** and Nodemailer **10.1.7**. Ruff **0.17.1** / pytest **9.1.1**, exact frontend tooling/types as researched, including temporary synthetic-development/CI-only ESLint 9.39.5. No ORM repository, new installer, auth plugins or extra UI/test framework. Approval is not resolved transitive-lock or compatibility evidence.

**Storage:** Product Owner-approved PostgreSQL **17.11**; Alembic baseline only, restricted migration/runtime/metadata-operator roles. Named DB/private-file volumes; no clinical/auth/job schema yet. Better Auth configured for PostgreSQL sessions without cookie/secondary cache; supported tables deferred to D-02.

**Testing:** Code-First production implementation → focused tests → relevant checks → independent review/evidence, never TDD. Deterministic small pytest suite, real PostgreSQL in CI for migration/privilege/context cleanup; HTTP smoke through running Next/API/Mailpit for F-07, no overlapping frontend runner or clinic E2E suite. TypeScript/ESLint/build and dependency/import checks locally where available. Intentional post-implementation local test fault verifies the check fails.

**Target Platform:** container-free Windows workstation using existing Windows PowerShell **5.1**; PowerShell **7** on Linux amd64 Docker-capable CI for Compose/container/real-PostgreSQL evidence. The single proposed `ops/check.ps1` must support both shells, checking `$LASTEXITCODE` after native commands and terminating nonzero on failure; no local `pwsh` installation is required. Exact Product Owner-approved Node/Python/PostgreSQL/Mailpit/ClamAV image tags and observed index digests in research R4, subject to CI identity verification. No local Docker installation/execution, production proxy or deployment.

**Project Type:** one-repository web modular monolith and one importing Python worker; existing `apps/{web,api,worker}`, `db/migrations`, `tests`, `ops` roots retained.

**Performance Goals:** readiness dependency probes have finite execution deadlines, including connection/pool waits and queries, and fail unready on timeout; no unnecessary DB/network work or invented throughput/latency/SLA or clinical workload. Liveness never queries dependencies; readiness checks only prerequisites for enabled foundation capabilities. Worker status must match the live process identity and have a bounded monotonic heartbeat age; missing/stale/mismatched state is rejected. Use minimal fixed operational timing constants under the [data model's rules](data-model.md#worker-status--f-05), not new configuration or security-policy numerics. No speculative caches or optimization infrastructure.

**Constraints:** exact Next pin; frozen pnpm and complete exact/hash-checked pip locks; verified approved runtime/DB/images; private API/DB, server-only secrets, least privilege, transaction-local context and fail-closed auth/policy. Synthetic-only test policy variance authorized on 2026-10-21. All production numerical/file/operations/legal/mail-provider/release gates stay open.

**Scale/Scope:** seven existing master task IDs, synthetic data and boot/auth-boundary page only. No clinical entities, operational account lifecycle, signed/replay-authorized business traffic, provisioning CLI, patient UI, job delivery, attachments, realtime, reports or later-Wave behavior. No new master tasks or `tasks.md` generated.

## Constitution Check

*Reviewed before research and again against Phase 1 design. A pass here applies to this planning scope, not implementation/release acceptance.*

| Constitution gate | Before research | Post-design evidence / result |
|---|---|---|
| I — Smallest secure maintainable solution | PASS: preserve approved architecture and existing roots | PASS: one shared Python package/pool utility, minimal dependencies, one contract file, one future check runner/workflow; no speculative layers/services/screens. |
| II — Security/data integrity | PASS for planning: protected functionality stays denied; policy decisions are not invented | PASS design boundary: secrets/role isolation, transaction cleanup and no auth/schema fallback. **Input/security acceptance remains blocked** below; no insecure candidate is accepted by planning. |
| III — Pragmatic performance | PASS: no new performance targets | PASS: process-only liveness, capability-scoped readiness and no worker HTTP server or duplicate pool/cache. |
| IV — Code-First tests and honest evidence | PASS: no TDD, synthetic/real-PG venue rules retained | PASS plan: focused positive/negative checks after implementation; real database/Compose evidence CI-only; document checks only in this invocation. No delivery state inferred from documents. |
| V — Focused execution and approval boundaries | PASS: F-01–F-07, exact dependencies, preserve existing work | PASS: native planning artifacts only; no authority edits, tasks, app code, migration or deployment; stop after planning validation/hooks. |

### Product Owner input decisions and remaining evidence/release gates

- **F-01 inputs — APPROVED by Product Owner (2026-10-21):** exact R1–R4 runtime/DB/direct dependency/tooling/type/image proposals and the URL/hash-verified Python pip 26.2.2 bootstrap. This resolves the pending Product Owner input choices, not frozen installs, complete lock resolution, container identity, image builds or real PostgreSQL compatibility evidence. Failed bootstrap or safety checks block the affected action, not independent synthetic-development work.
- **Frontend lint exception — APPROVED for synthetic development/CI only (2026-10-21):** use exact ESLint **9.39.5** temporarily with the approved Next **15.5.28** config; no incompatible ESLint 10, peer override or Next change. Log the unsupported/EOL risk and revisit at Wave 0 review. Complete Security/Operations disposition **before production deployment**; do not treat this temporary development exception as production clearance.
- **Remaining security/advisory review — evidence required:** review complete locked transitive dependencies during F-01/F-06, including bundled RSC and actual resolved `sharp` advisories. The two checked Next advisories patched at 15.5.27 do not block 15.5.28. Escalate an applicable exploitable critical issue immediately and fail closed on affected unsafe operation; other release-only risk acceptances are deferred to the pre-deployment gate, not a blanket synthetic-development prohibition. See research R7.
- **Production — CLOSED:** SECURITY-POLICY-NUMERICS, OPEN-OPS-01/02/03 and OPEN-FILES-01 remain OPEN; production mail/provider, hardening, encrypted storage, restore/legal/security evidence remain required. Authorized synthetic timing fixtures do not close any of them.

**Gate evaluation:** no unresolved scope/design clarification or pending Product Owner version/input approval. Conditional synthetic-development decisions are recorded. **Implementation remains UNAUTHORIZED until separately assigned**, and F-01/F-06 runtime/security/CI evidence is still UNVERIFIED; these cannot be claimed as passing. Defer production-only decisions to before deployment without skipping an actual required safe installation or security boundary. No authority or scope change follows from input approval.

## Project Structure

### Documentation (this feature)

```text
specs/001-wave-0-foundation/
├── spec.md                     # Existing, unchanged
├── checklists/requirements.md  # Existing, unchanged
├── plan.md
├── research.md
├── data-model.md
├── contracts/foundation.md
└── quickstart.md
```

These five generated Markdown files are the complete planning output. No auxiliary research report, duplicated acceptance register, `tasks.md` or implementation file is needed.

### Source Code (repository root; proposed reach, NOT created)

```text
apps/web/app/                   # Boot page, health, closed /api/backend/* boundary
apps/web/                      # Server-only config, Better Auth adapter, reserved mail templates/transport
apps/api/carepulse/             # FastAPI entrypoint, shared config/DB utility, health
apps/worker/                   # One entrypoint, status probe; imports carepulse
db/migrations/                 # Alembic baseline; no clinical/auth tables
tests/unit/                    # Focused deterministic foundation tests
tests/integration/             # PostgreSQL + running HTTP/mail boundary checks, CI only
ops/check.ps1                  # One prospective cross-platform check entrypoint
```

F-01 reserves the required API modules **provisioning, identity, patients, policies, appointments, queue, visits, files, notifications, reporting** as minimal package boundaries, not services, empty per-layer files or placeholder business implementations. API and worker import the same `carepulse` package via the same configured package path; no source duplication. Existing ignored `.venv`/`node_modules`/`.next` are not implementation inputs and are not deleted.

Root configuration reach during later authorized F tasks: pnpm workspace/package manifests/lock, Python dependency input/hash lock and Ruff/pytest configuration, `.env.example`/ignore rules, Compose/Dockerfiles and a single CI workflow. Keep checks/tool versions in these existing conventional mechanisms; do not add a custom configuration framework. No such files are written by planning.

## Dependency and acceptance preservation

This is a design trace to **existing** §5 owners, not task generation, assignment or a replacement execution inventory. Read each task's complete authoritative requirements/refs before execution.

| Owner | Exact Depends | Design reach and minimum acceptance evidence |
|---|---|---|
| F-01 | None | Minimal web/API/worker and ten domain package boundaries; approved exact runtime/DB/image input record; React peers, pnpm frozen install, exact direct/transitive pip hash lock, import/build smoke and no unnecessary packages. |
| F-02 | F-01 | Private application/database networks, web-only host port, named DB/file volumes, optional mail/scan; config validation, reproducible clean CI boot, no default host-published API/DB and web→API connectivity. |
| F-03 | F-01, F-02 | Distinct environments, scoped file secrets and full active policy; required service/auth/file keys or policy missing → protected startup denied. Tagged synthetic fixtures only; source/build/browser/image/log secret scans with no operator exposure. |
| F-04 | F-02, F-03 | Separate migration/runtime/operator metadata privileges, transactional Alembic empty baseline, pooled BEGIN/SET LOCAL/cleanup; empty upgrade/reset and failure rollback, runtime DDL denial and no context leakage after commit/rollback/error. No clinical RLS claim. |
| F-05 | F-02, F-03, F-04 | Application/worker process liveness and sanitized capability-scoped readiness; healthy/unhealthy, DB loss, incompatible revision, enabled dependency failure, recovery and Compose restart evidence without sensitive/version/stack output. |
| F-06 | F-01, F-02, F-03, F-04, F-05 | One command per formatting/lint, pytest, type/ESLint/build, inputs/import and real-DB integration check; frozen/hash installs, peer/version/image evidence, migration/container build and disposable DB run; reject unapproved/floating/lock drift; initial green and intentional local fault red, without weakening checks or changing approved pins. |
| F-07 | F-02, F-03 | Supported Better Auth PostgreSQL session configuration, cache off, closed BFF transport skeleton; reserved invite/reset mail → isolated Mailpit; no-session BFF 401/no passthrough and private API service-auth denial; no signup/business route/fake login. |

F-07 may proceed after **F-02/F-03 only** once explicitly authorized; its implementation does not acquire an F-04/F-06 dependency. Its smoke checks remain deliverable without D-02 auth schema. Final combined CI validation naturally exercises the whole delivered slice but does not rewrite the dependency DAG. D-01 becomes dependency-ready after F-04, not automatically authorized. A Wave exits only after all seven owners' evidence and approved §5/§13.3 W0 conditions.

## Phase 0 — Research outcome

[research.md](research.md) records approved exact candidates, raw compatibility constraints, image index identities and official source links; decisions/rationale/alternatives cover runtime/DB, locks, images, closed auth/mail, operational boundaries and security findings. No clarification placeholders remain. The Product Owner's 2026-10-21 approvals are distinguished from still-unexecuted checks and production-only gates. No change to the Next pin or architecture was made.

## Phase 1 — Design and validation outcome

- [data-model.md](data-model.md): Alembic version/role baseline and transient config/context/status/mail records; explicit absence of clinical/auth schema and later ownership.
- [contracts/foundation.md](contracts/foundation.md): network/secrets, live/ready interfaces, closed browser/private API behavior, mail, migration and reproducibility contracts.
- [quickstart.md](quickstart.md): prospective commands, prerequisites/safety, container-free workstation checks, Docker-capable CI-only integration scenarios, exact evidence by F owner, fault detection and truthful reporting. Commands are not represented as already implemented or run.

**Planning validation:** inspect all generated files against spec requirements, exact seven dependency edges, Constitution and authority boundaries; check required artifacts, filled template/no clarification placeholders, internal links, whitespace and changed-file scope. Required native pre/post hooks are checked as described below. No unrelated application tests are appropriate for this documentation-only invocation.

**Recorded planning result (2026-10-21):** native setup succeeded. Read-only Python document assertions passed: all five artifacts, required template sections, local file links, no unresolved template/clarification markers, all seven exact dependency rows, explicit open gates and absence of `tasks.md`; Markdown table/fence/whitespace checks passed. `git diff --check` passed. Scope inspection found only the five planned new artifacts attributable to the initial workflow. `.specify/extensions.yml` was absent at both before-plan and after-plan checks: no hooks registered or dispatched. **Subsequent Product Owner decision update (2026-10-21):** approved the exact candidates and conditional synthetic-development recommendations documented in R1–R4/R7. No tasks, application code, dependency installs, migrations, containers, CI runs or deployment executed. Existing specification/authorities and pre-existing work are preserved. **Runtime/integration acceptance remains UNVERIFIED; production/security release gates remain OPEN.**

## Complexity Tracking

No architectural or Constitution exception is requested or granted. The Product Owner granted a **temporary synthetic-development/CI-only ESLint 9 EOL exception**; its release-time security disposition is still required, and this does not weaken lint checks. Skipped: eager UI packages/screens, operational login, auth/clinical/job schema, full signed-request/replay implementation, duplicated senders, local containers, CI matrices and all later-Wave features. Security, dependency edges and acceptance evidence are not skipped.
