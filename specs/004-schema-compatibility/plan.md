# Implementation Plan — PRE-W3-COMPAT schema compatibility

**Git branch** `feature/pre-wave-3-runtime-identity` · **Spec Kit feature** `004-schema-compatibility` · **Date** 2026-11-18

**Input** [Clarified feature specification](spec.md) · **Status** Phase 1 design complete for synthetic engineering task decomposition.

## Summary

Use the approved controlled maintenance window to move one populated, synthetic database from the verified Wave 2 predecessor at 0040 to the F-01 candidate at 0046. Only the verified matching generation may serve protected work. Close new work before drain, account for the last booking and delivery, advance one published revision at a time, and reopen only after independent checks. A private durable control record and PostgreSQL locks tie that decision to the same database that Alembic advances. Service-entry guards cover both generations, including the immutable predecessor, native Web auth and the independently started sender.

PRE-W3-COMPAT is a separate pre-Wave-3 workstream. The existing 58 Master IDs, owning Waves and dependency edges stand. In particular, F-02 is the completed Wave 0 Compose network. The F-01 checkpoint's local shorthand creates no new Master. Permission-matrix alignment, Wave 3 product work, operational migration and release require their own authority. [Implementation Plan §§3–5, 14](../../docs/implementation-plan.md) · [Approved workstream decisions](spec.md#product-owner-decisions)

## Technical Context

| Item | Selected context |
| --- | --- |
| Languages and dependencies | Existing pinned Python 3.13.16, FastAPI 0.158.1, SQLAlchemy 2.0.55, psycopg 3.3.6 and Alembic 1.20.0. Existing Node 24.22.1, TypeScript, Next.js 15.5.28, Better Auth 1.7.12 and node-postgres. |
| Storage and target | PostgreSQL 17.11 on disposable Linux Compose. One target per cutover, with maintenance control outside published application schemas. No new broker, external coordinator or product-facing service. |
| Project type | Existing modular monolith, private FastAPI, Next.js BFF, worker and separately invoked sender. Use the original clinical transactions, live authorization, F-01 gate and post-commit outbox. |
| Performance and health | Preserve API/Web four-second readiness bounds and the worker's existing heartbeat/freshness contract. Gate at real entry and physical DB checkout. Hold a session permit for an admitted operation, with short database transactions and no clinical locks across transport. No downtime or throughput target has been approved. |
| Scale and test platform | Two application generations, one active at most, two or more synthetic clinics, six named Alembic successors, actual service logins, independent PostgreSQL sessions and an observable synthetic mail sink. Container-free checks locally, required Compose/real PostgreSQL in Foundation. |
| Dependencies and scope | Immutable 0040 control `6cf2973c07f6e0ef2d8040d1eb3c235fd1598df7`. F-01 reference `f7609a616519179b8feb2fad6721350c5a2c9d31`. Record later executable/adapter digests independently. Predecessor grants are valid only in their eligible source control. |
| Resolved research questions | Where to fence each actual participant, how to identify a real build and effective credentials, who owns each migration commit, how to verify a populated source, and how to settle a job that outlives its claim transaction. Decisions and rejected alternatives are recorded in [research](research.md). |

## Constitution Check

Constitution **1.1.0**. The initial review used the selected spec, its five PO decisions and the approved authority order. The post-design review used the control model, contract and validation guide below. PASS in this table is a **planning gate**. [Constitution](../../.specify/memory/constitution.md)

| Gate | Before research | After design |
| --- | --- | --- |
| I. Simplicity and maintainability | PASS. Extend the approved stack and preserve module ownership. | PASS. One target control record, scoped pending-work evidence and two PostgreSQL lock purposes suffice. Reuse the existing ingress, CLI, migration connection and sender. |
| II. Security and data integrity | PASS. Default-deny admission, tenant isolation, real actor and immutable history apply to both endpoints. | PASS. No mixed-generation service, bypass to raw handlers, clinical grants for maintenance, or privilege fallback. The contract preserves F-01 proof/transaction binding, replay, published revisions and external delivery uncertainty. |
| III. Pragmatic performance | PASS. Preserve bounded readiness and use the approved maintenance window. | PASS. Order only admission/closure and the named DDL unit. Session permits cover multi-step work without holding a clinical transaction over network I/O. |
| IV. Code-first testing and evidence | PASS. Design requires synthetic real PostgreSQL and final-SHA Foundation. | PASS. [Validation scenarios](quickstart.md) cover actual builds, lock observations, preservation and restart. Future implementation precedes its focused tests. No executable or operational evidence claimed here. |
| V. Focused execution | PASS. Existing feature and native planning outputs only. | PASS. Phase 0/1 produce this plan, research, data model, contract and quickstart. The next stage requires separate authorization. |
| Authority, scope and approvals | PASS. PRD → Domain Model → System Design → Implementation Plan. | PASS. Preserve the approved actors, clinical boundaries, Masters and ownership. Earlier populated starts, operational timing/custody, exceptional recovery, independent security and release retain their respective owners. |

No Constitution exception is required. The PRD governs disclosure, real-actor attribution, post-commit notification and release. Domain INV-01/09/10/15/16/19/20 and CON-14/15 govern tenant links, frozen evidence, revocation and rollback. System Design retains private service boundaries, PostgreSQL/RLS, the Python worker and rehearsed restore. [PRD §§3, 10, 12](../../docs/CarePulse_PRD_v1_APPROVED.md) · [Domain Model §§4, 10–11](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md) · [System Design §§2–4, 6–7](../../docs/CarePulse_System_Design_v1_APPROVED%281%29.md)

## Project Structure

### Documentation for this feature

| Artifact | Purpose |
| --- | --- |
| [Specification](spec.md) and [requirements checklist](checklists/requirements.md) | Existing requirements, matrix and Product Owner decisions. |
| [Research](research.md) | Investigated uncertainties, repository evidence, alternatives and platform semantics. |
| [Data model](data-model.md) | Target, generation and epoch, durable phase, work dispositions and retained evidence. |
| [Cutover contract](contracts/cutover.md) | Web/API/job admission, health, credential and controller behavior, reopening authority. |
| [Quickstart](quickstart.md) | Future reproducible matrix, races, populated migration, crash/restart and evidence collection. |

### Source and verification boundaries

| Repository area | Design responsibility for subsequent engineering |
| --- | --- |
| `apps/web/ingress.mjs`, `apps/web/auth/`, `apps/web/db.ts`, `apps/web/health.ts` | Gate the original HTTP dispatch, both Web pools, local auth/session changes and the trusted upstream before signing or forwarding. |
| `apps/api/carepulse/{main.py,db.py,health.py,identity/}` | Guard private ASGI admission and pooled transactions, preserve signed registration/activation, check effective API and gate identities, keep process-only liveness. |
| `apps/worker/`, `apps/api/carepulse/notifications/`, `apps/web/mail/` | Preserve heartbeat semantics, own each real poll/claim/attempt to its known completion, and account for transport outside PostgreSQL. |
| `compose.yaml`, `ops/` | Trusted build/credential/network bindings, controlled launch of both generations, owner-only operations and observable drain. Limited compatibility adapters can accompany archived images without rewriting their source. |
| `db/roles.sql`, `db/f01-synthetic-bootstrap.sql`, `db/migrations/{env.py,versions/}` | Reference boundaries for fresh bootstrap, targeted prerequisites, existing migration connection and immutable 0040–0046 sequence. |
| `tests/{unit,integration}/`, `apps/web/auth/`, `ops/check.ps1`, `.github/workflows/foundation.yml` | Fit new evidence into existing runners. The historical-only writer lane and separately protected T133 retain their exact-revision purpose. |

**Structure decision.** Keep work at these existing service and operations boundaries. The control data carries no clinical authority. A scheduled restart, cached health response or successful preflight cannot confer an admission permit.

## Design and verification handoff

| Requirements | Design decision | Acceptance evidence |
| --- | --- | --- |
| FR-001, FR-002, FR-003 | Require a verified generation/service/peer, actual target head, effective login/rights and open phase at admission. Enforce both allowed cells and every denied case, including stale pools and local auth. | SC-001 also checks independent liveness, bounded readiness and sanitized zero-effect denial. |
| FR-004 | Serialize close against new admission. Drain accepted requests, leases, sends and cleanup, then stop consumers and seal stale credentials. Keep unresolved outcomes fenced. | SC-002 and SC-004 observe both race orders, ownership takeover and loss of acknowledgement. |
| FR-005, FR-006 | Read-only preflight and scoped preparation. One owner advances a populated 0040 target through each of the six published successors with one verified commit per unit. | SC-002, SC-003 and SC-004 cover preflight rejection, retained values, rollback, repeat, intermediate recovery and contention. |
| FR-007 | Before advancement the verified intact source may reopen with still-valid credentials. After advancement routine recovery goes forward. Current owner alone may open with verified security, application and outstanding work. | SC-004 covers stopped/restarted and superseded processes, target verification failure and exceptional recovery authority. |
| FR-008, FR-009 | Preserve FORCE RLS, least privilege, F-01 binding, real authorship, original receipt and nonce semantics, and post-commit-only delivery. Preserve unknown evidence at its actual historical revision. | SC-001, SC-003, SC-004 and SC-005 compare real-login behavior, historical rows and new scoped results, including replay and rollback. |
| FR-010 | Require ordinary full Foundation on the final reviewed executable with both required jobs, all mandatory synthetic cases, zero mandatory skips and cleanup. | SC-006 separates matched-pair historical controls, new compatibility evidence and production approval. |

## Blockers and outstanding obligations

**Task decomposition is ready.** All Phase 0 design questions are resolved. The two historical Foundation runs prove their matched controls. Cross-generation compatibility, populated cutover under load, crash recovery and independently reviewed security results require future executable evidence.

| Open obligation | Owner, trigger and evidence |
| --- | --- |
| Historical populated starts | Database engineering with PO and Security/Operations. Before any applicable historical upgrade, verify representative populated sources through the intended destination, rejection/rollback, retained data and grants; review any remediation separately. [#13](https://github.com/ahmedali5696/care-pulse-3/issues/13) |
| Approved clinical and delegation contracts | Product/domain and downstream owners retain actual-actor, Doctor-precedence, permission and adoption evidence. Preserve those decisions through the synthetic comparison. [#24](https://github.com/ahmedali5696/care-pulse-3/issues/24) · [#25](https://github.com/ahmedali5696/care-pulse-3/issues/25) |
| Shared-runtime security disposition | Scoped F-01 engineering acceptance stands. Independent Security/Clinical review and PO disposition against the Issue's closure criteria remain open. The approved synthetic review process requires exact-code/PostgreSQL/CI evidence and a PASS, FINDINGS or BLOCKED outcome. [#26 and PO decision](https://github.com/ahmedali5696/care-pulse-3/issues/26#issuecomment-6980091882) |
| Operational deployment and real-patient release | Operations/Security, PO and applicable reviewers must resolve trusted ingress/throttling, dependency advisories, credential/key custody, SMTP/retry and security numerics. OPEN-OPS-01/02/03 and OPEN-FILES-01 retain their approved backup/attachment restore, recovery objectives, privacy/legal and file decisions. A maintenance event, production migration, exceptional recovery and release require their respective authorizations. [#2](https://github.com/ahmedali5696/care-pulse-3/issues/2) · [#4](https://github.com/ahmedali5696/care-pulse-3/issues/4) · [#5](https://github.com/ahmedali5696/care-pulse-3/issues/5) · [#18](https://github.com/ahmedali5696/care-pulse-3/issues/18) |

## Complexity Tracking

No unjustified gate violations. The separately versioned admission adapter is needed for the immutable generation, and durable work evidence is needed where process or transaction exit alone cannot establish an outcome. The alternatives and their failure cases are recorded in [research](research.md).
