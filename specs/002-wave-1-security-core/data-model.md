# Wave 1 Data Model

This is a design for the existing D-01–D-05/D-09 tasks, not executable migrations or approval of new entities beyond their substrate. Sources: [spec.md](spec.md), master plan §§2/6/11.1, Domain §§3–4/10. Logical names below guide migrations; Better Auth's generated model names/types remain library-owned.

## Shared rules

- Application IDs are immutable UUIDs; preserve Better Auth's native identity/session ID types rather than converting them to UUIDs. Every clinical row has `organization_id NOT NULL`; referenced application IDs have a unique `(organization_id, id)` pair and tenant-qualified FKs. No clinical cascade deletion.
- UTC `timestamptz` for events and validity instants; `date` for Cairo service/reference dates; `Africa/Cairo` is fixed. Revisions are server-incremented integers, never client-assigned authority. Expected revision is checked inside the locked transaction.
- Clinical tables have FORCE RLS with both read and write predicates. Missing/empty/invalid tenant context fails closed. Application runtime is not table owner and has no BYPASSRLS/DDL; operator has no clinical grants. Auth/operator/nonclinical delivery metadata has explicit narrower privileges rather than a global clinical bypass.
- READ COMMITTED; lock tenant/live permission → patient lifecycle → policy/service day → later Doctor capacity/dated lane → Appointment → later claim/confirmation → evidence. Lock applicable rows only; sort multiple IDs deterministically. External auth and SMTP work never holds these locks.
- Immutable rows have revoked runtime UPDATE/DELETE plus database mutation guards; updates to mutable delivery state never rewrite historical evidence. A privileged migration role is not represented as an untrusted runtime.

## D-01 — Organization and operator

| Entity | Essential fields | Relationships / constraints |
| --- | --- | --- |
| Organization | `id`, single `branch` value, `region=EGYPT`, `timezone=Africa/Cairo`, `currency=EGP`, `state`, `lifecycle_revision`, creation/update instants, operator evidence reference | States only PROVISIONING/ACTIVE/SUSPENDED. Immutable tenant ID and fixed MVP attributes. One branch value, not a multi-branch resource. |
| OperatorIdentity | `id`, credential digest, `enabled`, credential revision, issue/revoke evidence | Separate from Staff/Better Auth clinic session. Constant-time credential verification and configured abuse control; no credential in CLI arguments/output, image or application service configuration. |

D-01 allows provisioning metadata only; no activation before D-02's cross-table guard and B-03's verified command exist. Initial non-billed provisioning records PROVISIONING. ACTIVE → SUSPENDED and previously-ACTIVE SUSPENDED → ACTIVE require reasoned authorized-operator audit, lifecycle lock and a currently verified enabled Owner. Initial PROVISIONING → ACTIVE requires accepted Owner invitation. Suspending a never-ACTIVE/pending clinic cannot make it eligible for reactivation or let invitation acceptance bypass SUSPENDED; ownerless transitions deny. Preserve prior activation provenance to distinguish these cases. No delete or billing transition.

## D-02 — Staff, auth and proofs

| Entity | Essential fields | Relationships / constraints |
| --- | --- | --- |
| StaffAccount | `organization_id`, `id`, auth user reference, verified email association, role `DOCTOR_OWNER/RECEPTIONIST`, `enabled`, `bulk_reschedule=false`, `revision`, `security_revision`, authorization block/pending-operation reference | Tenant-bound auth association; no ambiguous session-to-clinic lookup. Owner-only management of Receptionists; no Owner transfer or extra Doctor. Bulk flag only authorizes the later operation, not staff/settings access. |
| Better Auth models | Supported user/account/session/verification schema and native uniqueness/FKs | Generated from installed supported tooling, reviewed/applied by migration role. Credentials and bearer session material never enter application DTOs or audit. No custom replacement session table. |
| Session authorization fields | Native session reference; server-only captured staff/security revision, immutable absolute deadline, last validated activity instant | Supported schema extension attached to actual session. Client cannot write these fields. Deleted/revoked sessions are never recreated by activity updates. Compare captured revision, never silently refresh to current revision. |
| Invitation | `id`, tenant, intended email, purpose (first Owner/Receptionist), intended staff reference where applicable, proof digest, `issued_at`, `expires_at`, `accepted_at`, `revoked_at`, supersession reference, issuer/acceptance evidence, revision | ISSUED → ACCEPTED or REVOKED; expiry derived, consumption requires `now < expires_at`. Reissue revokes prior proof atomically. At most one actionable first-Owner invitation per tenant; consume once under tenant+invitation lock. |
| RecoveryOperation | immutable operation ID, tenant/staff/native-auth-user references, purpose/proof digest/reference/expiry, keyed request fingerprint and key version, captured staff/tenant lifecycle and reserved security revisions, state, invalidation flag, auth fence, timestamps/`closed_at` | Unique `(purpose, proof_digest)` retained after consumption; partial unique native auth user where `closed_at IS NULL`. `RESERVED` → `AUTH_DONE` → `FINALIZED`, or explicitly reconciled `ABORTED`; no durable timeout takeover. Staff block owns this operation ID. Matching retries reuse it without another revision bump; changed credential conflicts. See [reset protocol](contracts/security-lifecycle.md#5-recovery-issuance-and-durable-reset-coordination). |
| AuthRecoveryCompletion | unique operation ID, proof/native-user binding, request fingerprint, reserved security revision, auth fence, completion instant | Immutable nonsecret evidence written in the same supported adapter transaction as proof consumption, credential update and session deletion. Finalization/audit are exactly-once per operation under the domain barrier; absent evidence is not success. |
| Auth recovery verification | Better Auth digest verification, auth user reference and expiry | Issuance shares an adapter transaction with delivery intent; consume/password write/session revocation share the supported durable auth transaction. Not a second password store. |

**Owner invariant**: Partial uniqueness allows at most one enabled Owner per tenant. A deferred cross-table constraint requires exactly one verified enabled Owner for ACTIVE; both Organization activation and relevant Staff/verification changes acquire the Organization lifecycle lock. Reject ownerless activation, a second enabled Owner and removal/disablement/unverification of the sole ACTIVE Owner. D-02 must demonstrate direct invalid DB writes fail, not merely BFF requests.

**Identity preparation**: Auth identity may exist before application activation, but cannot authorize a clinic operation. Verified possession of the intended invitation/email must match the prepared identity. Final staff association and tenant activation occur atomically in B-03; abandoned preparation does not create a working account.

**Revocation**: Disablement or grant withdrawal increments live security revision under the same barrier used by sensitive commands. Re-enabling cannot make older session revisions current. Tenant suspension denies both roles immediately. See [security-lifecycle.md](contracts/security-lifecycle.md) for login/reset interleavings.

## D-03 — Patient

| Field group | Representation / validation |
| --- | --- |
| Identity | `organization_id`, immutable `id`; tenant-qualified uniqueness and references. |
| Name | `name_parts`: exactly three or four nonempty parts; preserve entered text without inferring extra name parts. |
| Contact | Optional `phone_raw`; optional derived `phone_normalized` in E.164 when parseable, Egypt default with explicit international support. Keep original input even when no normalization is possible. Neither field is unique. |
| Age evidence | Either `date_of_birth`, or `declared_age` plus `age_reference_date`; reject missing/both/incomplete alternatives. Reject negative/nonfinite age or impossible date values; no invented maximum age or fabricated DOB. |
| Intake | Optional `preliminary_medical_history`, `chronic_illnesses`, `drug_allergies`, `food_allergies`, `prior_examination_notes`, `preliminary_test_information`; explicit authorized fields, separate from Doctor-authored Visit content. |
| Lifecycle | `archived=false`, `lifecycle_revision`, demographic revision, creation/update instants; archive actor/reason/time in immutable audit. |

Indexes start with tenant and support active-default listing, deterministic ID-tiebroken pagination, name and normalized phone lookup. Shared names/phones are valid independent records; search spans all authorized pages. An edit changes one selected patient only.

Active ↔ archived is an Owner-only command under patient lifecycle lock; archive never deletes history or cancels existing Appointments. New-ID creation checks active lifecycle under that same lock, including future replacements. Existing-ID continuation is not disqualified solely by archive. Wave 1 proves this guard/storage distinction; actual check-in/admission/reschedule remains with later owners. No merge/delete or foreign-key cascade that removes clinical history.

**Approved archived-record field access (A1)**: Under the live role/tenant boundary, the own-clinic Owner may read and PATCH only approved demographic/preliminary-intake fields of an archived Patient with expected demographic revision and sensitive audit; PATCH never clears the archive flag. The Receptionist's general Patient detail and PATCH are available for active Patients only. Archived Patient IDs are non-visible through those general Receptionist endpoints (non-oracular 404), with no read, mutation or disclosure. Future previously-confirmed-Appointment operations may expose only the minimum purpose-bound context their own contracts authorize, not a new general archived permission in Wave 1.

## D-04 — Policy versions and schedule substrate

Store tenant-qualified policy identity/current revision separately from immutable historical values. Relevant groups have explicit revisions/effective dates rather than one mutable JSON blob that silently changes confirmed history.

| Logical record | Fields / constraints |
| --- | --- |
| PolicyVersion | tenant, ID, policy kind, revision, values, creator/time, effective service-date metadata; version uniqueness per tenant/kind. |
| Price version | standard/preferential/urgent configured values, enabled state, EGP and provenance. Initial prices unset, never invented. |
| Calendar/schedule version | working-day calendar, operating periods, breaks, holidays, explicit date overrides, Open/Exact-Time mode, optional duration, manual slots, overlap/capacity configuration. Positive intervals when supplied; no inferred periods. |
| Queue policy version | booked-order Routine policy, late depth, next-operating-day effective metadata. Existing day snapshot references are preserved; no Wave 1 QueueDay table required. |
| Admission/capacity version | admission Both initially, capacity one and revision; distinguish immediate controls from future-effective queue policy. |
| Preview substrate | tenant, preview ID, actor, selected/affected references, captured policy/service-day/record revisions and candidate change evidence | 

Preview records support later B-07 revalidation; no DayReview dependency, Appointment hard FK before D-05, live preview API, activation command or slot generator. When a working calendar is not configured, do not guess a next working date; retain pending effectiveness metadata for the later authorized scheduler.

**Required initial values**: Open day-only; no inferred duration/hours/closing/cutoff/cap; no breaks; overlap OFF; holiday/nonworking-day booking OFF; Routine walk-in OFF; Paid Urgent OFF; capacity 1; admission Both; Routine booked order; late depth 5; fixed EGP and `Africa/Cairo`; follow-up day zero through seven inclusive where represented. No-show is explicitly `{enabled:false, local_time:null}`. Per-Receptionist bulk flag defaults false in D-02. Emergency independence is not changed by Routine/Urgent toggles.

**Initialization invariant (D-04)**: Every Organization, including PROVISIONING and SUSPENDED, receives exactly the applicable initial policy versions once D-04 is installed. In one transactional D-04 migration, create version storage/uniqueness and a minimal Organization AFTER INSERT initializer, then backfill existing organizations; migration locks must close the concurrent-create gap. Future Organization inserts seed defaults atomically with creation, without B-03 calling policy code. Use a narrowly scoped NOLOGIN SECURITY DEFINER writer, fixed safe search path, revoked PUBLIC invocation, policy-specific INSERT grants and FORCE RLS allowance; no operator policy grants or general RLS bypass. Seed only the inserted tenant's approved values with insert-if-missing/unique version protection. Never overwrite configured versions or invent prices/hours/cutoffs, and prove initialization/rollback for both D-04-before-B-03 and B-03-before-D-04 orders.

Revision collision rejects stale updates; old policy values remain available. Confirmed price/entitlement provenance is immutable. Actual schedule/price/queue command behavior remains B-06 onward.

## D-09 — Evidence, replay and delivery

| Logical record | Fields / constraints |
| --- | --- |
| SensitiveAudit | ID, tenant where applicable, actor/operator identity, role, action, UTC time accurate to seconds, reason where required, minimized meaningful before/after, correlation | Append-only; operator metadata audit contains no patient content. Receptionists/operators cannot read security/clinical audit contents. |
| ClinicalAudit | tenant, event ID, authorized Doctor actor, clinical revision/before-after references and protection | Separate stronger read restriction, append-only. Substrate only; no Visit correction behavior in Wave 1. |
| OutboxEvent | tenant, event ID, type, aggregate reference, minimized authorized payload, event time | Immutable committed intent; no hard Appointment FK. Domain success and event commit together. No delivery before commit. |
| Delivery/job state | job ID, unique logical deduplication key, kind, tenant metadata if needed, state, attempts, `not_before`, `claim_owner`, monotonic `claim_generation`, `claimed_at`, `lease_expires_at`, attempt outcome | `READY` → `CLAIMED` → `SENT`/`READY`/`FAILED`; unusable proof → `CANCELLED`. Expired `CLAIMED` is reclaimable only by a new generation. Every finish/material mutation requires matching owner/generation and unexpired lease; see [claim protocol](contracts/security-lifecycle.md#6-shared-post-commit-nonclinical-sender). Mutable delivery state is separate from immutable event; retries cannot duplicate logical domain effects. |
| Nonclinical email job | job ID, invitation/recovery reference, recipient, template/purpose, proof expiry, protected delivery material reference, issuance/delivery state | Proof + job commit together. Restricted metadata-only claim; no global clinical read needed. Recipient/purpose/proof/expiry are issuance context, not mutable retry input. |
| ProtectedDeliveryMaterial | job/proof reference, format version 1, delivery key ID, RSA-OAEP ciphertext (base64url) | Encrypt token only; RSA-3072/OAEP+MGF1 SHA-256 with context label binding version/key/job/proof/purpose/tenant/recipient/expiry. Maximum plaintext 318 bytes. No AES key/nonce/tag/wrapping fields. Restricted ciphertext is distinct from verifier digest and generic job payload; disposal is conditional on current claim ownership or fenced terminal invalidation. |
| RequestNonce | key identifier, nonce, authenticated request timestamp, accepted time | Durable unique `(key_id, nonce)`, insert only after valid signature/freshness; accepted nonce survives domain rollback. Purge only after it cannot be accepted under configured freshness/skew. |
| IdempotencyReceipt | checked scope kind (`TENANT` or `OPERATOR_CREATE`), scope identity, actor kind/ID, operation, idempotency key, normalized request digest, committed result reference/projection and revision; tenant identity required for `TENANT` rows, absent before `OPERATOR_CREATE` allocation | One shared table with unique (scope kind, scope ID, actor, operation, key). `OPERATOR_CREATE` is limited to verified operator + `clinics.create`, with scope ID derived from operator identity; its committed safe result references the newly created Organization. Ordinary clinic rows remain tenant-scoped and RLS protected. Same key/body returns that result, changed body conflicts, and the receipt commits atomically with effects. Live operator/tenant authorization is rechecked before reading; no cross-scope/clinical receipt disclosure. No second receipt table. |

Secrets in delivery material are a narrow transport exception to digest-only verification storage, not a second usable plaintext proof column. Ciphertext is inaccessible to operator list/read DTOs and ordinary staff; issuers have public keys only. Decrypt only within the trusted sender invocation/container (including its Python launcher) with separately held private keys. OAEP context/digest checks prevent substitution but do not authenticate the issuer; restricted writes establish authority. Ciphertext lifecycle is bounded by proof usability/delivery need, without inventing production retention values; retain key versions only as needed for outstanding deliverable jobs. See [encryption/execution protocol](contracts/security-lifecycle.md#6-shared-post-commit-nonclinical-sender).

## D-05 — Appointment and immutable historical numbering

| Logical record | Fields / constraints |
| --- | --- |
| Appointment | tenant, immutable ID, patient and Doctor references, Cairo service date, status, service type, tier/lane, current number/assignment reference, optional start/end instants, revision, frozen snapshot reference | Tenant-qualified patient/Doctor relations. Exactly Booked, Waiting, In Examination, Completed, Cancelled, No-show. No derived seventh status. |
| PriceSnapshot | tenant/Appointment ID, service classification, EGP base, discount type/value/reason, free flag, final net, pricing version, creation-time standard base, applicable entitlement provenance | Immutable 1:1 snapshot. NUMERIC and server Decimal; finite values, EGP two fractional digits; exact intermediate calculations, one final-net ROUND_HALF_UP. API money strings. |
| NumberCounter | tenant, service date, last assigned number | One allocation boundary for entire clinic/day, not lane-specific. Counter and assignment commit with Appointment/history. |
| NumberAssignment | tenant, service date, number, Appointment ID, assignment time and prior-assignment/history reference | Unique tenant/day/number across all successful historical assignments; append-only. No reuse on cancel/replacement/reschedule. Failed rolled-back assignment is not a successful allocation. |
| AppointmentHistory | tenant, ID, Appointment, action/from-to, old/new service date and number, actor/role/time/reason, snapshot and replacement references | Immutable history; original terminal record retained; same-tenant replacement links. No operational transition API introduced. |

Partial UNIQUE `(organization_id, patient_id, service_date)` counts Booked/Waiting/In Examination/Completed. Cancelled and No-show permit a distinct new ID; Completed blocks another counted ID. Terminal records never reopen/reschedule; database guards reject prohibited terminal field changes. Six-state vocabulary alone is not full command-state-machine proof.

Open has no interval. Timed records have both instants and `start < end`, interpreted half-open `[start,end)`. The stored Cairo service date remains explicit. Later B-08 owns calendar/DST/overlap validation; a positive interval alone does not authorize a booking.

Snapshot constraints reject negative/above-base discounts, absent required discount reason, inconsistent net/free flags, nonfinite values and Free Urgent. Urgent base **and rounded net** must exceed creation-time standard base. Store provenance needed to verify the frozen value; do not manufacture a FollowUpClaim/Visit before their owning tasks. Policy edits and same-ID reschedule never reprice old snapshots. No payment status/receipt model.

Use D-09 receipts/audit/outbox; number/history/event writes roll back with failed domain work. Persistence fixtures demonstrate simultaneous counted creation and cross-lane allocation without exposing booking, queue, Visit or pricing APIs.
