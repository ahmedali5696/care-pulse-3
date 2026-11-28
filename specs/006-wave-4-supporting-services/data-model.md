# Data Model: Wave 4 Supporting Services

**Status**: Proposed physical design under D-10/B-21–26; no migration executed. [Plan](plan.md), [wire contract](contracts/http.md), [transaction contract](contracts/transactions.md).

## Shared Rules

- Reuse UUID resource IDs, UTC event instants, Cairo `DATE` service days, integer revisions and exact decimal EGP strings/NUMERIC. Tenant IDs and actual actor/role are server-derived.
- Every clinical relation carries `organization_id`; use tenant-qualified unique/FK keys and FORCE RLS. Add a Visit `(organization_id, patient_id, id)` unique key for the attachment triple FK, without editing published `0021`.
- New tables/functions receive explicit effective grants by actual API/Web/worker/gate/sender/operator login and F-01 operation/resource; absent context or unauthorized target denies. Operator and nonclinical sender receive no clinical access.
- Technical lifecycle enums below are local implementation states. Appointment retains its approved six states; no new clinical status is introduced.
- Audit/history and successful receipt projections remain immutable. Store no raw bearer, cryptographic master key, clinical document copy or unnecessary recipient document in audit/receipt/logs.

## Attachment — D-10/B-21

| Fields / relation | Validation |
| --- | --- |
| `id`, `organization_id`, `patient_id`, nullable `visit_id`, `class` | Patient mandatory. FK `(organization_id,patient_id,visit_id)` references same-patient Visit; null Visit permitted only for approved preliminary classes enabled by policy (see the semantic mapping below). Clinical report/handwritten-prescription requires Visit. |
| `uploader_id`, captured actual role, creation/finalization time | Actual authorized actor, tenant-qualified staff reference and immutable attribution. Upload does not grant read. |
| Original display filename, declared/detected MIME, extension, byte length/digest | Normalize safe display name; never use it as a path. Configured class/type/signature/size/count/storage policy authoritative. |
| `state`, `revision`, `generation`, scan outcome/engine metadata | Only a definitive clean scan plus authenticated encrypted object permits CLEAN. Failures remain unreadable. |
| `private_object_id`, `finalized_at`, minimal failure category | Only CLEAN can reference its committed clean object for content retrieval. Failure text excludes paths/clinical bytes/secrets. |

Technical transitions: `INITIATED → UPLOADING → STAGED → SCANNING → CLEAN`; a rejected validation/scan may become `REJECTED` or `QUARANTINED` under configured policy. Dependency/finalization failure remains non-readable `FAILED`; abandoned/uncommitted generations may become `CLEANUP_CLAIMED → DISPOSED`. Claims/retries require current generation. No CLEAN transition without the complete proof, no cleaner transition for committed CLEAN, no implicit quarantine release.

Abandoned eligibility requires non-clean state, definitively failed/abandoned attempt or expired configured lease, no live owner or committed reference, and reconciled transaction outcome. Valid configured disposal policy is mandatory. Claim/fence the exact generation under resource serialization; only its immutable locator may be disposed, and stale generation completion cannot release quota or delete a replacement. Missing policy, uncertain commit, active lease or CLEAN reference denies cleanup.

**Canonical attachment-class semantics (Domain INV-29 and implementation-plan §2/D-10).** D-10 owns the fixed server-side mapping from recognized class IDs to the following permissions. B-21 consumes that same mapping for upload, finalize, grant issue and redemption. Preliminary reports/test images permit a Patient-only association and authorized Receptionist upload. Only material classified as a preliminary report permits authorized Receptionist original-content access. Clinical reports and scanned handwritten prescriptions each require a Visit of the same Patient and Organization and permit authorized Receptionist upload of the approved scans. Both reserve original-content access to an authorized Doctor. In every class the Doctor needs live authority and CLEAN content to read the original. Both Receptionist variants may see minimal upload/scan metadata for their own permitted upload. The configured allowlist can restrict recognized classes. Neither a configured name nor a caller-supplied class can define or override these semantics. Reject unmapped classes even when allowlisted. Keep stored classification immutable and enforce its Visit and read restrictions across policy changes. These categories select no production IDs or allowlist.

## PrivateObject and StagingAttempt — D-10/B-21

| Fields | Validation / lifecycle |
| --- | --- |
| Object ID, tenant/patient, owner kind/ID and artifact kind | Exact attachment/export ownership; no client-chosen storage locator or cross-owner reuse. |
| Opaque server-generated relative locator, ciphertext digest/length | Resolve beneath configured private root; reject traversal/symlinks/nonregular objects. Never project locator to client. |
| Algorithm/version, `key_version`, nonce, authentication tag | AES-256-GCM; parse actual 256-bit key from protected versioned configuration. Fresh unique nonce per key; authenticated metadata binds version/tenant/patient/object/owner/artifact. Master key stays outside DB/image/repo. |
| Immutable source digest, generation, claimant/lease, created time, state | Staging ownership and CAS finalize/cleanup; cleaner cannot remove a generation referenced by a committed owner. |

Candidate ciphertext is durable before publication, then referenced atomically by owner metadata. Crash before publication leaves an unreadable orphan for safe scoped cleanup. Tag failure never produces plaintext delivery. Quarantined/plain temporary bytes retain protected access and configured disposal, without an invented retention period.

`StagingAttempt.organization_id` is explicit, tenant-qualified with its attachment/export owner, patient, actor/session and candidate relations. Each `StagingAttempt.attempt_id` is an immutable server-generated UUID with unique tenant-qualified identity `(organization_id, attempt_id)`; all attempt references are tenant-qualified and must match the bound operation, owner, business phase and generation. It is an identity/fence, not authorization. For multi-transaction operations persist actual actor/session/security revision, operation/key/target/typed digest, business phase, expected revision, generation/claim/lease and immutable snapshot/input/candidate digests. These fields fence work; they are not F-01 authorization. Each protected phase and recovery read needs a newly registered signed request with its own single PRIMARY. Proof expiry never extends candidate lease, grant expiry or session authority. See [authorization lifecycle](contracts/transactions.md#multi-transaction-f-01-authorization-lifecycle).

Upload progress additionally records contiguous committed offset, per-chunk length/digest, BFF-observed whole-file length/digest and assembly verification outcome, all bound to the same upload generation. Unique chunk position is tenant/upload identity/generation/offset, independently of `StagingAttempt.attempt_id`; identical bytes at distinct positions are valid. Separately signed publication commits the immutable position and progress once; status reads reconcile lost acknowledgments. Chunk bodies remain at most the existing 1 MiB transport/proof bound; total-file and cumulative quota limits remain configured policy. Staged API assembly must independently match full length/digest before STAGED/scan/CLEAN. Never publish an incomplete assembly or append duplicate chunk effects on recovery. `StagingAttempt.upload identity` is the existing attachment ID qualified by organization; `StagingAttempt.chunk starting offset` is the immutable zero-based byte index of this chunk. The key is (organization, attachment ID, generation, chunk starting offset). The contiguous committed offset is the next append position. Neither the attempt UUID nor a second upload ID identifies a chunk position.

## PatientExport and ExportArtifact — D-10/B-22

| Fields / relation | Validation |
| --- | --- |
| Export ID, tenant, one Patient ID, creator actual ID/role | Any active authorized Doctor, Manager or ordinary Receptionist. Archived patient allowed. Bulk/foreign/operator denied. |
| `format` (`pdf` or `csv`), `as_of`, extraction/dictionary schema version | Full one-patient record from one statement snapshot; no general UI projection reuse. |
| Specified purpose, intended recipient reference, authority/identity verification method/outcome, verifier/time | Preparatory verification at creation; distinct positive matching confirmation by actual acting staff at handoff before grant issuance, linked immutably to the grant/audit and rechecked at redemption. No mandatory identity-document copy, TTL or invented consent/legal assertion. |
| State/generation/owner, normalized request digest/idempotency identity | `PREPARING → READY` or non-readable `FAILED`; retry/reconcile same logical operation, never create duplicate success after unknown commit. |
| Success audit and immutable receipt linkage | Audit actor/role/tenant/patient/time/purpose/recipient verification/action/outcome; no full record copy. |
| Artifact ID, export FK, `kind` (`RECORD` or `MANIFEST`), private object FK, content type/digest/length | Unique `(organization_id,export_id,kind)`. Exactly one full record and one separate manifest at READY; same patient/as-of. Each artifact encrypted and independently granted. |

Manifest lists attachment reference/class/Visit/date/scan availability and independent access requirements; no raw bytes, storage path, key or bearer URL. It does not grant original-file read. Full record includes permitted Doctor-authored history without granting later ordinary clinical browsing/editing.

Uncommitted export record/manifest candidates use the existing StagingAttempt/PrivateObject generation, ownership and quota fences with the B-21 cleanup handler. Claim only a reconciled, eligible, unreferenced generation under configured disposal policy; READY publication and cleanup claim are mutually exclusive. Both candidates remain protected and unreadable until atomic READY; stale cleanup cannot dispose a READY artifact or replacement generation. B-22 owns the ordered publication-versus-cleanup proof.

## DownloadGrant — D-10/B-21/B-22

Fields: grant ID, bearer digest, organization/patient, actual actor/session/security revision, resource kind/ID, artifact ID/kind, issued/expiry/consumed timestamps and minimal redemption audit linkage. Discriminated FK/checks require exactly one matching Attachment or ExportArtifact target. Unique digest; configured expiry; state derived as ISSUED/CONSUMED/EXPIRED/REVOKED.

Issue only after current authorization and resource readiness; export handoff must be verified. Redeem by atomic unused/unexpired CAS after fresh session/tenant/resource/class checks and full GCM validation. Concurrent use succeeds at most once. Record and manifest have separate grants. Streaming failure leaves consumption intact; a new grant requires fresh authorization. No bearer is stored in immutable receipt/result projections.

Export grants bind the positive actual-verifier/time/recipient handoff evidence for the same export and selected artifact. Creation evidence alone is insufficient; server redemption records disclosure to exporter, not onward recipient receipt. Actual external handoff requires staff verification at that time. Missing/invalid grant lifetime or verification denies issue/redeem.

## OutboxDelivery and RecipientNotification — B-23

Retain existing D-09 `outbox_events` as source. Add tenant-qualified delivery state: explicit `OutboxDelivery.organization_id`, source event ID, consumer/type, claimant, generation, lease, attempt count, next eligible time and outcome. Source relation and unique logical delivery identity are tenant-qualified. Only matching active generation can finish/retry. Persist one recipient notification keyed uniquely by `(organization_id,event_id,recipient_id,notification_type)` with safe operational payload, actual origin role, publication position/time, read marker and result linkage.

Delivery outcomes distinguish pending/retryable, committed publication success and terminal failure after configured retry exhaustion (technical outcomes, not Appointment states). Reconcile unknown commits first. Terminal failure stores minimal safe failure category and existing attempt reference for authorized operational diagnostics through existing protected maintenance access and sanitized telemetry only; no new role, endpoint or SELECT grant. It is not claimable for further automatic retry and creates no recipient notification if publication never committed. A committed unique notification remains stored and subject to live authorization; source outbox/history remain retained.

Map existing Doctor B-17/B-18 event names and Doctor-only B-16 exceptional cancellation to one review notification per event/recipient/type. Companion ordinary events do not duplicate review effects. Payload never includes Visit narrative or clinical before/after audit. Stored unread notifications are subject to live read filtering, not permanent access rights.

`TenantStreamState(organization_id,next_position)` serializes allocation with publication commit. Derived opaque `StreamCursor` fields are `organization_id`, `recipient_id`, `stream_projection_version`, `position` (committed position); integrity-protect and validate them against current authorized scope. Cursor grants no access and requires no new persisted cursor relation. A snapshot and its watermark share one SQL statement snapshot. No new cursor retention duration is chosen.

## DoctorDeviceSubscription and PushAttempt — B-24

Fields: tenant, Doctor ID, server device/subscription ID, originating session/security revision, protected endpoint/key material, endpoint fingerprint, explicit consent timestamp/revision, separate explicit patient-name inclusion consent (default off) with actual Doctor/time/revision and withdrawal, revoked timestamp/reason and last safe outcome. Device identity is bound server-side; client cannot select another staff member. Prevent duplicate active registration of the same subscription without verified ownership; withdrawal is scoped to device.

Transitions: absent → opted-in active; active → revoked on withdrawal/logout/reset/disable/lost authority; expired/invalid provider subscription becomes inactive. Fresh opt-in is required to reactivate. Sends recheck Doctor/session/tenant/consent and use fenced source-event/device attempts for committed Doctor attendance alerts only. Name consent is independent of push opt-in; absent/withdrawn name consent yields generic alert with no patient identifier, positive current consent permits name only. In-app sound metadata follows the HTTP alert routing table, not every stream frame. Endpoint validation/transport prevents internal-network destination and redirect bypass; never log endpoint secrets or payload PHI.

`PushAttempt` fields: `organization_id`, `event_id`, `device_subscription_id`, `claimant`, `generation`, `lease`, `attempt_count`, `outcome`, and next eligible time under configured retry semantics. Tenant-qualified source-event/device attempts are unique per logical eligible alert/device; only the matching active generation can authorize a handoff or finish/retry. Outcome distinguishes pending/retryable, authorized immediate handoff (including generic/name-only ceiling), definite transport result, held/uncertain and terminal failure; no retained payload, endpoint secrets or implied provider receipt. Reconcile unknown DB commit before handoff; uncertain external acceptance never implies definite failure or automatic retry. Final authorization and withdrawal order, handoff uncertainty and fresh reauthorization follow [Worker, Push and External Effects](contracts/transactions.md#worker-push-and-external-effects).

## Background Work — B-25

Reuse existing nonclinical delivery jobs/attempts and sender role. Do not create a parallel invitation/recovery queue or generic clinical scheduler authority. B-23 owns the narrow actual-worker capability enumerating nonclinical tenant IDs for notification work; B-25 consumes it to orchestrate B-23 claims without duplicating enumeration logic. B-25 separately invokes B-20's existing tenant/day/policy/target-scoped cutoff capabilities unchanged.

Durable attempt identity contains job/consumer, tenant/resource scope, claimant, generation, lease, bounded attempt and reconciliation outcome. Preserve compatibility holds and implement ordinary mail uncertainty fencing; a missing SMTP acknowledgment is not proof of no delivery. Unknown DB finish is read back before retry. Disabled cutoff remains no work and does not mutate Booked/Waiting/In Examination at midnight/restart. For ordinary mail, persist a matching-generation pre-handoff fence on the existing delivery-job substrate. An outstanding fence survives lease expiry, restart and lost acknowledgment; its protected material remains under sender custody until a definitive disposition.

Valid explicit configuration is required for file/size/count/quota and scan/key policy, grant expiry, retry/lease/cleanup eligibility and SSE poll/batch/maximum authorization-recheck interval; missing or invalid values disable affected work or required readiness without defaults. Synthetic fixture values do not approve production. See HTTP and transaction contracts for the enforcement points.

## MetricDefinition and ReportView — B-26

Definitions are versioned server-owned constants with ID, unit, source entity, included statuses, Cairo service/event basis, replacement treatment, numerator/denominator where applicable, and empty-population semantics. Results are derived views, not a new mutable financial ledger/cache.

Preserve `TODAY_BOOKINGS_TOTAL`, six current status counters, `GROSS_SCHEDULED_APPOINTMENTS`, `CANCELLATION_EVENTS`, `COMPLETED_EXAMINATION_VALUE_EGP`, `PATIENT_FLOW`, optional `UNIQUE_PATIENTS_SERVED` and `WEEKLY_MONTHLY_TRENDS` exactly per plan §11.3. One composed statement supplies each response and `as_of`; READ COMMITTED authority remains unchanged. Value is frozen net once per qualifying Completed appointment, never cash/subscription income.

Calendar-month buckets are Cairo-local first day inclusive to next month's first day exclusive. PO-approved CHK019 (2026-11-28) defines Saturday-through-Friday reporting weeks as half-open Cairo-local calendar intervals `[Saturday, next Saturday)`. Use `Appointment.service_date` directly for service measures and derive the Africa/Cairo local date from each actual event instant for event measures. Equivalent instant-range queries must preserve the same local-date membership across DST; do not use fixed elapsed-hour durations or UTC-date grouping. PO-approved CHK035 permits Doctor/Owner, Manager and ordinary Receptionist to read their own tenant's completed-value aggregate (including trends); no settings/pricing mutation permission or foreign/operator access.

## Acceptance Evidence — D-12/B-27

Use existing task/evidence records, not a new production database table. Each mapping records source identifier/scenario, owner, actual test/report/case, actor/boundary/conditions/effects, exact executable SHA/run/attempt, schema/inventory, observed outcome, applicability and unresolved reviewer/Issue disposition. See [integrated evidence map](contracts/transactions.md#integrated-evidence-map). A future test name or historical run alone is not passing evidence.
