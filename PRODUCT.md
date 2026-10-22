# CarePulse

<!-- impeccable:product-schema 1 -->

## Platform

web

## Authority and Scope

This is a summary of confirmed product context, subordinate to these approved documents in descending precedence:

1. [PRD v1.0](docs/CarePulse_PRD_v1_APPROVED.md) — product scope, roles, business rules and acceptance outcomes; approved 19 October 2026.
2. [Domain Model and State Machines v1.2](docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md) — domain invariants and command boundaries; approved 19 October 2026.
3. [System Design v1.0](docs/CarePulse_System_Design_v1_APPROVED%281%29.md) — architecture and technical constraints; approved 21 October 2026.
4. [Implementation Plan](docs/implementation-plan.md) — execution scope, dependencies, field permissions, contracts and evidence; approved 21 October 2026.

Exact requirements and acceptance criteria remain in those sources. The [Constitution](.specify/memory/constitution.md) governs engineering practice within their authority. This record establishes no new requirements, visual preferences, implementation authorization or production approval.

## Users

- **Doctor/Owner:** the clinic's single Doctor is also its Owner/Admin. Examines patients, authors and corrects clinical documentation, completes Visits, controls admission/Pause, and manages clinic settings, prices, Receptionists, patient archive and authorized one-patient exports.
- **Receptionist:** handles patient/family intake, booking, physical check-in, permitted on-site intake, queues, individual changes and authorized admission. Eligible bulk rescheduling requires an explicit per-user Owner grant. Access is limited to approved operational and preliminary intake fields.
- **Patients are records, not authenticated users.** Family members may share a phone number while retaining separate identities and medical histories.
- **Platform provisioning operator:** a separate, restricted operational identity for audited tenant bootstrap, initial Owner invitation and suspension/reactivation. This is an internal CLI responsibility, with minimal nonclinical metadata access and no clinical access or full SaaS administration UI.

Sources: PRD §§1–3; Domain §§2–4; Plan §2 and B-03–05.

## Product Purpose

CarePulse is a multi-tenant clinic management SaaS, initially targeting Egypt. It supports the staff workflow from patient registration and booking through attendance, queue admission, examination, documentation and operational reporting. Each active clinic Organization has exactly one Doctor/Owner and one physical branch in MVP; Organizations remain independently isolated.

The product is a Next.js web application; frontend and BFF location: `apps/web`. This root record captures shared CarePulse 3 product context.

Success means completing these approved workflows with clear current state, correct role access, preserved medical history and reliable server-confirmed outcomes. Feature acceptance is defined by the PRD's AC-01–43, not by this summary.

## Operating Context

- Clinic staff use responsive, accessible web workflows, with desktop/tablet reception and examination layouts and mobile-safe behavior.
- System-owned UI supports Arabic RTL and English LTR. On first use, detect a supported browser language, otherwise use English; provide manual switching.
- Egypt is the initial market and default phone-country context. Clinic currency is fixed **EGP**, and clinic-local dates/time use **Africa/Cairo**; neither is configurable in MVP.
- Default booking is an Open service day without an invented clock time or duration. Exact-Time booking is available only when configured. No closing time or automatic No-show cutoff is enabled by default.
- Clinic onboarding is invitation-based. Operator-approved non-billed pilots do not require subscription plans, checkout or trial expiry, and remain subject to production safety gates.

Sources: PRD §§1, 4, 7–8 and FR-084–094; System Design §§1–2; Plan U-01/12.

## Capabilities and Constraints

- **Patient identity and records:** patient/family search, registration, permitted demographic/preliminary updates and history. Preserve raw and normalized phones; shared phones and similar names never merge histories. Store date of birth or declared age with its reference date without inventing a birth date. Owner archive hides a patient from the active directory and blocks new Appointment creation until unarchive; valid existing appointments retain their authorized continuity.
- **Appointment and Visit lifecycle:** booking and check-in do not create a Visit. Admission creates exactly one Visit. The six Appointment states are Booked, Waiting, In Examination, Completed, Cancelled and No-show. Terminal states never reopen; Doctor-only exceptional cancellation during examination retains the Visit, and later clinical corrections retain immutable before/after audit.
- **Independent queues:** Medical Emergency has its own FIFO lane; the main lane prioritizes Paid Urgent FIFO before Routine. Neither lane globally outranks the other. Authorized staff explicitly choose a dated lane and ordinarily admit its server-verified head. Doctor-only non-head exceptions require reason and audit. Both lanes share Doctor Pause, capacity and per-case confirmation rules; booking numbers differ from live waiting positions.
- **Intake distinctions:** Medical Emergency is a standard-price New Examination with its own intake rules. Paid Urgent is a premium New Examination service label, not a payment record or emergency diagnosis; it is Receptionist-only, physically present, same-day and available only when enabled with valid premium and operating periods. Routine walk-in is separately controlled and OFF by default. Preserve the source rules for atomic Routine-to-Urgent cancel-and-replace; never silently convert status, priority or price.
- **Booking and follow-up:** support authorized individual cancellation/reschedule, Open/Exact-Time scheduling and qualifying preferential Follow-up from a Completed New Examination. Preserve confirmed price/deadline evidence, at most one active preferential claim and at most one Completed use per source. Eligible same-day-cohort bulk rescheduling requires preview, exclusion reasons, typed confirmation and all-or-none commit; bulk cancellation is deferred.
- **Service dates and reconciliation:** midnight does not change status, close a Visit or migrate queues. Show overdue Booked/Waiting against their original service date. Manual No-show requires verified absence and applies only to Booked; older Waiting needs explicit handling and fresh presence reconfirmation before admission. Guided day close and automatic cutoff are optional, governed by the approved rules.
- **Service values and reporting:** EGP prices are immutable server-calculated decimal snapshots, including authorized discounts/free non-Urgent services. Completed Examination Value sums Completed service values; it does not assert collected cash. Dashboard counts, trends and throughput use the Plan's Metric Dictionary with explicit service-date versus event-date meaning.
- **Clinical files and access:** authorized report and handwritten-prescription scans are attachments; no electronic prescription authoring is included. Receptionist permission to upload a handwritten prescription does not grant preview/download or Doctor clinical-content access. Follow the Plan §2 read/write allowlists. Files remain tenant-private, encrypted, scanned and quarantined until safe; no public file URLs. Owner export covers one patient in PDF/CSV plus an attachment manifest, with attachments downloaded separately under authorization.
- **Staff communication:** authorized in-app realtime updates, including Receptionist review alerts for committed Doctor cancellations/reschedules; Doctor-only browser push requires device consent and contains at most the patient's name. Audible cues respect browser interaction requirements. Access revocation must end protected delivery.
- **Security and architecture:** default-deny, live role/account/tenant checks and strict Organization isolation apply to every operation, file, export and event. Next.js handles UI/authentication/BFF; FastAPI owns clinical business rules in the approved modular monolith. Reuse the approved frontend stack and components only as needed. Hidden controls never replace server authorization; stale/conflicting operations require authoritative refresh or preview.

Sources: PRD §§4–10; Domain §§4–10; System Design §§1–6; Plan §§2, 10–11. Preserve their detailed guards, defaults and acceptance requirements by reference.

## MVP Exclusions

No patient login/portal, patient payment collection or paid/unpaid tracking, receipts, patient WhatsApp/SMS/reminders, bulk cancellation, electronic prescription composition/signing/printing, bulk patient export, general medical-record deletion/merge, additional Doctors/branches, cross-Doctor history, multi-country/currency/timezone operation or desktop app. Patient memberships, laboratory/radiology partnerships and home emergency visits are also excluded.

Public clinic signup, SaaS subscriptions/checkout/billing, commercial plans/trials/discounts and a full SaaS Administration Dashboard are post-MVP. Minimal secure operator provisioning remains in MVP. Future capabilities require separate approved scope; they are not implementation suggestions.

Sources: PRD §§1.2–1.3, 11–12; System Design §7; Plan §1.

## Product Principles

1. Deliver the smallest practical, secure and maintainable approved MVP; avoid speculative features and infrastructure.
2. Make staff tasks and current state clear while preserving domain rules, independent patient identity and medical history.
3. Enforce tenant isolation and role/field permissions at the server; presentation never grants authority.
4. Preserve confirmed prices, entitlements and audit history; conflicts and failed operations must not leave partial changes or premature notifications.
5. Treat Arabic and English, accessibility and responsive clinic workflows as core requirements.

## Accessibility & Inclusion

Provide semantic controls, clear labels, keyboard navigation, visible focus, accessible form errors and appropriate loading, empty and error states. Support responsive desktop/tablet workflows and mobile-safe layouts. Arabic RTL and English LTR cover navigation, layouts and forms, with locale-aware Cairo date/time, number and EGP formatting. Keep machine identifiers stable and display user-authored clinical/patient/staff text and attachments as stored, without automatic translation.

Sources: PRD FR-084–089 and §8; System Design §1; Plan U-01/12. The supplied authorities do not name a specific WCAG conformance level; none is asserted here.

## Evidence on Hand

The four approved documents above establish product, domain, architecture and execution intent. They are not evidence that these capabilities are implemented, tested or production-ready. This initialization supplies no customer testimonials, adoption figures, performance benchmarks or medical outcome claims.

## Unresolved Decisions

| Decision | Unresolved scope and consequence | Source |
|---|---|---|
| OPEN-OPS-01 | Database/attachment backup schedules, retention, encryption/key custody and demonstrated restore evidence; blocks real patient-data production. | PRD §12.2; Plan §14 |
| OPEN-OPS-02 | Approved numeric RPO/RTO and demonstrated end-to-end recovery for database and files; blocks real patient-data production. | PRD §12.2; Plan §14 |
| OPEN-OPS-03 | Qualified Egypt-specific privacy/healthcare legal review, residency, retention, access, hosting/processor arrangements and release authorization; blocks real patient-data production. | PRD §12.2; Plan §14 |
| OPEN-FILES-01 | Approved file types/signatures, size/count/storage limits, scanner failure/quarantine/release policy, download lifetime and key recovery/rotation evidence; production medical uploads remain disabled. | PRD §12.2; Plan §14 |
| SECURITY-POLICY-NUMERICS | Production session expiry, login/recovery throttles, invite/reset/Doctor-confirmation expiry, signed-request freshness/skew and download-grant lifetime require Owner/Security approval and evidence before real-user/patient production. | Plan §§2, 14 |
| Production email | Suitable secure SMTP provider, delivery configuration and credentials remain unselected; development/test uses Mailpit. | System Design §5; Plan §§14–15 |
| FUTURE-SAAS-COMMERCE | Subscription/pricing/trial/payment/provider/tax/legal policies remain unchosen and outside MVP; separate approval is required before paid SaaS launch. | PRD §12.2; Plan §14 |
| Brand context | Brand voice and binding brand assets are not established by the supplied authorities or request. No visual preferences or design system are inferred. | Unconfirmed in this initialization |

SECURITY-POLICY-TIMING-VARIANCE was authorized on 21 October 2026 for separately authorized synthetic-data development/testing with explicitly labeled test-only fixtures. It supplies no production numbers or release permission. Required missing production policy must fail closed. Non-billed pilots do not bypass safety gates.

Initialization ends with this product record. Visual design, application implementation, workflow configuration and production decisions remain outside this task.
