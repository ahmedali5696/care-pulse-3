# CarePulse — System Design v1.0

**Version:** v1.0  
**Approval status:** APPROVED — Product Owner confirmation.  
**Approving authority / approval date:** Product Owner / 2026-10-21.  
**Approved product-alignment amendment:** 2026-11-03 — the Follow-up policy/role authorization boundary below reflects amended PRD and Domain Model; this does not authorize code, migration, deployment or release.
**Approved product reconciliation:** 2026-11-04 — latest PO decisions in Issues #24/#25, reflected in the higher-priority authorities, supersede older conflicting permissions. Current authorization covers four authoritative documents only, not downstream artifacts, code, migration, merge or release.
**Higher-priority authorities (in order):** `CarePulse_PRD_v1_APPROVED.md` (approved PRD v1); `CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md` (approved Domain Model v1.2).

## 1. Architecture & Stack

- **Architecture:** Modular Monolith, with clearly separated modules aligned with Domain Model v1.2.
- **Frontend:** Next.js `15.5.28` (exact pinned version), React 19, TypeScript, and App Router.
- **Styling & UI:** Tailwind CSS v4, shadcn/ui, and Lucide React icons. Install only the components required by the MVP.
- **Internationalization:** next-intl, supporting Arabic RTL and English LTR, with locale-aware formatting and direction.
- **Forms:** Native React/HTML forms by default; React Hook Form + Zod for complex forms when needed. Backend validation remains authoritative.
- **UI Standards:** Responsive desktop/tablet layouts, accessible components, consistent design tokens, and appropriate loading, empty, and error states.
- **Backend:** Python + FastAPI, serving as the sole owner of business logic and clinical operations.
- **BFF:** Next.js handles authentication and forwards requests to FastAPI over a secured internal network.
- **Deployment:** Docker Compose for the approved containerized development/test topology and later self-hosted production deployment with an HTTPS reverse proxy. Docker/Compose runs in a Docker-capable CI environment for development verification, not on the Product Owner's workstation.
- **Dependencies:** pnpm with a committed lockfile and reproducible installations. Use verified compatible versions and review security advisories before deployment. No additional state-management or UI frameworks unless required.

## 2. Authentication & Authorization

- **Better Auth + PostgreSQL Sessions**, without Session Cookie Cache.
- Public registration is disabled; accounts are created exclusively through authorized invitations.
- The BFF validates sessions for every protected request and securely forwards trusted identity information to FastAPI.
- FastAPI independently revalidates tenant context, roles, permissions, account status, and clinic status, following a **Default Deny** policy.
- Access is revoked immediately when an account is disabled or permissions are withdrawn. Sessions are also revoked upon password reset.
- The single Doctor remains Owner/default Administrator and controls activation, initial Receptionist creation, grant/revoke of explicit operational administration delegation and ultimate clinic ownership. A Manager is an existing Receptionist with specifically approved Owner-granted settings/actions, not a new role/Owner or Doctor identity. Ordinary Receptionists gain no delegated settings/staff administration. Exact settings/subsequent staff scope remains PRD §12.3 / Domain §12.3; deny unapproved scope. Future subscription ownership/renewal stays with Owner; billing is outside MVP.
- All active authorized staff, including ordinary Receptionists, may perform the five approved operations: justified within-lane non-head admission, Follow-up decisions/confirmed extensions subject to Doctor precedence, archive/unarchive, full single-patient export and eligible atomic same-day bulk reschedule. These require neither Manager designation nor a separate `bulk_reschedule` grant. Live operation/field/tenant checks still apply.
- Idle and absolute session expiration, rate limiting, and secure account recovery are required. Specific numerical limits and implementation details will be defined in the Implementation Plan.

## 3. Database & Concurrency

- A single **PostgreSQL database**, using tenant-scoped tables containing `organization_id`.
- Row-Level Security (RLS), tenant-qualified foreign keys, and database constraints enforce data isolation and integrity.
- Database transactions, row-level locks, unique constraints, idempotency, and revision checks protect critical operations.
- The MAIN and EMERGENCY queues maintain independent ordering while sharing doctor capacity, pause state, and confirmation rules.
- Non-head admission by any staff permitted under current admission mode requires explicit justification and immutable actual-actor/role audit; selected lane/day, Waiting/presence, revisions, Doctor Pause, shared capacity and Doctor-issued per-case extra-slot confirmations remain mandatory. No exceptional Routine-cursor advancement, cross-lane reorder or forged confirmation.
- Doctor completion or authorized Receptionist administrative attestation may set In Examination → Completed only after actual examination. Retain Visit and actual actor/role/time/tenant immutable evidence; diagnosis/treatment/notes and later clinical corrections remain Doctor-only. No seventh Appointment status; exceptional In Examination cancellation, Pause and case-confirmation issue/revoke remain Doctor-only. Archive/unarchive never deletes history or implicitly cancels valid existing appointments; serialize against new-ID booking.
- Bulk rescheduling is atomic (**All-or-Nothing**). Booking numbers are unique and never reused within each clinic/day.
- EGP monetary values are stored using `NUMERIC`, with immutable price snapshots.
- Append-only audit records capture all sensitive events and clinical modifications.
- **Follow-up authorization boundary (2026-11-04):** Current Owner-configured policy governs fresh claims (initially seven inclusive Cairo-local days, completion day zero), including after release and absent an explicit Doctor decision; `DEFAULT` follows policy unless constrained by a distinct Doctor restriction. All authorized staff may record source decisions and authorize distinct justified audited confirmed-claim extensions with actual actor/role, never forged Doctor clinical revisions. Prior explicit Doctor `OPTED_OUT`/`DEADLINE` cannot be replaced, modified, overridden, bypassed or extended beyond through any Receptionist source decision, date exception or extension; only genuinely authenticated Doctor revision with immutable provenance/concurrency checks may change it. A **NEW booking date exception** is separate: Doctor reason required, independent Receptionist reason optional, immutable audit always mandatory. Preserve single-use, completion-date lower bound, slot/day/capacity guards, frozen confirmation price/entitlement and original evidence with append-only extension authorization before reschedule. Release/rebook uses current policy/price, not old extension. Serialize policy/source/claim changes, live authorization, revisions, audit and idempotency in backend transactions. No general Receptionist clinical narrative/audit browsing or editing is granted; the full medical export exception below is distinct. Minimum source/extension/attestation evidence and DTO/storage contracts remain PRD/Domain §12.3 blockers, not implemented assumptions. Published D-07/D-08 migrations (0021/0022) are immutable; changes require reviewed forward-only migrations.

Source eligibility and frozen claims remain distinct: an explicit justified Doctor DEADLINE defines the source window instead of the clinic default, shorter or longer, subject to the completion-date lower bound. A confirmed-claim extension is non-shortening append-only authorization, not a source/date-exception rewrite of frozen evidence or price (PRD BR-002a–c).

## 4. Realtime & Background Processing

- **Server-Sent Events (SSE):** Real-time updates for doctors and receptionists, with automatic reconnection and snapshot recovery.
- **Native Web Push / VAPID:** Notifications are delivered only to authorized doctor devices, with restricted content and revocable consent.
- **Python Worker + PostgreSQL:** Background jobs, transactional outbox processing, and retries, without Redis.
- **No-show:** Automatic conversion is disabled by default. The scheduler applies the configured cutoff only when explicitly enabled, using the `Africa/Cairo` time zone.

## 5. Storage & Email

- **Private Encrypted File Storage** on a protected volume.
- Files undergo MIME type validation, size validation, malware scanning, and quarantine checks before becoming accessible.
- File downloads require authorization; public file URLs are prohibited.
- A free SMTP relay, where a suitable free plan is available, handles staff invitations and account recovery. Mailpit is used for development/test email verification in CI (no local Docker required).
- All active authorized clinic staff may export a **FULL single-patient medical record** in readable PDF/structured CSV including permitted clinical history, plus a separate attachment manifest. Exporter necessarily can read the downloaded record; output is not a redacted Receptionist projection. This grants neither unrestricted in-app clinical browsing/editing nor original attachment downloads; originals retain independent class/access checks. Bulk export and operator clinical export remain denied.
- Export requires specified legitimate purpose, recipient identity/authority verification at handoff and immutable sensitive-operation audit of actual actor/role, tenant/patient/time/purpose/recipient verification/action, without needless PHI in logs. Server-created outputs are encrypted/private; escape CSV formulas; protected bounded single-use retrieval rechecks live staff/tenant/resource authorization and revocation. No public URLs or invented lifetime/identity-document retention/consent format. Full export disclosure/legal basis and handoff process require OPEN-OPS-03 Egypt-specific privacy/legal signoff before production; Owner permission is not consent or proof of HIPAA/Egyptian compliance.

## 6. Operations & Quality

- A secured and audited **Internal CLI** handles clinic provisioning, initial Doctor/Owner invitations, clinic suspension, and reactivation.
- Database migrations, independently encrypted backups of database records and attachments, and restoration tests are required.
- Integration tests cover concurrency, RLS, six-state transitions, actual-actor administrative completion, all-staff non-head/archive/bulk/export success, all Receptionist paths against explicit Doctor restrictions, genuine Doctor revision, policy/release/rebook/extension races, immutable evidence, session revocation at retrieval, purpose/handoff denial and file security. Historical CI does not prove the amended contract; downstream Wave 2/3/4/5 alignment and independent review remain required under Issues #24/#25.
- Development and production environments have separate configurations and secrets. Logging must not expose clinical or other sensitive patient information.
- **Workstation limitation (2026-10-21):** Docker is unavailable locally and cannot be installed; agents MUST NOT attempt local Docker installation or require local Compose execution. Run available container-free checks locally and prove Compose, PostgreSQL integration/concurrency, network isolation, and container builds in CI. Unexecuted checks remain unverified, and production gates remain unchanged.

## 7. Scope & Release Gates

- Conformance to **PRD v1** and **Domain Model v1.2**, including explicitly PO-approved 2026-11-03/04 amendments; no unapproved business-rule changes or additional features. This is a product/architecture requirement, not a legal compliance certification.
- **Excluded from MVP:** SaaS billing, public signup, a full administrative dashboard, patient payments, bulk cancellation, and e-prescription generation.
- Before production deployment, **OPEN-OPS-01/02/03** and **OPEN-FILES-01** must be resolved, together with a documented Egyptian legal and regulatory review.
- Approval of this System Design does not constitute approval of implementation or production readiness.
