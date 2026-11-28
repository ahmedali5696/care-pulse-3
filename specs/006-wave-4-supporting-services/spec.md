# Feature Specification: CarePulse 3 — Wave 4: Supporting Services & Integration Proof

**Feature Branch**: `feature/wave-4-Supporting-Services-&-Integration-Proof`

**Created**: 2026-11-28

**Status**: Draft, derived from approved authorities; specification quality and runtime acceptance are separate. No Wave 4 implementation or integration acceptance is claimed.

**Input**: User description: "Wave 4 — Supporting Services & Integration Proof, as defined in docs/implementation-plan.md §9 (D-10, D-12, B-21–B-27)."

## User Scenarios & Testing *(mandatory)*

The scenarios below define required outcomes, not executed tests. Each journey is independently assessable using valid prerequisite data and operations; priority does not replace Master dependencies. Doctor is the single Doctor/Owner. Manager is a Receptionist with live Owner-granted `operational_manager`, not a third role. “Authorized staff” means Doctor, Manager or ordinary Receptionist within that operation's permissions. Patients have no clinic login; provisioning operators have no clinical access.

### User Story 1 - Keep each attachment with the correct patient and Visit (Priority: P1)

As authorized staff, I need uploaded material and export records to belong to exactly the intended clinic and patient, so a valid identifier cannot expose someone else's record. **Owner: D-10.**

**Why this priority**: Every upload, download and export depends on reliable patient, Visit, class and privacy boundaries.

**Independent Test**: Record preliminary patient-only material, Visit-bound clinical material and export metadata for two patients in each of two clinics; attempt mismatched associations and access before approval.

**Acceptance Scenarios**:

1. **Given** a permitted preliminary class, **When** authorized staff associates material with a patient without a Visit, **Then** that association is permitted without inventing a Visit or changing clinical authorship.
2. **Given** a clinical report or handwritten-prescription class, **When** staff supplies no Visit, another patient's Visit in the same clinic, or a Visit from another clinic, **Then** the association fails without an accessible object or partial successful record. A valid association requires the same patient and clinic.
3. **Given** pending, rejected or quarantined material, **When** anyone attempts preview or retrieval, **Then** content remains unreadable. Knowing an attachment or export identifier alone reveals neither content nor a private storage location.

**Trace**: FR-073/079/096/104; BR-039–041; INV-01/29; CON-31.

### User Story 2 - Upload and retrieve private medical attachments safely (Priority: P1)

As permitted clinic staff, I upload reports and handwritten-prescription images while patients' files remain private and Doctor-authored content remains protected. **Owner: B-21.**

**Why this priority**: Unsafe or over-permissive file access would disclose medical information or admit harmful content.

**Independent Test**: Exercise allowed classes and actors, safe and infected samples, misleading file types, unavailable scanning/storage/key dependencies, incomplete uploads and private retrieval under changing access.

**Acceptance Scenarios**:

1. **Given** authorized staff and a matching patient/Visit/class, **When** a file satisfies the configured extension, declared type, detected content, size and class rules and passes malware inspection, **Then** it becomes privately retrievable only after protected finalization and encryption succeed. No public location is exposed during upload or afterward.
2. **Given** infected content, a spoofed type, a disallowed or over-limit file, unavailable scanning, or failed encryption/finalization, **When** upload processing runs, **Then** the file is rejected or remains non-readable under the applicable quarantine policy, with no clean status or scanner bypass. Incomplete or abandoned material remains non-readable and can be cleaned up without deleting a committed clean object. A compatible validated server-only file policy is required before BFF spooling; separate raw-byte stage and publication proofs plus status recovery fence attempt identity, generation, offset and digest as defined by the HTTP contract. After BFF interruption, complete-file resubmission must revalidate the file identity, committed prefix and explicit current generation/revision/offset through fresh signed status before continuing; stale selectors, unverified chunks or unresolved outcomes cannot advance progress.
3. **Given** an authorized Receptionist, **When** they upload permitted preliminary reports/test images to a Patient or scanned handwritten prescriptions/reports to that patient's Visit, **Then** the permitted upload and minimal upload/scan metadata are available without clinical editing authority. Preview/download is allowed only for authorized classified preliminary reports; uploading a handwritten prescription or other Doctor-only material does not authorize its download.
4. **Given** a clean attachment and an otherwise valid download grant, **When** retrieval occurs, **Then** current actor, clinic, resource, class and patient/Visit authorization are checked again. Expired, reused, leaked-to-another-actor, wrong-clinic or path-manipulated requests, and requests for which disablement, suspension or access revocation commits before redemption authorization, reveal no file. A prior committed authorized redemption cannot be recalled.
5. **Given** production medical uploads, **When** the required approved file policy is absent or OPEN-FILES-01 remains open, **Then** uploads remain disabled. Labeled synthetic fixture policy does not enable production.

6. **Given** an incomplete non-clean upload generation whose lease has expired or whose attempt is definitively failed/abandoned, **When** cleanup evaluates it, **Then** cleanup may claim only under supplied disposal policy after reconciling unknown commit outcomes and proving no live owner or committed object reference; it fences the old generation before disposal. An active lease, uncertain commit, missing policy, or committed CLEAN reference prevents disposal.

**Trace**: FR-069/073/075/080/081/096; BR-035/039/041; INV-01/20/29; AC-11/29/36; CON-31.

### User Story 3 - Export one patient's full record with a verified handoff (Priority: P1)

As Doctor, Manager or ordinary authorized Receptionist, I need a full one-patient record for a specified legitimate purpose and verified intended recipient, including when the patient is archived. **Owner: B-22.**

**Why this priority**: Staff need the approved disclosure workflow without weakening general clinical or attachment permissions.

**Independent Test**: For each staff variant, export an active and an archived patient's full history in both approved formats, inspect the manifest and audit, then exercise missing-purpose, handoff, access and content-safety denials.

**Acceptance Scenarios**:

1. **Given** one patient in the actor's active clinic, specified legitimate purpose, intended recipient and preparatory verification, **When** any authorized staff variant privately generates the export and completes verified handoff/retrieval, **Then** readable PDF or structured CSV contains the patient's full permitted medical history, including Doctor-authored clinical history, and a separate attachment manifest. The exporter can read the full downloaded record; an archived patient remains exportable.
2. **Given** an export request or handoff without specified purpose or required recipient verification, **When** disclosure is attempted, **Then** no full record is handed over. Success retains immutable actual actor/role, clinic, patient, time, purpose, recipient/verification and action/result linkage without needless clinical copies in audit or logs.
3. **Given** a generated export, **When** the authorized actor retrieves it, **Then** the server-created output is private and encrypted, retrieval is bounded and single-use, current access is rechecked, and CSV cells cannot execute user-supplied formulas. A repeated identical authorized creation request returns its committed result without another logical export effect; changed content under the same request identity conflicts.
4. **Given** a known export identifier or previously issued grant, **When** a foreign-clinic actor, operator, disabled/revoked staff member, suspended clinic or replayed/public/unscoped request tries retrieval, **Then** no record is disclosed. A bulk or multi-patient request is denied.
5. **Given** successful full export, **When** that Receptionist subsequently reads ordinary patient/Visit/search/event views or requests an original attachment, **Then** the usual field and attachment-class permissions still apply; no Doctor clinical edit, general narrative access or automatic original-file download is granted.

6. **Given** recipient verification recorded during export creation, **When** the actual handoff/disclosure is prepared, **Then** creation evidence alone is not proof of handoff: the acting authorized staff member confirms the intended recipient's identity and authority at handoff before grant issuance, and redemption rechecks matching verified evidence and live authorization before any bytes leave the server. Missing, failed or mismatched verification denies disclosure; an external physical/electronic delivery requires staff verification at that actual handoff and is not inferred from artifact creation or download. Failed/abandoned export candidates use B-21's shared fenced cleanup; B-22 proves eligible failed-candidate disposal and both READY publication versus cleanup orders, with no deletion of referenced READY artifacts.

**Trace**: FR-079/080/104; BR-030/035/038–040; INV-01/18/19/20; AC-02/11/31; November amendments under Issue #25.

### User Story 4 - Receive committed, role-appropriate in-app updates (Priority: P1)

As Doctor or Receptionist, I need current operational updates and reliable reconnection without seeing uncommitted work or unauthorized clinical content. **Owner: B-23.**

**Why this priority**: Cancellation review, attendance and admission coordination depend on trustworthy delivery.

**Independent Test**: Commit and roll back the existing owner operations, repeat delivery, reconnect after an event gap, and change account/clinic access while a stream or saved notification is in use.

**Acceptance Scenarios**:

1. **Given** committed check-in, admission, permitted status or Pause changes, **When** in-app delivery occurs, **Then** the currently authorized recipients receive only their permitted operational updates, including Doctor check-in alerts and Receptionist admission updates. Pending or rolled-back mutations produce no delivered notification.
2. **Given** a Doctor-initiated Booked/Waiting cancellation, exceptional In Examination cancellation, or individual reschedule committed by B-17, B-16 or B-18 respectively, **When** delivery successfully publishes, **Then** eligible Receptionists in that clinic receive an in-app review notification for each committed event, carrying safe operational references/status/date and no Doctor Visit narrative. A Receptionist-origin action does not masquerade as Doctor-origin work.
3. **Given** the same event and recipient, **When** successful dispatch or replay repeats, **Then** there is one logical notification per event, recipient and notification type. Delivery consumes the committed event without repeating cancellation, rescheduling, admission or any other business transition.
4. **Given** a disconnected client, **When** it reconnects with its last event position, **Then** ordered authorized updates resume or a current authorized snapshot resolves an unavailable history gap. A cursor cannot expose another clinic's or role's events.
5. **Given** a connected user or unread stored notification, **When** their account, role access or clinic becomes invalid, **Then** delivery, streaming and subsequent reads deny protected content using live authorization, including periodic and event-send rechecks. Clinical narrative never leaks through replay or snapshot. An authorized recipient may mark only their own notification read; identical authorized replay adds no effects, and another recipient (same or different tenant) cannot read or mark it.

6. **Given** a committed eligible event whose in-app publication definitively fails through the configured retry bound, **When** the final fenced attempt finishes, **Then** its delivery outcome is durably terminal failed, no further automatic retry occurs, and sanitized failure metadata is available to authorized operational diagnostics only. If publication never committed, no recipient notification exists; if it committed, reconciliation preserves the one existing notification and does not mark that publication failed or duplicate it. The source event and business history remain intact; authorized snapshot recovery remains available. Diagnostics are limited to existing protected operational/maintenance access and sanitized telemetry; no clinic inbox, diagnostic API or expanded grant.
7. **Given** missing/invalid SSE poll or authorization-recheck configuration, or a session/recheck failure, **When** a stream is opened or already active, **Then** refuse opening or terminate it, discard buffered protected data, and send no further protected frames. Recheck at subscribe, each bounded read, before protected emission and at the configured maximum recheck interval even while idle. Both BFF and API consume compatible validated server-only CP_POLICY_FILE policy before stream resources are consumed; missing, invalid or mismatched policy fails closed.

**Trace**: FR-025/027/028/036/039/072/075; BR-018/019/035/043; INV-01/16/20; AC-12/22/40; CON-14/15.

### User Story 5 - Continue safe background work after interruption (Priority: P1)

As clinic staff, I need committed notifications, invitation/recovery email and an explicitly enabled No-show cutoff to keep operating safely when background processing stops or restarts. **Owner: B-25.**

**Why this priority**: Recovery and operational automation must neither duplicate business effects nor bypass tenant or policy boundaries.

**Independent Test**: Stop/restart processing around claim, send and acknowledgment, simulate delivery failure and competing claims, and invoke disabled/enabled cutoff across check-in and Cairo date boundaries.

**Acceptance Scenarios**:

1. **Given** committed work, **When** background processing invokes it, **Then** it reuses B-23's delivery behavior, B-20's cutoff transition, and B-03/B-04's shared invitation/recovery sender without reimplementing their rules. Clinical work is processed within a fresh clinic scope; B-25 consumes B-23's narrow nonclinical tenant-enumeration capability for notification orchestration, without duplicating it or changing B-20's existing cutoff capabilities. With mail enabled, the running worker schedules the shared sender through the protected request-driven invocation in the transaction contract, preserving separate actual database identities and sender-only credentials/material; no manual CLI is needed for scheduled delivery.
2. **Given** no explicitly enabled valid cutoff, **When** background processing runs at day end, midnight or after restart, **Then** no automatic No-show occurs. With cutoff enabled, only the matching Cairo service day's eligible unarrived Booked appointments can change through B-20, with check-in concurrency preserved; Waiting/In Examination remain unchanged. The existing approved B-20 resolution of repeated or missing local cutoff times remains binding. The running scheduler discovers bounded due tenant/day/current-policy tuples from persisted state without supplied selectors, including after downtime; discovery grants no mutation authority and B-20 revalidates each tuple.
3. **Given** an interrupted or retried job, **When** processing resumes or another processor reclaims it, **Then** durable ownership and attempt state prevent stale completion or retry updates and duplicate committed business effects. Lost responses with unknown commit outcome are reconciled before another mutation. Email transport is not promised exactly-once delivery. Before external SMTP handoff, persist and confirm the generation-fenced handoff marker; after a crash across handoff or unknown acknowledgment, restart beyond lease expiry must not cause another handoff or protected material disposal, even with an otherwise usable proof and remaining retry budget.
4. **Given** email transport failure, **When** the shared sender runs, **Then** definitive failure is recorded and retried only under configured bounded policy when nonacceptance is established; ambiguous acceptance remains durably held pending definitive reconciliation, without logging credentials, tokens or patient content. Request-time authentication/recovery controls remain effective and are not deferred to background delivery.
5. **Given** absent or wrong clinic/operation authority, **When** background work is attempted, **Then** no clinical rows are exposed or mutated. Cleanup is limited to approved lifecycle work and preserves retained clinical/history/audit evidence.

**Trace**: FR-035/070/075/076/100/103; BR-027/034/035; INV-11/16/19/20; AC-21/22/33; CON-08/14/15/36.

### User Story 6 - Prove persisted clinic separation and domain integrity (Priority: P1)

As a clinical/security reviewer, I need evidence that invalid associations, denied actions and competing operations preserve patients' records across deployment and transaction boundaries. **Owner: D-12.**

**Why this priority**: Backend completion requires demonstrated integrity, not only successful ordinary workflows.

**Independent Test**: Use two isolated synthetic clinics and actual persisted operations to assess applicable invariants, field privileges, migration outcomes, transaction ordering and denial effects.

**Acceptance Scenarios**:

1. **Given** two clinics and each actual service/operator authority, **When** valid, foreign, missing-scope and pooled/reused-context access is exercised, **Then** only currently authorized rows and fields are visible, clinical access by operators is denied, and denied changes leave related records and pending notifications unchanged.
2. **Given** every relevant INV-01–38 and database-mappable CON-01–45 obligation, **When** integrated assessment runs, **Then** each is mapped to an applicable owning proof, with real competing operations for races. This includes schedule/policy and archive-versus-booking conflicts, old-day queues, admission/claims and all-or-none bulk changes.
3. **Given** an empty installation, clean redeployment and a changed existing schema with representative synthetic predecessor records, **When** the applicable migration checks run, **Then** identifiers, clinical/history links, tenant isolation, required constraints/indexes and effective permissions remain correct. Rejected changes do not claim successful migration or expose partial state; published migration history is preserved.
4. **Given** a transaction/dependency failure or competing lock chain, **When** the operation fails or its result is reconciled, **Then** no partial success, residual clinic context, unexpected event or lost retained history remains. Required lock ordering and denial outcomes are demonstrated without mocked concurrency or isolation checks.

**Trace**: INV-01–38; database-mappable CON-01–45 and November amendments; AC-01/04–09/14–18/23–29/32–34/38–43; Domain §11; implementation-plan §§9/11.4.

### User Story 7 - Verify integrated backend security and acceptance (Priority: P1)

As Product Owner and independent reviewers, I need a traceable integrated result for the backend's security, file, worker and clinical boundaries before declaring that scope complete. **Owner: B-27.**

**Why this priority**: Passing an individual operation does not prove its interactions with authorization, delivery, files or revocation.

**Independent Test**: Map applicable AC-01–43 and critical CON-01–45 to owner evidence and focused integrated proofs, then evaluate the current executable candidate against the required complete acceptance procedure.

**Acceptance Scenarios**:

1. **Given** a request attempting forged identity, replay, invalid origin, expired/throttled access, revoked session, tenant suspension or disallowed staff fields, **When** it crosses a protected boundary, **Then** access is denied without successful domain/event effects or sensitive disclosure. Reusing a connection cannot retain another clinic's authority.
2. **Given** the approved November staff operations, **When** Doctor, Manager and ordinary Receptionist success/denial combinations run, **Then** all five universal operations remain available within their guards, the fixed Manager settings allowlist and Owner/Doctor-only boundaries hold, and real-actor evidence cannot become Doctor impersonation. Follow-up source decision, new-date exception and confirmed-extension paths each preserve Doctor restriction precedence, mandatory reasons/dates where required and immutable provenance.
3. **Given** committed and rolled-back Doctor cancellation/reschedule, file scanner/key failures and competing background delivery/revocation, **When** the integrated flows run, **Then** only clean authorized files are retrievable; successful publication of committed Doctor events yields one permitted Receptionist review notification per eligible recipient/type (exhausted failure and unknown commit follow US4-AS6); rollback yields zero; and loss of access prevents further protected delivery/retrieval. Sensitive audit remains append-only and logs contain no secrets or clinical copies.
4. **Given** the binding shared operation contracts, **When** successful, denied, invalid, stale, repeated and unavailable-dependency requests are assessed, **Then** the agreed result/error behavior, field filtering, bounded inputs and idempotency outcomes hold without weakening production behavior to satisfy tests.
5. **Given** completed integration evidence, **When** reviewers state the result, **Then** the accepted executable revision and complete run/attempt are identifiable, mandatory cases actually ran without skips, and missing/unresolved evidence is explicitly unverified. Synthetic acceptance leaves independent Issue dispositions, production policy and release approvals separate.

**Trace**: FR-003/069–076/100–108; INV-01–38; AC-01–43; critical CON-01–45; implementation-plan §§11–14.

### User Story 8 - Opt in to private Doctor device alerts (Priority: P2)

As Doctor, I choose which of my devices receive browser alerts, while sensitive details stay in the authenticated clinic view. **Owner: B-24.**

**Why this priority**: Optional device alerts supplement the committed in-app workflow without expanding disclosure.

**Independent Test**: Opt in and withdraw separately on Doctor devices, attempt Receptionist subscription, inspect delivered content and revoke access before a pending send.

**Acceptance Scenarios**:

1. **Given** an active Doctor on an explicitly opted-in device, **When** a permitted committed push event is sent, **Then** current Doctor/device consent and clinic authorization are checked at send time; patient name is included only with separate explicit current Doctor consent for that device, otherwise the alert is generic. Never include age, notes, diagnosis or other patient identifiers. Protected in-app details retain their own authorization.
2. **Given** no opt-in, withdrawn device consent, logout, disablement or lost authorization, **When** a later send is attempted, **Then** the subscription cannot deliver protected notifications. Withdrawal on one device does not silently authorize another. The final authorization/handoff boundary serializes live consent, session/access and generation checks immediately before transport: withdrawal first prevents new protected handoff (name-only withdrawal permits generic); handoff first retains its fenced attempt, with transport outside locks, honest uncertainty and no recall guarantee. Revoked devices require fresh explicit opt-in.
3. **Given** a Receptionist, including a Manager, or provisioning operator, **When** browser push subscription is attempted, **Then** it is denied. Receptionist in-app review delivery remains distinct.
4. **Given** a permitted in-app sound event, **When** the interface consumes it, **Then** it contains only role-permitted detail and supports sound after user interaction; the event does not bypass device/browser interaction requirements. Final notification interface ownership remains Wave 5.

5. **Given** committed attendance entering Waiting (ordinary check-in or immediate authorized on-site intake), **When** the eligible Doctor alert is dispatched, **Then** it may produce Doctor-only opted-in push and Doctor in-app sound metadata. The permitted Receptionist admission alert and the three Doctor-origin cancellation/reschedule review alerts may produce Receptionist in-app sound metadata only. Other status/Pause/snapshot/replay updates do not independently trigger sound or push; replay does not create a new logical alert. Without separate explicit Doctor consent for name inclusion on that device, even an opted-in push contains no patient name.

**Trace**: FR-025/037/038/095; BR-035/043; AC-12/35; INV-01/20.

### User Story 9 - Understand clinic counts, activity and completed value (Priority: P2)

As authorized clinic staff, I need reports whose units, dates and populations are clear, so cancelled originals, rescheduling and midnight completion do not misstate work or money. **Owner: B-26.**

**Why this priority**: Reports depend on stable clinical history and must support sound operational decisions.

**Independent Test**: Reconcile reports against a small synthetic set containing cancel-and-replace, cross-day reschedule, cross-midnight completion, zero-price completion and two-clinic records.

**Acceptance Scenarios**:

1. **Given** a selected Cairo service date and a consistent as-of view, **When** staff reads the report and Metric Dictionary, **Then** each measure names its entity/unit, included statuses, service/event date basis, replacement treatment and applicable numerator/denominator. Weekly/monthly trends identify the base measure and time basis.
2. **Given** a Routine original Cancelled and a distinct replacement Urgent Completed on day D, **When** day D is reported, **Then** gross current-day bookings count both records, service-date cancellations count the original once, and completed value counts only the replacement's frozen net once. Cancellation activity belongs to the actual cancellation event date.
3. **Given** a booking rescheduled D→D+1, **When** current status counts are read, **Then** it appears only on D+1; separately labeled historical assignment measures can include D. Previous-day Waiting is not counted as today's Waiting or moved by midnight.
4. **Given** an examination begun on D and completed after Cairo midnight, **When** completion/value and admission-flow reports are read, **Then** completed counts/value remain attributed to original service date D, while flow follows actual admission event time. A cancelled retained Visit contributes no completed value; an authorized zero-price Completed service contributes zero EGP and still counts as Completed.
5. **Given** a report request, **When** results are produced, **Then** no other clinic's data enters the result, no uncompleted price enters completed value, and no figure claims collected payments or subscription revenue. Admission counts and optional unique-patient counts are explicitly distinct.

6. **Given** Doctor/Owner, Manager or ordinary Receptionist with live access, **When** each requests completed examination value for their own tenant, **Then** each may read the aggregate; no pricing mutation or settings permission is conferred. Foreign tenant, operator and revoked access deny.
7. **Given** Cairo-local dates across week/month/year boundaries or DST changes, **When** weekly or monthly trends are requested, **Then** reporting weeks run Saturday through Friday using half-open Cairo-local calendar intervals `[Saturday, next Saturday)` (PO-approved CHK019, 2026-11-28). Calendar months run from the first local day inclusive to the first local day of the next month exclusive. Service-date metrics compare `Appointment.service_date` directly to these date boundaries; event-time metrics attribute each actual event instant to its Africa/Cairo local calendar date before bucketing. Friday belongs to the ending week and the next Saturday to the new week, including DST changes. Never use UTC-date grouping, a fixed 168-hour week or a locale/library week-start default.

**Trace**: FR-048/050/098; BR-023/026/042; INV-01/09/17/32; AC-07/10/37; CON-18/34; Domain §9.5 and implementation-plan §11.3.

### Edge Cases

- Valid Patient and Visit identifiers that belong to different patients or clinics; a missing Visit for a prescription versus a permitted preliminary patient-only report.
- Upload permission without read permission; full-export permission without ordinary clinical browsing, editing or original-attachment permission; archived patient with retained medical history.
- Infected or type-spoofed content, configured limit boundaries, interrupted/abandoned uploads, scan/storage/key outage, failed finalization and cleanup racing a committed clean object.
- Grant expiry at its configured boundary, concurrent one-use redemption, leaked/replayed grant, path manipulation and actor/tenant revocation between grant issue and retrieval.
- Empty or formula-like patient-authored export fields; missing purpose, recipient or verification; repeated creation with identical versus conflicting request content; cross-patient or bulk export attempts.
- Doctor cancellation from each of Booked, Waiting and In Examination; Doctor individual reschedule versus Receptionist-origin work; rollback, duplicate delivery, stale recipient access, reconnect cursor gaps and filtered snapshot recovery.
- Doctor device consent withdrawn, logout or disablement immediately before send; unauthenticated/Receptionist subscription; blocked sound before user interaction.
- Worker crash before/after send, lost acknowledgment, lease reclaim and stale attempt completion; distinguish one logical business effect from transport delivery uncertainty.
- Disabled/default cutoff, repeated/missing Cairo local cutoff time under the existing B-20 policy, midnight without forced changes, cutoff versus check-in and no automatic change to Waiting/In Examination.
- Cancel-and-replace retaining both IDs, cross-day current versus historical assignments, midnight completion, free completed service, repeated admission of a patient on distinct occasions and empty metric populations.
- Missing tenant context, pooled connection reuse after failure, concurrent suspension/grant revocation, privilege escalation attempts and migration failure with populated synthetic predecessor records.

## Requirements *(mandatory)*

### Functional Requirements

#### Authority, scope and Master ownership

Descending precedence: [approved PRD](../../docs/CarePulse_PRD_v1_APPROVED.md), [approved Domain Model](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md), [approved System Design](../../docs/CarePulse_System_Design_v1_APPROVED(1).md), then [implementation plan](../../docs/implementation-plan.md), especially §§2/9/11/12/14. The [Constitution](../../.specify/memory/constitution.md) governs practice within those authorities. This specification adds no canonical FR/BR/AC/INV/CON identifiers and renumbers none; inclusive ranges name existing source identifiers only.

Complete scope is **D-10, D-12 and B-21–B-27**, all Wave 4-owned. Preserve **W4-M1** (attachment/export data, worker and notifications) and **W4-M2** (private access, metrics and combined integrity/security/concurrency evidence). Exact prerequisites remain:

| Master | Approved scope | Dependencies, unchanged | Acceptance trace |
| --- | --- | --- | --- |
| D-10 | Attachment classes, scan/quarantine and private object/export metadata; same-patient/clinic Visit linkage | D-03, D-07, D-09 | Story 1; FR-073/079/096/104; INV-29; CON-31 |
| D-12 | Integrated persisted invariants, tenant/field permissions, migrations, constraints/indexes and lock ordering | D-01–11 | Story 6; INV-01–38; database-mappable CON-01–45 |
| B-21 | Private upload, validation, scanning, quarantine, encryption, finalize and authorized retrieval | D-10, B-01/05/16 | Story 2; FR-073/081/096; AC-11/29/36 |
| B-22 | Full all-staff one-patient PDF/CSV export, separate manifest, purpose/handoff/audit and private retrieval | B-05/16/21, D-09 | Story 3; FR-079/080/104; AC-02/11/31 |
| B-23 | Reusable committed-event delivery, in-app notifications and reconnectable authorized streams | D-09, B-01/02/15/16/17/18 | Story 4; FR-025/027/028/036/039; AC-12 |
| B-24 | Doctor-only consented device push and permitted sound events | B-23, B-04 | Story 8; FR-025/037/038/095; AC-12/35 |
| B-25 | Background lifetime, invocation, bounded retries and shared invitation/recovery email scheduling | D-09, F-07, B-01/03/04/20/23 | Story 5; FR-035/070/076/100/103; AC-21/22/33 |
| B-26 | Metric Dictionary, service/event-date counts, completed EGP value, flow and trends | D-11, B-16–20 | Story 9; FR-048/050/098; AC-07/10/37 |
| B-27 | Integrated identity/session/tenant/field/file/worker/domain security and acceptance | B-01–26 | Story 7; FR-003/069–076/100–108; INV-01–38; AC-01–43 |

#### Access and clinical provenance

- **PRD §3; FR-003/069/072/075/080/101; INV-01/19/20/28**: Every protected operation MUST use the actual authenticated actor and live account/clinic/resource/field authority. Missing or invalid authority denies. Staff cannot obtain cross-clinic access; operators cannot read clinical data, files or exports. General Receptionist views, events and logs MUST exclude Doctor narrative and clinical before/after audit content.
- **FR-071/091; BR-036; implementation-plan §11.2**: Integrated proof MUST preserve the five universal staff operations independently of Manager designation: justified within-lane non-head admission, Follow-up decisions/extensions, archive/unarchive, full one-patient export and eligible bulk reschedule. Each keeps its normal guards. Manager settings stay within the fixed allowlist; Owner alone manages all Receptionist accounts/delegation/capacity. Doctor-only Pause, case-confirmation issue/revoke, clinical authorship/corrections and exceptional In Examination cancellation remain nondelegable.
- **FR-009a–009c/034/067/074; BR-002a–002c/007; Domain §11 amendments**: D-12/B-27 MUST verify actual-actor positive examination attestation, immutable Doctor clinical authorship, no-Doctor-decision/default current-policy eligibility, actual-actor source decision reasons/dates and Receptionist-origin revision limits. They MUST separately cover new-date exceptions and confirmed-claim extensions against genuine Doctor restrictions, single-use consumption, release/rebook, frozen entitlement/price and concurrent source/policy/claim/completion/cancel/grant changes. Previously accepted evidence is not automatically proof of amended behavior.

#### Attachment and export protection

Required operation configuration MUST be explicitly supplied and valid (F-03, implementation-plan §2): file class/type/size/count/quota, storage/key/scan policy, download grant lifetime, attempt/lease/retry bounds, cleanup/disposal eligibility, SSE polling/batch/recheck bounds and push configuration/consent. Missing or invalid required configuration MUST refuse affected startup/readiness or operation, issue/redeem no unbounded grant, accept/publish no unsafe file, claim/retry/dispose no work without its policy, and open/continue no unbounded stream or send unapproved push. No framework defaults or unlimited fallback; disabled optional cutoff and unconsented push remain disabled. Explicitly labeled synthetic fixtures are allowed only under the approved timing variance; production numerics and gates remain open.

- **FR-073/081/096; BR-039/041; INV-29**: D-10/B-21 MUST retain attachment class, patient/Visit/clinic association, upload/scan/quarantine state and private object metadata. Preliminary patient-only material is permitted only by class; clinical reports and handwritten prescriptions require a Visit of that same patient and clinic. Neither an identifier nor upload permission is content-read authority.
- **FR-069/073/080/096; implementation-plan §2 field permissions**: B-21 MUST enforce the approved class-specific upload and independent read permissions. Receptionists may upload the approved preliminary and Visit-bound scanned material, read minimal metadata/status for their upload and preview/download only authorized classified preliminary reports; handwritten-prescription/other Doctor-only original content remains denied.
- **FR-073; BR-039; implementation-plan §§9/11.4**: B-21 MUST validate configured file type/content/size/class rules and fail closed on malware, missing scanning or failed protected finalization. Pending, quarantined, failed and abandoned bytes MUST remain non-readable and non-public; cleanup MUST preserve committed clean objects. Successful finalized content MUST be encrypted at rest and accessible only through the private authorized workflow. Missing storage/key policy or failed required protection cannot become successful delivery.
- **FR-073/075/079/104; BR-035/039/040; INV-20**: Original-file and export retrieval MUST use bounded, resource-bound, single-use private authority and recheck live actor/tenant/resource permissions at serving. Expiry, replay, concurrent second use, path traversal, direct public-object access and revoked/disabled/suspended authority MUST deny disclosure. No numerical lifetime or file limit is selected here.
- **FR-079/080/104; BR-038–040; INV-18**: B-22 MUST allow Doctor, Manager and ordinary authorized Receptionist to create a full one-patient readable PDF or structured CSV, including an archived patient's permitted clinical history, with a separate attachment manifest and no embedded raw attachment files. Bulk/foreign/operator export is denied. Full downloaded content MUST NOT be replaced by a redacted intake projection or grant ordinary narrative browsing/editing/original-file access.
- **FR-079/104; BR-030/040**: Export MUST require specified legitimate purpose, intended recipient and identity/authority verification at handoff, immutable minimal sensitive-operation audit and private server-created encrypted output. CSV MUST neutralize formula interpretation. No invented consent form, retained identity document, legal certification, retention period or timeout is required; production disclosure remains subject to OPEN-OPS-03.

#### Committed events, device consent and background work

The bounded notification retry exhaustion, Doctor push/sound routing and independent name consent, and configuration-bound live SSE rechecks in US4/US8 are mandatory outcomes; technical state, routing and authorization contracts are specified in [data model](data-model.md), [HTTP](contracts/http.md) and [transactions](contracts/transactions.md). Terminal failure is not successful delivery and does not undo the originating committed operation.

- **FR-025/027/028/036/039; BR-018/019; INV-16**: B-23 MUST deliver only committed operational events. Every committed Doctor-initiated Booked/Waiting cancellation, exceptional In Examination cancellation and individual reschedule MUST enter durable delivery processing targeting eligible Receptionists in the clinic; successful publication MUST produce exactly one logical in-app review notification per event/eligible recipient/type, with operational references/status/date only. Definitive failure through the configured retry bound MUST persist terminal failed delivery with sanitized operational diagnostics only and no further automatic retry or fabricated recipient notification; unknown commit MUST be reconciled first, preserving any committed unique publication and success outcome (US4-AS6). Rolled-back work produces none. Origin attribution MUST reflect the actual actor, and dispatch MUST NOT repeat the originating business transition.
- **FR-036/039/072/075; BR-035; implementation-plan §9**: B-23 MUST retain durable delivery/attempt state and at most one logical notification per event/recipient/type, exactly one on successful publication; reconnect MUST resume from an ordered event position or recover an authorized snapshot on a gap. Subscribe, periodic/event-send delivery and notification read MUST recheck access and filter fields. B-23 MUST own its narrow nonclinical tenant-ID enumeration capability, consumed by B-25 for notification orchestration; fresh tenant-scoped authority remains mandatory for clinical work.
- **FR-025/037/038/095; BR-043**: B-24 MUST restrict browser push to explicitly opted-in, currently authorized Doctor devices and the committed alert allowlist in US8-AS5, checking consent/access at send. Patient name inclusion MUST require separate explicit current Doctor consent for the device (default off); absent or withdrawn name consent permits generic push only, never other patient identifiers, age/notes/diagnosis. Device withdrawal/logout/disablement/lost access MUST revoke future protected delivery; Receptionist/operator push subscription denies. Sound events MUST follow US8-AS5, remain role-safe and support playback only after user interaction.
- **FR-035/070/076/100/103; BR-027/035; implementation-plan §§9/12**: B-25 MUST own background invocation, lifetime, bounded retries and approved cleanup while reusing B-20's transition, B-23's nonclinical tenant-enumeration capability and claim/delivery handler, and B-03/B-04's shared metadata-only sender. B-23 does not own a scheduler; B-25 does not duplicate transition/delivery semantics. Authentication/recovery rate limits remain at the request boundary. Email transport policy is configured; production provider/credentials are not chosen by this specification.
- **FR-035/092/106/107; BR-027/034/048/049; INV-11/17**: No automatic cutoff is active by default. B-25 MUST invoke B-20 only for an explicitly enabled valid matching Cairo service-day cutoff, retaining its approved local-time resolution policy and check-in safeguards. Midnight/restart MUST NOT imply No-show, forced closure, cross-day queue movement or changes to Waiting/In Examination.
- **INV-16/20; implementation-plan §11.4**: Background recovery MUST preserve durable claim/attempt ownership and reject stale completion/retry writes. Unknown commit outcome MUST be reconciled against authorized retained results before another mutation. Logical business effects MUST remain idempotent; no claim of exactly-once email transport is permitted. Logs MUST contain no credentials, one-use tokens or unnecessary clinical content.

#### Metric meaning

B-26 MUST expose the approved [Metric Dictionary](../../docs/implementation-plan.md#113-metric-dictionary-contract), using a consistent as-of view and clinic-local Africa/Cairo day. Units, included statuses, time basis, replacement treatment and applicable numerator/denominator MUST be explicit. The following source identifiers are preserved, not new product decisions:

| Metric | Required population and time basis |
| --- | --- |
| `TODAY_BOOKINGS_TOTAL` | Distinct Appointments currently assigned to the selected service day across all six statuses, including retained Cancelled replacement originals; exclude records now rescheduled elsewhere. Gross bookings, not outstanding work. |
| `TODAY_BOOKED_CURRENT`, `TODAY_WAITING`, `TODAY_IN_EXAMINATION`, `TODAY_COMPLETED`, `TODAY_CANCELLED_SERVICE`, `TODAY_NO_SHOW` | Distinct current service-day records in the named status; old-day Waiting stays old-day. All-date examination capacity remains a separate concept. |
| `GROSS_SCHEDULED_APPOINTMENTS` | Distinct IDs historically assigned to the date, using retained assignment history; explicitly historical and not a mandatory dashboard tile. |
| `CANCELLATION_EVENTS` | Distinct audited cancellation actions on their Cairo event date, separately labeled from service-date cancelled records. |
| `COMPLETED_EXAMINATION_VALUE_EGP` | Frozen net EGP once per Completed qualifying New Examination or preferential Follow-up on original service date; exclude uncompleted/cancelled Visits and all payment/subscription concepts. Authorized free completion contributes zero. |
| `PATIENT_FLOW` | Distinct Visit admissions by Cairo admission event date/time bucket; not a Completed-count or unique-person proxy. |
| `UNIQUE_PATIENTS_SERVED` | Optional distinct Patients among Completed services on selected service date; not an Appointment count. |
| `WEEKLY_MONTHLY_TRENDS` | Named buckets of a defined measure, retaining its service/event date and replacement rules; never mix bases silently. PO-approved Saturday-through-Friday weeks use Cairo-local `[Saturday, next Saturday)`; calendar months use `[first local day, first local day of next month)`. Both follow US9-AS7 and retain DST-safe date/instant attribution. |

**PO decision (2026-11-28, CHK035):** `COMPLETED_EXAMINATION_VALUE_EGP` is readable by Doctor/Owner, Manager and ordinary Receptionist in their own tenant, including the corresponding aggregate trend; this grants no pricing mutation or settings authority.

#### Integrated assurance and failure outcomes

File/export and notification publication MUST serialize with privilege changes using the concrete shared authority-first lock order in [Lock and Receipt Rules](contracts/transactions.md#lock-and-receipt-rules). Prove both commit orders with observed PostgreSQL blockers; prior loss of the authority required for that operation denies publication/redemption; native invalidation denies stale session proofs, while withdrawal of Manager designation alone preserves ordinary Receptionist eligibility, including worker publication without an active recipient session, while a prior authorized committed effect retains its evidence and later reads/disclosure reauthorize.

- **D-12; INV-01–38; database-mappable CON-01–45**: Persisted isolation/field privileges, required indexes/constraints, migrations from empty and clean redeployment, relevant populated predecessor preservation, lock-order/deadlock behavior, schedule/archive/old-day and all-or-none bulk interactions MUST have the approved real-database evidence. Denied operations MUST preserve related domain/history/outbox state; security-denial evidence may be logged separately without sensitive content.
- **B-27; AC-01–43; critical CON-01–45**: Integrated assessment MUST cover the approved identity forgery/replay, origin, configured expiry/throttle, session/recovery revocation, suspension, pooled scope, field permissions, append-only audit, scanner/key failure, private files, worker/outbox and November amendment boundaries. Shared contract success/failure, safe bounded inputs, idempotency and dependency-failure behavior MUST follow implementation-plan §11; denied actions MUST have no successful domain/outbox effect. Missing evidence MUST NOT be marked passed.
- **Implementation-plan §§11.4/12 and permanent scalability rules**: Owner-task evidence MUST be mapped to the same invariant, actors, conditions, boundary and effects before reuse in D-12/B-27. Distinct integrated interactions and uncovered/changed behavior still require focused proof; mandatory existing execution and deliberate repeated cases remain required. Final UI/E2E ownership remains U-12, without expanding into exhaustive interface permutations.

### Key Entities *(include if feature involves data)*

- **Attachment**: Clinic/patient, class, matching Visit when required, actual uploader, validation/scan/quarantine state and protected object linkage; upload and content-read permissions are independent.
- **Private object and download grant**: Protected attachment/export content and its bounded single-use retrieval authority, tied to current actor, clinic and resource; no public storage path.
- **Patient export and attachment manifest**: One patient's full record, selected PDF/CSV format, purpose, intended recipient/verification, creation/result linkage and separate attachment inventory; original files remain independently protected.
- **Sensitive-operation audit**: Immutable actual actor/role, tenant, patient/resource, time, purpose, verification and action/outcome evidence; distinct from Doctor-only clinical correction history.
- **Committed workflow event and in-app notification**: Actual originating actor/action, clinic-safe operational reference/date/status, event position, recipient and type, durable delivery/attempt outcome.
- **Doctor device subscription**: Doctor/device association, explicit consent and current authorization/revocation state, with permitted minimal push content.
- **Background job/attempt**: Tenant-scoped clinical work or separate nonclinical email metadata, durable ownership/generation, bounded retry state and recoverable result; no general clinical enumeration privilege.
- **Metric definition and report view**: Stable measure identifier, entity/unit/status population, Cairo service/event time basis, replacement rule and as-of view; values derive from retained Appointment, Visit, price and audit/assignment history.
- **Acceptance evidence mapping**: Owning Master, approved requirement/invariant/scenario, observed result, exact executable revision/run/attempt, applicability and explicit unresolved review/gate status.

## Success Criteria *(mandatory)*

### Measurable Outcomes

These are acceptance targets, not measured results. Quantities describe approved correctness outcomes rather than new latency, volume, retention or production security thresholds.

- **Private files**: Every accepted clinical/prescription attachment has one same-patient/same-clinic Visit association; every permitted preliminary patient-only attachment remains Visit-optional. All pending/quarantined/infected/mismatched/unauthorized retrieval scenarios disclose **zero** content. (Stories 1–2; INV-29; AC-29/36.)
- **Export completion and boundaries**: Each of the **three** staff variants can export both PDF and CSV for an active and archived patient with full permitted history, one separate manifest and required purpose/handoff/audit evidence. Every bulk, foreign, operator, unverified or revoked disclosure attempt yields **zero** records; export grants **zero** additional ordinary clinical/original-file permissions. (Story 3; AC-31.)
- **Retrieval and revocation**: A valid protected grant permits at most **one** successful authorized retrieval. Expired, replayed and wrong-resource/clinic/actor grants, and retrievals for which loss of authority commits before the final redemption authorization and consumption commit, deliver **zero** protected bytes. Authorization and single-use consumption commit before serving; disclosure authorized by a prior committed redemption cannot be recalled. (Stories 2–3; INV-01/20.)
- **Committed review updates**: Each of the **three** Doctor-origin cases—Booked/Waiting cancellation, In Examination cancellation and individual reschedule—targets **one** logical in-app review notification per eligible recipient/type on successful publication despite retries; rollback produces **zero**, and Receptionist-origin work produces **zero** false Doctor attribution. Definitive exhausted failure produces a terminal delivery outcome rather than fabricated success; unknown commit is reconciled first. (Story 4; AC-12.)
- **Reconnect and field safety**: All defined disconnect/gap cases recover an authorized current view by replay or snapshot, with **zero** foreign-clinic or Doctor-narrative disclosures to Receptionists; loss of access prevents subsequent protected delivery/read. (Story 4; INV-01/20.)
- **Device privacy**: Every pushed event is an eligible committed Doctor attendance alert with active Doctor/device consent and contains **zero** patient identifiers without separate current name consent, and **zero** patient details beyond name with that consent. No new protected push handoff may be authorized after withdrawal or loss of access commits; a previously authorized handoff may already be in flight and cannot be recalled. (Story 8; AC-35.)
- **Background recovery**: Every defined restart/reclaim/retry case preserves at most **one** committed logical business effect, **zero** stale-owner updates and **zero** clinical/secret log leaks. Disabled cutoff causes **zero** automatic No-shows; enabled cutoff never converts Waiting/In Examination. Transport uncertainty is reported rather than called exactly once. (Story 5; AC-21/22/33.)
- **Metric reconciliation**: Every required replacement, reschedule, midnight and zero-price fixture reconciles exactly to the Metric Dictionary, with **zero** foreign-clinic contributions, duplicated completed value or amounts represented as cash. Staff can identify each measure's unit/time basis from its definition. (Story 9; AC-10/37.)
- **Integrated coverage**: **All nine** Wave 4 Masters retain their approved prerequisites and acceptance mappings. Every relevant INV-01–38, database-mappable/critical CON-01–45 and applicable AC-01–43 has attributable current evidence or an explicit unverified/blocking disposition; required denials/races show **zero** partial domain/outbox effects. No applicable mandatory proof is silently omitted. (Stories 6–7.)
- **Evidence honesty**: Backend integration acceptance requires **zero** mandatory failures/skips in the required complete exact-revision run/attempt and verified evidence provenance. Every still-open independent review or production gate remains explicitly open; synthetic verification yields **zero** inferred production approvals. (Stories 6–7; implementation-plan §§13.3/14.)

## Assumptions

- **Historical specification invocation:** authorized specification and its separate quality checklist only, not planning, task generation, implementation, migration execution, CI dispatch, commit/push/merge, Issue closure, production cutover or release. Subsequent explicitly authorized documentation stages have their own scope; this record grants no new authority. The current five-finding remediation is document-only and authorizes no implementation, migration execution, CI, commit/push/merge, Issue changes, production activation or automatic Spec Kit progression. No prerequisite's implementation/acceptance is re-established here.
- Existing Wave 1–3 ownership is reused: D-09 supplies durable audit/event/job substrate; B-03/B-04 own invitation/recovery sender semantics; B-16/B-17/B-18 own originating clinical/appointment transitions; B-20 owns cutoff eligibility/transition and its approved Cairo local-time resolution. B-23 owns reusable delivery and its narrow nonclinical tenant enumeration, B-25 consumes that capability and owns invocation/lifetime/retries. No new business transition or duplicated policy is introduced.
- The existing [Wave 3 specification](../005-wave-3-queue-clinical/spec.md), approved amendments and retained task evidence are prerequisite context, not new Wave 4 runtime proof. Where an implementation/evidence gap is found during later planning, retain the approved dependency sets and seek a scoped reviewed correction instead of inventing a new edge or silently reopening accepted work.
- Existing technical architecture and binding operation contracts remain in the approved sources; this specification selects no language, framework, endpoint shape, persistence mechanism, cryptographic design, numerical security setting or new infrastructure. Later authorized work must follow those existing contracts rather than redesign them through this document.
- [PostgreSQL CI diagnostics](../../docs/postgresql-ci.md) and [permanent Full Foundation scalability rules](../../docs/full-foundation-scalability.md#permanent-scalability-rules-for-wave-4-wave-5-and-future-masters) govern future D-12/B-27 and owner-task evidence: dependency-aware isolation, real database races and lock observations, unchanged mandatory inventory/provenance, five existing lanes and the exact final acceptance gate. Fast Feedback is for routine checkpoints; diagnostics or historical results do not substitute for complete Full Foundation acceptance at the actual tested SHA. Required reruns run all jobs in one attempt; receipts never mix across attempts/runs/SHAs.
- Future evidence must record per-suite and full/common/lane/gate elapsed times, critical path, aggregate unrounded runner-minutes, inventory/schema, pinned inputs and observed environment, using the already required acceptance run. Assign suites by dependencies and measured cost; retain independent cross-operation proofs while reusing applicable traceability. No automatic repartition, added lane, extra CI run, weakened assertion/retry/skip/timeout or invented performance gate is authorized. U-12 keeps its approved representative UI scope.
- Docker is unavailable and must not be installed locally. Documentation checks are sufficient for this specification change; required real-database/Compose/security/concurrency runtime evidence belongs in an authorized Docker-capable CI environment. No application checks or CI results are claimed by this document.
- Production remains default-deny while required policy is absent. SECURITY-POLICY-NUMERICS and OPEN-OPS-01/02/03 remain open; production medical uploads additionally require OPEN-FILES-01. Labeled synthetic-only fixture values are permitted by the existing timing variance, not production defaults or legal approval. No backup frequency/retention, RPO/RTO, file size/count/quota, token lifetime or Egyptian legal conclusion is invented.
- Final bilingual/responsive notification, file/export and report interfaces, and representative end-to-end user journeys, remain Wave 5-owned. Wave 4 provides the authorized supporting behavior and backend proof; it does not add patient messaging/login, bulk export/cancellation, payment collection, subscriptions, public signup, an admin dashboard or generated e-prescriptions.

### Existing open Issues and review boundaries

The repository's open-Issue search on **2026-11-28** returned the Issues below. This is a status/traceability check, not independent revalidation of their underlying fixes or evidence. CHK019's product clarification is resolved by the explicit PO decision of 2026-11-28: reporting weeks run Saturday through Friday, using half-open Cairo-local calendar intervals `[Saturday, next Saturday)`. The final independent reviewer PASS for CHK001–CHK040, including this correction, is recorded in `checklists/review.md` on 2026-11-28 as reported by the PO; no Master dependency or existing gate is changed.

| Existing Issue | Relevance, responsible role and review trigger | Required disposition / verifiable closure boundary |
| --- | --- | --- |
| [#24](https://github.com/ahmedali5696/care-pulse-3/issues/24), [#25](https://github.com/ahmedali5696/care-pulse-3/issues/25) | Approved November contracts; engineering and independent Clinical/Security/PO reviewers at affected D-12/B-22/B-23/B-27 acceptance | Reconcile downstream artifacts and obtain authorized exact-revision role/provenance/concurrency/privacy evidence plus independent review/PO acceptance. Product decisions are settled; open tracking does not mean they must be selected again. |
| [#26](https://github.com/ahmedali5696/care-pulse-3/issues/26) | Historical runtime identity/actor-spoofing risk disposition; DB/API Security, Security/Operations, Clinical and PO before affected privileged-writer/integrated security acceptance and production | Preserve effective actual-login/grant, forged-context denial and real actor evidence, then record explicit independent disposition. The plan reports later F-01 engineering proof; neither that report nor this spec closes the Issue or proves a current exploit. |
| [#28](https://github.com/ahmedali5696/care-pulse-3/issues/28) | Historical compatibility worker/sender and signed-receipt replay findings; compatibility engineering, Security/Operations and Clinical/PO at related compatibility review | Plan §12 records subsequent synthetic observations addressed by CI/T057 review, while the Issue remains open. Preserve source/candidate provenance and explicit disposition; do not reassert historical gaps as current failures or infer operational cutover approval. |
| [#13](https://github.com/ahmedali5696/care-pulse-3/issues/13) | Historical populated-upgrade evidence; migration engineering and Security/Operations at D-12 planning/acceptance and before populated adoption | Preserve this review trigger without broadening the bounded compatibility exercise. Applicable populated historical upgrades require retained-record/isolation/failure evidence and approved review; empty-install success is insufficient for production upgrade clearance. |
| [#18](https://github.com/ahmedali5696/care-pulse-3/issues/18) | Canonical security numerics, backup/recovery/legal/file production gates; PO, Security/Operations, Data Governance and qualified Egyptian legal/privacy adviser before production and uploads | Named approvals plus reproducible enforcement, backup/restore/retrieval and legal/disclosure evidence for each applicable gate. Synthetic fixtures and current checklist completion cannot close them. |
| [#2](https://github.com/ahmedali5696/care-pulse-3/issues/2), [#4](https://github.com/ahmedali5696/care-pulse-3/issues/4), [#5](https://github.com/ahmedali5696/care-pulse-3/issues/5) | Deferred production ingress/throttling, dependency security and operator/email credential/provider/retry policy; PO and Engineering/Security/Operations before real-user activation, with B-25 reviewing applicable delivery policy | Verify approved deployment topology/source controls, current advisory treatment and credential/key/mail custody/configuration with retained evidence and signoffs. These are production obligations, not newly authorized remediation or blockers to writing this specification. |

No Issue was created, modified or closed. Specification readiness is distinct from implementation readiness, integrated backend acceptance, independent risk acceptance and production readiness.
