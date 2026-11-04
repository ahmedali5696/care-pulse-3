# CarePulse — Standalone Product Requirements Document

**Document status:** v1.0 APPROVED PRODUCT CONTRACT (revision 2 baseline).  
**Date prepared:** 19 October 2026.  
**Approved by:** Product Owner — explicit approval on 19 October 2026.  
**Effective date:** 19 October 2026.  
**Document language:** English (workbook decision notes retained in Arabic in the decision appendix).  
**Approved product amendment (3 November 2026):** The Product Owner approved the configurable Follow-up window and role-scoped, date-specific out-of-window booking authorization in FR-009a/c, BR-002a/c and AC-06/27 below. This amendment does not itself authorize implementation, migration or release.
**Approved product reconciliation (4 November 2026):** The latest Product Owner decisions in [Issue #24](https://github.com/ahmedali5696/care-pulse-3/issues/24) and [Issue #25](https://github.com/ahmedali5696/care-pulse-3/issues/25) supersede older conflicting role, completion, Follow-up and export rules. The present authorization covers edits to the four authoritative documents only; implementation, downstream Spec Kit changes, migration, merge and release remain separately gated. Unapproved delegation details and evidence contracts remain explicit in §12.3.
**Authority:** This is the CURRENT Product Owner-approved, self-contained CarePulse MVP Product Contract, effective 19 October 2026. It incorporates the 47 completed workbook choices and explicit owner clarifications of 19 October 2026. It prospectively supersedes approved CarePulse PRD v0.5; preceding documents remain historical and are not needed to interpret this standalone contract. Product approval grants NO implementation, migration, payment integration, deployment or production permission.  
**Scope warning:** clinic Emergency lanes, deferred SaaS features, flexible day reconciliation and Bulk-only cancellation deferral are now resolved product scope choices. Production safety and future paid-platform commercialization still have separate gates (see §12).  
**Reference conventions:** FR = functional requirement; BR = canonical behavior; AC = acceptance criterion. Existing retained IDs remain stable; added IDs start FR-090 / BR-032 / AC-18. Revised clarifications add FR-106–108, BR-047–049 and AC-38–43 without renumbering prior identifiers.

## 1. Product vision and fixed concepts

CarePulse is a multi-tenant clinic SaaS product aimed initially at Egypt. Each independently isolated clinic Organization has **one Doctor who is simultaneously its Owner/Admin and one physical branch** for its MVP clinic operations; Receptionist accounts can support front-desk workflows. Patients are clinical records, not authenticated users. The MVP supports Arabic RTL / English LTR and fixed **EGP** patient-service values and **Africa/Cairo** clinic-local dates. A change in currency, regional timezone, clinic country, or number of Doctors/branches is a post-MVP decision.

### 1.1 Clinic product scope

Patient and family registration, routine day-only or explicitly timed bookings, check-in and queueing, separate Medical Emergency presentation, Paid Urgent new examinations, permitted follow-up claims, individual cancellation/reschedule, eligible **bulk rescheduling** (not bulk cancellation), Visit documentation, handwritten-prescription photos as attachments, searches, receptionist management, privacy, audit, operational reports, staff-only realtime and consent-based Doctor browser push, full one-patient authorized-staff export, archive, and backup/restore production readiness.

**Service-money boundary:** Every Appointment has a frozen EGP **service value**. CarePulse clinic operations do **not** collect patient payment, track paid/unpaid status, generate clinic receipts, or assert settled revenue. Paid Urgent is a PREMIUM SERVICE LABEL, not confirmed payment. Medical Emergency is a clinical priority, never a price multiplier.

### 1.2 MVP onboarding boundary and future SaaS commercialization

**Approved scope deferral (owner clarification, 19 October 2026):** self-service Organization registration, customer checkout, platform subscription/payment collection, plan catalogs/renewals/trials/discounts, and a full browser-based SaaS Administration Dashboard are **all OUTSIDE MVP**. Deferring them does not block the clinic domain provided that MVP has secure, audited, minimal **administrative tenant provisioning**: an authorized platform operator can create a clinic Organization, assign the first Doctor/Owner via expiring invitation, list minimal nonclinical tenant metadata, and suspend/reactivate tenant access safely. This may be delivered via a tightly controlled operations CLI/internal administrative API and runbook, not a customer-facing portal or full administration dashboard. Do not expose the provisioning interface to ordinary clinic users. A tenant's Organization ID is stable; staff, patient and Appointment references remain tenant-scoped. The platform operator does not gain clinical access.

**Pilot operation:** clinics may be manually invited and activated as non-billed pilot tenants under an explicit operator decision and verified first Doctor/Owner acceptance; no automated subscription/trial expiry, payment status, plan limit or billing assumption shall control clinical access in MVP. Paid SaaS launch requires separately approved subscription policies, billing/security/legal specifications and product scope; subscription policy remains unchosen now. Future subscription ownership/renewal remains with the Doctor/Owner, not a delegated Receptionist. The above does not defer tenant isolation, clinic staff authentication, protected recovery, suspension or security audit.

### 1.3 MVP exclusions

Patient WhatsApp/SMS, customer reminders/outbound vendor integration and delivery logs; multi-Doctor clinics, second branch and cross-doctor history; patient portal; **bulk cancellation only** (bulk rescheduling remains in MVP); digital E-Prescription composition/signing/printing/PDF; patient examination payment collection/checkout/payment status; general medical deletion, record merge, bulk patient export, annual patient membership, laboratory/radiology marketing partnerships, home emergency visits, desktop app; multi-country/multi-currency/multi-timezone. **Deferred platform capabilities:** public self-registration, automated SaaS payments/subscriptions and packages, commercial discounts/trials and a full SaaS Admin Dashboard. Minimal audited administrative tenant bootstrap remains IN MVP.

### 1.4 Authority of resolved product decisions

- The completed workbook selection and its written qualifications govern the retained product decisions. The explicit 19 October 2026 owner clarifications settle four subsequent questions; they supersede earlier *provisional* assumptions about Emergency, SaaS scope, day closure and Bulk Cancellation only.
- **Emergency independence:** Medical Emergency has its own FIFO waiting/admission lane. There is **no global precedence** between the Emergency lane and the main Urgent/Routine lane. Permitted staff choose which lane to serve; the ordinary admission target must be its server-verified head. No cross-lane choice advances or reorders the other lane. Both lanes share the **same single Doctor's Pause, active examination capacity and Doctor per-case confirmation**. An exceptional selection by any admission-authorized clinic staff may bypass the head inside a lane only with explicit justification and audit under the same safeguards.
- **Day flexibility:** default no closing time and no automatic No-show cutoff. Service-day queues remain date-scoped and do not silently migrate at midnight. Explicit administrative reconciliation is available for overdue Booked/Waiting; an optional guided closing and No-show cutoff may be enabled.
- **SaaS boundary:** minimum secure administrative tenant provisioning is mandatory; customer-facing registration, subscriptions, payment and full SaaS dashboard are post-MVP. Subscription commercial policy is reserved for readiness to offer paid SaaS, not invented now.
- **Bulk boundary:** bulk cancellation alone is deferred. Atomic eligible-only bulk rescheduling remains active MVP.

## 2. Domain and identity model (product concepts, not schema prescription)

- **Organization / Clinic:** tenant data boundary, one Doctor/Owner, one branch; region Egypt, clinic local timezone Africa/Cairo and EGP currency fixed in MVP.
- **Platform provisioning operator:** authorized operational identity for minimal tenant provisioning, first Doctor/Owner invitations and tenant suspension/reactivation. No clinical or billing privileges and no required full SaaS UI in MVP.
- **Staff User:** Doctor/Owner or Receptionist, authenticated and clinic-scoped. A delegated Receptionist Manager is an existing Receptionist with explicit Owner-issued, auditable, revocable operational administration grants, not a new role or Owner. The five universally allowed operations in §3 require no managerial delegation or separate bulk_reschedule flag. Doctor alone owns clinical authored fields and exceptional In Examination cancellation.
- **Patient:** independent clinic patient ID; optional raw and normalized phone may match family members; can be Archived but cannot be generally deleted/merged.
- **Appointment:** operational booking with one of six statuses, service type, queue classification, clinic-local service date, preliminary booking number, immutable EGP price/entitlement snapshot, histories and links to cancel-and-replace.
- **Visit:** associated with actual admission and clinical care, never assumed from simple booking/attendance. May survive exceptionally cancelled In Examination appointment.
- **Queue membership:** physically Waiting patient in main lane (Urgent/Routine) or fully independent Emergency lane, anchored to the original Cairo clinic-local service day. Dynamic position is NOT preliminary booking number. The lanes share Doctor capacity and Pause, not head selection or ordering.
- **Follow-up entitlement:** one active preferential claim and at most one Completed claim from qualifying original Completed New Examination, with frozen deadline/decision and price.
- **Attachment, Audit Event, Staff Notification, Schedule Policy Version, Doctor Confirmation, Metric Definition:** enforce the rules in this PRD; implementation form is determined in technical specifications.

## 3. Actor roles and access matrix

| Operation / data | Doctor = clinic Owner/Admin | Delegated Receptionist Manager | Ordinary authorized Receptionist | Provisioning operator (limited MVP) |
|---|---|---|---|---|
| Owner activation/initial Receptionist creation; grant/revoke delegation; ultimate ownership | Owner-controlled after verified invitation | Deny | Deny | Initial Owner invitation only; no clinic ownership |
| Daily operational administration / clinic settings / subsequent staff management | Allow | Only specifically approved settings/actions explicitly granted by Owner; unresolved allowlist in §12.3, otherwise deny | No delegated settings/staff administration | No clinical setting privilege |
| Patient demographic search/intake/update | Allow own tenant | Allow own tenant and fields | Allow own tenant and fields | Deny clinical data |
| Book, check-in, eligible individual cancel/reschedule | Allow | Allow subject to policies | Allow subject to policies | Deny |
| Same-day eligible bulk RESCHEDULE | Allow | Allow; no separate bulk_reschedule grant | Allow; no separate bulk_reschedule grant | Deny |
| Bulk CANCEL | Deferred | Deferred | Deferred | Deferred |
| Medical Emergency / Paid Urgent intake | Emergency allowed; Paid Urgent creation remains Receptionist-only | Allow prescribed intake | Allow prescribed intake | Deny |
| Ordinary admission | Current mode permits; server-verified eligible head | Same | Same | Deny |
| Exceptional non-head admission | Allow when current mode permits; justification/audit and all safeguards | Same | Same | Deny |
| Follow-up source decision / confirmed-claim extension | Allow with actual actor/role audit | Allow subject to explicit Doctor OPTED_OUT/DEADLINE precedence | Same | Deny |
| Administrative completion after examination actually occurred | Allow completion | Allow attestation; no clinical authorship | Same | Deny |
| Doctor Pause / per-case extra-capacity confirmation / exceptional In Examination cancel | Doctor only | Deny | Deny | Deny |
| Doctor clinical notes/diagnosis/treatment, later audited edits | Doctor authority only | Deny | Deny | Deny |
| Preliminary history / tests / handwritten prescription-image upload | Allow | Explicit field/attachment allowlist only | Same | Deny |
| General in-app clinical history read | Doctor / Owner | Approved preliminary subsets and operational evidence only | Same | Deny |
| Patient archive/unarchive | Allow | Allow | Allow | Deny |
| FULL one-patient PDF/CSV medical export + separate attachment manifest | Allow under BR-040 | Allow under BR-040; exporter can read full downloaded record | Same | Deny |
| Minimal tenant bootstrap/invite/suspend and nonclinical identifiers | No cross-tenant privilege | Deny | Deny | Specifically authorized metadata-only operation; audit, no clinical access |
| SaaS trial, subscription, commercial discount, checkout and customer self-registration | Deferred; future subscription ownership/renewal retained by Owner | Deferred; no ownership transfer | Same | Deferred |
| Data of another Organization | Deny | Deny | Deny | Clinical data DENY; metadata-only gated |

Manager delegation is optional and does not gate the five all-staff grants (non-head admission, Follow-up decisions/extensions, archive/unarchive, single-patient export and eligible bulk reschedule). It never transfers ownership, Doctor identity or clinical authorship. Routine operation without Doctor login starts only **after** secure Owner activation, initial Receptionist account creation and any explicit delegation. It cannot bypass Doctor Pause or per-case extra-capacity confirmation. Full export is an operation-specific disclosure exception, not unrestricted in-app clinical browsing or editing.

**Session control:** strong verified-email recovery for Doctor/Owner, idle/absolute expiry, failed-login throttling and immediate server access invalidation after account disable or permissions removal. Clinical files and attached prescriptions never become public by exposing a URL.

## 4. Operational flows and state machine

### 4.1 Booking / attendance / examination

1. Authorized staff finds or registers Patient and books Routine New Examination or qualifying preferential Follow-up in Open (day-only) or configured exact-time mode. Server allocates preliminary number and immutable EGP monetary / entitlement snapshot.
2. Receptionist confirms **physical presence**; Booked → Waiting creates main-lane membership and internal Doctor alert. No Visit is created at booking/check-in.
3. Receptionist may add distinct immediately present **Medical Emergency New Examination** into a separate emergency lane at ordinary examination pricing; may add **Paid Urgent New Examination** in the main lane only when enabled, valid premium and strictly inside configured clinic operating period (not breaks/holidays). Urgent intake atomically creates Appointment + Waiting.
4. Staff permitted by the current admission mode explicitly choose the **Emergency lane** or **main lane**. Each ordinary admission selects only that lane's latest server-verified eligible head; choosing a lane is NOT an exceptional bypass. Emergency FIFO determines its own head; main lane uses Urgent FIFO then Routine policy. There is no cross-lane priority rule. One transaction enforces tenant/role/mode, target lane and appointment, head/revision, Doctor Pause, **shared Doctor capacity**, and case-specific Doctor confirmation. Successful Waiting → In Examination creates exactly one Visit.
5. Doctor may complete In Examination → Completed; an authorized Receptionist may attest administrative completion **only after examination actually occurred**, recording actual actor/role/time/tenant and immutable audit without asserting Doctor-authored notes. Only Doctor may exceptionally cancel In Examination → Cancelled with reason and retained Visit/clinical audit. Terminal cannot reopen or reschedule. Doctor alone may later amend retained clinical documentation with old/new immutable audit.

### 4.2 Appointment state transition table

| From | Trigger and actor | To | Non-negotiable consequence |
|---|---|---|---|
| None | Valid authorized ordinary booking | Booked | Save day, number, EGP snapshot |
| None | Present authorized Urgent/Emergency quick intake | Waiting (via immediate create+check-in) | New Appointment and immutable Waiting lane; no Visit |
| Booked | Receptionist attendance confirmation | Waiting | Queue entry; in-app Doctor event |
| Waiting | Authorized server-validated admission | In Examination | Create one Visit, respect Pause/capacity/head/exceptions |
| In Examination | Doctor completion or authorized Receptionist administrative attestation after actual examination | Completed | Terminal; retain Visit and actual completion actor/role/time/tenant audit; service value counts |
| In Examination | Doctor exceptional cancel with reason | Cancelled | Terminal; keep Visit and audit, no completed value/follow-up eligibility |
| Booked / Waiting | Authorized reasoned individual cancel | Cancelled | Terminal, preserve old record |
| Booked / Waiting | Valid individual or bulk cross-day reschedule | Booked on new date | Detach old queue, preserve attendance history, new booked sequence/availability check |
| Booked | Configured and enabled unattended cutoff OR authorized manual overdue/no-show reconciliation with confirmed absence | No-show | Terminal, preserve actor/reason/time; safe racing check-in. No automatic cutoff by default |
| Completed / Cancelled / No-show | Reopen/reschedule | DENY | Any future service uses a NEW Appointment |

### 4.3 Pricing, priority, service matrix

| Examination type | Queue lane | Money rule (EGP) | Follow-up source after Completed? |
|---|---|---|---|
| Routine New Examination | Main / Routine | Standard New Exam or authorized non-Urgent discount/free | Yes |
| Paid Urgent New Examination | Main / Urgent FIFO | Enabled fixed premium; base and net > standard base; never Free | Yes |
| Medical Emergency New Examination | Separate Emergency | Standard New Exam or authorized non-Urgent discount/free | Yes |
| Qualifying preferential Follow-up | Main / Routine | Frozen reduced/free preferential price | No recursive entitlement source |
| Nonqualifying follow-up request | New Examination instead | Current standard New Exam price | If Completed New Exam, normal eligibility rules apply |
| Urgent Follow-up / Emergency Follow-up | NOT allowed as separate product combination | N/A | N/A |

**Binding independent-lane admission rule:** lane choice alone never bypasses its head; an admission-authorized Doctor or Receptionist may explicitly select a non-head Waiting patient in that lane only with justification and immutable audit. Emergency does not globally block main-lane admission and vice versa. Emergency FIFO remains the ordinary order. In one shared capacity slot, only one admission may commit; Doctor Pause disables admissions on BOTH lanes. Emergency activity does not change main-lane positions, Urgent arrival order or Routine cursor. A non-head exception never advances that cursor and never bypasses lane/day, state, presence, revision or Doctor extra-capacity safeguards.

### 4.4 Follow-up entitlement and days

A qualifying Completed New Examination on 1 November has day zero on 1 November. **Under the initial seven-day clinic FOLLOWUP setting**, its default last day is 8 November, ending at the end of 8 November in Africa/Cairo, inclusive; current Owner-configured policy governs new claims, including after release and when no explicit Doctor decision exists. Doctor, delegated Manager and ordinary authorized Receptionist may record a source eligibility decision or approve a distinct extension of an already-confirmed claim, with actual actor/role provenance. An existing explicit Doctor `OPTED_OUT` or `DEADLINE` cannot be replaced, overridden, bypassed or extended beyond by any Receptionist through any path; only the authenticated Doctor may revise it. A separate out-of-window **new-booking** date exception is allowed to Doctor with reason or independently to Receptionist with optional reason; immutable audit is mandatory. Only one active preferential claim per original across Booked/Waiting/In Examination and one Completed use ever. Cancellation/No-show releases without consumption; a fresh booking requalifies at current policy/price. Confirmed evidence and price never silently change: a later extension is separately appended, justified and audited before an outside-deadline move, preserving the original snapshot. A later Doctor restriction does not retroactively cancel a confirmed claim but constrains subsequent Receptionist decisions, exceptions and extensions. After use or nonqualification the request is New Examination.

### 4.5 Exact-time interval behavior and configuration changes

Open mode stores service DAY without invented clock time or inferred duration. For exact time, manual slots and generated slots can coexist; manual controls overlapping generated times. Slot intervals are half-open [start,end), avoiding clashes at touching endpoints. Work breaks/holidays prohibit generated times and on-site Urgent. Overlap is OFF unless expressly enabled with the allowed Doctor-capacity checks. Schedule changes with confirmed booking conflicts require affected-list preview and explicit change before activation. Routine queue policy settings apply next Cairo clinic-local day.

## 5. Functional requirements — canonical register

All numbered FR below are applicable MVP requirements; FR-100–102 were revised to minimum secure provisioning and explicit post-MVP commercial exclusions. Operational production gates in §12 remain. Missing numeric IDs are intentionally not resurrected or invented. A requirement cannot authorize behavior contrary to the actor matrix or open-decision gates.

### 5.1 Clinic and patient identity

**FR-001** — Maintain distinct clinic Organizations with one physical branch and one Doctor each in the MVP; data must be scoped to its Organization.

**FR-002** — Operate as a single-doctor clinic; its Doctor remains Owner/default Administrator. Optional daily administration delegation to an existing Receptionist never transfers ownership, Doctor identity, clinical authorship or future subscription ownership/renewal.

**FR-003** — Enforce the operation and field-level permissions in §3 for Doctor/Owner, Receptionist and restricted internal tenant-provisioning operators. Provisioning operators never gain clinical-record access by virtue of platform role.

**FR-005** — Doctor/Owner controls secure activation, initial Receptionist creation and delegation grant/revocation, retaining ultimate clinic control and staff management. A Manager may perform only specifically approved and Owner-granted operational settings/staff actions; the exact settings and subsequent-account-management scope remains unapproved (§12.3). Ordinary Receptionists do not inherit it; no self-delegation or platform-operator bypass.

**FR-006** — Register patients with a three- or four-part name, contact phone, and either date of birth or declared age with its entry/reference date. Do not fabricate a date of birth from an approximate age. Complaint Summary is not required for quick booking.

**FR-006a** — Permit distinct patient profiles to share a contact phone without merging medical histories. Every patient has an internal unique tenant-scoped identity; duplicate demographics may warn but do not block registration by themselves.

**FR-006b** — Find all matching distinct family member records by normalized phone, preserve the original entered phone, and allow adding an additional patient on that phone; changing one patient phone affects only that profile unless other profiles are individually selected.

**FR-007** — Capture optional free-text medical history, chronic illnesses, drug allergies and food allergies.

**FR-008** — Capture optional prior examinations/test reports and relevant preliminary examination information.

### 5.10 Booking, follow-up and prices

**FR-009** — Offer New Examination and preferential qualifying Follow-up/Consultation. A nonqualifying or already consumed Follow-up claim must be booked as a standard New Examination; no standalone standard-price Follow-up in MVP.

**FR-009a** — Use the Cairo-local date of the original Completed examination as day zero. Current Owner-configured FOLLOWUP policy determines the inclusive default new-claim window (initially day seven), including after release and absent an explicit Doctor decision. All authorized clinic staff may record a source eligibility decision with actual actor/role and immutable operational provenance for Receptionist actions, separate from Doctor clinical revisions. `DEFAULT` uses current policy unless constrained by an explicit Doctor decision. If Doctor previously explicitly recorded `OPTED_OUT` or `DEADLINE`, no Receptionist may replace, modify, override, bypass or extend beyond it through source decision, date exception or confirmed-claim extension; only a genuinely Doctor-authenticated, revision-checked, immutably audited revision may change it. Confirmed entitlement/price is not retroactively rewritten.

**FR-009b** — Enforce at most one Completed discounted/free Follow-up per qualifying original Completed examination; Cancelled and No-show Follow-ups consume none, and multiple concurrent active preferential claims are prohibited (BR-002b).

**FR-009c** — Preferential Follow-up requires an original Completed Routine, Medical Emergency or Paid Urgent New Examination for the same patient/clinic/Doctor and an eligible service date under FR-009a, not a fabricated Doctor source decision. For a **new booking** outside the effective clinic-default window, Doctor may approve a justified date-specific exception; an authorized Receptionist may independently approve that specific date without Doctor coapproval and with optional reason. Both require immutable actor/role/source/date/policy/revision audit linked to the confirmed claim. This is distinct from source eligibility and from a justified, audited **already-confirmed-claim extension**, which all authorized clinic staff may approve before an outside-frozen-deadline move. No Receptionist action may defeat an explicit Doctor OPTED_OUT/DEADLINE. No selected date precedes Cairo-local completion or bypasses single-use, patient/day, slot, capacity or other ordinary guards. These permissions confer no diagnosis/treatment/note editing or unrestricted in-app clinical read. Minimum source-decision/extension evidence rules remain §12.3 contract work; optional Receptionist new-date reason is already approved.

**FR-010** — Maintain distinct Appointment (operational booking) and Visit (clinical encounter) records per BR-001.

**FR-011** — Support authorized Open day-only or configured exact-time bookings and individual reschedules. A counted Booked/Waiting Routine appointment being replaced by Paid Urgent requires explicit authorized atomic cancel-and-create, preserving its old terminal record and freezing a new Urgent price. On cross-day Waiting reschedule, remove the existing queue membership, retain historic attendance, restore Booked and require new check-in. In Examination and terminal records cannot be rescheduled.

**FR-012** — Record immutable appointment history: booked clinic-local day and original/reassigned booking number, schedule/queue/service classification, reason/actor/time, individual and confirmed same-day replacement links. For Urgent record walk-in physical-attendance confirmation, service tier, server arrival sequence, premium price provenance, creator and price authorization. Preserve original terminal records.

**FR-013** — Store immutable server-calculated monetary snapshots in EGP for MVP, with service class, base, fixed/percentage authorized discount, reason, free flag only for non-Urgent, and net. Retain immutable preferential Follow-up entitlement evidence. Never silently reprice confirmed bookings.

**FR-014** — Offer authorized percentage and fixed-amount Appointment discounts with a reason; for Urgent a discount is permitted ONLY if resulting net remains strictly GREATER than current configured standard New Examination BASE at creation, and its immutable Urgent snapshot stays auditable.

**FR-014a** — Allow Free Examination/Free Follow-up designation for eligible NON-URGENT service with net zero; Free Urgent is PROHIBITED by premium-price policy.

**FR-015** — Configure basic New Examination and Follow-up prices at the clinic level.

**FR-015a** — The Owner/Admin configures one clinic-local Urgent New Examination fixed BASE price and explicit ENABLED toggle; default unavailable until configured/enabled; premium base must be strictly greater than current standard New Examination base. No surcharge formula or payments.

**FR-017** — Use only the six appointment states defined in BR-003.

### 5.23 Queue, admission, emergency

**FR-018** — On physically confirmed check-in, add a Waiting patient to the appropriate clinic queue lane. Routine and Paid Urgent constitute the main queue; Medical Emergency uses its own independently ordered lane. Queue selection is explicit and server-authoritative; both lanes are subject to shared Doctor Pause/capacity.

**FR-019** — Show live main waiting lane (Paid Urgent and Routine) alongside a distinct Medical Emergency FIFO lane, with stable per-service-day position information, pause, shared capacity and admission state. Displaying or serving Emergency separately must not change positions or routine-turn cursor in the main lane. Historical overdue Waiting lanes are visibly dated, not silently merged with today.

**FR-020** — Assign every Appointment a server-authoritative preliminary booking order/number for its clinic-local day; by default, among actually checked-in patients, Waiting order uses that preliminary booking order, not check-in FIFO. The number and live Waiting position are different (BR-010).

**FR-021** — Support clinic setting choosing preliminary booked-order Waiting priority (default) or physical check-in FIFO, with patients not yet checked in excluded from Waiting positions (BR-010/011).

**FR-021a** — Receptionist may create an immediate Medical Emergency New Examination with standard New Examination price (or authorized non-Urgent discount/free), Waiting entry in a separate emergency lane, and internal Doctor alert. A Completed Emergency New Examination may qualify for preferential Follow-up. The emergency label is clinical priority, never inferred from payment or Urgent premium.

**FR-021b** — Only actual ordinary Routine-head admissions advance the routine missed-turn cursor. Emergency-lane activity, paid Urgent admission, and exceptional admission by any actor do not progress Routine missed turns; late Routine check-in goes after configured number of Routine Waiting (default five), with justified reception manual placement inside Routine only.

**FR-021c** — Offer Receptionist-authorized physically-present same-day Paid Urgent New Examination intake, with a server-created new Appointment, immutable EGP premium snapshot, booking number and instant main-lane Waiting entry in one transaction. Urgent is neither an emergency diagnosis nor a Follow-up.

**FR-023** — Support capacity of one concurrent In Examination by default, with optional increased capacity. For each additional admission above one, the Doctor must issue a fresh case-specific confirmation, even in Receptionist-only admission mode; permission and queue/capacity must be rechecked when admission executes.

**FR-024** — Offer admission permission setting Doctor only / Receptionist only / Both (default Both).

**FR-025** — Provide authenticated in-app Doctor alerts with authorized queue details; optional browser push to an explicitly opted-in Doctor device may display patient NAME ONLY, not age, diagnosis or notes, and must be revoked on loss of access. Push content is separate from protected in-app detail.

**FR-026** — Any admission-authorized Doctor/Owner, delegated Manager or ordinary Receptionist may choose Emergency or main lane. Ordinary admission admits its server-verified head; explicit non-head selection within it requires justification and immutable actual-actor/role audit. Switching lanes is not exceptional. Recheck role/tenant/current admission mode, lane/day, Waiting state, presence, queue/Appointment revisions, Doctor Pause, shared capacity and case-specific Doctor additional-capacity approval. No exception bypasses these safeguards or advances Routine cursor.

**FR-027** — Reflect Doctor admission and examination state changes to Receptionist in realtime.

**FR-028** — Notify Receptionist in-app when Doctor admits a patient.

**FR-029** — Receptionist authorized by current admission mode may select either lane independently and admit its current eligible head ordinarily, or a non-head Waiting patient only through FR-026's justified audited exception. Deny unreasoned, forged, stale, wrong-lane/day or otherwise ineligible targets. Neither lane globally blocks the other; Doctor Pause/shared capacity constrain both. Lane choice and exceptional admissions never advance another lane's sequence or Routine missed-turn cursor.

**FR-030** — Allow Doctor to pause or resume examination admission without pausing queue check-in or future bookings.

**FR-031** — Display Doctor pause/active state on the Receptionist queue screen.

### 5.39 Clinical encounter and audit

**FR-032** — Store Visit examination, diagnosis, clinical notes, treatment, test results, Doctor follow-up recommendation and any genuine Doctor eligibility decision, and authorized medical attachments including scanned/photo handwritten prescriptions. Store Receptionist completion/Follow-up operational evidence separately with actual actor/role, never as Doctor-authored clinical content. Structured electronic prescription composition, printing and PDF generation are out of MVP. A recommended follow-up date is not an automatic booking.

**FR-033** — Allow the Doctor to add/edit clinical documentation after Completed or after an In Examination Appointment is exceptionally cancelled by the Doctor, without reopening the terminal Appointment or changing its outcome.

**FR-033a** — On every Doctor clinical correction after Completed or exceptional In Examination cancellation, create immutable audit evidence of previous and new values, modifying actor identity/role and timestamp accurate to seconds.

**FR-034** — Allow Doctor completion or authorized Receptionist administrative attestation of In Examination → Completed only after examination actually occurred. Preserve the admission-created Visit, actual actor/role/time/tenant and immutable completion evidence/audit; never fabricate Doctor clinical authorship or require invented notes. No seventh Appointment status or terminal reopening.

**FR-035** — Default no closing time and no automatic No-show conversion. If the Owner explicitly configures/enables an automatic clinic-local day cutoff, transition only still-Booked unattended appointments for THAT service day atomically to No-show, safely racing check-in. Without a cutoff, overdue Booked remains Booked and appears in an authorized review list, from which authorized staff may mark verified absence as No-show with actor/reason/time; never infer patient absence from calendar date alone.

**FR-036** — Provide in-app notifications for Doctor and Receptionist operational events.

### 5.45 Internal realtime and bulk

**FR-037** — Support browser push notifications for staff operational events on consented, authenticated, currently authorized devices, constrained to permitted content; Doctor push may include patient name only if personally enabled, never age or clinical content. Revoke active subscriptions after access termination.

**FR-038** — Support audible alerts accompanying important in-app workflow notifications.

**FR-039** — Display realtime updates of queue, attendance, admission, completion and Doctor pause status.

**FR-041a** — Allow all active authorized clinic staff typed-confirmation eligible-only atomic SAME-DAY BULK RESCHEDULING for existing Routine Booked/Waiting appointments to a valid new local day, without a separate per-Receptionist bulk_reschedule grant. Exclude In Examination, terminal, Urgent Waiting and other ineligible items with preview counts/reasons. ALL eligible selected changes commit or none with fresh authorization/revisions and scheduling/claim guards. Bulk Cancellation remains DEFERRED.

**FR-048** — Provide today’s booked total, completed, cancelled and remaining Waiting KPIs; weekly/monthly booking trends and daily patient throughput charts.

### 5.50 Dashboard and navigation

**FR-050** — Completed Examination Value is the sum of EGP net snapshot prices from Completed Examinations only (including completed Emergency standard-rate, paid Urgent and preferential Follow-up). Never represent it as received cash or subscription revenue. Reporting must use clinic-local service day and explicit metric definitions.

**FR-051** — Support Patients Directory search by patient name or phone, with shared-phone family results.

**FR-052** — Support appointment timeline/calendar filtering by date and status, and the applicable action shortcuts.

**FR-053** — In Exact-Time mode default to no overlapping appointment intervals for the one Doctor; explicit overlap enablement is available only when exact times have been configured and must obey saved examination-capacity policy. Open date-only booking has no hidden hourly or daily cap.

### 5.54 Clinic settings

Owner-controlled settings below may be delegated only where the specific setting/action is expressly approved and Owner-granted under FR-071/BR-036. Manager designation alone is insufficient; exact allowlist remains §12.3. Doctor Pause and per-case confirmation are never delegated.

**FR-054** — Examination duration defaults to Open (no inferred clock time). An explicit positive duration can generate timed slots from configured working periods excluding breaks. Explicit manual slots are independently configurable.

**FR-055** — Default to calendar-day Open Booking; exact-time mode may use explicit manual slots, generated duration-based slots, or both. If both are configured for the same day, explicit manual slots are authoritative and generated duplicates are not displayed or bookable. Validate booking intervals against operating periods/breaks and interval conflicts per BR-032.

**FR-056** — Provide weekly clinic working days and optional configured working periods. No closing time/cutoff is set by default. Where a valid period is not configured, Paid Urgent cannot be accepted because its permitted operating period cannot be proven.

**FR-057** — Allow the Doctor/Owner to configure daily start/end working periods and separate explicit No-show cutoff; changes impacting confirmed bookings require affected-booking preview and explicit cancellation/rescheduling before activation.

**FR-058** — Provide configurable working-day break times, default none.

**FR-059** — Offer routine Walk-in enable/disable (default OFF) independent of new Paid Urgent service enablement; existing Medical Emergency entry remains available regardless of either setting.

**FR-059a** — Paid Urgent is OFF by default and separate from Routine Walk-in. Enable only after Owner configures valid premium and full valid clinic operating period; prohibit intake on breaks/holidays/outside configured periods or with no valid schedule. Doctor Pause and capacity delay admission but not permitted Waiting registration.

**FR-060** — Provide booking on holidays/non-working days enable/disable, default disabled.

**FR-061** — At most one COUNTED Appointment per patient/clinic-local day (Booked, Waiting, In Examination, Completed), enforced for Routine, Emergency and Urgent through concurrency-safe DB/transactions. Cancelled or No-show terminal history permits a NEW distinct same-day booking; Completed always blocks, Urgent cannot bypass the rule. Explicit cancel-and-replace must be all-or-none.

**FR-062** — Choose Routine Waiting booked-order (default) or arrival FIFO; late-depth five by default and auditable manual Receptionist Routine placement. Changes to queue policy or late-depth take effect on the NEXT clinic-local operating day, retaining policy snapshot for an active day.

**FR-063** — Provide Doctor examination capacity setting (default one concurrent examination).

**FR-064** — Provide Doctor only / Receptionist only / Both admission actor setting (default Both).

**FR-065** — MVP pricing currency is fixed Egyptian pound (EGP) for all clinic Organizations and cannot be changed in settings. Multi-currency pricing and currency change are post-MVP.

**FR-066** — Owner sets EGP standard New Examination and preferential Follow-up prices, and optionally an enabled Urgent fixed premium above standard Examination price. Do not expose configurable multi-currency settings in MVP.

**FR-067** — Preferential reduced/free Follow-up is eligible only from a qualifying Completed New Examination (Routine, Emergency or Urgent), effective source/policy eligibility and valid date or specific exception under FR-009a/c. Administrative completion is sufficient service evidence without inventing a Doctor decision; explicit Doctor OPTED_OUT/DEADLINE takes precedence over every Receptionist path. At most one Completed preferential use; nonqualifying treatment is New Examination.

**FR-069** — Apply resource- and field-level Doctor/Receptionist tenant authorization. Receptionists may enter allowed intake data and attach report/image of a handwritten prescription as designated attachment; may not author/edit Doctor diagnosis, clinical notes or structured prescriptions.

**FR-070** — Require authenticated identity for clinic and platform protected screens; safe self-service password reset via verified registered email, with abuse limits and audit. Platform account recovery and subscription setup belong to the separately gated SaaS platform scope.

**FR-071** — Support explicit Owner-managed, audited, revocable operational administration delegation to an existing Receptionist, limited to specifically approved settings/actions (§12.3). No new roles framework, self-grants, ownership or clinical-authoring transfer. Ordinary Receptionists lack delegated settings/staff privileges. The five all-staff grants in §3 require no such delegation or optional bulk_reschedule flag. Live checks enforce immediate revocation across sessions and downloads.

### 5.72 Authentication, privacy and records

**FR-072** — Prevent one clinic Organization from accessing, searching, changing, downloading or exporting another clinic Organization’s information.

**FR-073** — Encrypt clinical/patient data in transit and at rest; attach allowed MIME/size validations, malware scanning, tenant-scoped protected file access and short-lived download links. Numerical limits are set in the Security/Data Governance specification before file uploads go live.

**FR-074** — Protect immutable audit for all after-completion and post-exception-cancellation Doctor clinical edits. Separate durable sensitive-operation event audit from previous/new clinical-content audit; require actor/role/time/tenant/action and before/after when meaningful.

**FR-075** — Use idle and absolute session expiry, rate-limit sign-in attempts, safe password reset, and immediate effective access revocation after user disablement/permission change across all sessions.

**FR-076** — Periodically back up operational/clinical data and attachments. Frequency, retention, tested restore, RPO/RTO and Egypt legal compliance constraints are OPEN-OPS release gates and may not be assumed from a daily-backup default.

**FR-079** — Permit all active authorized clinic staff a FULL single-patient medical-record export in readable PDF and structured CSV, including permitted clinical history, with a separate attachment manifest. The exporter can read the downloaded full record; it is not a redacted receptionist projection. Require a specified legitimate purpose, recipient identity/authority verification at handoff, immutable sensitive-operation audit, private server-created encrypted output and protected bounded single-use download with live authorization recheck. Attachments remain separately access-controlled downloads. Bulk patient export is out of scope; Egypt-specific disclosure/legal review remains OPEN-OPS-03.

**FR-080** — Enforce Doctor-only clinical authored fields and later audited correction. Receptionist may access approved preliminary/operational projections and upload permitted attachments without changing Doctor content. All authorized staff may archive/unarchive and perform FR-079 full export; the export-specific full-file read does not confer unrestricted in-app clinical browsing, original attachment downloads or clinical editing. No general clinical deletion.

**FR-081** — Show Visit details with patient, diagnosis, medical notes, findings, tests and permitted attachments, including uploaded photo/scan of a handwritten prescription. No native E-Prescription editor or generated e-prescription PDF/print template in MVP.

**FR-082** — Show patient directory/detail with previous appointment/Visit history, related attachments and family-phone lookup.

### 5.81 Arabic and English

**FR-084** — Localize all system-owned MVP user-interface text in Arabic and English for Doctor/Owner/Admin and Receptionist views.

**FR-085** — Use RTL for Arabic and LTR for English for application navigation, layouts and form controls.

**FR-086** — On first use detect a supported browser language, otherwise default to English, and allow manual Arabic/English switching.

**FR-087** — Keep system state names, role identifiers, error values and machine-readable identifiers stable regardless of UI language.

**FR-088** — Display all user-authored patient, staff, clinical and administrative content, including attachments, as stored without automatic translation.

**FR-089** — Localize displayed dates/times, numbers and currency without changing stored timestamps, amounts, booking times or business outcomes.

### 5.10 Additional requirements from the completed workbook

**FR-090** — Clinic Owner may update authorized demographics with sensitive-event audit; all patient records use stable internal IDs, no general patient merge capability in MVP.

**FR-091** — Expose Owner-controlled grant/revoke of specifically approved operational administration delegation, with immutable actor/role/tenant/scope/time audit and live security revision. Do not expose a separate bulk_reschedule opt-in gate; both ordinary and Manager Receptionists have the five approved operations. Initial Receptionist creation, delegation control and ultimate ownership remain Owner-only; unresolved subsequent-staff/settings scope is not implicitly granted.

**FR-092** — Offer OPTIONAL guided close/review for a selected Cairo service day. Show all unresolved Booked, Waiting and In Examination; on finalizing an explicit day close, all Waiting for that service day must first be intentionally admitted, reason-cancelled or rescheduled by authorized staff. Existing In Examination may continue across midnight and remains linked to original date. Manual close does not alter other states, trigger No-show without the separately enabled cutoff, or prevent continuing an in-progress Visit. Without chosen close, no forced day-end occurs; older pending items stay flagged for review.

**FR-093** — Present dated booking policy change preview of affected confirmed appointments, require explicitly approved reschedule/cancel for conflicts and apply schedule changes only after resolution; never silently delete or reprice bookings.

**FR-094** — Fixed MVP time zone is Africa/Cairo for all clinic-local days and stored scheduling/display rules; changing the timezone is unavailable. Egypt is the default patient phone country, not a restriction on storing international numbers normalized when possible.

**FR-095** — Permit opt-in browser push to the Doctor only, with Doctor patient-name-only notifications and protected in-app details. Store per-device consent and revoke when session/account permission is withdrawn.

**FR-096** — Support safe preliminary/visit attachment upload for handwritten prescription images by authorized Receptionist, with clinical authorship unchanged. The system must not generate a new electronic prescription or claim signature validity.

**FR-097** — Maintain a versioned clinic-local queue policy snapshot for each active operating day; new Routine priority/FIFO/late-depth settings begin next clinic day.

**FR-098** — Provide a Metric Dictionary contract for each Dashboard KPI: counted entity, statuses, event/service date basis, Cairo day, numerator/denominator, replacement treatment and whether it measures value versus cash.

**FR-099** — Provide separate generated-versus-manual exact-time slot handling with mutual-exclusion, break and interval conflict rules from BR-032; avoid generating duplicate visible/bookable times.

**FR-100** — Provide minimal SECURE ADMINISTRATIVE TENANT PROVISIONING for MVP, operable through authenticated restricted CLI/internal API and documented runbook: create an Organization with immutable tenant ID, one Doctor/Owner and one branch; invite first Doctor/Owner through single-use expiring email verification; activate only after invite acceptance. No public signup/checkout/subscription precondition.

**FR-101** — Restrict MVP provisioning-operator capabilities to creation/listing of minimal nonclinical tenant metadata, verified Doctor/Owner invitation, tenant suspension/reactivation and audit of actions. Allow only explicitly authenticated platform operator accounts outside clinic staff roles; never authorize patient/clinical read, attachments, export, pricing or staff clinical management. Do NOT require a browser-based SaaS Admin Dashboard for MVP.

**FR-102** — Keep a clean product boundary between clinic service-value snapshots and future platform subscription commerce. MVP permits operator-approved NON-BILLED pilot tenant activation without automated plans, charges, trial expiry, SaaS checkout or patient payment recording. Public SaaS registration, subscriptions, payment/discount/trial lifecycles and full dashboard are post-MVP and require new explicit approval BEFORE first paid-SaaS launch.

**FR-103** — Restore Doctor/Owner login through verified registered email with single-use expiring token, rate limits, active-session revocation after credential changes and audit; do not require a new broad platform super-user role for password recovery.

**FR-104** — Allow FR-079 full one-patient PDF/CSV export to Doctor/Owner, delegated Manager and ordinary authorized Receptionist under BR-040's purpose, handoff verification, immutable audit and private retrieval controls. No bulk export, public object links, operator clinical export or inferred legal compliance.

**FR-105** — Treat back-up retention, RPO, RTO, restore test, medical data residency, access and retention policy as mandatory documented production readiness gates, with Egypt launch as target and legal validation prior to production.

**FR-106** — Provide a date-scoped Overdue Appointments & Queue Reconciliation view for authorized staff when no automatic cutoff exists; prior-day Booked and Waiting are flagged by derived `requires_review` presentation only (no seventh status), retaining original service date and queue history. No automated cancellation, No-show or reassignment occurs at midnight.

**FR-107** — Allow an authorized Doctor or Receptionist to manually mark only an actually unarrived overdue Booked appointment No-show with required absence confirmation/reason and immutable actor/time audit. A still-physically-present Waiting patient must be individually admitted, reason-cancelled or validly rescheduled, not mislabeled No-show. Verify current state/tenant/date inside transaction and reject races with check-in.

**FR-108** — Persist independently selected Emergency/main admission lane in authorized admission audit. Doctor Pause and examination capacity are shared; server-head checks, revisions, FIFO and Routine turn cursor are lane-scoped. Offer explicit multi-service-day visibility for unresolved previous-day Waiting without automatically enrolling them in the next service-day queue.


## 6. Canonical business rules

These rules are normative. A product question marked OPEN cannot be answered by implementation guesswork; associated acceptance must remain blocked until the Owner signs a policy.

**BR-001** — An Appointment is an operational booking; a Visit is the clinical event associated with actual examination. They remain distinct; merely booking or check-in does not assert completed clinical care.

**BR-002** — MVP bookable types are New Examination and qualifying preferential Follow-up. Nonqualifying requests use New Examination, rather than a standard-price unqualified Follow-up.

**BR-002a** — A qualifying Completed New Examination uses Cairo-local completion day zero and current Owner-configured inclusive FOLLOWUP policy (initially day seven) for fresh claims, including after release. No explicit Doctor decision is required; `DEFAULT` uses policy unless constrained by a distinct explicit Doctor decision. All authorized clinic staff may record source eligibility with actual actor/role and immutable provenance; Receptionist evidence is operational, not a Doctor clinical revision. A previously explicit Doctor `OPTED_OUT` or `DEADLINE` cannot be replaced, overridden, modified, bypassed or extended beyond by Receptionist source decision, booking exception or confirmed-claim extension. Only authenticated Doctor revision with concurrency checks and immutable provenance may change it. Doctor may justify a specific out-of-window new-booking date; Receptionist may independently authorize that date with optional reason, mandatory immutable audit and no Doctor restriction bypass. Previously confirmed claims are not retroactively invalidated.

**BR-002b** — One original Completed qualifying New Examination may support no more than one Completed preferential Follow-up. Active preferred claims across Booked/Waiting/In Examination are mutually exclusive. Cancelled/No-show claims release entitlement; after consumption further care is a New Examination at its appropriate standard rate.

**Source/deadline distinction (BR-002a–c):** A justified explicit Doctor `DEADLINE` defines the source's inclusive eligibility deadline instead of the clinic default, whether shorter or longer; it is not merely a selected NEW-booking date exception. Receptionists may act only without modifying or exceeding that Doctor decision. Confirmed-claim extensions must be non-shortening, separately authorized and appended; neither a source decision nor a new-date exception silently rewrites an existing frozen claim.

**BR-002c** — Confirmation freezes source Visit, policy revision, source decision or policy-derived eligibility, actual source actor/role if any, date-specific booking exception if any, approved service day/deadline, price and provenance. Doctor, Manager or ordinary authorized Receptionist may approve a distinct justified audited extension of an **already-confirmed** claim before an outside-deadline move, preserving original frozen evidence and price through append-only extension evidence. Every Receptionist path respects BR-002a's explicit Doctor OPTED_OUT/DEADLINE precedence, including restrictions recorded after confirmation; a new-booking date exception is not a confirmed-claim extension. Audit binds tenant/patient/source/claim, requested date, actor/role/time, policy/source/claim revisions and prior/new extension evidence. Minimum evidence/reason contract details remain §12.3; no implication that implementation exists. A new booking after release requalifies using current policy and price, not an old claim's extension.

**BR-003** — Appointment statuses are Booked, Waiting, In Examination (stakeholder In-Progress), Completed, Cancelled and No-show. No seventh status is introduced.

**BR-004** — Completed, Cancelled and No-show are terminal. A cancelled or No-show appointment is never restored or rescheduled; any subsequent service requires a new Appointment.

**BR-005** — Authorized cancellation from Booked or Waiting requires a recorded reason and retained history. Only the Doctor may exceptionally cancel an already In Examination Appointment with a recorded reason; the existing Visit, clinical documentation and audit history remain preserved, and the Appointment becomes terminally Cancelled. Doctor-origin cancellation alerts Receptionist in-app. No Receptionist or bulk action may cancel In Examination.

**BR-006** — Individual rescheduling must preserve actor/reason, validated new service day and exact time if configured. A cross-day Waiting reschedule atomically detaches old lane membership, returns status to Booked on the new day, records old check-in as historical only, assigns new-day booking sequence and requires fresh attendance. Terminal/In Examination cannot reschedule; Urgent cannot carry its on-site premium priority into another day. Follow-up and day uniqueness revalidated.

**BR-007** — In Examination → Completed requires examination actually occurred: Doctor completion or authorized Receptionist administrative attestation, with actual actor/role/time/tenant and immutable evidence/audit. Retain Visit and Doctor-only clinical authorship/correction; operational completion never fabricates Doctor notes or a clinical decision. Completed service counts for value and a qualifying New Examination may source Follow-up. Doctor exceptional cancellation is not completion/value/eligibility. Terminal states never reopen; six Appointment statuses remain unchanged.

**BR-008** — Receptionist check-in changes Booked to Waiting upon confirmed physical attendance.

**BR-009** — Only checked-in Waiting appointments occupy active queue positions within their ORIGINAL service-date lane. Emergency uses its independent FIFO lane; Urgent and Routine use the main lane. Waiting ends on admission/cancel/reschedule/authorized departure. On an optional explicit close, unresolved Waiting for that service date must be handled by authorized staff; with no close there is no automatic queue cancellation or overnight lane migration.

**BR-010** — Every booking receives an immutable-in-history server-issued preliminary sequence/number for its relevant clinic-local day (reassign on authorized new-day reschedule, preserving former number in history). Default Waiting ordering prioritizes that booking number among ACTUALLY checked-in patients; preliminary booking number is not live Waiting position.

**BR-011** — Two INDEPENDENT admission lanes: Emergency (its own server-arrival FIFO) and main lane (Urgent server-arrival FIFO ahead of Routine booked-order/default or arrival FIFO). No lane globally outranks the other, and choosing one lane does not reorder or block the other, except that the single Doctor's Pause/capacity constrain both. Routine missed-turn cursor advances only on ordinary Routine-head admissions within main lane.

**BR-011a** — Medical Emergency is a separate prominently displayed lane; quick intake is immediate same-day New Examination and Waiting. It uses standard New Examination pricing unless authorized non-Urgent discount/free and may create Follow-up eligibility after Completed. Emergency E1 precedes E2 under immutable-in-history server-arrival FIFO. It does not mutate main-lane position, priority or Routine cursor.

**BR-011b** — Missed Routine turns advance only on successful ordinary Routine-head admission, never Emergency, Urgent or exceptional admission by any actor. Late Routine checks in after first min(lateDepth, routineWaitingCount) active Routine entries; manual receptionist placement requires reason/audit and stays within Routine. Policy changes apply next local working day, not mid-queue.

**BR-011c** — SERVER-ENFORCED independent-lane admission: any admission-authorized Doctor/Owner, Manager or ordinary Receptionist explicitly selects lane/day/target. Recompute that lane's eligible head and validate role/current mode, tenant, Waiting/presence, revisions, Doctor Pause, shared capacity and fresh Doctor per-case extra-admission confirmation. Admit the head ordinarily, or permit an explicit justified immutable-audited non-head exception WITHIN that lane for any of these staff. No exception waives other guards, changes another lane or advances Routine cursor. Serialize both lanes against shared Doctor capacity and each dated lane revision; reject stale/forged targets and duplicate Visit.

**BR-013** — Capacity is one In Examination by default; when owner enables more, the Doctor must issue a distinct confirmation for each additional admission above one. A receptionist can perform permitted admission only after that Doctor confirmation and fresh transactional validation.

**BR-014** — Do not exceed configured capacity. When extra capacity is permitted and current active examinations require an extra admission confirmation, store time-bounded, case-specific Doctor confirmation and revalidate the requested patient/queue/capacity before applying it. No blanket confirmation carries over between cases.

**BR-015** — Doctor Pause blocks admission by both Doctor and Receptionist but permits check-in, Waiting entries and future bookings; Receptionist sees paused state.

**BR-016** — Receptionist handles permitted intake/uploads, booking/check-in/individual cancellation, Emergency and enabled Paid Urgent physical intake, administrative completion after actual examination and the five all-staff operations in §3. These require no managerial delegation or separate bulk_reschedule grant. Settings/staff administration requires specifically approved Owner delegation; ordinary Receptionists have none. No Receptionist may author/edit Doctor clinical fields, control Doctor Pause/case confirmation or exceptionally cancel In Examination.

**BR-016a** — Paid Urgent is Receptionist-authorized physical SAME-DAY walk-in only during configured clinic operation, when separately enabled with valid premium; patient selection/registration and presence attestation precede transactionally consistent new Urgent Appointment+Waiting entry. A patient with counted Routine Booked/Waiting needs an explicit authorized cancel-and-replace, with typed/clear confirmation and both original terminal cancel/history and new Urgent booking/price/Waiting creation in ONE transaction: on failure rollback both, not a silent status/price conversion. Completed or In Examination existing same-day appointments BLOCK a new Urgent; terminal Cancelled/No-show permit a NEW ID. Visit only on admission, no staff remote Urgent booking, no client-set priority/price.

**BR-017** — Doctor retains own clinical workflow and sole Pause/resume, per-case extra-capacity confirmation and exceptional In Examination cancellation authority. Authorized Receptionist admission and administrative completion follow FR-026/034 without Doctor impersonation. Exceptional cancellation preserves Visit/terminal status under BR-005; it is not delegated by routine operational authority.

**BR-018** — Doctor cancellation records its reason and notifies clinic Receptionist in-app; it never sends a patient WhatsApp/SMS message.

**BR-019** — Clinic cancellation/rescheduling updates internal status; Doctor-initiated cancellation or rescheduling also notifies Receptionist for review. No action creates patient outbound messaging.

**BR-019a** — Bulk RESCHEDULE only in MVP: same-day selection, typed confirmation, preview eligible/excluded and reasons; In Examination, terminal, Urgent Waiting and other ineligibles never moved. Commit all selected eligible replacements or none with locks/revisions, fresh day uniqueness and Follow-up validation and post-commit events. Bulk CANCEL is deferred; when reconsidered, selected policy C requires fresh preview/confirmation on revision changes.

**BR-023** — Clinic Completed Examination Value sums fixed-EGP immutable net prices for Completed examinations only (including standard-price Medical Emergency and Paid Urgent), attributable to service day; never collected patient payments or SaaS subscription billing.

**BR-024** — All MVP clinic prices use EGP. Emergency is a New Examination at standard base (except authorized non-Urgent reasoned discount/free). Urgent is a separately enabled fixed premium, with neither base nor final net at/below standard New Examination base. Only non-Urgent may be Free. Monetary calculations use decimal, currency precision and final-net rounding, with valid discounts not negative nor above base.

**BR-024a** — Owner controls Urgent enablement after valid standard/premium base and operating-period configuration; Manager settings access requires specifically approved Owner delegation under BR-036, not an automatic grant. Server ensures discounted Urgent net remains strictly above standard base, refuses free Urgent, invalid periods and invalid premium; snapshots remain frozen. Invalidating standard-base edits are rejected until an authorized explicit premium correction/disablement.

**BR-025** — All confirmation-time prices are immutable EGP decimal snapshots. Use consistent final-net rounding, no floating-point, negative or above-base discounts. Follow-up freezes effective source/policy eligibility and any actual-actor date exception/deadline; never retroactively shorten/reprice it. Later justified audited extensions are separate append-only evidence, available to all authorized staff subject to BR-002a/c's Doctor precedence, not a rewrite of the frozen snapshot.

**BR-026** — Clinic service value is NOT payment collection. Patient/clinic payment collection, status, receipts and payment gateways are excluded. SaaS tenant subscriptions, paid self-registration and checkout are independently DEFERRED; no subscription revenue or pilot payment status appears in completed examination value.

**BR-027** — MVP defaults to no end-of-day time and no automatic No-show. Optional Owner-configured/enabled Cairo-local cutoff marks only untouched Booked for its matching service day No-show with safe check-in concurrency. Without enabled cutoff, past Booked remains Booked, is flagged for authorized reconciliation and may be manually marked No-show only after verified absence and recorded reason. NEVER automatically No-show Waiting or In Examination.

**BR-029** — Every patient retains independent internal identity/history even when names/phones match. Store entered phone plus normalized form, with Egypt as default country; changing one profile phone never silently changes others. No merge in MVP.

**BR-030** — Two-tier audit: (1) durable sensitive-event audit for settings, permissions, exports, archives, lifecycle and admission; (2) immutable previous/new clinical values on all Doctor clinical corrections after completion or exceptional cancellation. Record actor/role, tenant and second-precision time; preserve previous evidence.

**BR-031** — System text localized Arabic/RTL and English/LTR; user-authored notes/attached scans are displayed unchanged. Canonical statuses, EGP monetary snapshots and Cairo timestamps do not change with UI language.

### 6.1 Additional canonical business rules from the workbook

**BR-032** — EXACT-TIME interval policy: intervals are half-open [start,end), in Africa/Cairo, start < end; Duration-generated starts derive from configured work periods and exclude breaks. Explicit manual slots control their declared times and, if overlapping generated slots on the same date, take precedence without duplicate bookable entries. By default overlap is rejected for the same Doctor if intervals intersect; intervals touching at endpoints are not overlapping. When explicit overlap is enabled, still enforce configured same-time capacity and transactionally validate actual affected slots. Open day-only booking has no time interval or automatic numeric day cap.

**BR-033** — Schedule change policy: proposed new work periods, breaks or holiday settings must preview confirmed bookings made invalid; such changes cannot activate until each affected booking is explicitly cancelled or moved by authorized staff. Prior terminal and appointment history remain intact.

**BR-034** — Clinic/appointment timezone is fixed Africa/Cairo in MVP; local service date is determined consistently on server. No closing time/No-show cutoff is configured by default. Optional cutoff is distinct from operating periods. At local midnight, a new service-day context can open without forcing closure of older service dates or modifying original appointment dates/statuses.

**BR-035** — User accounts must be checked at operation time; account disable or permission revocation immediately prevents protected operations and invalidates active access. Password reset requires verified primary email, single-use expiring reset link and abuse throttling.

**BR-036** — Doctor remains Owner/default Administrator and alone grants/revokes explicit audited operational administration delegation to an existing Receptionist. Manager may exercise only specifically approved Owner-granted settings/actions; exact allowlist and subsequent-staff-management scope await §12.3 approval. Ownership, initial staff creation, delegation control, clinical authorship, Doctor Pause/case confirmations and future subscription ownership never transfer. Five universal operations are independent of delegation; no separate bulk_reschedule permission. Check live grants and invalidate access immediately on revocation.

**BR-037** — Patient identity uses stable unique record IDs; both raw and normalized phone variants may be stored. Egypt is default country for parse. Duplicate patient matches alert but do not force merge; update one selected patient only unless each other update is separately confirmed.

**BR-038** — All active authorized clinic staff may archive/unarchive with actor/role/tenant/time audit and Patient lifecycle serialization. Archive hides from default active directory and blocks new Appointment IDs, including replacements, until unarchive; never deletes history or implicitly cancels existing appointments. Previously confirmed check-in/admission/same-ID reschedule remain eligible under ordinary guards. In-app reads remain role-filtered; full single-patient export follows BR-040, including archived patients.

**BR-039** — Attachment access checks live authenticated tenant + resource + field role at upload, preview and retrieval; full medical export does not authorize unrestricted original-attachment downloads. Allowed MIME/size and malware scanning apply; links private, bounded and protected. Export includes a separate manifest, not embedded attachment files. Limits remain security-release decisions, not user-supplied or invented values.

**BR-040** — All active authorized clinic staff may export ONE patient's FULL medical record (permitted clinical history, readable PDF/structured CSV) plus separate attachment manifest, with no other patient/tenant data or bulk mode. Exporter necessarily can read the downloaded full record; never label it redacted or extend that permission to general clinical UI/editing. Require specified legitimate purpose, intended recipient and identity/authority verification evidence at handoff; internal Owner permission alone is not patient consent/legal basis. Immutable sensitive audit records tenant/patient, actual actor/role/time, purpose, recipient/verification evidence and action/result linkage without unnecessary PHI duplication. Create server-side private encrypted output; escape CSV formulas; require protected bounded single-use retrieval with live staff/tenant/resource recheck, including disablement/revocation. Attachment originals retain separate class/access checks. OPEN-OPS-03 requires written Egypt-specific disclosure/privacy/legal review before production; no unverified HIPAA or Egyptian compliance claim, mandated consent format, document retention or numerical timeout.

**BR-041** — A Receptionist can upload handwritten prescription photos as authorized attachment, with linked patient and visit; cannot edit Doctor-authored diagnosis/prescription instructions. No native E-Prescription or generated standalone digital prescription in MVP.

**BR-042** — Clinic operational metrics are attributed to clinic-local service date; activity events use their own event date and are separately labeled. EGP monetary values sum only completed services. Patient throughput and booking trends count distinct configured entities precisely per Metric Dictionary.

**BR-043** — Browser push contains no medical notes, diagnosis or age; Doctor-specific opted-in push may contain patient NAME only. Consent per authorized device; revocation on deactivation; full clinical details remain inside authenticated app.

**BR-044** — Subscription money, customer checkout, paid SaaS plan/package/trial, discounts and payment statuses are explicitly OUT OF MVP and independent of clinical service value. They require a future separately approved commercial product contract before paid SaaS launch; no current billing implementation or plan limits are implied.

**BR-045** — Authorized provisioning operators can create/list minimal tenant metadata, invite the initial Owner and suspend/reactivate a tenant with reason/audit through restricted operational tools. Access is separate from clinic staff and never authorizes patient/Visit/Attachment contents, clinic medical pricing or export. A full platform SaaS Admin Dashboard and paid support/commercial management are out of MVP.

**BR-046** — Bulk Cancellation ONLY is deferred. Bulk Reschedule uses preview with live server version, typed confirmation and all-or-none commit for eligible selected. If Bulk Cancellation is later introduced, revision mismatch requires new preview and confirmation (recorded deferred policy P1-14-C).

**BR-047** — Lane choice itself is not an exception: either lane head may be admitted independently of the other lane. Emergency FIFO remains server-authoritative ordinary ordering; all admission-authorized staff may explicitly justify/audit a within-lane non-head exception under BR-011c. Doctor Pause, shared capacity and Doctor per-case extra-admission confirmation remain binding across both lanes.

**BR-048** — Flexible calendar rollover: Cairo midnight never automatically ends service for a still-Waiting patient or an In Examination Visit, nor pushes them into a fresh date lane. Display older Waiting as dated carry-over requiring review and explicit lane/service-day selection; if attendance is no longer valid, staff must reason-cancel or cross-day reschedule, with new check-in on the new date. Where actual physical attendance continues into the next calendar day, authorized staff may admit from the original dated lane after explicit presence reconfirmation, preserving original service date and shared Doctor capacity. No invisible queue carry-over or implicit change to today's Routine positions.

**BR-049** — Optional guided closing: a user-triggered close for a selected service day shows unresolved Booked/Waiting/In Examination, validates no Waiting remains on that date at final closure, and records actor/time. Staff must resolve Waiting by lawful admission, cancellation or reschedule; In Examination may finish after the close without changing service day, clinical audit or value attribution. Booked without cutoff stays Booked until individually reviewed/No-show-confirmed; merely marking a day reviewed never changes Booked to No-show. Reconciliation can be reopened or amended as a separate audited management action without reopening any terminal Appointment. No forced closing time is added by enabling review. Access and transitions remain tenant-scoped and audited.


## 7. MVP settings and initial defaults

| Configuration | MVP value / control | Authority / scope |
|---|---|---|
| Country, clinic-local time zone | Egypt / Africa/Cairo fixed | Not changeable in MVP |
| Clinic service currency | EGP fixed | Not changeable in MVP |
| Booking form | Open calendar day only | Exact-time optional explicit enablement |
| Time slots / duration | Open duration; optional manual/generation | Manual slot precedence and BR-032 interval checks |
| Time overlap | Disabled | Enable only in exact-time mode with capacity checks |
| Working periods | Weekly pattern configurable | No preconfigured daily closing time |
| Automatic No-show cutoff | OFF unless explicitly enabled | Overdue Booked review and manual verified-absence No-show without cutoff |
| End-of-day reconciliation | Optional guided review; no mandatory close time | Historical Waiting separately visible; do not auto cancel |
| Breaks | None | No Urgent admission registration during break |
| Routine walk-in | OFF | Independent from Urgent |
| Medical Emergency | Independent FIFO lane; available without Urgent premium | Authorized user chooses lane; shared Doctor Pause/capacity |
| Paid Urgent | OFF pending valid fixed premium/working periods | Premium base and net > standard New Examination |
| Routine Waiting order | Preliminary booked-number default, or check-in FIFO | Applied next operating day on edits |
| Late depth | Five Routine Waiting patients | Configurable; manual justified placement within Routine |
| Admission actors | Doctor + Receptionist (Both) | Current mode/authorization, live lane head or explicit justified audited exception |
| Concurrent examination capacity | One | Doctor per-case confirmation beyond one |
| Follow-up default | Owner-configured inclusive duration, initially completion day zero through seven; no Doctor decision required | All-staff decisions/extensions and separate new-date exceptions; explicit Doctor OPTED_OUT/DEADLINE precedence, actual-actor audit and frozen claims |
| Medical record currency | EGP service value only | Not collected money |
| Receptionist operational delegation | None unless explicitly Owner-granted within approved scope | Does not gate five all-staff operations; no separate bulk_reschedule flag; bulk cancel remains deferred |
| Browser push | Opt-in on Doctor device | At most patient name; in-app richer authorized detail |
| Clinic tenant provisioning | Restricted audited internal CLI/API + first-owner invitation | MVP, no full SaaS admin dashboard |
| SaaS self-registration/subscriptions/trials/discounts | All post-MVP | Commercial policy adopted before paid SaaS launch |

## 8. MVP screens and navigation

1. **Clinic Dashboard** — today service-day counts, Completed/Cancelled/Waiting, weekly/monthly trends, throughput and EGP completed-service value with transparent KPI definitions.
2. **Appointment Calendar / Timeline** — day-only and exact-time booking, check-in, reasoned individual cancel/reschedule, eligible-only atomic bulk reschedule; no bulk cancel.
3. **Live Reception / Queue** — main Paid Urgent/Routine and visibly separate Emergency lane, true waiting positions vs booking numbers, late-depth, Doctor Pause/capacity, authorized admission, Doctor alert.
4. **Visit Detail / Clinical Documents** — Doctor examination, diagnosis and notes, findings, report and handwritten-prescription image attachments; no structured E-Prescription composition/print/PDF.
5. **Patients Directory / Patient Detail** — normalized phone family chooser, independent medical history, archived filter and attachment access based on field-level grants.
6. **Reception Staff Management** — Owner manages Receptionist identities and explicit operational delegation; Manager settings/subsequent staff controls require the approved allowlist, not self-grants. No optional bulk permission.
7. **Clinic Settings** — working calendar/time, booking mode, price/discount, Urgent, priority, follow-up and operational controls within a single Doctor/branch; fixed EGP/Cairo displayed read-only.
8. **Tenant bootstrap (internal operations; NOT a clinic UI view)** — authenticated operator CLI/internal admin API with secure first-Owner invitation, tenant suspension/reactivation and reasoned audit; no SaaS Admin Dashboard, signup or billing screens in MVP.

All clinic-owned text supports Arabic/RTL and English/LTR, including validation, forms, dates, numbers and errors. User-authored clinical text and handwritten scanned images must not be auto-translated.

## 9. Acceptance criteria — regression and new decisions

Legacy scenarios reflect the workbook and approved October/November decisions. Every active FR/BR needs positive/negative and applicable race/security tests for Doctor, Manager and ordinary Receptionist. Product grants are settled except the bounded contract questions in §12.3; production numerical/legal gates remain open. Historical CI proves only the implemented baseline, not this amendment.

**AC-01** — tenant isolation Given separate Organizations A and B and an authenticated A clinic user, when B patient IDs, documents, search, export or bookings are requested, then no B information is disclosed or changed. Authorized within-A access still works.

**AC-02** — Doctor clinical editing succeeds; both Manager and ordinary authorized Receptionist succeed at the five §3 operations and administrative completion after actual examination. Archive/export need no managerial delegation; full export requires BR-040 purpose/handoff verification. Receptionist diagnosis/treatment/notes editing, exceptional In Examination cancel, Doctor impersonation, Pause/case-approval changes and ungranted settings/staff administration fail. Initial creation and delegation control remain Owner-only; revoke/disable is effective immediately.

**AC-03** — Given two distinct patients with the same normalized Egyptian family phone, search shows both without merging histories. Age-only registration saves declared age and its reference date, not an invented DOB; duplicate names warn without blocking.

**AC-04** — appointment vs Visit Given a Booked then Waiting patient, the system keeps an Appointment and queue status without treating an examination as completed. Admission begins a clinical Visit; if the Doctor then cancels In Examination, the Visit, documentation and audit remain available for authorized read/Doctor correction without converting the Appointment to Completed. A Completed Appointment cannot reopen; later Doctor clinical edits remain audited.

**AC-05** — Doctor completion or authorized Receptionist administrative attestation after actual examination sets In Examination → Completed with retained Visit and actual actor/role/time/tenant audit, no fabricated clinical notes/Doctor decision and no seventh state. False/stale/wrong-state completion and completion-vs-cancel/duplicate races cannot commit twice. Only Doctor may exceptionally cancel with reason; Receptionist attempts fail. Cancelled and Completed never reopen/reschedule; retained Visit Doctor corrections preserve immutable old/new actor/time audit.

**AC-06** — Completed qualifying New Examination, including administratively attested completion without a Doctor decision, uses current clinic policy for fresh eligibility. Doctor, Manager and ordinary Receptionist source decisions and distinct confirmed-claim extensions record actual actor/role audit, never forged Doctor authorship. Doctor reasoned or independent Receptionist optional-reason new-date exception succeeds only without a conflicting explicit Doctor decision. Each Receptionist path attempting to replace/override/bypass Doctor OPTED_OUT/DEADLINE or extend beyond it fails; authenticated Doctor revision succeeds with provenance/concurrency checks. Confirmed evidence/price survives prospective change; separate justified extension precedes an outside-deadline move. Single-use, release/rebook current policy/price and races remain enforced.

**AC-07** — Reject forged/negative money, Free Urgent or Urgent discounted net less than/equal standard base. EGP immutable price snaps survive setting changes; invalid standard price change that breaks enabled Urgent is rejected. Completed Examination Value includes only Completed service snapshots and never implies patient collection or SaaS subscription revenue.

**AC-08** — Only ordinary MAIN Routine-head admission advances Routine cursor. Emergency/Urgent or any actor's exceptional admission does not. Doctor, Manager and ordinary Receptionist may admit E1 or U1 ordinarily, or E2/R1 as explicit justified audited non-head exceptions in the chosen lane when current mode and all safeguards permit. Missing justification, stale revisions or wrong lane/day fail without Visit/cursor mutation.

**AC-09** — For selected same-day Booked/Waiting Routine and excluded In Examination/Urgent/terminal records, bulk RESCHEDULE shows eligible/excluded with reasons and commits all eligible selected changes or none on conflict/staleness. Bulk Cancellation is absent from MVP UI and APIs; authorized individual cancel remains.

**AC-10** — Dashboard clinic-local service-day KPIs, trends, patient throughput and EGP Completed Examination Value derive from a defined Metric Dictionary. Service-date and activity-date measures are separately labeled; no cross-tenant or cross-currency aggregation.

**AC-11** — Doctor, Manager and ordinary authorized Receptionist may export one patient's full PDF/CSV medical file plus separate manifest under BR-040. Wrong-tenant, disabled, missing specified purpose or missing recipient/authority verification at handoff fail; revoked/expired/replayed retrieval fails. Full clinical history is present in downloaded output, not redacted to intake; general in-app Doctor narrative and original attachments remain independently restricted. Immutable sensitive audit, safe CSV and private delivery are verified; production legal/backup gates remain OPEN until evidence.

**AC-12** — Authenticated in-app events contain authorized operational detail and are emitted post-commit only. Browser push on an opted-in Doctor device may contain patient NAME only, never age/diagnosis; disabled accounts lose ability to receive current protected details and subscriptions are revoked. No patient WhatsApp/SMS.

**AC-13** — localization Given an Arabic browser preference, UI first displays Arabic/RTL; given an unsupported locale, English/LTR. Manual switching affects system-owned text/layout only, not stored patient notes, canonical identifiers, business outcomes or permissions.

**AC-14** — Default Open day-only without set closing time, exact-time only when configured; Egypt/Cairo fixed clinic local time and EGP fixed currency. Routine walk-in OFF, Urgent OFF until valid premium/working period, independent Emergency FIFO lane, admission actors Both, capacity one. No auto No-show without explicit cutoff, but dated overdue review and audited manual verified-absence No-show are available.

**AC-15** — Emergency FIFO E1/E2 and main U1/R1 remain ordinary order; either head can be chosen without affecting the other lane. All admission-authorized staff may select E2/R1 exceptionally only with explicit justification and immutable actual-actor audit. Both ordinary and exceptional paths enforce selected lane/day, Waiting/presence, current mode/revisions, Doctor Pause/shared capacity and per-case extra-capacity confirmation.

**AC-16** — A physically present enabled paid Urgent uses new same-day Appointment, immutable EGP premium and Waiting entry atomically. Existing counted Routine Booked/Waiting requires explicit atomic cancel-and-replace; failures rollback. Emergency is a separate standard-price New Examination, and completed New Examinations in either lane may be Follow-up sources.

**AC-17** — Deny cross-tenant/forged lane or priority, missing non-head justification, false Doctor actor, stale revisions and duplicate Visit/capacity bypass. Simultaneous ordinary/exceptional admissions by any staff across both lanes serialize against SAME Doctor capacity; one succeeds at capacity one. Recompute lane head and audit any justified exception; no other lane reorder or exceptional Routine-cursor advancement.

### 9.1 Additional acceptance cases from owner decisions

**AC-18** — Given a Waiting Routine appointment moved to a subsequent clinic date, old queue entry disappears, status becomes Booked, old attendance is historical and fresh check-in is required for the new date. No queue ghost survives.

**AC-19** — An optional staff-initiated guided close for a selected service date previews Booked, Waiting and In Examination. Closing cannot finalize while Waiting remains on that date; each Waiting must be admitted, cancelled with reason or cross-day rescheduled. In Examination continues with original service date and clinical history. By default no configured closing time exists, and midnight alone changes no status.

**AC-20** — Emergency New Examination uses standard EGP pricing; independent FIFO activity does not alter main positions/cursor. Either lane's server-validated head may be admitted ordinarily without global priority, or authorized staff may explicitly justify/audit a within-lane non-head exception. Shared Pause/capacity and all FR-026 safeguards remain enforced.

**AC-21** — A restricted platform provisioning operator creates a clinic tenant and sends an expiring, single-use verified-email invitation to its first Doctor/Owner. Without that invitation and successful acceptance there is no self-registration path or paid activation; authorized pilot tenant access requires no subscription plan, gateway or checkout. Platform operator access to patient clinical data is DENIED.

**AC-22** — Owner reset email uses a single-use expiring link, and resetting credentials revokes prior sessions. Disabled Receptionist cannot continue API use despite old browser tab or previously issued token.

**AC-23** — Exact-time manual slot takes precedence over generated slot duplicates. Unenabled overlap rejects intersecting times but permits touching intervals. Slots in breaks/outside work periods fail; Open booking does not inherit a daily time cap.

**AC-24** — A schedule change affecting confirmed appointments generates a before/after conflict preview and cannot activate until expressly resolved bookings; old confirmation never silently disappears or re-prices.

**AC-25** — When user changes Routine priority or late-depth during active day, current positions do not change; new policy applies from next Cairo clinic service day with both versions audit-visible.

**AC-26** — Under Receptionist-only admission and increased capacity, extra admission fails without individual Doctor-issued confirmation. Success needs fresh approval plus selected lane/day head or justified audited exception, Waiting/presence, current mode/revisions/Pause/capacity checks; Manager authority cannot substitute for Doctor confirmation.

**AC-27** — Initial policy allows day seven, denies day eight absent authorized new-date exception; current clinic duration applies to fresh claims including after release. Completion is Cairo day zero; pre-completion dates fail. No Doctor decision/DEFAULT uses policy; explicit Doctor OPTED_OUT/DEADLINE wins over all Receptionist decisions, exceptions and extensions, including a concurrent Doctor revision. Test a date inside and beyond Doctor deadline, opt-out, stale policy/source/claim revision, optional Receptionist new-date reason with mandatory audit, actual actor, frozen price/deadline, and release/rebook without carrying old extensions. Only authenticated Doctor may revise their restriction.

**AC-28** — If Follow-up claim is unavailable, service is booked as New Examination; the app cannot create an independent standard-price nonqualifying Follow-up.

**AC-29** — A receptionist editing diagnosis/prescription text is denied, but permitted upload of scanned handwritten prescription attached to authorized patient/visit works with malware/MIME/size checks and tenant access.

**AC-30** — Doctor, Manager and ordinary authorized Receptionist archive/unarchive with immutable audit and lifecycle locks. Archive hides active directory entry and blocks new Appointment IDs/replacements; it neither deletes history nor implicitly cancels/blocks valid existing check-in, admission or same-ID reschedule. Test archive-vs-new-booking/existing-operation races and wrong-tenant/disabled denial.

**AC-31** — Each active authorized clinic staff variant exports exactly one patient's FULL medical PDF/CSV and separate attachment manifest with specified purpose and recipient identity/authority verification at handoff. Audit actor/role/patient/time/purpose/verification/action immutably without excess PHI. Formula-safe CSV, encrypted private bounded single-use retrieval and authorization recheck pass; disabled/revoked/wrong-tenant, missing purpose/verification, bulk/unscoped attachment and public URL paths fail. Export exposes full downloaded clinical record, not general UI/edit rights. OPEN-OPS-03 legal/privacy release signoff remains unverified.

**AC-32** — All monetary calculations avoid binary floating-point, apply consistent EGP precision and final net rounding. Discount negative, >base, free Urgent and loss of premium fail server validation.

**AC-33** — At default configuration no closing time or automatic No-show is active. After explicit cutoff, unarrived Booked may become No-show and concurrent check-in wins or fails atomically, never both.

**AC-34** — Doctor, Manager and ordinary authorized Receptionist may execute eligible same-day bulk reschedule without separate bulk_reschedule grant. Disabled/wrong-tenant, stale preview, ineligible state or destination conflict denies all selected moves; typed confirmation and live authorization remain required. Bulk cancellation UI/API stays absent.

**AC-35** — Doctor push contains at most name and is never sent without active device opt-in; age and diagnosis remain available only in authorized in-app view. Withdrawal disables future protected notification delivery.

**AC-36** — Medical attachments retain strict clinic tenant isolation across presigned links, storage object operations and backups. Recovery tests, retention/RPO/RTO and Egypt legal constraints remain explicit release gates, not assumed pass.

**AC-37** — Dashboard booked/cancelled service-date count is distinct from cancellation activity by event date; original cancelled/replaced bookings are counted per Metric Dictionary, and value never includes uncompleted services.


**AC-38** — Either lane head may be admitted while the other waits. E2/R1 attempted as ordinary admissions fail; Doctor, Manager or ordinary Receptionist explicit justified non-head exceptions may succeed only inside chosen lane under all FR-026 guards. Emergency leaves main ranks/cursor unchanged; all exceptional admissions leave Routine cursor unchanged. Lane choice alone never requires exception; Pause/capacity/presence/concurrency denials apply to every actor.

**AC-39** — A uniquely authorized provisioning operator invokes an authenticated internal bootstrap flow, creates tenant Organization and first Doctor/Owner invitation once, records actor/time/reason, and establishes access only after secure expiring invitation acceptance. A clinic staff user or unauthenticated caller cannot invoke tenant bootstrap. A provisioning operator cannot read patient, Visit or attachments in any tenant. Duplicate/replayed invites fail safely.

**AC-40** — Operator suspension of a tenant revokes protected clinic requests for both Doctor and Receptionist immediately while preserving data/history; reinstatement requires authorized event audit. Non-billed MVP clinic activation does not invoke subscription state, checkout, trial limits or a SaaS dashboard; no SaaS payment data is stored or required. No future subscription architecture is silently mandated.

**AC-41** — With no configured cutoff, past-date Booked remains Booked and is displayed in dated overdue review, not auto No-show. Authorized staff can verify absence and manually mark a stale Booked No-show with a reason and timestamp. A record concurrently checked in to Waiting cannot also become No-show; Waiting/In Examination cannot be manually marked absent No-show under this workflow.

**AC-42** — At Cairo midnight E1 or R1 Waiting from day D remains tagged D and visibly carry-over; it is not inserted into day D+1 rankings. If physically still present, re-confirmed attendance permits chosen original-date lane admission with shared capacity. Otherwise staff may cancel/rebook lawfully. A Doctor whose Visit started on D can complete after midnight without date or clinical-history rewrite. Optional guided closure cannot finalize with unresolved Waiting and cannot cancel In Examination; marking a day reviewed does not change an untouched Booked to No-show.

**AC-43** — Staff may preview and execute authorized same-day bulk rescheduling all-or-none, with current eligibility and required confirmation; a bulk cancel UI/endpoint does not exist. Individual authorized cancellation remains available. Future-only Bulk Cancellation stale-set policy does not constrain active rescheduling beyond its own established transaction checks.

## 10. Security, compliance, operational and nonfunctional requirements

### 10.1 Security and access

- Enforce tenant scoping for API, search, objects, attachments, exports, notifications, jobs and logs. Default deny, field/resource/operation allowlists for Receptionist; authenticated active role and ownership checks on all protected reads and writes.
- Doctor only can author diagnosis/treatment and audit-correct a retained Visit; reception may enter allowed intake and upload paper prescription/report scans, not alter Doctor-authored clinical content.
- Password reset through verified primary email using expiring single-use tokens; throttle failed attempts and revocation; idle/absolute session expiry; disabled/permission-removed accounts immediately fail protected requests regardless of previously opened tabs.
- Protect every sensitive action with durable audit. Doctor clinical correction after completion or exceptional cancellation records before/new content, actor, role, tenant and timestamp to seconds. Store sensitive event logs without leaking excess clinical detail.
- Browser push opt-in per Doctor device, at most patient name, no age/diagnosis; deny/deactivate subscriptions when authorization ends. In-app authenticated Doctor notification may show authorized clinical/operational details.
- Attachments: configurable MIME allowlist, numeric size limit, malware scan, encrypted storage, time-limited tenant-scoped private retrieval and no public paths. Values and scanner failure behavior must be approved in the Security specification before production.

### 10.2 Backups and legal release gate

Operational+clinical database and medical attachments must be backed up and recoverable, with a documented and exercised restore procedure. Do not assert or invent numeric backup frequency, retention, RPO/RTO, jurisdiction retention period, residency or session timeout in this PRD. Market one is Egypt; local healthcare/privacy/data-protection requirements require Egypt-specific review and written production approval. OPEN-OPS-01/02/03 block patient-data production launch until exact targets, verification owner and evidence are defined. Developing isolated non-production features is not equal to production readiness.

### 10.3 Concurrency and integrity assurance

- Counted patient/day uniqueness for Booked/Waiting/In Examination/Completed; terminal Cancelled/No-show permits new distinct ID. Cancel-and-replace is fully atomic.
- Queue arrival, admission, manual placement, pause and capacity must use fresh server state and transactional locks/conditional updates, not UI order. One admission creates one Visit; no stale response may skip the valid ordinary head or advance Routine cursor wrongly.
- EGP snapshot, preferential entitlement, follow-up claim and period numbers are immutable-in-history; rescheduling is transactional with current-day validation and old history preserved.
- Post-commit only internal notifications; rollback must emit none. Bulk reschedule all-or-none; bulk cancellation not an MVP endpoint.
- Defined Metric Dictionary determines all reporting SQL/view semantics, service versus event date and replacement counting.

**Development environment clarification (2026-10-21; non-product):** Docker is unavailable and cannot be installed on the Product Owner's workstation. Docker/Compose and database integration evidence must be produced in a Docker-capable CI environment; workstation checks may run without containers. This changes no product requirement, acceptance outcome, or production gate. A missing integration run is not a passing test.

## 11. Explicit exclusions and post-MVP features

- **Deferred**: direct clinic/patient payment collection and paid/unpaid, payment gateway for examination, WhatsApp/SMS and patient reminders, bulk cancellation, E-Prescription composition/printing/signing, more than one clinic Doctor or branch, medical cross-Doctor sharing, patient portal, bulk patient exports, merging/deleting medical records, multi-currency and timezone changes, multi-country country-specific clinic operations, patient memberships, laboratory networks, home visits and desktop apps.
- **Deferred SaaS commercial scope:** self-service customer clinic registration, subscription billing/checkout, paid plans, trials, discounts, full SaaS Admin Dashboard and support/commercial management are post-MVP. Minimal authenticated audited tenant bootstrap + first Owner invitation + tenant suspend/reactivate remain mandatory MVP operations and do not give platform staff medical-data access.
- **Future-only policy preserved**: if Bulk Cancellation is later approved, option C requires a fresh server-side preview and typed confirmation if selected set/queue revision changed.

## 12. Resolved owner choices and remaining production gates

### 12.1 Four resolved product decisions — no implementation ambiguity

| Decision | Binding result | Consequence |
|---|---|---|
| CLOSED-EM-01 | Emergency is a wholly independent FIFO queue with explicit lane selection and **no cross-lane priority**; same single-Doctor capacity/Pause | BR-011/011a/011c/047; FR-018/026/029/108; AC-08/15/17/38 |
| CLOSED-SAAS-01/02 | Defer customer self-registration, payments/subscriptions/trials and full SaaS admin dashboard; minimal audited operator-only tenant bootstrap is mandatory | FR-100–102; BR-044/045; AC-39/40 |
| CLOSED-DAY-01 | No cutoff by default; overdue records stay intact and flagged; manual verified-absence No-show, optional guided close, original-day carry-over and shared capacity | FR-035/092/106/107; BR-027/048/049; AC-19/33/41/42 |
| CLOSED-BULK-01 | Only Bulk Cancellation deferred; Bulk Rescheduling remains in MVP | FR-041a; BR-019a/046; AC-09/34/43 |

### 12.2 Still open — production and future commercial release gates

| Gate | Required disposition | Blocks |
|---|---|---|
| OPEN-OPS-01 | Approved numeric database/attachment backup schedule, retention, test restore evidence, owner and procedure | Production with patient data |
| OPEN-OPS-02 | Approved numeric RPO/RTO and demonstrated recovery | Production with patient data |
| OPEN-OPS-03 | Written Egypt healthcare/privacy/legal review: residency, retention/access/disclosure basis, full staff medical exports, legitimate purpose and recipient/authority verification process; internal Owner permission is not consent or legal certification | Production with patient data; no unverified HIPAA/Egypt compliance claim |
| OPEN-FILES-01 | Approved MIME extensions, per-file/count/size limits, malware scan failure policy and storage quotas | Production medical attachment upload |
| FUTURE-SAAS-COMMERCE | Approve SaaS subscription models, trials, platform discounts, pricing, invoicing, gateway/provider, taxes, payment failure/grace/cancel and legal obligations | First paid SaaS subscription launch, NOT non-billed MVP clinic deployment |

These are separate production/commercial release gates, not instructions to invent numeric policies. This v1.0 Product Contract is **approved as of 19 October 2026**; its approval does not waive any remaining gate or authorize implementation.

### 12.3 Bounded amendment contract work — Issues #24 / #25 remain OPEN

| Unresolved detail | Safe boundary pending approval / review | Owner, trigger and closure |
|---|---|---|
| Exact Manager settings allowlist and management of subsequent Receptionist accounts | Delegation model approved, but no blanket settings/staff/finance/privacy grant; initial staff creation and delegation control remain Owner-only. Existing Owner-only setting wording is the fallback until that specific setting is approved for delegation | Product Owner + Security/domain review before delegation implementation; close with explicit operation/field matrix, revocation and escalation-denial acceptance |
| Source-decision / confirmed-extension minimum reason/evidence and DTO/storage migration contracts; actual-examination attestation evidence | All-staff permissions and Doctor precedence are approved. Retain justified immutable extension audit; do not infer that optional Receptionist **new-booking** reason applies to these distinct actions, forge clinical revisions or rewrite published migrations | Product Owner for minimum evidence/reason approval; domain/security/database engineering for contract/forward-only migration review before affected D-07/D-08/B-10/B-16/B-18 work. Close with reviewed actor/source/policy/claim/revision contracts and positive/denial/race proof |

Existing Doctor non-default source-decision justification requirements are retained; only Receptionist **NEW-booking date exception** reason is expressly optional. The remaining contract review must preserve those established Doctor safeguards rather than infer their removal from the permission expansion.

These bounded questions block the affected implementation contracts, not the approved five-operation grants. No exceptional Receptionist In Examination cancellation, Doctor capacity-confirmation delegation or Owner-activation bypass has been approved. Issues #24/#25 require downstream alignment, separately authorized implementation, exact-SHA PostgreSQL evidence, independent review and PO acceptance; this documentation action does not close them. Production-only gates remain tracked by Issue #18.

## 13. Decision register — all 47 completed workbook choices

This is historical workbook evidence, not a second active requirement register. Verbatim selections/Owner notes are preserved; explicit 19 October and 3–4 November 2026 amendments above take precedence. In particular, older Doctor-only source/extension/non-head, Owner-only archive/export and optional bulk/no-delegation interpretations are superseded. Selection letters alone do not authorize implementation.

### P0-01 — سياسة تغيير العملة ومجموع القيمة المالية

- **Selected option:** D. D — خيار آخر: في MVP العملة الافتراضية هي الجنيه المصري وغير متاح تغييرها، لكن مستقبلا سوف يكون هناك عدة عملات ويمكن ضبطها من الاعدادات

### P0-02 — ترتيب Medical Emergency عند تساوي الأولوية

- **Selected option:** D. D — خيار آخر: بكل بساطة حالات الطوارئ لا تدخل ضمن الطابور الرئيسي ولا تؤثر فيه بل يكون لها طابور منفصل أو ما شابه ذلك
- **Resolution:** CLOSED-EM-01 (19 October): full independent FIFO admission lane, no cross-lane priority; shared Doctor capacity and Pause.

### P0-03 — مصير Waiting عند إعادة الجدولة

- **Selected option:** A. A — Waiting ← Booked بتاريخ جديد؛ إزالة عضوية Queue الحالية، وحفظ check-in السابق تاريخيًا؛ يتطلب حضورًا جديدًا.

### P0-04 — إغلاق اليوم مع Waiting وIn Examination

- **Selected option:** B. B — إغلاق تشغيلي موجّه: يعرض النظام قائمة المرضى ويُلزم الموظف بتسوية كل Waiting قبل إقفال اليوم.
- **Owner note (verbatim):** ملاحظات: افتراضيا يجب أن لا يوجد وقت إغلاق إلى لو تم تحديد ذلك من الاعدادات
- **Resolution:** CLOSED-DAY-01 (19 October): no default closing time; optional guided close, overdue review, manual verified-absence No-show, dated carry-over without status mutation.

### P0-05 — مصفوفة الخدمة/أولوية الطابور/السعر/المتابعة

- **Selected option:** A. A — Emergency = New Examination فقط، بسعر New Examination العادي (مع خصم/مجانية NON-Urgent عند التفويض)، وتؤهل متابعة بعد Completed.

### P1-01 — تأسيس Organization وحساب Doctor/Owner الأول

- **Selected option:** A. A — Provisioning إداري خارج التطبيق؛ تُمنح دعوة موثقة لأول Doctor/Owner.
- **Owner note (verbatim):** ملاحظات: هذا مشروع SaaS يعني ان العميل سوف يقوم بالتسجيل ويدفع ويؤسس عيادته، وبالتالي سوف يكون هناك SaaS admin dashboard ليتثنى لي أو فريق العمل إضافة عيادات أو دعوتهم أو تحديدهم والخصومات والفترات التجريبية والدعم وما إلى ذلك
- **Resolution:** CLOSED-SAAS-01/02 (19 October): defer public signup, SaaS payment/subscriptions and dashboard; MVP uses restricted audited operator provisioning. Commercial policy is future paid-launch gate.

### P1-01b — استعادة حساب Doctor/Owner وتعطيله

- **Selected option:** B. B — استعادة ذاتية موثقة عبر البريد الإلكتروني المسجل مع حماية إساءة الاستخدام.

### P1-02 — تفويض Bulk والإعدادات التشغيلية للاستقبال

- **Selected option:** A. A — أعلام صلاحية لكل موظف يضبطها Owner مثل bulk_cancel/bulk_reschedule؛ لا تفويض للإعدادات الإدارية في MVP.
- **Superseded (4 November 2026, #25):** no separate bulk_reschedule grant; optional explicit Owner delegation of approved operational settings is allowed under BR-036/§12.3. Bulk cancel remains deferred.

### P1-03 — سحب صلاحيات الجلسات والسياسات الأمنية

- **Selected option:** A. A — تعطيل الحساب/سحب الدور يبطل الوصول فورًا لكل الجلسات، مع idle/absolute expiry، وحد للمحاولات واستعادة آمنة.

### P1-04 — معنى Exact-Time وتوليد Slots والتداخل

- **Selected option:** C. C — طريقتان مستقلتان Slots يدوية أو Slots مولدة من Duration، مع أسبقية محددة عند تفعيلهما معًا.
- **Owner note (verbatim):** ملاحظات/استثناءات: قم انت بضبط مواصلة واضحة للتعارض استنادا للمشروع

### P1-05 — تغيير الجدول أو الإجازات بوجود حجوزات مؤكدة

- **Selected option:** C. C — تفعيل التغيير بعد مراجعة قائمة الحجوزات المتأثرة وإعادة جدولتها أو إلغائها صراحة.

### P1-06 — المنطقة الزمنية وموعد No-show cutoff

- **Selected option:** A. A — Timezone محددة للعيادة عند الإنشاء؛ cutoff آخر فترة عمل فعلية، مع قفل معاملاتي لسباق Check-in/No-show.
- **Owner note (verbatim):** ملاحظات: توقيت النظام افتراضيا هو توقيت القاهرة وغير متاح تغييره في MVP
- **Resolution:** Fixed Cairo timezone note controls MVP configuration; no changing tenant timezone.

### P1-07 — Urgent خلال الاستراحات وغياب ساعات عمل صالحة

- **Selected option:** A. A — مسموح فقط داخل فترة عمل مهيأة وغير واقعة في استراحة/عطلة؛ عدم وجود جدول صالح = رفض.

### P1-08 — هل Follow-up بالسعر الكامل مسموح بلا استحقاق؟

- **Selected option:** A. A — لا يوجد Follow-up مستقل دون استحقاق؛ يتحول الطلب إلى New Examination بسعره المعتاد.

### P1-09 — متى وكيف تُسجّل موافقة الطبيب على خصم المتابعة؟

- **Selected option:** C. C — قرار Visit يكفي ضمن المهلة الافتراضية؛ الحالات الاستثنائية تحتاج موافقة منفردة.
- **Superseded in part (3–4 November 2026, #24/#25):** no mandatory Doctor source decision; current policy and all-staff source/date/extension authorities follow BR-002a/c with explicit Doctor precedence and actual actor audit.

### P1-10 — حدود السبعة أيام واحتساب تاريخ التأهيل

- **Selected option:** A. A — تاريخ إكمال الكشف = اليوم 0، وآخر يوم مسموح نهاية اليوم المحلي السابع.
- **Superseded in part (3 November 2026, #24):** day zero/Cairo inclusivity retained; seven is the initial Owner-configurable default, not a permanent limit.

### P1-11 — تأكيد الطبيب لإدخال مريض ثانٍ

- **Selected option:** A. A — طلب تأكيد لكل حالة يوافق عليه الطبيب؛ بعده ينفذ المصرح له admission مع إعادة التحقق لحظيًا.

### P1-12 — تغيير إعداد Queue أثناء وجود Waiting

- **Selected option:** C. C — تسري مع اليوم التشغيلي التالي فقط، مع حفظ نسخة سياسة اليوم الحالي.

### P1-13 — هل يسمح Doctor Exception بتجاوز Emergency؟

- **Selected option:** A. A — نعم، وفق سياسة BR-011c الحالية وبسبب إلزامي وسجل أولوية وقت القرار.
- **Current interpretation (4 November 2026, #25):** independent lanes have no global precedence; justified within-lane exceptions are available to all admission-authorized clinic staff, not Doctor-only.

### P1-14 — ذرّية Bulk Cancellation عند تغيّر الأهلية

- **Selected option:** C. C — إذا اختلفت Revision المجموعة يُرفض الطلب بالكامل ويُطلب Preview/Confirm جديدان.
- **Owner note (verbatim):** ملاحظات/استثناءات: هذه ميزة خارج نطاق MVP
- **Resolution:** CLOSED-BULK-01 (19 October): only Bulk Cancellation deferred; Bulk Reschedule retained. Option C retained for future Bulk Cancellation.

### P1-15 — دقة الأسعار والخصومات وحدود القيم

- **Selected option:** A. A — قيم Decimal بحسب وحدة العملة، تقريب موحد عند قيمة NET النهائية، خصومات غير سالبة ولا تتجاوز السعر؛ لا تعديل للسعر بعد التأكيد.

### P1-16 — تغيير Standard Price وإبطال صلاحية Urgent

- **Selected option:** A. A — رفض حفظ السعر العادي حتى يعدل المالك سعر Urgent أو يعطّله صراحة.

### DATA-01 — تسجيل العمر بدل تاريخ الميلاد

- **Selected option:** A. A — تخزين العمر المصرّح به + تاريخ إدخاله/مرجعه؛ لا تخمين DOB.

### DATA-02 — الأسماء المتشابهة وتعديل هوية المريض

- **Selected option:** A. A — هوية سجل داخلية فريدة دائمًا؛ تنبيه تشابه غير مانع؛ تعديل ديموغرافي مدقق؛ لا Merge في MVP.

### DATA-03 — تطبيع رقم الهاتف والبحث العائلي

- **Selected option:** A. A — حفظ raw phone مع نسخة normalized معيارية مثل E.164 عند توفر سياق الدولة؛ عرض الأسر بجميع التطابقات.
- **Owner note (verbatim):** ملاحظات: البلد الافتراضي مصر حاليا, ومستقبلا سوف يكون متاح عدة دول

### DATA-04 — تعديل رقم اتصال أسرة مشتركة

- **Selected option:** A. A — تغيير ملف المريض المختار فقط؛ إتاحة اختيار واعٍ لتعديل ملفات أخرى على نحو مستقل.

### DATA-05 — حقول القراءة والكتابة الخاصة بالاستقبال

- **Selected option:** A. A — Allowlist لكل مورد/حقل/عملية للاطلاع والتعديل والإرفاق مع اختبارات رفض.

### DATA-06 — مواصفة رفع وتنزيل المرفقات الطبية

- **Selected option:** A. A — قائمة أنواع MIME آمنة وأحجام محددة، فحص برمجيات خبيثة، تخزين مشفر وروابط تنزيل قصيرة العمر ومقيدة بالمستأجر.

### DATA-07 — نطاق تصدير بيانات المريض بصيغ PDF وCSV

- **Selected option:** A. A — تصدير مريض واحد مع السجل المصرح به: PDF مقروء وCSV للبيانات الهيكلية وقائمة المرفقات؛ تنزيل المرفقات منفصل محمي.

### DATA-08 — سلوك أرشفة ملف المريض

- **Selected option:** A. A — إخفاء من القائمة التشغيلية الافتراضية، منع حجز جديد حتى إعادة التنشيط، والسماح بالقراءة/التصدير للمالك.
- **Superseded access scope (4 November 2026, #25):** all authorized staff may archive/unarchive and export a full single-patient record under BR-038/040; general clinical UI reads remain restricted and existing bookings are not cancelled.

### DATA-09 — تدقيق العمليات الطبية والعمليات الحساسة

- **Selected option:** C. C — طبقتان: Audit دائم لكل قرار حساس، ونسخ المحتوى previous/new للبيانات السريرية فقط.

### DATA-10 — قالب ومخرجات E-Prescription

- **Selected option:** D. D — خيار آخر: الاطباء لا يمكنهم الاستغناء عن كتابة الروشتة بالقلم على ورق لذلك فكرة E-Prescription غير متواجدة حاليا فقط يمكن تكون ميزة متقدمة ما بعد الـ MVP, لكن المسموح به الان هو رفع صورة من الروشتة المكتوبة يدويا على النظام من قبل مسؤول الاستقبال ضمن فحوصات واوراق الكشف

### SEC-01 — خصوصية بيانات Browser Push على الأجهزة

- **Selected option:** B. B — الاسم فقط في إشعار الطبيب بعد موافقة صريحة مع إلغاء الاشتراكات عند سحب الوصول.

### OPS-01 — تكرار النسخ الاحتياطية وفترات الاحتفاظ

- **Selected option:** C. C — سياسة تفصيلية بناءً على تصنيف بيانات وتشغيل العيادة والميزانية قبل اعتماد الأرقام.

### OPS-02 — أهداف التعافي من الأعطال RPO/RTO

- **Selected option:** C. C — لا تعتمد أرقام الآن للتطوير فقط؛ يمنع إطلاق الإنتاج قبل اختيار قيم وإثبات Restore.

### OPS-03 — الخصوصية والولاية القانونية والاحتفاظ

- **Selected option:** A. A — تحديد دولة/سوق الإطلاق ومراجعة القانون المحلي قبل اختيار Residency/Retention/Access Processes.
- **Owner note (verbatim):** ملاحظات: دولة/سوق الاطلاق سوف يكون مصر بشكل اساسي واول اطلاق

### MET-01 — تاريخ إسناد إلغاءات وحجوزات Dashboard

- **Selected option:** A. A — KPIs التشغيلية حسب يوم الخدمة clinic-local؛ مؤشرات Activity مستقلة حسب يوم حدوث الفعل.

### MET-02 — تعريف Throughput وTrends ووحدة العد

- **Selected option:** A. A — Metric Dictionary مفصل لكل KPI: الكيان، الحالات، Date Basis، Timezone، معادلة العد، التعامل مع replacement.

### DOC-01 — تعارض صياغة atomic cancel-and-replace

- **Selected option:** A. A — تعديل FR-011 ليصبح إلزاميًا Atomic كما في BR-016a (توحيد تحريري دون تغيير السياسة).

### DOC-02 — إحالة خاطئة إلى مصفوفة صلاحيات §3

- **Selected option:** A. A — نقل المصفوفة إلى §3 وإبقاء الإحالات كما هي.

### DOC-03 — موضع جداول الإعدادات والشاشات خارج §§7–8

- **Selected option:** A. A — نقل جدول الإعدادات إلى §7، وجدول الشاشات إلى §8.

### DOC-04 — لغة تاريخية توحي بأن v0.5 ما زالت Draft

- **Selected option:** B. B — نقل السجل التاريخي إلى Revision History/Appendix وعدم الإبقاء عليه وسط المتطلبات.

### DOC-05 — إحالات AS-11/13/29 غير المفسرة داخل الملف

- **Selected option:** A. A — إرفاق تعريف AS وقائمة روابط ثابتة إلى وثيقة المصدر المعتمدة.

### DOC-06 — تغطية Traceability وAcceptance للحالات الحدية

- **Selected option:** A. A — كل FR/BR مرتبط بقرار مصدر واختبار إيجابي وسلبي، وحالة تنافس عند الحاجة، وصاحب الاختبار.

### PLAN-01 — طريقة توثيق تعديلات النسخة المعتمدة

- **Selected option:** D. D — خيار آخر: الغاء اي رقم لاصدار حالي واعتبر هذه نسخة اولى مستقلة بذاتها وكاملة اي لا تعتمد على اي ملفات اخرى مكملة
- **Resolution:** This standalone v1.0 contains all operative product requirements and was expressly **approved by the Product Owner on 19 October 2026**. Prior drafts are historical only; product approval is not implementation authorization.

### PLAN-02 — الحزمة اللازمة قبل طلب التنفيذ من AI Agent

- **Selected option:** A. A — PRD مصحح → Domain Invariants/State Machines → Security/Data Governance → Acceptance Matrix → Architecture → Implementation Plan.

### PLAN-03 — حدود النطاق المؤجلة في PRD الجديد

- **Selected option:** A. A — إبقاء جميع الاستبعادات كما هي؛ أي خيار يغيرها يسجل Scope Change مستقلًا ويحتاج اعتماد المالك.

## 14. Product approval record and outstanding release gates

- [x] Four product interpretation decisions (Emergency, SaaS deferral, day close, Bulk only) are incorporated into the Product Owner-approved v1.0 contract.
- [x] SaaS self-service/billing/admin and Bulk Cancellation explicitly deferred per owner clarification, while existing E-Prescription exclusion from the completed workbook is retained.
- [ ] Security/Operations determines production OPEN-OPS/OPEN-FILES numbers and tests, with Egypt regulatory counsel review as applicable.
- [ ] Acceptance coverage and role access mapping updated to the resulting final decisions; all active FR/BR and AC include positive, negative and race tests where relevant.
- [x] Product Owner explicitly approved this standalone v1.0 Product Contract on 19 October 2026, effective the same date. No code, payment integration, data migration, deployment or production permission derives from product approval alone.
