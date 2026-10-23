# Wave 1 HTTP and Operator Contracts

Sources: master plan §§2/6/11.1; [feature requirements](../spec.md). This is a normative implementation design for the Wave 1 slice, not a declaration that routes exist today. Next owns browser `/api/*`; FastAPI owns corresponding private `/v1/*` product commands. Retain the existing BFF catch-all as an internal forwarding implementation if useful; do not expose the private API directly.

## Common protocol

- JSON snake_case; RFC3339 UTC instants and ISO dates; EGP money as decimal strings. Stable enum values independent of UI language.
- Success `200/201 {data, revision, event_id?}`. Collection `data` contains `items` and an opaque `next_cursor`; server-capped pages are not a clinical limit. Cursor/query scope must not expand tenant/role access.
- Failure `{error:{code,message,request_id}}`: 401 invalid/expired session, 403 live role/clinic denial, 404 non-visible resource, 409 stale revision/state or `IDEMPOTENCY_CONFLICT`, 422 invalid input, 429 configured throttling, 503 required security/storage/mail dependency unavailable. Do not expose PHI, tokens, passwords, keys, SQL, stack traces or foreign-resource existence.
- GET cannot mutate business state. Session validation/activity bookkeeping is not a business command. Unsafe browser requests require strict same-origin and request-forgery protection, even through server-side Better Auth wrappers.
- `organization_id`, actor, role, verified state, grants outside the designated grant command, archive transitions and other server-owned fields are rejected in ordinary input. Unknown input fails 422; forbidden known fields fail 403/422 according to resource schema. No mass assignment or silent persistence.
- Creates use `Idempotency-Key`; mutation revision fields are mandatory where state/revision is relied upon. Ordinary clinic operations use tenant+staff actor+operation+key; the operator-only `clinics create` command uses a distinct verified-operator+operation+key scope before a tenant exists, in the **same** D-09 receipt table. Normalized request content determines duplicate versus 409 conflict. Revalidate actual operator/staff authority before returning any receipt; browser protected retries also require a fresh valid signed transport nonce. Same authorized body returns only the committed role-safe result. No credentials/proofs in saved response bodies.
- All detail IDs, search terms, cursors and nested relation requests are evaluated within live tenant authorization. Denial leaves no successful business/history/audit/outbox writes. Minimal separate security-denial/nonce evidence is permitted.

## B-02/B-04 — Authentication surface (BFF only)

| Route | Input / output | Required behavior |
| --- | --- | --- |
| `GET /api/session` | No tenant/role input; return actor/staff ID, own clinic, role, current capabilities and revision, never bearer/session token | Actual Better Auth DB session plus current staff/clinic and deadline checks; stale/revoked access denied. |
| `POST /api/auth/login` | Registered invited email and password; return sanitized session projection and secure session cookie | Verified enabled invited account, ACTIVE clinic, configured throttle, Origin/CSRF; revision captured before credential check and still valid at session binding. No token in JSON. |
| `POST /api/auth/logout` | Current cookie | Revoke actual session, clear cookie; subsequent protected call denied. Do not forward cookie to API. |
| `POST /api/auth/invite-accept` | Single-use invitation proof, intended verified email/identity preparation input and credential setup as supported | First Owner uses only narrow activation exception. Receptionist acceptance applies current tenant/invitation guards. No general pre-ACTIVE session. Never return proof or raw auth internals. |
| `POST /api/auth/reset-request` | Registered email | Uniform non-enumerating acknowledgment for eligible/ineligible email; configured abuse controls. Eligible verified Owner proof + job commit together; no token returned and no inline SMTP. |
| `POST /api/auth/reset-complete` | Recovery proof and new credential; operation identity is server-resolved from proof | Matching proof+exact credential resumes the same operation or minimal finalized acknowledgment; different credential is 409 `IDEMPOTENCY_CONFLICT`, another unresolved proof for the identity is 409 `RESET_IN_PROGRESS`. No password/proof in receipts. Exact expiry applies to consumption, not completion-evidence-only finalization. Success only after durable auth/session effect and safe finalization; uncertainty stays blocked. See [reset protocol](security-lifecycle.md#5-recovery-issuance-and-durable-reset-coordination). |
| `GET /api/events/stream` | Cookie only | B-02 proves unauthenticated 401 and live authorization guard. Authenticated requests remain closed without event delivery until B-23; no cursor/reconnection/snapshot feature claimed. |

Use narrowly exposed handlers, not an unrestricted Better Auth catch-all enabling public signup, alternate reset paths, email change or other unapproved endpoints. A GET invite/reset landing page may display a form only: it never consumes proof. Redirects are allowlisted and proofs are excluded from request/access logs and referrers.

## B-04 — Receptionist management

Doctor/Owner only, own clinic. Private routes mirror `/v1/staff/receptionists`; these are not operator operations.

| Browser route | Body / result |
| --- | --- |
| `GET /api/staff/receptionists` | Paginated authorized Receptionist metadata, enabled/invite state, bulk flag and revisions; no auth secrets. |
| `POST /api/staff/receptionists` | Email/approved identity metadata and reason, create idempotency key; server role Receptionist, bulk false, activation only by invitation. Return safe ID/status/revision. |
| `GET /api/staff/receptionists/{id}` | Same safe projection; foreign/non-visible ID 404. |
| `POST /api/staff/receptionists/{id}/disable` or `/enable` | Reason + expected staff/security revisions. Re-enable does not revive old sessions or consume a pending invitation. |
| `POST /api/staff/receptionists/{id}/invite` or `/revoke-invite` | Reason + expected revisions; reissue supersedes prior proof, committed delivery intent only. |
| `PATCH /api/staff/receptionists/{id}/permissions` | Only `bulk_reschedule: boolean`, reason + expected staff/security revisions. |

Changes are audited; disable/grant withdrawal commits live revision barrier before cleanup. Attempts to change Owner, role, tenant, own grants as Receptionist, clinical settings authority or other grants fail. Old-tab and revoked-retry checks are mandatory; no bulk-reschedule command appears in Wave 1.

## B-05 — Patient registry

**Independent delivery boundary (I1)**: B-05 requires exactly D-03 and B-01. Its independently deliverable surface is the private `/v1/patients` API, domain queries/commands and their focused backend evidence. The browser route names below specify the eventual corresponding `/api/patients` surface, not a prerequisite to backend completion. Before B-02 is available, exercise the real B-01 signature/session/live-authorization boundary using legitimately persisted supported auth fixtures and private test connections; never ship a fixture route, signing bypass or public API port. No B-02 proxy import, Next build of an absent helper, browser session or invite flow may gate this backend delivery.

Once B-02's guarded proxy is available, connect the matching browser paths without duplicating patient rules. After B-03, repeat Owner cases through an actual invited Owner session; only after B-04/T049 implements Receptionist invitation acceptance can the real invited Receptionist cases run. These are deferred integration checks of the same B-05 surface within Wave 1, not new B-05 master dependencies. Report each unavailable stage BLOCKED/NOT RUN separately from independently verified backend work; final Wave 1 exit requires both invited roles.

| Browser route | Input / result and authorization |
| --- | --- |
| `GET /api/patients` | Optional name/phone search and opaque cursor; active-default results. Both roles receive permitted fields only. Owner may explicitly query archived records; no general Receptionist archive-administration listing. Shared-phone matches remain individually selectable across all pages. |
| `POST /api/patients` | `name_parts`, optional `phone_raw`, DOB or age/reference date, optional intake fields; create idempotency key. Both roles allowed. Server derives tenant/ID/normalized phone/archive/revision. |
| `GET /api/patients/{id}` | Both roles' authorized demographic/intake projection for active patients. Own-clinic Owner can read archived detail. Receptionist general archived detail is unavailable (non-oracular 404). Minimum purpose-bound context for an existing Appointment belongs to later separately guarded operations, not this general detail endpoint. |
| `PATCH /api/patients/{id}` | Only demographic/intake fields with expected revision. Both roles may edit active records; only own-clinic Owner may PATCH these permitted fields while the patient is archived, preserving archive state and auditing the edit. Receptionist PATCH of archived records is denied without record-existence disclosure (non-oracular 404). Editing a phone affects only this ID. |
| `POST /api/patients/{id}/archive` or `/unarchive` | Owner only, reason + expected lifecycle revision. Increment revision and append audit, retain history. |

**Readable patient projection**: ID, name parts, raw/normalized phone, DOB or age/reference date, archive flag, revision and the six intake fields below. Return no implicit populated clinical relations. IDs/normalized phone/archive/revisions are read-only in ordinary mutations.

**Intake read/write allowlist for both roles**: `preliminary_medical_history`, `chronic_illnesses`, `drug_allergies`, `food_allergies`, `prior_examination_notes`, `preliminary_test_information`.

**Denied through every Patient DTO/search/expansion**: Doctor-authored Visit examination/findings/diagnosis/medical notes/treatment/tests/follow-up decisions and clinical before/after revisions for Receptionists; all patient content for operators. Do not copy Doctor content into intake fields as a projection shortcut. No Visit/history/attachment/export endpoint is added here.

**Validation**: exactly 3/4 nonempty name parts; DOB or nonnegative declared age with reference date (no fabricated DOB or invented maximum); optional phone with raw preservation and possible E.164 normalization; duplicate demographics do not block by themselves. [data-model.md](../data-model.md) owns persistence rules. Unknown/server-owned fields are rejected. Archive guards new IDs, not blanket existing-ID continuation.

## B-03 — Restricted one-shot operator CLI

Planned entrypoint, matching master §11.1:

```text
ops/carepulse-admin clinics create
ops/carepulse-admin clinics list
ops/carepulse-admin clinics invite
ops/carepulse-admin clinics revoke-invite
ops/carepulse-admin clinics suspend
ops/carepulse-admin clinics reactivate
```

This grammar does not prescribe unsafe credential flags. Each invocation obtains an individually issued operator credential through protected secret input, verifies its digest against an enabled identity in constant time, applies configured abuse controls and records actor/reason. A reason is required for every action, including metadata listing under the master operator contract. Mutating existing metadata requires expected lifecycle/invitation revision. Reissue uses `invite`, revoking superseded proof atomically; no extra public action or operator HTTP/socket listener.

**Operating procedure to implement/document with B-03**:

1. Authorized operations staff start a disposable command under the restricted Compose operator profile, supplying metadata-only DB credentials and individual operator secret separately from ordinary service configuration. No host API/DB publication and no credentials in command arguments/shell history or images.
2. `create` takes minimal clinic/branch and first-Owner email metadata plus reason and idempotency key. After authenticating the current operator, look up its unique `OPERATOR_CREATE/clinics.create` receipt by operator ID and key **before** allocating a clinic ID. Same normalized request returns the prior safe clinic/invitation status; changed request is 409. A new request commits PROVISIONING clinic, invitation, audit, nonclinical delivery job **and operator-scoped receipt** in the same transaction. A concurrent same-key create has one committed winner; loser rolls back all tentative effects and reads the winner's committed receipt after resolving the uniqueness conflict. A lost-response retry never creates another clinic, invitation, job or audit. Stdout contains only authorized metadata IDs/status/revisions, never invitation token or clinical data.
3. After successful commit, invoke the shared one-shot sender through the authorized CLI/test harness. It claims committed jobs and uses existing Mailpit transport. A delivery failure does not activate the clinic or recreate an invitation; later retry uses the same job/proof.
4. The synthetic intended recipient accepts through the BFF's restricted verified-email route. Only atomic unique Owner association plus ACTIVE transition grants clinic access. CLI itself cannot establish a staff session.
5. `list` returns only clinic ID, lifecycle/revision and approved bootstrap/invite metadata. `suspend` revokes effective clinic access immediately; `reactivate` permits SUSPENDED → ACTIVE only for a clinic previously ACTIVE, with a currently valid verified enabled Owner under the lifecycle lock. Never-ACTIVE/ownerless suspended clinics cannot be reactivated; suspended invitation acceptance is no bootstrap bypass. Preserve all data/history. Audit every action and inspect redacted outcomes.

Invalid/disabled credentials or staff sessions fail. Operator grants cannot read/write patients, Appointments, clinical audits, prices/settings, exports or auth credential/session secrets. Provisioning commands are one-shot Python domain operations with narrowly scoped metadata DB access; they do not bypass FastAPI-owned business rules by implementing a second ruleset in shell/Next.

## Excluded surfaces

No booking/intake/reschedule/queue/Visit/pricing/schedule management, generic job administration, public registration, operator HTTP, billing/payment, files/export, notifications or general audit feeds. D-04/D-05/D-09 evidence uses DB fixtures and internal checks. Later master contracts remain binding, but are not operational Wave 1 endpoints.
