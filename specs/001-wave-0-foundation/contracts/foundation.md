# Wave 0 boundary contracts

**Date:** 2026-10-21 | **Owners:** F-02–F-07 | **Status:** planned interfaces, not implemented endpoints

Binding upstream: [implementation plan](../../../docs/implementation-plan.md) §§2/5/11 and [specification](../spec.md). These contracts introduce no business API or new authentication protocol. JSON errors on protected HTTP boundaries use `{error:{code,message,request_id}}`; messages and opaque correlation IDs disclose no patient/tenant identity, credentials, versions, database internals or stack traces.

## 1. Network and configuration — F-02/F-03

- Browser/host → web only: development web port 3000 bound to loopback by default. No published API, worker, PostgreSQL, SMTP or scanner port; no `network_mode: host`.
- Web → API on private service network; web → PostgreSQL for Better Auth on the private database network. API/worker → PostgreSQL; mail-enabled web → Mailpit SMTP. Mount protected named database/file volumes only into required consumers. Web never gets a public file-serving mount.
- Use one private application network and one internal database network; only necessary consumers join each. Mailpit/ClamAV are opt-in `mail`/`scan` profiles. Only containers needing verified scanner signature updates get narrowly necessary outbound connectivity; no additional public service is introduced. This is container reachability separation, not a claim that Compose provides per-request identity or protects against a Docker-host administrator.
- Host denial evidence tests absent **host-published API/DB endpoints** and inspects resolved port/network configuration; it must not claim a privileged Linux host cannot reach bridge IPs. If stronger host isolation is required, escalate rather than claiming `internal: true` alone provides it.
- Secrets are file-mounted to the consuming service, never Docker build ARG/ENV, frontend `NEXT_PUBLIC_*`, committed `.env`, logged configuration or image layer. Do not print interpolated Compose config or secret files in CI. `config --quiet` is the normal validation path.
- Development/test/production are distinct configurations. Explicit synthetic fixtures may run only in labeled synthetic development/test; missing required keys/policy fail startup. No production launch or policy approval follows from this contract.

## 2. Health — F-05

| Interface | Healthy result | Unhealthy result / access |
|---|---|---|
| API `GET /health/live` | `200 {"status":"alive"}`; process only | No DB/session calls; API remains private |
| API `GET /health/ready` | `200 {"status":"ready"}` | `503 {"status":"not_ready"}` for required DB, incompatible baseline or enabled required dependency failure |
| Web `GET /health/live` | `200 {"status":"alive"}` | Process only; public diagnostic, no protected passthrough |
| Web `GET /health/ready` | `200 {"status":"ready"}` | `503 {"status":"not_ready"}` when dependencies required for the enabled foundation boundary fail |
| Worker in-container status probe | Exit 0 for requested live/ready state | Nonzero for dead/stale status or required dependency failure; no host listener |

Responses are intentionally minimal; `Cache-Control: no-store`. Health does not report version strings, exception text, tenant IDs, session state or policy values. A running process may be live and unready. Optional inactive mail/scanner does not block core foundation readiness. Mail-enabled validation requires Mailpit; scanner readiness matters only if an enabled capability actually requires it. No scanner/upload capability is enabled in Wave 0. Foundation readiness is not a claim that clinical work or login is operational. Monitor DB failure and recovery without using health to create schema.

## 3. Closed browser-to-backend boundary — F-07

- Reserve Next.js App Router `/api/backend/[...path]` as a protected browser-facing boundary. An unauthenticated request returns **401**, including a request with forged `X-CarePulse-*` headers or fabricated session cookie; no backend request or business side effect occurs.
- Configure the Better Auth server with PostgreSQL storage, `session.cookieCache.enabled = false`, no secondary session cache, `emailAndPassword.enabled = false` and `emailAndPassword.disableSignUp = true`; configure no social providers or signup plugins. Reserve framework auth handling internally; do not expose an unrestricted Better Auth catch-all handler or operational login/reset/invite routes in this Wave. Disable signup in configuration as defense in depth even though signup has no route.
- Any session candidate still cannot authorize business traffic before D-02/B-01/B-02. Missing storage/dependency yields sanitized 503 if reached; otherwise deny unsupported authenticated work with 403. Never treat an adapter exception as anonymous success, use in-memory fallback, seed a working account or offer a development login bypass.
- Strip/overwrite inbound `X-CarePulse-*` authority headers. Browser cookies are never forwarded to FastAPI. Same-origin/CSRF checks must precede any future unsafe forwarding; no mutation forwarding is enabled now. Use framework protections, not a new CSRF library or custom session engine.
- Private FastAPI `/v1/*` rejects missing/invalid service authorization with **401**. Even a correct service key alone never grants clinic identity: unsupported protected work remains denied (403), not forwarded/processed. No business routes exist.
- A private connectivity probe may prove web → API over `/health/live`; it accepts no tenant authority and is not a clinic endpoint. This avoids inventing an authenticated business command for connectivity evidence.
- Retain the approved HMAC-SHA256 envelope contract in upstream plan §§2/11 unchanged. Full signing, canonical identity, durable nonce replay rejection and live authorization are B-01/B-02 with D-09/D-02, not a partial Wave 0 trust shortcut. No positive protected passthrough until those owners provide evidence.

Externally reachable health paths are the only public diagnostics; all other unrecognized paths follow normal sanitized 404 behavior. Public registration routes are absent/404, never a registration UI or writable account-creation API. Operator credentials, CLI actions and operator routes do not exist in this Wave.

## 4. Reserved mail transport — F-07

CI activates `mail`, renders both reserved templates and sends to Mailpit using synthetic addresses and non-operational links. Inspect Mailpit through its internal API from a network-attached CI check, not a published mailbox UI. Expected: each reserved message is delivered with the correct template kind/recipient and no usable credential. Missing Mailpit fails the mail check; it never falls back to a real SMTP provider. There is no durable sender/job/retry implementation until D-09/B-03/B-04/B-25.

## 5. Migration and transaction boundary — F-04

`alembic upgrade head` is an explicit migration-role command on a pre-provisioned target. Application startup uses runtime credentials, reads but never updates migration version, and reports unready for incompatible schema. Schema creation is never an automatic side effect of readiness/auth initialization.

Runtime and metadata operator attempts to create/alter application objects must fail. A transaction using local organization context must leave no effective context in a subsequently reused connection, including after an SQL error/rollback. See [data model](../data-model.md) for ownership and lifecycle. Clinical RLS and metadata table grants remain future work.

## 6. Reproducibility and evidence — F-01/F-06

Exact approved direct pins, committed complete transitive locks and recorded interpreter/database/container inputs are installation contracts. Frontend uses `packageManager`-pinned pnpm and `pnpm install --frozen-lockfile`; backend uses the existing pip installer with a complete exact/hash-checked lock, not `pip freeze` as proof. Worker installs/imports the same API package. Do not add another package manager, dependency scanner service or generic repository layer.

CI rejects missing/unapproved/floating version inputs, incompatible peers, lock drift and image-identity mismatches. Version/image evidence contains the Git revision, platform, resolved runtime/package versions, lock hashes, exact image references and observed manifest/image digests (or equivalent immutable built artifact identity), check results and CI run URL. Image digests are observed, never invented; image existence and peer metadata alone are not integration verification. Advisory review reports transitive findings without silently replacing the approved Next.js pin.
