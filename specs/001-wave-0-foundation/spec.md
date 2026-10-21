# Feature Specification: CarePulse 3 — Wave 0: Foundation

**Feature Branch**: `feature/wave-0-foundation`

**Created**: 2026-10-21

**Status**: Draft — specification quality validated; implementation not authorized

**Input**: Specify only F-01–F-07 of the approved Implementation Plan §5: the smallest secure, reproducible, bootable development foundation that enables subsequent Waves without clinical features.

## User Scenarios & Testing *(mandatory)*

The users are developers and operators. Scenarios are independently verifiable once their task prerequisites exist; priority does not replace the plan's dependency order.

### User Story 1 - Boot a reproducible private foundation (Priority: P1)

As a developer, I can reproduce application and worker startup from a clean checkout in the CI test environment using recorded inputs, with internal services accessible only through their intended development boundaries.

**Why this priority**: Subsequent work needs a reproducible environment that does not expose protected services or secrets.

**Independent Test**: Exercise F-01–F-03 installation, build, boot, connectivity and configuration evidence in a Docker-capable CI environment with synthetic data and without clinical functionality.

**Acceptance Scenarios**:

1. **Given** a clean checkout and complete development configuration, **When** installation, build and boot checks run, **Then** the application and worker start using verified locked inputs and the approved architecture, without unnecessary scaffold packages or duplicated business rules.
2. **Given** the running environment, **When** connectivity is checked, **Then** the web entrypoint is host-accessible, web-to-backend communication succeeds, and direct host database/backend access is denied by default.
3. **Given** a missing required secret or security policy, **When** protected startup is attempted, **Then** it fails closed without printing secret values; source, build, browser-visible configuration and logs expose no secrets or operator credentials.
4. **Given** a synthetic test profile, **When** policy fixtures are supplied, **Then** they are labeled test-only, remain separate from production configuration and cannot substitute for approved production policy.

### User Story 2 - Verify migration, isolation and service status (Priority: P1)

As an operator, I can establish the empty database baseline, verify restricted access and transaction cleanup, and distinguish a running process from a service ready for its permitted work.

**Why this priority**: Later data and authorization work must inherit safe database access and truthful operational signals.

**Independent Test**: After F-02–F-03, exercise F-04–F-05 baseline upgrade, disposable empty-database reset, privilege/context checks and health/restart checks without clinical schema.

**Acceptance Scenarios**:

1. **Given** an empty disposable database, **When** the approved migration command runs, **Then** the baseline and migration version are established transactionally and reproducible from empty; runtime credentials cannot change schema and operator privileges remain metadata-only.
2. **Given** successive requests sharing pooled connections, **When** transactions commit, roll back or fail, **Then** each new transaction has isolated context and cannot inherit a prior request's tenant context.
3. **Given** a running process, **When** the database is unavailable, migrations are incompatible or a required dependency fails, **Then** readiness fails while liveness reports only process status; healthy and restart checks reflect recovery when prerequisites recover.
4. **Given** any health outcome, **When** status is inspected, **Then** it contains no patient data, tenant identifiers, secrets, version details or stack traces; inactive optional services do not make unrelated work unready.

### User Story 3 - Check quality and the closed authentication boundary (Priority: P2)

As a developer, I can run repeatable checks and demonstrate development email delivery and denied unauthorized access before later Waves add identity and business workflows.

**Why this priority**: A small reliable check suite and closed routing boundary enable later work without temporary access bypasses.

**Independent Test**: With F-06/F-07 prerequisites satisfied, run foundation checks, prove failure detection with an intentional local test fault, and exercise synthetic email and unauthorized-request smoke checks.

**Acceptance Scenarios**:

1. **Given** the initial scaffold and approved pinned inputs, **When** F-06 checks run, **Then** installation, build, quality, migration and disposable real-database integration checks pass and record resolved versions/image identities; an intentional local test fault makes the relevant check fail.
2. **Given** floating, unapproved or inconsistent dependency/build inputs, **When** automated verification runs, **Then** it rejects them; advisory review does not silently replace approved pins.
3. **Given** the baseline authentication adapter and development mail transport, **When** reserved invitation/reset template delivery is exercised in CI, **Then** messages reach the isolated development/test mailbox, with persistent server-side session configuration and session-cookie caching disabled.
4. **Given** an unauthenticated request to the protected browser-facing backend boundary, **When** submitted, **Then** it receives 401 without passthrough; the private backend denies requests lacking required service authorization. Public signup and fake operational login bypasses are unavailable.

### Edge Cases

- Missing/invalid active-environment configuration must not trigger permissive defaults or secret disclosure.
- Failed transactions and reused connections must not retain prior tenant context.
- Database failure, incompatible migrations and restart must not produce false readiness; checks depend only on services needed for enabled capabilities.
- Optional mail/scanning profiles preserve F-02 boundaries; development mail must be available for F-07 delivery evidence.
- Authentication skeleton availability must not enable business requests, public registration or operator credential exposure.

## Requirements *(mandatory)*

### Authority and Scope

The approved sources govern in descending precedence:

1. [PRD v1](../../docs/CarePulse_PRD_v1_APPROVED.md): product scope and acceptance outcomes.
2. [Domain Model v1.2](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md): domain invariants and ownership.
3. [System Design v1.0](../../docs/CarePulse_System_Design_v1_APPROVED%281%29.md): architecture and technical constraints.
4. [Implementation Plan](../../docs/implementation-plan.md): task ownership, dependencies, contracts and evidence.

[Constitution v1.1.0](../../.specify/memory/constitution.md) governs engineering practice within those authorities, including minimal scope, security, focused verification and honest evidence reporting.

Scope is **only F-01–F-07 in Implementation Plan §5**. Each task's complete requirements, references and acceptance evidence are incorporated by reference, without reduction by the summaries below. Applicable fixed choices and pinning/reproducibility/security constraints in plan §2, execution rules in §§3–4, shared boundaries in §11, W0 verification in §13.3 and unresolved production gates in §14 remain binding. Detailed implementation instructions stay in those authorities; this specification introduces no alternative stack or version selections.

### Functional Requirements

Feature-local IDs below do not renumber the PRD FR register. Evidence is required for later task acceptance; it is not obtained by writing this specification.

| ID / plan owner | Required developer/operator outcome | Exact dependencies | Required acceptance evidence (complete requirements remain in plan §5) |
| --- | --- | --- | --- |
| **W0-FR-01 / F-01** | Developers MUST have the minimal approved application/worker structure, preserving backend ownership and shared domain modules. Runtime, dependency and image inputs MUST be verified, recorded and reproducible under §§2/5; unspecified versions are not implicitly approved. | None | Exact approved frontend pin and peer compatibility; frozen frontend and exact direct/transitive backend locked installs; verified interpreter/database/image selections recorded before implementation; import/build smoke; no unnecessary packages. |
| **W0-FR-02 / F-02** | Developers MUST be able to boot the private development environment with the intended web entrypoint, internal backend/worker/database, network separation, persistent named database/file volumes and the plan's optional mail/scanning profiles. | F-01 | Configuration validation, reproducible clean boot, direct host backend/database denial, web-to-backend connectivity. |
| **W0-FR-03 / F-03** | Development, test and production configurations/secrets MUST be isolated. Required secrets and the full active-environment security policy MUST be validated; missing service, authentication or file keys or required policy MUST refuse protected deployment. Synthetic overrides MUST be explicitly test-only. | F-01, F-02 | Missing-secret startup failure without disclosure; source/build scans; no secrets in source control, images/build output, public/browser configuration or logs. |
| **W0-FR-04 / F-04** | Operators MUST have a transactional, versioned migration baseline with separate migration, restricted runtime and metadata-only operator privileges, and isolated per-request transaction contexts with cleanup. No clinical schema is introduced. | F-02, F-03 | Baseline upgrade and disposable empty-database reset; runtime schema-change denial; no context leakage across pooled connections. |
| **W0-FR-05 / F-05** | Operators MUST receive process-only liveness and readiness reflecting database/migration compatibility and dependencies actually needed, with sanitized application and worker status. | F-02, F-03, F-04 | Healthy/unhealthy checks, unavailable-database not-ready result, restart verification and absence of sensitive/version/stack details. |
| **W0-FR-06 / F-06** | Developers MUST have the plan's minimal formatting/lint, test, type/build, lock-integrity, migration and disposable real-database integration checks, one command per check. Verification MUST reject floating/unapproved inputs and record compatible versions/pinned image evidence; transitive advisory review MUST preserve approved pins. | F-01, F-02, F-03, F-04, F-05 | Reproducible backend install/container build and database run; frontend frozen install/type/lint/build; recorded resolved versions/image digests or equivalent pinned artifact evidence and peer checks; initial scaffold checks pass and intentional local fault fails. |
| **W0-FR-07 / F-07** | Developers MUST have the approved server-side authentication adapter with persistent database session configuration, no session-cookie cache, private browser-facing backend transport skeleton, and reserved invitation/reset templates delivered through development mail. Public signup, business endpoints and fake operational login bypasses MUST be absent. | F-02, F-03 | Development mailbox delivery smoke, unauthenticated browser-facing boundary 401 and private backend service-authorization denial. |

### Boundaries and Handoff

- F-07 establishes adapter/configuration, routing and transport only. D-02 owns supported identity/session/invitation schema; D-09 owns nonce/audit/jobs substrate; B-01–B-04 own complete signed identity/replay enforcement, live authorization, session lifecycle, provisioning and recovery. F-07 must remain closed without pretending those workflows are complete.
- F-04 establishes privileges and transaction isolation; D-01 owns tenant isolation schema and subsequent clinical RLS proof. F-04 alone does not establish clinical isolation acceptance.
- No clinical schema/behavior, clinic UI beyond boot/auth boundary, business endpoints, operational provisioning/recovery flows, job delivery processing, attachment handling, realtime, reporting or later-Wave features are included. No additional infrastructure or production deployment is authorized.
- D-01 is dependency-ready after F-04; other downstream work follows its own plan dependencies. Readiness and this specification grant no execution permission. Wave 0 completion requires all seven tasks' evidence and §5 exit/§13.3 W0 checks.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A developer can reproduce clean installation, build and boot in Docker-capable CI using recorded inputs with no unrecorded substitutions; all F-01–F-02 evidence is available.
- **SC-002**: Every required negative case for configuration, network access, runtime privileges, transaction isolation and unauthorized routing in F-02–F-07 produces its prescribed denial or safe status, with no secret/operator credential disclosure or inherited tenant context.
- **SC-003**: An operator can reproduce the empty-database baseline and distinguish healthy, unready and restarted services using every F-04–F-05 acceptance case without accessing clinical data.
- **SC-004**: All foundation quality checks pass on the initial scaffold, an intentional local test fault is detected, and development email delivery is demonstrated without public registration or a business workflow.
- **SC-005**: All **7 of 7** foundation tasks have their plan-mandated evidence before Wave 0 is reported complete; reports distinguish implementation, testing, CI integration verification and production readiness. Specification validation alone satisfies none of those delivery states.

## Assumptions

- This internal engineering foundation uses synthetic data only. No clinical user journeys, production performance targets or new product requirements are inferred.
- Docker is unavailable and cannot be installed on the Product Owner's workstation. Agents must not request local Docker installation or Compose execution. All container-dependent F-01–F-07 proof, including reproducible boot, network isolation, real PostgreSQL and migration checks, must run in Docker-capable CI with verifiable run evidence; container-free development checks may run locally. This changes only the execution venue, never the acceptance requirements.
- SECURITY-POLICY-TIMING-VARIANCE is authorized for labeled synthetic-data development/testing as recorded on 2026-10-21. SECURITY-POLICY-NUMERICS, OPEN-OPS-01/02/03 and OPEN-FILES-01 remain OPEN; test values are neither recommendations nor production defaults. Required absent production policy fails closed; real-user/real-patient operation remains gated by plan §§2/14.
- Version compatibility selection and executable evidence belong to subsequently authorized F tasks. This specification selects no unspecified versions/security numerics and claims none of those checks ran.
- This invocation ends after specification and native requirements-quality validation. Planning, task generation, implementation, migration execution and deployment require separate authorization.
