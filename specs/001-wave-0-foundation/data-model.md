# Wave 0 foundation data model

**Date:** 2026-10-21 | **Scope:** F-01–F-07 only | **Status:** design, not applied schema

Authorities: [specification](spec.md), [implementation plan](../../docs/implementation-plan.md) §§2/5/11, Constitution v1.1.0. FastAPI owns domain logic; worker imports the same Python package. No clinical entities, tenant tables, authentication tables, jobs, nonces or audit/outbox tables are introduced in this Wave.

## Persistent baseline — F-04

### Alembic version record

| Field | Meaning / validation |
|---|---|
| `version_num` | Alembic-managed revision identifier, primary key; use Alembic's supported version-table representation, not an application migration-history model. |

One linear migration head establishes the empty baseline. The migration role owns DDL and the version table. Runtime can read the installed revision for readiness but cannot alter it. Apply schema/version changes in a PostgreSQL transaction; failure leaves neither a partial baseline nor an advanced version. Deployment does not automatically run migrations. Reset means destroying/recreating **only an explicitly disposable CI database**, then upgrading from empty; it is not a production downgrade or shared-volume deletion. Subsequent Waves append migrations; never rewrite an applied baseline.

### Database principals (roles, not application tables)

| Principal | Allowed | Denied |
|---|---|---|
| Migration | Own approved application schema and migration DDL; controlled invocation only | Credential distribution to normal web/API/worker processes |
| Runtime | Connect; schema USAGE; baseline-version read; future explicit task-owned grants | Superuser, database/schema ownership, BYPASSRLS, schema creation, role creation, DDL, migration credentials |
| Operator metadata | Connect and only explicitly granted nonclinical metadata access when those tables exist | Clinical grants, DDL, runtime-role inheritance, operator credentials in app images/processes |

Use `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOBYPASSRLS` and no privileged membership for application/operator roles. Revoke permissive PUBLIC schema/database creation grants as required by the selected PostgreSQL baseline. No blanket grants on future tables; grants belong to each schema owner task. There are no metadata tables to grant to the operator in Wave 0. Better Auth uses restricted runtime credentials, not migration or operator credentials; supported auth-schema privileges are assigned by D-02.

## Transient records (no new tables)

### Request transaction context — F-04

- Organization context is an optional validated server-derived identifier, not a browser authority field. Wave 0 creates no tenant and accepts no clinical operation.
- Each operation obtains a connection, begins one synchronous SQLAlchemy Core/psycopg transaction, and sets any permitted context transaction-locally (parameterized `set_config('app.organization_id', value, true)`, equivalent to `SET LOCAL`). Never interpolate untrusted identifiers into SQL or use session-wide `SET`.
- Success commits; exceptions/cancellation roll back; release always returns a clean connection. Without supplied authorized context, no previous request context may survive. Worker uses the same transaction utility, not a second pool/context abstraction.
- State: **checked out → transaction begun → local context set (if applicable) → commit OR rollback → returned clean**. No connection with an active failed transaction is reused.
- Focused real-PostgreSQL checks force reuse of the same pooled connection after commit, rollback and a database error; verify the next transaction starts with no effective organization context. Empty string and absent setting both confer no tenant authority. This is cleanup evidence, not D-01/D-12 clinical RLS proof.

### Validated environment configuration — F-03

Server-only fields: active environment, synthetic-test marker, role-scoped database connection inputs, internal API URL, service authorization key, Better Auth secret, file encryption key, required security-policy values, and enabled mail/scanner dependencies. Keep key material in ignored local files/CI secret files mounted only into consumers; `.env.example` contains names and non-secret placeholders only.

Validate the **full active-environment policy** from plan §2: idle/absolute expiry, login/recovery throttles, invitation/reset/Doctor-confirmation expiry, signed-request freshness/skew, download-grant lifetime and required file policy. Missing/invalid required inputs reject protected process startup without serializing values. Production never accepts synthetic markers/fixtures or framework fallback policy. Production file capabilities also remain gated by OPEN-FILES-01. No numeric production defaults are selected here.

### Worker status — F-05

An atomically replaced process-owned status file contains only process-alive/dependency-readiness state, process identity (PID plus process-start identity, not PID alone) and monotonic heartbeat time; it is not a job queue or history store. An in-container probe accepts only a matching live process with heartbeat age between zero and the fixed maximum age inclusive; missing, stale, future-dated, mismatched or dead-process state fails. Restart invalidates the previous process identity; a fresh matching heartbeat restores status. No worker HTTP server, scheduler, job polling, tenant enumeration or delivery engine in Wave 0. DB/migration failures or dependency probe timeout make readiness false without changing fresh process liveness; optional disabled scanner/mail does not.

All API/web/worker readiness dependency probes enforce finite execution deadlines, including connection/pool waits and queries, and report unready on timeout. During F-05, choose and record fixed positive probe-deadline, heartbeat-interval and maximum-age constants in the existing health/status code; maximum age must exceed the heartbeat interval plus the worst-case bounded dependency-probe cycle. Keep Compose probe timeouts longer than the bounded probe cycle. No new settings interface, infrastructure, product SLA or security-policy numeric is introduced. Use controlled monotonic time in focused tests where practical, with exact freshness boundaries and timeout/restart/recovery cases; real-service CI evidence remains mandatory.

### Reserved development email — F-07

Template kind `invitation` or `reset`, synthetic recipient, subject, and rendered text/HTML with a visibly non-operational test link. Transport sends only to isolated Mailpit in the test profile. No persisted token, invitation, delivery job, account or recovery state is created. A non-public smoke harness exercises the shared template/transport directly; no send-email HTTP route is exposed. Mailbox contents are synthetic; no real address or secret token in CI evidence.

## Explicit future ownership

| Later owner | Deferred persistent data / behavior |
|---|---|
| D-01 | Organizations, metadata/operator identities, tenant isolation schema and RLS |
| D-02 | Better Auth supported user/account/session/verification schema; Staff, invitations and revocation metadata |
| D-09 | Signed-request nonces, audit, outbox, idempotency receipts and nonclinical delivery jobs |
| B-01/B-02 | Full HMAC/replay verification, live tenant/staff authorization and operational session boundary |
| B-03/B-04/B-25 | Provisioning/recovery lifecycle, durable post-commit sending and scheduled retries |

F-07 configures PostgreSQL-backed Better Auth sessions with cookie caching disabled, but does not auto-create the D-02 tables or claim working login/session persistence. Missing schema or later authorization infrastructure never results in passthrough or an in-memory session fallback. All business requests remain denied.
