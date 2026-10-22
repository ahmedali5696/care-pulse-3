# CarePulse — System Design v1.0

**Version:** v1.0  
**Approval status:** APPROVED — Product Owner confirmation.  
**Approving authority / approval date:** Product Owner / 2026-10-21.  
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
- Idle and absolute session expiration, rate limiting, and secure account recovery are required. Specific numerical limits and implementation details will be defined in the Implementation Plan.

## 3. Database & Concurrency

- A single **PostgreSQL database**, using tenant-scoped tables containing `organization_id`.
- Row-Level Security (RLS), tenant-qualified foreign keys, and database constraints enforce data isolation and integrity.
- Database transactions, row-level locks, unique constraints, idempotency, and revision checks protect critical operations.
- The MAIN and EMERGENCY queues maintain independent ordering while sharing doctor capacity, pause state, and confirmation rules.
- Bulk rescheduling is atomic (**All-or-Nothing**). Booking numbers are unique and never reused within each clinic/day.
- EGP monetary values are stored using `NUMERIC`, with immutable price snapshots.
- Append-only audit records capture all sensitive events and clinical modifications.

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
- PDF/CSV exports are restricted to a single patient's data for the clinic owner and include an attachment manifest.

## 6. Operations & Quality

- A secured and audited **Internal CLI** handles clinic provisioning, initial Doctor/Owner invitations, clinic suspension, and reactivation.
- Database migrations, independently encrypted backups of database records and attachments, and restoration tests are required.
- Integration tests cover concurrency, RLS, state machines, session revocation, and file security.
- Development and production environments have separate configurations and secrets. Logging must not expose clinical or other sensitive patient information.
- **Workstation limitation (2026-10-21):** Docker is unavailable locally and cannot be installed; agents MUST NOT attempt local Docker installation or require local Compose execution. Run available container-free checks locally and prove Compose, PostgreSQL integration/concurrency, network isolation, and container builds in CI. Unexecuted checks remain unverified, and production gates remain unchanged.

## 7. Scope & Release Gates

- Full compliance with **PRD v1** and **Domain Model v1.2**, without modifying business rules or introducing additional features.
- **Excluded from MVP:** SaaS billing, public signup, a full administrative dashboard, patient payments, bulk cancellation, and e-prescription generation.
- Before production deployment, **OPEN-OPS-01/02/03** and **OPEN-FILES-01** must be resolved, together with a documented Egyptian legal and regulatory review.
- Approval of this System Design does not constitute approval of implementation or production readiness.
