# Feature Specification: CarePulse — Wave 1: Security & Core Data

**Feature Branch**: `feature/wave-1-security-&-core-data`

**Created**: 2026-10-22

**Status**: Draft — specification quality validated for synthetic-data scope; implementation not authorized

**Input**: Specify exactly D-01–D-05, D-09 and B-01–B-05 from the approved Implementation Plan §6, building on completed Wave 0 and Constitution v1.1.0. Preserve approved requirements, dependencies, security, concurrency and evidence; stop before planning or implementation.

## User Scenarios & Testing *(mandatory)*

Actors are the restricted provisioning operator, the clinic's single Doctor/Owner, Receptionists, and maintainers verifying the data boundary. Patients are records, not login identities. Stories are independently verifiable once their approved prerequisites exist; priority does not change dependencies. All examples and later acceptance fixtures use synthetic identities and patient data.

### User Story 1 - Establish a clinic through a verified invitation (Priority: P1)

As an authorized provisioning operator, I can establish a non-billed clinic and invite its first Doctor/Owner without gaining access to patient information. As that invited Doctor, I gain clinic access only after verified acceptance.

**Why this priority**: Every clinic needs an accountable Owner and a secure tenant boundary before clinical use.

**Independent Test**: Demonstrate restricted operator creation, committed invitation delivery, verified acceptance, login, suspension and reactivation using two synthetic clinics. No booking workflow or clinic management screen is needed.

**Acceptance Scenarios**:

1. **Given** an enabled, individually authenticated operator and a reason, **When** a clinic is created and its first Owner invited, **Then** an immutable clinic identity, one branch, PROVISIONING state, invitation and nonclinical delivery job are durably recorded with operator evidence; delivery occurs only after commit and clinical access remains denied.
2. **Given** the issued invitation and matching verified email identity, **When** acceptance succeeds, **Then** the invitation is consumed once and exactly one verified enabled Doctor/Owner association and ACTIVE clinic are established atomically. An interrupted identity preparation grants no clinic access and can be safely retried or reconciled.
3. **Given** expired, revoked, reissued or already consumed invitation proof, **When** acceptance is attempted, **Then** it is denied without another Owner, unauthorized activation or partial success; simultaneous acceptance cannot establish two Owners.
4. **Given** clinic staff, an anonymous caller or an invalid operator credential, **When** provisioning is attempted, **Then** access is denied. A valid operator can list only approved nonclinical metadata and cannot read or change patients, clinical records, settings, pricing or exports.
5. **Given** an ACTIVE clinic with open staff sessions, **When** an operator suspends it with reason, **Then** the next protected operation by either staff role is denied and data/history remain intact. Audited reactivation requires a still-valid verified Owner; invitation races cannot bypass suspension.

### User Story 2 - Use an authenticated, isolated clinic session (Priority: P1)

As clinic staff, I can use only my currently authorized clinic capabilities; a stale browser tab, forged identity or another clinic's identifier never grants access.

**Why this priority**: Patient privacy depends on current authorization at every protected boundary.

**Independent Test**: Exercise the real invited session through the browser-facing boundary and private backend, plus direct database isolation checks, with positive same-tenant and negative cross-tenant requests.

**Acceptance Scenarios**:

1. **Given** an active verified staff account and ACTIVE clinic, **When** a protected request is made, **Then** the actual server session and current account, clinic, role, grants and security revision are validated; only permitted fields of that clinic are returned or changed.
2. **Given** missing/expired sessions, forged tenant or role headers, altered signed content, an unsigned internal request or a replay, **When** protected work is requested, **Then** it is denied without business changes, successful-operation audit or outgoing domain events. Possessing a service credential alone is insufficient.
3. **Given** clinic A credentials and clinic B identifiers, **When** reads, searches, relationship expansions or writes are attempted, **Then** no B data or existence-sensitive detail is disclosed or changed; valid A operations still work.
4. **Given** the exact configured test-only idle/absolute expiry or request-freshness boundary, **When** requests occur before, at and after that boundary, **Then** validity matches the configured policy, expired/replayed proofs fail, and activity or refresh cannot silently extend absolute session lifetime.
5. **Given** a cross-origin unsafe request or missing required request-forgery protection, **When** a session cookie is presented, **Then** mutation is denied. Protected event-stream entry without a valid session is also denied; this does not deliver realtime functionality.
6. **Given** a PROVISIONING clinic, **When** its verified invitation is accepted through the narrowly allowed bootstrap entry, **Then** only Owner activation is permitted; every ordinary patient, staff and settings operation remains denied until ACTIVE.

### User Story 3 - Manage Receptionists and recover access safely (Priority: P1)

As Doctor/Owner, I can invite and manage Receptionists, withdraw their access immediately, and recover my own account through verified registered email.

**Why this priority**: Staff changes and credential compromise must not leave usable old access behind.

**Independent Test**: Use an Owner and Receptionist with multiple real sessions; exercise invitation, permission changes, disable/enable and recovery through the shared committed-email path.

**Acceptance Scenarios**:

1. **Given** the current Owner, **When** they create/invite a Receptionist, **Then** the account is tenant-scoped, activation requires valid invitation acceptance, and `bulk_reschedule` defaults to false. Owner-authorized listing, viewing, enable/disable, invite/revoke-invite and permission changes are audited.
2. **Given** a Receptionist or foreign-tenant Owner, **When** they attempt staff management, self-escalation, Owner modification or administrative-settings delegation, **Then** all such changes fail; only the own-clinic Owner can manage the approved Receptionist grant.
3. **Given** a Receptionist's open sessions, **When** disablement or grant withdrawal commits, **Then** the next protected operation cannot use former authority, even before asynchronous cleanup finishes; stale staff revisions fail. Re-enabling does not make revoked sessions valid again.
4. **Given** a recovery request for the Owner's verified registered email, **When** accepted, **Then** expiring single-use recovery proof and a nonclinical delivery job commit together; the same post-commit sender delivers the synthetic email. Responses and throttling do not reveal account existence or secrets.
5. **Given** valid recovery proof for a currently eligible Owner and existing sessions, **When** credential reset succeeds, **Then** single-use proof consumption, credential change and old-session invalidation are durable together, with coordinated application security revision and one success audit. An expired proof without durable auth completion cannot authorize a credential change, and reuse of a consumed proof cannot cause another change. An exact matching retry of the existing operation may finalize from matching durable `AUTH_DONE` evidence (even after proof expiry) or acknowledge an already `FINALIZED` result without re-consuming proof or repeating credential, session or audit effects; a different-credential retry is denied. Missing evidence, current recovery-purpose ineligibility, changed revision/block ownership or an unproven durable guarantee fails closed: the identity stays blocked pending reconciliation and reset is not reported successful.
6. **Given** two concurrent reset attempts or reset/disable/grant withdrawal racing protected work, **When** they finish, **Then** only a valid serial outcome exists and no request authorized under withdrawn authority commits sensitive work after revocation has committed.

### User Story 4 - Keep distinct patient and family records (Priority: P1)

As permitted clinic staff, I can register, find and update distinct patients, including family members sharing a phone. As Owner, I can archive and unarchive a patient without deleting history.

**Why this priority**: Safe identification and restricted intake are the usable clinical-data foundation for later booking.

**Independent Test**: Register synthetic patients with shared phones/names, exercise both roles' permitted fields and search, then archive/unarchive with lifecycle revision checks. Use persistence fixtures, not booking endpoints, for the future-booking boundary.

**Acceptance Scenarios**:

1. **Given** a three- or four-part name and either date of birth or declared age with reference date, **When** a patient is registered, **Then** a stable tenant-scoped identity is assigned; age never fabricates a birth date, and optional preliminary history/allergy/test information remains separate from Doctor-authored records.
2. **Given** two distinct patients sharing a normalized Egyptian family phone, **When** searched by phone or name, **Then** both selectable records remain distinct; another family member can be added. Duplicate demographics may warn but do not force merging or block registration by themselves.
3. **Given** a supplied phone, **When** stored or edited, **Then** the entered value is preserved and normalized when possible, with Egypt the default country and international numbers supported. Editing one selected patient changes no other family member.
4. **Given** a Receptionist and an active patient, **When** permitted demographics or the explicit preliminary-intake fields are read/edited, **Then** they succeed with appropriate audit; attempts to write server-owned identity/archive fields or read/write Doctor clinical narrative, including nested history, fail without disclosure or persistence. For an archived patient, the general patient detail and demographic/intake PATCH are denied to Receptionists, without disclosing archived-record existence. Only future purpose-bound minimal context for an already-confirmed Appointment may be exposed under its separate authorization contract; no such consumer is added in Wave 1.
5. **Given** an Owner archive action, **When** committed, **Then** the patient disappears from the default active directory, remains available through authorized archived access, and becomes ineligible for new Appointment IDs until unarchived; no record, history or already-confirmed appointment is deleted or auto-cancelled.
6. **Given** an archived patient with a previously confirmed Appointment, **When** Wave 1 persistence and authorization contracts are examined, **Then** archive distinguishes new-ID creation from permitted existing-ID continuation. Actual check-in/admission/reschedule acceptance is reserved for its owning later tasks, not claimed by this scenario.
7. **Given** an archived patient, **When** the own-clinic Owner reads or PATCHes allowed demographics/preliminary-intake fields with the expected revision, **Then** the edit succeeds with sensitive audit while the patient remains archived; the same general detail/PATCH by a Receptionist fails without revealing the record. Only the Owner may archive/unarchive; ordinary PATCH cannot change archive state.

### User Story 5 - Trust defaults, appointment records and durable evidence (Priority: P2)

As a maintainer and clinic Owner, I need safe initial policies, consistent future booking records and immutable sensitive-operation evidence without premature booking functionality.

**Why this priority**: Later clinical workflows must inherit correct data rules rather than repair inconsistent records.

**Independent Test**: Exercise the policy, appointment, audit and job persistence contracts using real database fixtures and concurrent transactions; no scheduling, queue, Visit or worker service behavior is required.

**Acceptance Scenarios**:

1. **Given** a new clinic's policy records, **When** defaults are inspected, **Then** they match PRD §7: Open day-only, no inferred duration/hours/closing/cutoff/cap, no breaks, overlap OFF, Routine walk-in OFF, Urgent OFF, capacity one, admission Both, booked-order Routine priority, late depth five, fixed EGP and Africa/Cairo. Policy edits retain version history and stale revisions cannot overwrite newer values.
2. **Given** simultaneous persistence attempts for the same patient/service day, **When** counted records are committed, **Then** at most one of Booked, Waiting, In Examination or Completed counts for that patient/day. Cancelled/No-show history permits a new distinct ID; Completed still blocks one.
3. **Given** simultaneous assignments across main and Emergency classifications for different patients, **When** numbers are committed, **Then** they are unique across the whole clinic/day and never reused after successful assignment, including cancellation/replacement; former assignments remain in history.
4. **Given** an appointment persistence fixture, **When** its status, price, interval or history is validated, **Then** only the six approved states are representable, confirmed EGP snapshots and historical assignments remain immutable, Open stays untimed and timed intervals are positive and half-open. No booking or Visit is created through an exposed Wave 1 workflow.
5. **Given** a sensitive operation and its evidence/job writes, **When** the transaction rolls back, **Then** no successful business change, success audit or outgoing event/job becomes visible. After commit, the event is durable and the permitted one-shot email delivery can be claimed safely.
6. **Given** existing audit/history evidence or a claimed job/request nonce, **When** modification, deletion, duplicate claim or replay is attempted, **Then** immutable evidence is protected, duplicate processing cannot create duplicate logical results, and replayed signed requests are rejected. Clinical audit content is not exposed to Receptionists or operators.

### Edge Cases

- Missing, empty, invalid or pooled stale tenant context must never expose another tenant's rows; commit, rollback, SQL error and cancellation all release transaction authority.
- Ownerless activation, removal/disablement of the sole enabled Owner of an ACTIVE clinic, and concurrent second-Owner activation fail at the data boundary, not merely in a screen.
- Reissued invitations invalidate superseded proof; expiry is checked at consumption, not only when a link is opened. Acceptance racing suspension/reactivation must respect the committed tenant state.
- Auth identity preparation, mail failure or session cleanup failure cannot activate an unverified account, report an incomplete reset successful or restore revoked authority.
- Signature failures include duplicate/missing identity headers, unknown signing key, body/path/query/method changes, future/stale timestamp, reused nonce and private-network calls without trusted identity.
- Replaying a business idempotency key is distinct from replaying a transport nonce: an authorized retry uses a fresh signed request and must recheck live access before receiving a committed result.
- Missing phone, shared phone, international phone, duplicate names and age-only records must not invent identity or demographic facts; see authority interpretations below.
- Archive must not be treated as general deletion or automatic cancellation. Race proof involving operational booking/check-in/reschedule endpoints remains downstream.
- Cairo midnight alone does not rewrite stored service dates/statuses. Policy revision storage does not imply active schedule changes, live queue changes or automated No-show work.

## Requirements *(mandatory)*

### Authority and Scope

The governing sources are incorporated by reference in this precedence order:

1. [PRD v1](../../docs/CarePulse_PRD_v1_APPROVED.md): especially §§2–3, applicable FR/BR, §7 defaults, §9 acceptance, §§10/12 security and release gates.
2. [Domain Model v1.2](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md): §§2–4, §9.2.2, §10 tenant/patient/recovery commands and §11 concurrency cases.
3. [System Design v1.0](../../docs/CarePulse_System_Design_v1_APPROVED%281%29.md): approved architecture, stack, authentication, database and operations constraints.
4. [Implementation Plan](../../docs/implementation-plan.md): **§6 is the exact execution scope**; §§2, 3–4, 11.1–11.2, 12, 13.3 and 14 supply binding shared contracts, ownership, evidence and gates.

[Constitution v1.1.0](../../.specify/memory/constitution.md) governs execution within those authorities. This specification neither changes them nor grants execution permission. Feature-local `W1-FR-*`, `SEC-*`, `RACE-*` and `SC-*` identifiers are requirements/evidence labels, not new master tasks or replacements for PRD IDs.

Only the **11 existing master tasks** below belong to this feature. Their complete approved text, references and evidence remain binding where a summary is shorter. Technical terms below identify inherited security/data acceptance constraints explicitly requested for this feature, not newly selected architecture or an implementation plan.

### Functional Requirements

#### W1-FR-01 — Tenant isolation and operator separation (D-01)

The system MUST preserve immutable Organization identity, exactly one physical branch, fixed MVP region/currency/timezone, and PROVISIONING/ACTIVE/SUSPENDED lifecycle. All clinical records MUST carry non-null tenant identity and tenant-qualified relationships. Missing context MUST fail closed; a runtime or operator identity MUST not gain migration privileges or cross-tenant clinical access. Activation MUST remain unavailable until D-02's Owner invariant is installed and B-03 verifies acceptance. D-01 alone MUST not be reported as cross-table Owner enforcement.

**Acceptance evidence**: two-tenant allowed/denied reads and writes, no-context denial, cross-tenant relationship rejection, runtime/table-owner RLS distinction and metadata-only operator clinical denial (SEC-01; Story 2).

#### W1-FR-02 — Staff, invitations and session persistence (D-02)

Each ACTIVE clinic MUST have exactly one verified enabled Doctor/Owner; every clinic MUST have at most one enabled Owner. The invariant MUST be database-enforced and concurrency-proven across tenant activation and Owner removal, including deferred cross-table validation under the tenant lifecycle lock. PROVISIONING MUST never activate ownerlessly. Persist staff state, the approved per-Receptionist grant, security revisions, invitation and recovery metadata. Invitation/reset proof MUST be expiring, single-use and stored digest-only as verification credentials. Sessions MUST use the supported Better Auth persistence/migrations, not a duplicate session engine. Public signup MUST remain disabled.

**Acceptance evidence**: valid Owner activation, ownerless/second-Owner/last-Owner-removal denials, invitation uniqueness/replay, staff/grant revision changes and competing tenant/Owner transactions (RACE-02/03/04).

#### W1-FR-03 — Patient persistence (D-03)

Persist stable tenant-scoped patient identity, exactly three/four name parts, raw and nonunique normalized phone when supplied, DOB or declared age with its reference date, the approved optional preliminary intake fields, archive state and lifecycle revision. Relationships/search access MUST remain tenant-scoped. Duplicate phones/names MUST not become unique identity constraints. General merge, deletion and cascading clinical deletion MUST be unavailable. Archive and later appointment consumers MUST share the patient lifecycle concurrency boundary without confusing new-ID creation with existing-ID continuation.

**Acceptance evidence**: valid demographic alternatives, invalid name/age combinations, distinct duplicate/family records and independent phone edits, wrong-tenant constraints/search denial and archive lifecycle-lock proof (Story 4; RACE-06).

#### W1-FR-04 — Policy and schedule persistence prerequisites (D-04)

Persist versioned clinic policies, prices, working periods, breaks, holidays/date overrides, manual slots, capacity/admission/queue revisions and revision-bound affected-booking preview substrate. Defaults MUST match PRD §7, including Story 5 scenario 1, false per-Receptionist bulk grant and the approved inclusive seven-day follow-up default where represented; no unapproved clinical price or operating period is invented. No-show enablement is separate from its optional explicit cutoff, initially disabled with no cutoff. Current-day queue policy MUST remain pinned; a new queue-policy version is effective next operating day. The substrate MUST support later preview/revalidation without depending on DayReview or implementing settings/slot APIs.

**Acceptance evidence**: persisted defaults for clinics created both before and after D-04 is installed, without duplicate initial versions or uninitialized clinics; fixed EGP/Cairo, no implicit hours/cap/cutoff, preserved old versions and deterministic revision-collision rejection. Exact-time scheduling/activation and live queue behavior are later proofs (RACE-07).

#### W1-FR-05 — Immutable evidence, jobs and replay substrate (D-09)

Persist separate sensitive-operation audit and restricted clinical-audit evidence, transactional outbox, jobs, signed-request nonces and one shared idempotency-receipt store reused by D-05/B-01. Shared event tables MUST NOT require Appointment existence through a hard Appointment foreign key. Evidence MUST be append-only with database mutation guards and revoked update/delete privileges; delivery/attempt state is separate from immutable business history. Sensitive evidence records tenant, actor, role, action and time accurate to seconds, reason where required and meaningful before/after values, without unnecessary clinical or secret content. Domain effects and their success audit/outbox/jobs MUST commit together; no event is visible for delivery before commit. Nonclinical invite/recovery delivery MUST remain separate from clinical events and require no clinical RLS bypass.

**Acceptance evidence**: successful atomic writes, rollback leaves none, denied audit update/delete, clinical-audit read denial for Receptionist/operator, duplicate job/claim and nonce rejection, and shared receipt ownership (Story 5; RACE-08/09).

#### W1-FR-06 — Appointment persistence only (D-05)

Persist Appointment and immutable numbered/history/replacement records with tenant-qualified patient links, clinic-local service date, revisions, optional positive half-open timed intervals and frozen EGP snapshots. Permit exactly Booked, Waiting, In Examination, Completed, Cancelled and No-show; no derived review flag becomes a seventh state. Enforce counted patient/day uniqueness over Booked/Waiting/In Examination/Completed and clinic/day number uniqueness across every service type/lane and historical successful assignment. Cancelled/No-show permit a new ID; Completed blocks it. Preserve terminal records and the terminal no-reopen/no-reschedule contract. Confirmed price/provenance, former number/date and replacement links MUST remain immutable-in-history, with server decimal/NUMERIC EGP at two fractional digits and the approved final-net rounding contract; no floating-point or silent repricing. Snapshot evidence includes service classification, base, authorized discount type/value/reason, free flag, net and applicable pricing/entitlement provenance. Preserve INV-09: no negative/above-base discounts, no Free Urgent, and Urgent base/net strictly above the creation-time standard base; operational calculation/settings/entitlement commands remain later scope. Use D-09 receipts, not another receipt store.

**Acceptance evidence**: real parallel counted-create and cross-lane number allocation; retained cancelled/replaced history and never-reused successful assignments; Completed conflict; invalid status/interval/tenant relationship, invalid stored monetary snapshot and snapshot mutation rejection; rollback leaves no successful orphan assignment/history/event (RACE-05/08). These are persistence fixtures/internal checks, **not working booking, pricing, reschedule, queue or Visit APIs**; full command behavior remains with its later owners.

#### W1-FR-07 — Live authorization and atomic request boundary (B-01)

Every protected backend operation MUST validate the signed browser-facing service envelope and one-use nonce, then independently revalidate live session-security revision, tenant, staff status, role and grants in the tenant transaction. Browser input MUST never establish tenant or privilege authority. Reads and writes MUST use the explicit default-deny resource/field allowlists from plan §2, including nested relationships. Only B-03's single-purpose verified invitation activation may operate for a PROVISIONING clinic; no general pre-ACTIVE staff session exists. Provide the approved sanitized error, expected-revision and idempotency contracts without exposing sensitive details (SEC-02/04/05).

**Acceptance evidence**: positive authorized operations; missing context, forged/replayed/tampered envelope, direct bypass, stale security revision, forbidden field and all other PROVISIONING-route denials; idempotent concurrent retry/conflicting-body and revoked-retry checks.

#### W1-FR-08 — Real invited sessions and secure BFF (B-02)

Replace the foundation's deny-only auth boundary with supported Better Auth server-side PostgreSQL session lookup plus live staff/clinic checks on every protected operation. Session-cookie caching and public registration MUST stay disabled. Apply secure cookie and same-origin request-forgery protection, configured idle/absolute expiry and login throttling; logout/revocation MUST invalidate access. Strip/overwrite untrusted identity headers, sign only verified requests and keep service secrets and bearer session cookies out of backend forwarding/client output. Preserve the private backend boundary. The only pre-ACTIVE bootstrap is verified one-use Owner-invitation acceptance. A protected SSE entry must reject unauthenticated access; event delivery is not in scope.

**Acceptance evidence**: real invite/login/session/logout flow, no-session and revoked-session denial, secure cookie/CSRF/origin checks, forged tenant/header denial, configured expiry/throttle boundaries and protected SSE 401 (Story 2; SEC-02/03).

#### W1-FR-09 — Restricted provisioning and Owner activation (B-03)

Only individually authenticated enabled operators through the approved one-shot internal CLI/operator profile and metadata-only privileges may create/list minimal tenant metadata, invite/reissue/revoke first-Owner invitations or reasonedly suspend/reactivate a clinic. The restricted operating procedure MUST be documented as required by FR-100. No operator HTTP/socket service or clinic-facing operator route is introduced. Operator credentials require protected digest verification, constant-time comparison, throttling and durable actor/reason audit; they MUST not enter application images or public requests. Invitation and nonclinical delivery job MUST commit atomically. A retried operator clinic-create request MUST not create a second clinic/invitation after a lost response. After commit, a single shared one-shot claim/sender path with the existing Mailpit transport MUST prove delivery; B-25 later schedules that same path. Owner acceptance MUST serialize tenant/invitation state, verify email/proof and atomically establish the unique enabled Owner and ACTIVE tenant. Prepared auth identities remain unusable until this succeeds. Suspension immediately denies protected use and preserves data; a previously ACTIVE clinic may be reactivated by an authorized operator only with a currently valid verified enabled Owner. A never-ACTIVE/ownerless suspended clinic cannot use reactivation or invitation acceptance to bypass activation guards. No billing condition controls activation.

**Acceptance evidence**: first-Owner invitation→mail→acceptance→login integration/E2E smoke using the documented operating procedure, expiry/replay/reissue/revoke/rollback/suspension races, denied ordinary pre-ACTIVE use, disabled/invalid operator denial and operator clinical-access denial (Story 1; RACE-02/03).

#### W1-FR-10 — Staff and recovery lifecycle (B-04)

Only the own-clinic Owner may create, list, view, enable/disable and invite/revoke invitations for Receptionists or change their sole approved optional `bulk_reschedule` grant. Changes require reason and expected staff/security revision; no Owner transfer, extra Doctor, self-grant or administrative delegation is implied. Accounts remain invitation-only. Grant withdrawal/disablement MUST immediately remove effective access and invalidate associated active access through the approved live-revision/session mechanism. Verified registered-email recovery MUST be rate-limited, expiring and single-use, with recovery proof and email job committed together and the B-03 shared sender used only post-commit. Successful reset MUST satisfy SEC-03's durable credential/session/revision guarantee. Every sensitive action MUST be audited without proof/credential disclosure.

**Acceptance evidence**: authorized staff lifecycle and default/changed grant; unauthorized and stale-revision denial; disabled old tab; exact expiry/throttle boundaries; reset replay and concurrent reset/revocation; committed/rolled-back Mailpit recovery delivery through the single sender (Story 3; RACE-04/08). No bulk reschedule operation is added.

#### W1-FR-11 — Patient registry, family lookup and archive (B-05)

Allow authorized Doctor/Receptionist registration, paginated active-default name/phone search, selection and independent demographic/preliminary-intake updates of active records under the exact plan §2 field projection. Return all matching family records across authorized pages without merging histories. For archived patients, only the own-clinic Owner may use general patient detail and PATCH of otherwise permitted demographic/preliminary-intake fields (with expected revision and audit); Receptionists may neither read general archived detail nor PATCH archived records. Future already-confirmed-Appointment commands may expose only separately authorized minimum context, not general archived-patient permissions in Wave 1. Owner-only archive/unarchive MUST preserve history, record sensitive evidence and change the lifecycle revision. Deny general delete/merge and arbitrary authority fields; Receptionists MUST not obtain Doctor-authored content via detail/history/expanded relations. Preserve future existing-Appointment continuation after archive and the shared lifecycle lock; actual booking/check-in/reschedule endpoints are excluded.

**Acceptance evidence**: Story 4 positive/negative cases, allowed preliminary fields versus forbidden full-clinical reads/writes, Owner archived-detail/PATCH positive and Receptionist archived-detail/PATCH denial, Owner-only archive/unarchive and default directory behavior, wrong-tenant requests, independent family edits and lifecycle-lock checks. Full appointment races remain B-09/B-12/B-18 evidence.

### Exact Master-Task Coverage and Dependencies

This is traceability to the existing approved inventory, not generated implementation tasks or a new execution sequence. Dependencies are expanded without adding or removing edges; D-09 precedes D-05 by its approved dependency.

| Master task | Exact approved dependencies | Feature coverage | Approved requirement / evidence references |
| --- | --- | --- | --- |
| D-01 | F-04 | W1-FR-01; SEC-01 | FR-001/002/072/100–102; BR-045; INV-01/19/28; AC-01/39/40 |
| D-02 | D-01, F-07 | W1-FR-02; RACE-02/03/04 | FR-003/005/070/071/075/100/103; INV-20/28; AC-21/22/39/40 |
| D-03 | D-01 | W1-FR-03; Story 4; RACE-06 | FR-006/006a/006b/007/008/090; BR-029/037/038; INV-18/33; AC-03/30 |
| D-04 | D-01 | W1-FR-04; Story 5; RACE-07 | FR-053–066/092–094/097/099; BR-032–034; INV-24/26/34; AC-14/23–25 (persistence slice) |
| D-09 | D-01, D-02 | W1-FR-05; SEC-06; RACE-08/09 | FR-012/025/036/074; BR-030/043; INV-16; AC-11/12 (substrate slice) |
| D-05 | D-03, D-04, D-09 | W1-FR-06; RACE-05/08 | FR-010–017/020/061; BR-001/003–006/024/025; INV-02/03/09/31; CON-01/33 (persistence slice) |
| B-01 | F-03, F-04, D-01, D-02, D-09 | W1-FR-07; SEC-01–05 | FR-003/069–075; BR-035; INV-01/19/20; AC-01/02/22 (available resources) |
| B-02 | F-07, D-02, B-01 | W1-FR-08; Story 2 | FR-070/075/100/103; BR-035; AC-21/22 |
| B-03 | D-01, D-02, D-09, B-01, B-02, F-07 | W1-FR-09; Story 1 | FR-100–103; BR-044/045; INV-19/28; AC-21/39/40 |
| B-04 | D-02, D-09, B-01, B-02, B-03, F-07 | W1-FR-10; Story 3 | FR-005/070/071/075/091/103; BR-035/036; INV-20; AC-02/22/34 (grant slice) |
| B-05 | D-03, B-01 | W1-FR-11; Story 4 | FR-006/006a/006b/007/008/051/082/090; BR-016/029/037/038; INV-18/33/38; AC-03/30 (registry slice) |

Ranges reference existing approved identifiers only. An AC/FR cited for a substrate slice is not claimed fully delivered when its remaining behavior belongs to another Wave.

### Security Requirements

- **SEC-01 — Fail-closed tenant boundary**: Preserve plan §2's PostgreSQL FORCE RLS on clinical tenant tables, non-owner `NOBYPASSRLS` runtime, migration-only DDL ownership, non-null tenant identity, tenant-qualified foreign keys and per-transaction `SET LOCAL app.organization_id`. Prove no clinical rows without valid context and no leaked context on pool reuse after success/failure/cancellation. Operators have only explicitly granted metadata operations, not clinical row access; metadata job delivery cannot become a global clinical bypass. FORCE RLS must be distinguished from privileged migration/maintenance access rather than claiming superusers are constrained by it.
- **SEC-02 — Signed BFF-to-API requests**: Preserve the approved HMAC-SHA256 envelope covering method, normalized received path/query, exact body digest, identity, timestamp and unique nonce; stable UTF-8/query ordering and raw body-byte hashing; allowlisted identity headers; valid service/signing key identifier; constant-time comparison. Verify before trusting identity. Reject missing/duplicate identity headers, tampering, unsigned/non-BFF calls, unknown key, stale/future timestamp and repeated nonce. Nonce uniqueness is durable by key identifier+nonce; purge only once the authenticated timestamp cannot be accepted under configured freshness/skew. Strip browser-supplied `X-CarePulse-*` identity headers and never forward bearer session cookies. No host-published privileged backend path or additional signing infrastructure is authorized.
- **SEC-03 — Current sessions and immediate revocation**: Better Auth PostgreSQL sessions with cookie cache disabled; server lookup plus live staff/clinic authorization on every protected operation. Secure HttpOnly/SameSite cookies and Origin/CSRF checks apply to unsafe methods under the approved environment configuration; production HTTPS remains a release control, not a synthetic-fixture waiver. Enforce explicitly configured idle/absolute expiry, login/recovery abuse limits and invite/reset expiry without numeric defaults. Disable/grant removal/suspension commits the live authorization/security-revision barrier before session cleanup; the BFF live-session endpoint and backend already deny stale authority. Owner reset must durably consume proof, change credentials and invalidate sessions with coordinated app revision; if the supported adapter cannot prove this, keep the identity blocked pending reconciliation and do not report success. Cleanup failure never reauthorizes. Actual adapter effects require real-session evidence, not assumed method names.
- **SEC-04 — Field and resource access**: Plan §2's allowlist is exhaustive and applies to reads, writes, search and nested history. Doctor/Receptionist may access approved demographics and only these preliminary intake fields: `preliminary_medical_history`, `chronic_illnesses`, `drug_allergies`, `food_allergies`, `prior_examination_notes`, `preliminary_test_information`. Archive transitions are Owner-only and server-owned. Doctor-authored examination/findings/diagnosis/medical notes/treatment/tests/follow-up decisions and clinical before/after revisions are not Receptionist content. Unknown/Doctor-owned mutation fields are rejected, never silently persisted; ID validity does not establish tenant ownership. Operators get no patient content and Receptionists get no security/clinical-audit contents, export or administrative management. Future Visit/attachment projections remain bound by the same matrix without introducing those resources now.
- **SEC-05 — Requests, conflicts and privacy**: Preserve plan §11.1's success/error and idempotency contracts: invalid session 401; live permission denial 403; non-visible resource 404 without existence oracle; stale revision/state or changed idempotency payload 409; invalid input 422; throttling 429; required dependency unavailable 503 without bypass. Errors use sanitized code/message/request correlation, never clinical data, credentials, tokens or stack details. Read operations cannot mutate business state. Tenant-scoped list pagination does not invent a clinical limit. Authorized same actor+operation+normalized-body retries return the committed result; a reused key with another body conflicts. A cached receipt cannot bypass current access or tenant scope.
- **SEC-06 — Audit and secrets**: Required sensitive changes and immutable history retain actor/role/tenant/action/second-precision time and appropriate reason/before-after evidence. Security denial evidence may be recorded separately, minimally and without PHI; it is not success audit. Runtime/operator attempts cannot alter/delete immutable audit; clinical content has stronger read restrictions than operational metadata. Keep secrets/proofs/patient information out of diagnostics and public output; preserve existing environment separation and required-policy fail-closed configuration. Production transit/at-rest encryption, key custody and release evidence remain governed by authorities, not certified by this specification.

### Concurrency and Atomicity Acceptance

All applicable checks require real parallel database transactions, deterministic synthetic clocks and assertions on both the winning result and absence of partial effects. Preserve plan §2's existing transaction/lock contract: tenant/live permission → patient lifecycle → policy/service-day schedule → Doctor capacity → dated lane → Appointment → claim/confirmation → history/audit/outbox; lock only applicable Wave 1 records. No network/email work may hold critical transaction locks. Later entries in that ordering are inherited contracts, not authorization to create later-Wave features.

| Evidence label / source | Wave 1 interleaving and required result | Boundary |
| --- | --- | --- |
| RACE-01 / INV-01/19/20 | Concurrent/reused tenant connections after commit, rollback, error or cancellation never inherit another clinic's authority; wrong-tenant relationships and no-context access fail. | D-01/B-01 plus affected F-04 regression |
| RACE-02 / CON-17; INV-28 | Two Owner acceptances, failed preparation, replay and ownerless/second-Owner writes yield at most one enabled Owner and no ACTIVE ownerless clinic; failed activation leaves no usable partial identity. | D-02/B-03 |
| RACE-03 / CON-30 | Acceptance versus expiry/revoke/reissue/suspend/reactivate is serialized under tenant/invitation state; superseded proofs fail, suspended clinical use stays denied, reactivation verifies Owner. | D-02/B-03 |
| RACE-04 / CON-14/36 | Reset, disable, grant removal or suspension versus an available protected patient/staff operation has a before-or-after authorization outcome. Old authority cannot commit sensitive work after the revocation commit; reset consumes once and denies all old sessions. | B-01–04; repeat with booking later |
| RACE-05 / CON-01/33; INV-02/31 | Concurrent counted persistence for one patient/day commits at most one; number allocation for distinct patients across both lane classifications commits distinct clinic/day numbers; terminal cancellation/no-show does not reuse assignments, Completed blocks new counted record. | D-05 persistence only; command proofs B-09 onward |
| RACE-06 / INV-18/33/38; CON-35/40/41 | Competing patient archive/update or new-record persistence checks serialize on lifecycle state/revision. After archive, new-ID creation guard fails; already-confirmed records/history are preserved and existing-ID continuation is not disqualified solely by archive. | D-03/B-05 lock/substrate proof now; complete create/check-in/reschedule races B-09/B-12/B-18 and admission B-15 later |
| RACE-07 / INV-24/26/34 | Concurrent policy/preview-substrate revisions cannot lose an update or replace a pinned earlier version; defaults invent no operating hours and queue effective-date data retains next-operating-day semantics. | D-04; actual pricing/schedule/queue races CON-24/26/27/28 stay with later owners |
| RACE-08 / CON-15; INV-16 | Business failure/rollback exposes no success audit, outbox or email job; invitation/recovery job is claimable only after proof issuance commits. Parallel job claims/retries preserve one logical job and no duplicate account/credential effect. | D-09/B-03/B-04; email exactly-once delivery is not asserted beyond approved job/retry guarantees |
| RACE-09 / plan §§2/11.1 | Concurrent use of one signed nonce admits at most one request; accepted nonce replay is denied, including retries after domain failure. Fresh signed requests sharing an idempotency key yield one committed business result; changed body conflicts and revoked authority gets no cached protected result. | D-09/B-01/B-02; receipt and business effects atomic |

Every denied/failed command MUST leave zero successful domain, price/history/number, success-audit or outgoing-event effects as applicable. Necessary anti-replay/security-denial records are not successful domain writes. Tests MUST not claim nonexistent queue/Visit/claim consumers were exercised.

### Existing Foundation and Later-Wave Boundaries

- Wave 0 is complete by user instruction. Reuse its pinned stack, Modular Monolith, private network, configuration validation, runtime/operator/migration role separation, transaction cleanup, Better Auth adapter, mail transport and CI. FastAPI remains sole business-logic owner; Next.js remains authentication/BFF; the existing Python worker and PostgreSQL architecture are retained without extra services.
- Observed baseline anchors: `apps/web/auth.ts` has uncached Better Auth, signup disabled and schema preflight deferred explicitly to D-02; `db/migrations/versions/0001_foundation.py` is the empty baseline. `tests/integration/test_database.py` and `test_auth_mail.py` assert the Wave 0 no-schema/deny-only behavior. Later authorized Wave 1 work must evolve those affected assertions with the new legitimate schema/login path while retaining unauthorized denial, privilege, context-cleanup and no-secret regressions. Do not rewrite the historical foundation migration or repeat initialization.
- D-04 supplies policy/preview persistence, not policy management, slot generation or schedule activation. D-05 supplies appointment/snapshot/numbered-history persistence, not booking/intake/check-in/reschedule/admission APIs, a working six-state workflow, Visit creation or Follow-up claims. D-09 supplies audit/outbox/jobs/nonces/receipts, not clinical correction workflows or general background processing.
- B-03/B-04 must prove real synthetic invitation/recovery delivery now through one post-commit sender/claim path. B-25 owns later scheduled processing/retries of that same path. B-02 proves a protected SSE boundary, not SSE delivery, reconnection/snapshots or push; those remain B-23/B-24. Account/tenant revocation contracts must remain usable by those later consumers without implementing them now.
- Patient historical/full-clinical/attachment/export functionality, final clinic UI, booking/scheduling/queue/Visit/reconciliation/bulk operations, notifications, reports, file handling, broad D-12/B-27 assurance and Wave 5 screens remain outside these 11 tasks. Referenced AC-02/11/12/14/23–25/30/34 and CON cases are covered only to their specified Wave 1 slice; their later owning tasks retain complete acceptance obligations.
- No public signup, patient login, operator clinical privileges, SaaS billing/trials, patient payments, bulk cancellation, merge/general deletion, extra Doctor/branch, e-prescription generation or additional infrastructure is authorized.

### Acceptance Evidence and Exit

Wave 1 delivery acceptance requires **all 11 tasks' own evidence** and plan §6 exit/§13.3 W1 checks: affected F/D/B regressions; real PostgreSQL tenant/auth/patient/appointment/audit tests; Better Auth+BFF/backend integration; signed forgery/replay denial; invite/reset replay; old-session denial; operator PHI denial; patient family/archive behavior; first-Owner acceptance smoke and committed Mailpit invite/recovery delivery. Use focused positive, negative and race checks, not UI-only assertions or mocks for persistence/security races.

Required container, real PostgreSQL and applicable E2E evidence runs in Docker-capable CI with identifiable commands, results, run links/logs/artifacts. Docker is unavailable locally and MUST NOT be installed or requested. Container-free document/static checks are local. Missing CI proof is BLOCKED/UNVERIFIED, not passed. Later D-12/B-27 integration cannot substitute for a Wave 1 task's focused proof. IMPLEMENTED, TESTED, INTEGRATION VERIFIED and PRODUCTION READY remain distinct; this specification claims none of them for Wave 1 implementation.

### Key Entities *(include if feature involves data)*

- **Organization / Operator Identity**: immutable clinic boundary, one branch, administrative lifecycle and a separate enabled metadata-only operational actor with audit identity.
- **Staff Account / Session / Security Revision**: tenant-bound Doctor/Owner or Receptionist, enabled state, explicit bulk grant, verified identity, supported server session and current revocation authority.
- **Invitation / Recovery Proof**: invited/verified email, purpose, digest, issue/expiry/consumption/revocation evidence; acceptance grants only the approved role and cannot be replayed.
- **Patient**: stable independent clinic record, name parts, optional raw/normalized phone, DOB or age/reference date, permitted preliminary intake and archive/lifecycle revision.
- **Policy Version / Preview Substrate**: immutable historical configuration versions and effective dates, schedule/pricing/admission/queue settings and affected-booking revision evidence, without operational settings flows.
- **Appointment / Snapshot / Number Assignment / History**: future operational booking record separate from Visit, service date, six-state value, optional interval, frozen EGP/provenance, unique historic number allocation and retained change/replacement evidence.
- **Audit / Outbox / Job / Idempotency Receipt / Request Nonce**: distinct immutable accountability, committed delivery intent, nonclinical email work, committed retry result and one-use trusted-request proof; no clinical-access capability follows merely from possessing an identifier.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In every acceptance case, an authorized operator can establish one invited clinic Owner, and **zero** uninvited, replayed or competing attempts create a second enabled Owner or an ownerless active clinic.
- **SC-002**: **100%** of the specified cross-clinic, forbidden-role/field, unauthenticated, forged and replayed access cases disclose no protected information and make no successful business changes, while their authorized counterparts succeed.
- **SC-003**: The **first protected operation after committed access withdrawal** cannot use the old authority, across all existing sessions in the acceptance fixtures; successful reset leaves **zero** usable prior sessions and its proof cannot be consumed twice.
- **SC-004**: Staff can register and independently select all synthetic family members sharing a phone, preserve each patient's history and age evidence, and edit one member with **zero** unintended changes to another. Every archive case hides the patient from active-default results without deleting existing history.
- **SC-005**: Every specified simultaneous counted-record and number-assignment case preserves **at most one** counted patient/day record and **zero** reused successful clinic/day numbers; every failed transaction leaves **zero** partial successful business/history/outgoing-event effects.
- **SC-006**: **100%** of required sensitive-operation cases retain the correct authorized actor/action/time evidence; unauthorized evidence changes disclose or alter **zero** protected records. Invitation and recovery deliveries occur only for committed jobs and let the intended synthetic recipient complete the valid flow.
- **SC-007**: Initial clinic settings match every applicable approved default, and **11 of 11** master tasks have traceable positive/negative and applicable concurrency evidence before Wave 1 delivery is called complete. No out-of-scope workflow or unapproved production policy is needed to demonstrate this slice.

## Assumptions

- Completed Wave 0 and Constitution v1.1.0 are the starting baseline; source inspection here identifies handoff points, not a new execution/evidence audit of completed work. Existing pins and approved architecture remain binding.
- Minimal role-authorized backend/BFF flows and the restricted operator CLI satisfy Wave 1; final user-interface work is owned by Wave 5. Story acceptance does not demand those screens early.
- Test-only security policy values may be supplied only in explicitly labeled synthetic environments under the authorized 2026-10-21 SECURITY-POLICY-TIMING-VARIANCE. No values, performance targets, user-volume promises or production recommendations are selected here. Acceptance exercises configured boundaries with deterministic clocks rather than assuming durations.
- **Demographic authority interpretation**: PRD §2 explicitly permits an optional phone; FR-006b/BR-029/037 govern preservation/normalization when one is provided. FR-006's DOB-or-declared-age-with-reference-date rule governs age capture; lower-priority B-05 shorthand saying “optional DOB or declared age+reference” does not waive that requirement. No fictitious DOB, phone or new age policy is inferred.
- **Archived-access approval (A1)**: Product Owner explicitly approved that the own-clinic Doctor/Owner may read and PATCH permitted demographic/preliminary-intake fields on archived patients without unarchiving; Receptionists are denied general archived detail and demographic/intake PATCH. The minimum purpose-bound context for permitted pre-existing-Appointment work is reserved to later commands and does not grant Wave 1 general archived access. Archive does not grant Doctor narrative access, general archival administration or new booking rights. Export remains later scope.
- This invocation authorizes specification and native quality-checklist generation only. It ends before a new implementation plan, task generation, migrations, code changes, execution or deployment.

### Material Ambiguities and Unresolved Decisions

No unresolved product choice blocks this **synthetic Wave 1 specification** after applying the stated authority precedence. Existing deferred production decisions are explicit gates, not clarification markers to be silently filled or reasons to invent defaults:

| Decision | Required clarification/approval and owner | Effect on this scope |
| --- | --- | --- |
| SECURITY-POLICY-NUMERICS | Product Owner + Security/Operations must approve actual idle/absolute timeouts, login/recovery attempt/window/reset policies, invitation/reset expiry, BFF freshness/skew, secure email provider and the production evidence required by plan §§2/14. Other listed policies (Doctor confirmations/download grants) remain with their later capabilities. | Synthetic boundary tests use labeled fixtures only. Required absent production policy fails closed; no real-user/patient operation is authorized. |
| OPEN-OPS-01 | Product Owner + Operations/Security: database/attachment backup schedule, retention, custody and successful restore evidence. | Production with patient data remains blocked; no backup values selected by Wave 1. |
| OPEN-OPS-02 | Product Owner + Operations: numeric RPO/RTO and demonstrated recovery for records and files. | Production remains blocked; no recovery promises inferred. |
| OPEN-OPS-03 | Product Owner + qualified Egyptian legal/privacy adviser + Security/Operations: residency, retention/access, legal basis, hosting and written authorization. | Production remains blocked; no legal conclusions or retention defaults invented. |
| OPEN-FILES-01 | Product Owner + Security/Data Governance: allowed file policy/limits, scan/quarantine behavior, download lifetime and evidence. | Production uploads remain blocked; files are not part of Wave 1 delivery. |

These decisions retain their full ownership/evidence/capability restrictions in plan §14 and PRD §12. SECURITY-POLICY-TIMING-VARIANCE is already authorized, not awaiting reapproval; non-billed pilots do not bypass production gates. FUTURE-SAAS-COMMERCE remains a separately approved future scope, not a Wave 1 decision.
