# Feature Specification: Pre-Wave-3 schema compatibility

**Feature Branch** `feature/pre-wave-3-runtime-identity`

**Created** 2026-11-18

**Status** Clarified. Ready for planning within the approved synthetic engineering scope.

**Input** Define how to stop protected clinic work on the verified Wave 2 predecessor and resume on the F-01 candidate while preserving records, job outcomes and access controls.

**PRE-W3-COMPAT** is the working label for a separately authorized pre-Wave-3 security workstream. The approved plan assigns F-02 to the completed Wave 0 Docker Compose dev network. The “F-02” compatibility reference in the F-01 checkpoint is local shorthand. This directory number and its FR/SC references identify specification material only. The 58 approved Masters retain their IDs, owning Waves and dependency edges.

## Clarifications

### Session 2026-11-18

- Q: May both generations be present during cutover? → A: Both application generations may be present, but only one compatible generation may serve protected traffic or claim jobs at a time. The inactive generation must remain fully fenced. During migration, both generations must be fenced. No simultaneous protected service is permitted.
- Q: Do you approve a controlled maintenance window as the MVP cutover model? → A: Use a controlled maintenance window as the MVP cutover model. All protected traffic and affected background jobs must be fenced during migration. Resume operations only after successful schema, security, and application compatibility verification. Scheduling, downtime duration, and authorization for any actual maintenance event remain separate operational decisions. No deployment or production cutover is authorized by this approval.
- Q: Which populated starting revisions are in scope for current engineering acceptance? → A: Limit the current populated-database upgrade acceptance to the verified 0040 → 0046 path, including intermediate migration, failure, fencing, and recovery verification. Earlier populated starting revisions remain tracked under Issue #13 and must be independently validated before any applicable historical database upgrade. This approval is limited to synthetic engineering acceptance and does not authorize production migration or deployment.
- Q: Which recovery actions may the designated operator take within a separately authorized maintenance event? → A: Routine recovery requires independent verification of the committed database state, effective credentials and affected work outcomes. Before the first migration commit the operator may retry or reopen the verified intact predecessor with valid credentials. After a verified advance the operator may continue forward along the approved path. Reopening requires successful schema, security, application compatibility and outstanding-work verification. Keep all protected traffic and affected jobs fenced on missing, inconsistent or uncertain evidence. Exceptional restore, data repair, reconciliation of uncertain transaction or delivery outcomes, and any action risking data loss require explicit joint Product Owner and Operations approval. Actual maintenance, deployment and production migration require separate authorization.
- Q: Should PRE-W3-COMPAT retain its working label as a separately authorized pre-Wave-3 security workstream? → A: Yes. Preserve the completed Wave 0 F-02 Master, all 58 approved Master IDs, their Wave ownership and all existing dependency edges. PRE-W3-COMPAT creates no official Master and authorizes no change to the approved implementation plan. Approval covers workstream identity and specification scope only. Implementation, deployment, merge, release and progression to another Wave require separate authorization.

## User Scenarios & Testing

### User Story 1 — Keep incompatible processes away from clinic work (P1)

A staff member with an old browser tab must never see a protected record or receive a misleading booking confirmation from an incompatible service. An operator must be able to distinguish a running process from one ready for protected work.

**Independent test.** Exercise each matrix row through the Web, directly at the private API boundary and from an independently started worker. Inspect the response and the durable effects.

1. **Given** a verified matched pair in an eligible phase, **when** authorized staff read a permitted record and book an appointment, **then** they see the correct record and one booking attributed to the actual actor. A foreign-tenant or newly revoked request is denied.
2. **Given** a blocked combination, **when** a process receives a request, reuses a connection, forwards a signed request or tries to claim a job, **then** it reveals no protected data and produces no successful effect. A running participant stays live and reports sanitized unavailability for protected work.
3. **Given** a successful readiness check, **when** maintenance closes admission or the schema or credentials become incompatible, **then** the earlier result cannot authorize the next operation.

### User Story 2 — Account for the last booking and job before migration (P1)

During the controlled maintenance window, the operator needs to know which bookings finished and which jobs still have an owner before the schema changes.

**Independent test.** Hold a signed booking transaction and a claimed synthetic job across the start of maintenance. Submit a second booking, a new claim and a competing cutover attempt. Observe their outcomes and when migration may proceed.

1. **Given** work admitted before closure, **when** the fence takes effect, **then** earlier work can reach a known commit or rollback under its original authority. Later work is denied. Migration waits until affected transactions, claims and deliveries are accounted for and affected consumers have stopped.
2. **Given** two cutover attempts against the same database, **when** they overlap, **then** at most one may advance the database. The other cannot reopen admission or release the first operator’s fence.
3. **Given** an unresolved transaction or uncertain delivery, **when** drain cannot establish its disposition, **then** the cutover stays blocked with its evidence intact. Neither migration nor automatic redelivery proceeds on that uncertainty.

### User Story 3 — Recover from the last proven commit (P1)

After an interruption, the operator needs to distinguish an intact predecessor, a committed intermediate revision and a verified target before admitting staff or jobs.

**Independent test.** Interrupt a populated synthetic upgrade before its first commit, after durable advancement and after target commit but before reopening. Restart the controller and services, including a stale worker. Compare durable state and admission.

1. **Given** failure before any migration commit, **when** recovery inspects the database, **then** it must verify the predecessor revision, data, privileges and job outcomes before an authorized return to the predecessor can be considered.
2. **Given** a committed advance or lost commit acknowledgement, **when** the controller restarts, **then** it must establish ownership and inspect the durable revision and effective security state. If these checks or the affected work outcomes remain uncertain, protected traffic and affected jobs stay fenced.
3. **Given** verified target state, **when** an authorized operator reopens, **then** only the matched candidate resumes protected requests and job claims. A permitted retry of an already committed booking returns the recorded result under existing receipt rules.

### Edge Cases

Cover a stale Web paired with a current API and the inverse, an old worker reclaiming an expired lease, a connection surviving credential retirement, and a former cutover owner waking after a restart. Cover missing, empty, multiple, unknown and unreadable migration revisions, an unavailable database and a target revision with incorrect effective privileges. Each must produce the fenced outcome below.

## Requirements

### Functional Requirements

The inherited security and ownership constraints are approved. The Product Owner approved the coexistence rule (FR-001), controlled maintenance model (FR-004 and FR-007), populated-upgrade scope (FR-006), and recovery authority (FR-007).

| ID | Observable requirement |
| --- | --- |
| FR-001 | Determine eligibility from the actual build generation, service identity, committed revision and cutover phase. Apply the matrix before protected work. Both generations may be present. At most one compatible generation may serve protected traffic or claim jobs at a time. Fully fence the inactive generation and fence both during migration. A matching revision alone cannot establish readiness. |
| FR-002 | Enforce denial at actual admission points, including direct API calls, Web auth/session changes and signed forwarding, pooled connections, worker polls, job claims and retries. A routing change or an earlier health check cannot authorize a stale participant. |
| FR-003 | Keep process liveness independent of the database and the maintenance fence. A running participant unable to prove eligibility must report unavailable for protected work within its existing bounded readiness contract. User-visible responses and diagnostics must omit clinical data, tenant/actor identifiers, SQL, connection details, keys and raw errors. |
| FR-004 | Before altering the schema or retiring authority needed by admitted work, establish exclusive cutover control, close protected ingress and new job claims, settle or safely roll back admitted work, and stop affected consumers. Include scheduled jobs, retries, delivery and cleanup. Reconcile outstanding leases and delivery outcomes. Keep all protected traffic and affected background jobs fenced throughout migration, including on fresh or surviving processes. |
| FR-005 | Verify the target database, actual source head, intended build and head, published path, role prerequisites, effective grants, data compatibility and preservation/recovery evidence. Failed preflight must leave data and revision intact. Prohibit silent repairs, truncation, re-stamping, privilege escalation and wholesale bootstrap of a populated database. |
| FR-006 | For current synthetic populated-upgrade acceptance, start only at the verified 0040 predecessor. Advance under one owner in published order from 0040 through 0041, 0042, 0043, 0044, 0045 and 0046. Establish the last durable commit even after loss of acknowledgement. Repeated or competing attempts must leave one consistent history. Published migrations are immutable and forward-only. |
| FR-007 | Within a separately authorized event, the designated operator may perform routine recovery only after independent verification of committed database state, effective credentials and affected work outcomes. Reopen only after schema, security, application compatibility and outstanding-work checks pass. Keep all protected traffic and affected jobs fenced on missing, inconsistent or uncertain evidence. Keep stale generations fenced after reopening. |
| FR-008 | Preserve enforced row-level security (RLS), tenant-qualified relationships, live authorization, actual actor/role/resource audit, atomic commands and post-commit-only notifications. On the candidate preserve separate API, Web, worker and gate logins, independent signed identity proof and binding to login, backend, transaction, operation and permitted resource. Retired shared credentials, caller-set identity and maintenance logins cannot authorize product work. |
| FR-009 | Preserve domain and security evidence, including receipt scope/digest/result, immutable history, original authorship, revisions, outbox/lease state, nonce tombstones and binding evidence wherever present. Unknown provenance stays unknown. Never synthesize Doctor evidence, infer withdrawal from unrelated audit, discard an unresolved binding or resend an uncertain delivery to clear the fence. |
| FR-010 | Require the synthetic acceptance evidence below at the final executable SHA before claiming compatibility. Operational deployment, credential custody, backup/restore, independent review and production release require their respective decisions and evidence. |

### Compatibility matrix

The immutable Wave 2 predecessor is `6cf2973c07f6e0ef2d8040d1eb3c235fd1598df7` at `0040_t173_numeric_money`. The F-01 candidate is `f7609a616519179b8feb2fad6721350c5a2c9d31` at `0046_f01_audit_integrity`. Full Foundation passed at both SHAs. The F-01 checkpoint describes the security boundary. The later PO decision confirms scoped synthetic engineering acceptance and leaves Issue #26 open. [Predecessor CI](https://github.com/ahmedali5696/care-pulse-3/actions/runs/43621123700) · [Candidate CI](https://github.com/ahmedali5696/care-pulse-3/actions/runs/44296872990) · [F-01 checkpoint](../../docs/f01-runtime-identity.md) · [PO decision](https://github.com/ahmedali5696/care-pulse-3/issues/26#issuecomment-6980091882)

**Eligible** means the matched generation may perform its already-supported synthetic operations after all other admission and authorization checks pass. **Fenced** means zero protected reads, writes, forwarding, new claims or deliveries by the participant. Physical presence of a fully fenced generation does not disqualify the compatible active generation. Existing matched-pair CI provides controls for the new tests.

| Participants attempting protected work | 0040 predecessor | Each intermediate 0041–0045 | 0046 candidate | Other, newer, missing or unverifiable head |
| --- | --- | --- | --- | --- |
| Verified predecessor API, Web and worker | Eligible before closure on historical baseline | Fenced | Fenced, including surviving connections | Fenced |
| Verified candidate API, Web and worker with required service identities | Fenced | Fenced | Eligible after target verification and authorized reopening | Fenced |
| Protected work involving both generations, an unrecognized build or an unverified identity | Fenced | Fenced | Fenced | Fenced |
| Participant using a retired credential or unable to prove its effective role | Fenced | Fenced | Fenced | Fenced |

Closure overrides both eligible cells for new work. Only previously admitted work may settle during drain. During migration and target verification all product participants stay fenced. A predecessor control uses original, valid credentials in isolation. It grants no authority to restore a retired credential. An intermediate revision may be inspected during recovery without being supported for protected traffic or as a historical upgrade start.

### Cutover and recovery

**Approved MVP cutover model.** Use a controlled maintenance window for drain, migration and verification. Scheduling and downtime duration remain separate operational decisions. Each actual maintenance event requires separate authorization. This decision grants no deployment or production cutover approval.

| Point in cutover | Required evidence and admission decision |
| --- | --- |
| Before closure | Identify the target database, all ingress and autonomous consumers, builds, single source head, ordered path and designated migration authority. Inventory representative data and confirm the recovery basis. Missing prerequisites block the start. |
| Admission closed | Establish sole ownership and stop new requests and claims before observing drain. Account for every admitted transaction, lease and possible delivery. Verify zero affected work remains active and prevent old sessions, scheduled restarts and newly launched instances from taking work. Repeat source, role and data preflight under the fence. |
| Migration in progress | Only the authorized migrator advances the database. A failed migration unit must preserve its preceding committed state. Record the verified revision, effective role state and sanitized failure evidence. |
| Before any migration commit | After independently verifying the intact predecessor, its effective credentials and affected work outcomes, the designated operator may retry or reopen the predecessor with still-valid credentials. Reopening requires all FR-007 checks. Retired authority stays retired. |
| After a commit or lost acknowledgement | Retain the database and evidence. Determine the actual commit independently of the last client response. After a verified advance, the designated operator may continue forward along the approved path under FR-007. Keep fenced while evidence is missing, inconsistent or uncertain. |
| Target committed | Verify single target head, role separation, denial of retired authority on new and surviving connections, tenant/actor controls, preserved evidence, receipt/replay behavior and safe job ownership. Only the current owner may reopen for the matched candidate and release eligible jobs. Every FR-007 check must pass, including verification of outstanding work. An interrupted reopening requires revalidation. |

At any point, exceptional restore, data repair, reconciliation of uncertain transaction or delivery outcomes, or any action risking data loss requires explicit joint Product Owner and Operations approval. That decision does not waive the checks for reopening.

### Key Entities

- **Application generation** identifies the build and role of each participating API, Web and worker process and its required gate dependency.
- **Committed revision** identifies the durable schema state of the target database, including the last verified point for forward recovery.
- **Preserved evidence** comprises linked clinic records, actor history, request receipts, replay evidence and job outcomes that a reviewer can compare before and after an interruption.

## Success Criteria

### Measurable Outcomes

| ID | Passing outcome | Requirements |
| --- | --- | --- |
| SC-001 | With both generations present, only the eligible generation can complete protected work or claim jobs. During migration neither can do so. Both matched controls demonstrate supported work in their eligible phases. Every blocked matrix cell denies protected service, including each of the five intermediate revisions. Blocked participants expose zero protected records and produce zero successful writes, claims or deliveries. Running services continue answering liveness and give a bounded, sanitized unavailable result. | FR-001–003, FR-008 |
| SC-002 | In both orders of admission versus closure, each earlier operation has a known outcome and each later admission is denied. Zero active transactions or unresolved job outcomes enter migration. Of two concurrent cutover attempts, at most one advances or reopens the database. | FR-002, FR-004, FR-006 |
| SC-003 | After the populated 0040 → 0046 upgrade and repeat at head, all retained fixture identities, tenant links, values, histories, receipts and authors match their expected state. An incompatible fixture or missing prerequisite produces zero silent repairs and zero advancement of the failed unit. | FR-005–006, FR-008–009 |
| SC-004 | Every interruption in Story 3 has a verified durable disposition after restart. Zero protected requests or affected jobs resume while schema, security, application compatibility or outstanding work is unverified. With an uncertain transaction or delivery outcome, the operator preserves evidence and leaves work fenced. Neither exceptional recovery nor an action risking data loss proceeds on the operator’s authority alone. Zero stale processes regain admission on an advanced database. An authorized retry after an uncertain commit yields at most one business result. No unresolved claim or binding is lost. | FR-004, FR-006–009 |
| SC-005 | Each sampled permitted action retains its actor, tenant, operation, resource and field scope. Foreign-tenant, out-of-scope same-tenant, revoked and retired identities obtain zero protected results or successful effects. A consumed nonce never reauthorizes work. A rejected or rolled-back command leaves zero successful domain, audit or notification effects. | FR-008–009 |
| SC-006 | Reviewers can identify the baseline builds and final executable tested, inspect every mandatory result and distinguish the engineering decision from outstanding operational approval. Missing mandatory evidence keeps compatibility unverified. | FR-010 |

### Synthetic acceptance evidence required

- **Matrix and service boundary (SC-001, SC-005).** Exercise actual builds on disposable real PostgreSQL with their own runtime logins. Use normal Web, private API and worker entry points, including a job claim. Cover startup and already-running instances, stale pools, failed metadata reads and effective privilege mismatch despite a matching marker. Inspect responses and database effects. Use the immutable predecessor as a control and record any later executable submitted for compatibility acceptance.
- **Populated upgrade (SC-003, SC-005).** Start at the actual 0040 head with at least two synthetic clinics, differing staff roles, Patients, Appointments with frozen prices and number history, Visits with real-actor attestations, Follow-up claims/decisions, policy revisions, receipts, audit, nonces and pending/claimed jobs as applicable at source. Compare exact retained values, keys, relationships, FORCE RLS and effective grants. Verify target security evidence on newly authorized work, preserved unknown historical provenance, authorized receipt replay, changed-input handling and nonce reuse. Verify the intermediate migrations, fencing, injected failure and recovery along this path against SC-001–004. Intermediate recovery resumes the same 0040-origin upgrade. Retain the fresh-install, repeat-at-head and rejected populated-upgrade checks.
- **Drain and contention (SC-002, SC-004).** Use independent transactions, explicit checkpoints and observed PostgreSQL waiter/blocker relationships for ordered races. Exercise both orders of command/claim versus closure, two competing controllers and a superseded controller trying to reopen. Inspect committed state, receipts, leases, outbox and delivery observations. Timing sleeps cannot establish order.
- **Failure and restart (SC-003, SC-004).** Inject failure before commit and after durable advancement, including lost acknowledgement, intermediate progress and target verification failure. Restart services and controller. Compare data, role state, revision and admission at the last known commit. Verify forward continuation and repeat at target with zero duplicate effects. For a post-0040 interruption, include identity evidence already present at that revision. An unconsumed request, a consumed nonce and an unresolved binding must each retain its original disposition across recovery.
- **Final gate (SC-006).** In separately authorized future work, record exact reviewed executable SHA, revisions, test selections/counts, sanitized observations, CI run/jobs and disposable-environment cleanup. Require ordinary full Foundation at that SHA, successful `inputs-and-build` and `compose-integration`, all required real PostgreSQL cases and zero mandatory skips. Existing matched-pair runs and focused diagnostics alone do not prove cutover. Historical-only writers and protected T133 retain their original exact-revision evidence. [CI rules](../../docs/postgresql-ci.md) · [Wave 2 exit evidence, phases 21–23](../003-wave-2-booking-scheduling/quickstart.md)

## Assumptions

### Authority and scope

| Approved source | Constraints carried into this draft |
| --- | --- |
| [PRD §§3, 10.1–10.3, 12.2–12.3](../../docs/CarePulse_PRD_v1_APPROVED.md) | Actual actor, live tenant/role checks, immutable history, post-commit notifications and release boundaries underpin FR-002, FR-004, FR-008–010 and SC-002–006. |
| [Domain Model §§4, 10–11](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md) | INV-01/09/10/15/16/19/20 and CON-14/15 protect isolation, frozen evidence, authorship, revocation and rollback effects in FR-004, FR-008–009 and SC-002–005. |
| [System Design §§2–4, 6–7](../../docs/CarePulse_System_Design_v1_APPROVED%281%29.md) | Service boundaries, real PostgreSQL, RLS, transactions, worker/outbox behavior, sanitized diagnostics and exercised restore constrain FR-001–010 and SC-001–006. |
| [Implementation Plan §§3–5, 14](../../docs/implementation-plan.md) and [Constitution II, IV and Authority and Scope](../../.specify/memory/constitution.md) | Preserve Master ownership, forward-only history, independent liveness, verification at the executable SHA and separate release authority across FR-001–010 and SC-001–006. |

**Dependencies and limits.** The verified predecessor and scoped F-01 acceptance supply comparison endpoints. Existing revision checks are a starting point. General cross-version enforcement still needs its own evidence. Wave 2 acceptance, protected T133 evidence and the business decisions under #24/#25 keep their original scope. This draft introduces no permission alignment (F-03), new clinical permissions, Wave 3 work or operational execution.

### Product Owner decisions

| Decision | Approved disposition |
| --- | --- |
| Application-version coexistence | **Approved — Product Owner, 2026-11-18.** Both generations may be present under FR-001. Keep the inactive generation fully fenced and fence both during migration. |
| Planned downtime | **Approved — Product Owner, 2026-11-18.** Use the controlled maintenance model in FR-004 and the reopening checks in FR-007. Event scheduling, duration and execution authorization remain operational decisions. |
| Historical upgrade starts | **Approved — Product Owner, 2026-11-18.** Current synthetic populated-upgrade acceptance covers only 0040 → 0046. Earlier populated starts require independent validation under Issue #13 before any applicable historical database upgrade. Operational migration and deployment require separate authorization. |
| Recovery authority | **Approved — Product Owner, 2026-11-18.** Within a separately authorized event, the designated operator may perform verified routine recovery under FR-007. The exceptional actions listed under Cutover and recovery require explicit joint Product Owner and Operations approval. |
| Official identity and ownership | **Approved — Product Owner, 2026-11-18.** Retain PRE-W3-COMPAT as a separately authorized pre-Wave-3 security workstream under its existing working label. This decision covers identity and specification scope only. The approved implementation plan, its Masters, Wave ownership and dependency edges remain unchanged. |

### Outstanding operational obligations

[#13](https://github.com/ahmedali5696/care-pulse-3/issues/13) remains open before an applicable populated deployment, including historical validation and separately approved remediation. [#24](https://github.com/ahmedali5696/care-pulse-3/issues/24) and [#25](https://github.com/ahmedali5696/care-pulse-3/issues/25) retain adoption and downstream evidence obligations. [#26](https://github.com/ahmedali5696/care-pulse-3/issues/26) retains independent security disposition and closure criteria alongside scoped F-01 engineering acceptance. Their closure is separate from review of this draft.

Operational deployment requires approved credential custody, issuance cutoffs and retirement, a rehearsed restore of independently encrypted database and attachment backups, verified recovery objectives, privacy/legal review and named release approval. OPEN-OPS-01/02/03, OPEN-FILES-01 and production security numerics remain open alongside [#2](https://github.com/ahmedali5696/care-pulse-3/issues/2), [#4](https://github.com/ahmedali5696/care-pulse-3/issues/4), [#5](https://github.com/ahmedali5696/care-pulse-3/issues/5) and [#18](https://github.com/ahmedali5696/care-pulse-3/issues/18). A synthetic upgrade cannot approve a production backup schedule, RPO/RTO, real-patient use or release.
