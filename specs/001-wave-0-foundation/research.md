# Wave 0 research and decision record

**Date:** 2026-10-21 | **Scope:** F-01–F-07 | **Status:** exact proposed pins and conditional synthetic-development recommendations approved by Product Owner on 2026-10-21; execution evidence pending

## Evidence and approval rules

The PRD → Domain Model → System Design → implementation-plan authority order and Constitution v1.1.0 remain unchanged. Next.js **15.5.28** was previously approved; the Product Owner separately approved the exact R1–R4 version/dependency/image proposals and conditional recommendations on **2026-10-21** for synthetic-data development. React 19, TypeScript/App Router, pnpm, PostgreSQL, FastAPI, SQLAlchemy 2 Core/psycopg 3/Alembic and Better Auth database sessions remain fixed technical choices, not invitations to redesign. These approvals do not authorize task execution, waive verification, or approve production.

Read-only research used official release pages, exact-version npm/PyPI metadata, upstream source/advisories, Docker registry manifests/tag metadata and Context7 official-library documentation. Parallel research covered frontend/auth and backend/images; the planner independently checked central engine/peer metadata, Python's security release notice, and all five image tags/digests. **Published/metadata-compatible is not installed, built, tested, vulnerability-cleared or integration-verified.** No package install, lock generation, migration, Docker command or deployment ran.

Initial technical unknowns were exact runtimes/DB/images, dependency locking, auth-without-schema behavior, health/network isolation, and verification venue. Decisions below resolve the design unknowns. Product Owner approval of the specified candidates is now recorded; future executable evidence and production/security release gates remain distinct. Routine development must not be blocked by production-only decisions, but affected work must fail closed if a required safe bootstrap, compatibility check or critical security boundary cannot be satisfied.

## R1. Runtime and PostgreSQL baseline — F-01 (Product Owner approved; verification pending)

**Decision (approved by Product Owner on 2026-10-21):** standard-GIL **CPython 3.13.16 + securely bootstrapped pip 26.2.2**, **Node.js 24.22.1 LTS**, **pnpm 10.34.7**, and **PostgreSQL 17.11**, consistently across the applicable workstation/CI/container processes. Compatibility/install evidence remains pending and is not implied by approval.

**Rationale:** these exact runtime/image releases are published, the Python dependency candidates support CPython 3.13, and Node 24 satisfies Next, pnpm and Better Auth's transitive engine constraints. PostgreSQL 17 is a supported mature major with the required SQL/transaction/RLS facilities; use one DB version, not a compatibility matrix. Existing workstation `node v24.18.0`, `Python 3.14.3`, `pip 26.2.1` and ignored dependency directories are observations only, not approved selections or clean installs.

**Security qualification:** Python **3.13.17** was released on October 21 with bundled pip/certifi remediation for the eMudhra/emSign CA incident. Read-only checks returned **404** for `python:3.13.17-slim-bookworm` and `3.13.17-slim-trixie`; do not invent an available image/digest or silently mismatch interpreter patches. The approved 3.13.16 baseline **requires Python's documented exact URL + SHA-256 pip 26.2.2 bootstrap before any dependency downloads**, in fresh workstation/CI environments and the container build. Ordinary unpinned `pip install --upgrade pip` is not the bootstrap. The Product Owner approved this condition on 2026-10-21; successful verification has not occurred. If secure bootstrap fails, **affected dependency installation/F-01 is BLOCKED**, not bypassed; report the issue and a compatible safe remediation for separate approval without unnecessarily stopping independent work.

Official bootstrap artifact documented by Python:

`https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70`

**Alternatives considered:** newest Python without a published image (not reproducible now); older unpatched installer (reject); custom-building Python or adding another installer (unnecessary complexity); silently using workstation Python (unapproved, inconsistent).

Sources: [Python 3.13.16](https://www.python.org/downloads/release/python-31316/), [3.13.17 security/secure pip instructions](https://www.python.org/downloads/release/python-31317/), [pip 26.2.2 metadata](https://pypi.org/pypi/pip/26.2.2/json), [Node 24.22.1 security release](https://nodejs.org/en/blog/release/v24.22.1), [pnpm metadata](https://registry.npmjs.org/pnpm/10.34.7), [PostgreSQL 17.11](https://www.postgresql.org/docs/release/17.11/).

## R2. Minimal dependency candidates — F-01/F-06/F-07 (Product Owner approved; lock evidence pending)

**Decision (approved by Product Owner on 2026-10-21):** the following exact direct inputs, including listed tooling/types, subject to the temporary synthetic-development ESLint 9 exception in R7. This is **not a resolved transitive lock**; resolution/hash generation belongs to later separately authorized F-01 work. Required transitive packages are not optional simply because absent from this compact table.

| Area | Exact candidate inputs | Compatibility evidence |
|---|---|---|
| Web | `next=15.5.28` (already approved), `react=19.3.1`, `react-dom=19.3.1` | Next Node `^18.18.0 \|\| ^19.8.0 \|\| >=20.0.0`; React/DOM `^18.2.0 \|\| 19.0.0-rc-de68d2f4-20241204 \|\| ^19.0.0`. DOM requires React `^19.3.1`. |
| Web tooling | `typescript=5.9.3`, `eslint=9.39.5`, `eslint-config-next=15.5.28`, `@eslint/eslintrc=3.3.1` | Matching Next config accepts ESLint `^7.23.0 \|\| ^8.0.0 \|\| ^9.0.0`. Next's exact source rejects TypeScript `>=7.0.0-0`; do not follow `latest`. FlatCompat is the official Next 15 template's direct import. **ESLint EOL approval gate below.** |
| Type declarations | `@types/node=24.19.2`, `@types/react=19.3.0`, `@types/react-dom=19.3.0`, `@types/pg=8.23.1` | Metadata minimum TypeScript 5.6; React DOM types require React types `^19.3.0`. |
| Auth/DB, Node | `better-auth=1.7.12`, `pg=8.23.1` | Better Auth peers Next 14/15/16, React/DOM 18/19, pg 8. pg requires Node ≥16. Transitive Kysely's candidate branches require Node ≥20 or ≥22; Node 24 satisfies both. |
| Development SMTP | `nodemailer=10.1.7` | Published metadata requires Node ≥20 and includes its own TypeScript declarations; no separate types package or custom SMTP protocol. |
| API/worker | `fastapi==0.158.1`, `uvicorn==0.54.0`, `SQLAlchemy==2.0.55`, `psycopg[binary]==3.3.6`, `alembic==1.20.0` | Non-yanked published releases with Python 3.13-compatible requirements/wheels; Alembic requires SQLAlchemy ≥2.0. psycopg-binary has CPython 3.13 Windows/Linux wheels. |
| Python checks | `pytest==9.1.1`, `ruff==0.17.1` | Published Python 3.13-compatible artifacts; one test runner and one formatter/linter. |

**Rationale:** bare FastAPI/Uvicorn avoid their large optional `standard` extras; synchronous Core uses `postgresql+psycopg` and SQLAlchemy's existing pool, not asyncpg/a second pool or a generic repository. Normal blocking FastAPI handlers use `def`. The same Python domain package is imported by API and worker. Binary psycopg avoids local compiler/libpq setup. Pydantic is a FastAPI dependency and may be used directly for configuration validation; if imported directly it must be recorded as an exact direct dependency from the approved resolved lock, not fetched independently. The same applies to any directly imported transitive package.

Better Auth's built-in PostgreSQL adapter takes a `pg.Pool`; no Prisma, Drizzle, direct Kysely package, auth CLI, plugin or separate adapter dependency is needed. Use one Nodemailer SMTP transport for the reserved web templates and a private smoke invocation; no custom SMTP protocol, extra mail template framework, separate Python sender or later delivery engine. Existing Python HTTP capabilities can inspect the internal mailbox in CI. Transport reuse across later owners does not authorize their durable job/lifecycle code now.

Do not install screen-specific shadcn/ui, Lucide, complex-form libraries, Playwright, state-management libraries or full locale navigation in the foundation. Tailwind v4/next-intl remain approved stack decisions for their actual UI owner tasks; F-01's boot-only page does not justify installing the complete later-Wave UI stack. No alternative styling/i18n technology is selected.

**Alternatives considered:** ORM repositories, async database drivers, Poetry/uv, separate worker dependency stacks, overlapping frontend test runners and eager UI scaffolding (all unnecessary for this slice).

Exact metadata sources: [Next](https://registry.npmjs.org/next/15.5.28), [React](https://registry.npmjs.org/react/19.3.1), [DOM](https://registry.npmjs.org/react-dom/19.3.1), [TypeScript](https://registry.npmjs.org/typescript/5.9.3), [ESLint](https://registry.npmjs.org/eslint/9.39.5), [config](https://registry.npmjs.org/eslint-config-next/15.5.28), [FlatCompat template](https://github.com/vercel/next.js/blob/v15.5.28/packages/create-next-app/templates/app/ts/eslint.config.mjs), [Better Auth](https://registry.npmjs.org/better-auth/1.7.12), [pg](https://registry.npmjs.org/pg/8.23.1). Type sources: [Node](https://registry.npmjs.org/@types%2fnode/24.19.2), [React](https://registry.npmjs.org/@types%2freact/19.3.0), [DOM](https://registry.npmjs.org/@types%2freact-dom/19.3.0), [pg](https://registry.npmjs.org/@types%2fpg/8.23.1).

Backend sources: [FastAPI](https://pypi.org/pypi/fastapi/0.158.1/json), [Uvicorn](https://pypi.org/pypi/uvicorn/0.54.0/json), [SQLAlchemy](https://pypi.org/pypi/SQLAlchemy/2.0.55/json), [psycopg](https://pypi.org/pypi/psycopg/3.3.6/json), [binary wheels](https://pypi.org/pypi/psycopg-binary/3.3.6/json), [Alembic](https://pypi.org/pypi/alembic/1.20.0/json), [pytest](https://pypi.org/pypi/pytest/9.1.1/json), [Ruff](https://pypi.org/pypi/ruff/0.17.1/json), [sync dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg), [FastAPI blocking handlers](https://fastapi.tiangolo.com/async/).

SMTP sources: [exact Nodemailer metadata](https://registry.npmjs.org/nodemailer/10.1.7), [SMTP documentation](https://nodemailer.com/smtp/). Nodemailer 10 ships types; do not add the older `@types/nodemailer` package.

## R3. Locks and build identities — F-01/F-06

**Decision:** retain pnpm and pip. Frontend commits `pnpm-lock.yaml`, exact direct pins and approved `packageManager: pnpm@10.34.7`; CI frozen installs plus strict peer verification reject drift. Python commits one full exact direct/transitive **SHA-256 requirements lock**, with platform markers and Windows/Linux wheel hashes, installed using `pip install --require-hashes --only-binary=:all: -r requirements.lock` after the approved secure pip bootstrap. Record first-party source revision separately; do not hash/install local API source as an external dependency or make a separate worker lock.

**Rationale:** pip hash mode requires every applicable transitive dependency pinned/hashed; direct-only pins or `pip freeze` are not reproducible artifact proof. Record approved resolution inputs and tool versions in the existing repository documentation/configuration, not a new dependency-management service. F-06 compares against that explicit approved baseline; lock regeneration requires review, not automatic acceptance. The installed pip executable is available; no change of installer is needed.

**Alternatives considered:** unbounded requirements, environment freezes as proof, floating action/tool versions, auto-updating CI and separate lock managers (reject). Exact CI action/tool references, including the Compose tool version, must be recorded/approved during F-01/F-06 implementation; none are secretly selected here and no additional build container is authorized.

Source: [pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/). Install/build proof remains pending, not inferred from this research.

## R4. Exact container candidates and observed identities — F-01/F-02

**Decision (approved by Product Owner on 2026-10-21):** the following exact tags **with their observed index digests** as pinned build/Compose inputs, conditional on verifying actual platform/image identities during implementation. Registry/tag metadata returned HTTP 200 on 2026-10-21. All have Linux amd64 support; use one Linux amd64 CI target initially. Reverify actual pulled platform identities in CI; tag existence is not a successful pull/build/start.

| Candidate image | Observed index digest | Source |
|---|---|---|
| `node:24.22.1-bookworm-slim` | `sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205` | [Docker tag](https://hub.docker.com/v2/repositories/library/node/tags/24.22.1-bookworm-slim) |
| `python:3.13.16-slim-bookworm` | `sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d` | [Docker tag](https://hub.docker.com/v2/repositories/library/python/tags/3.13.16-slim-bookworm) |
| `postgres:17.11-bookworm` | `sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27` | [Docker tag](https://hub.docker.com/v2/repositories/library/postgres/tags/17.11-bookworm) |
| `axllent/mailpit:v1.31.6` | `sha256:23fe39e81bde596a918000b3135c0f85dc8bb4c9ea6be274b051143f3e300387` | [Docker tag](https://hub.docker.com/v2/repositories/axllent/mailpit/tags/v1.31.6) |
| `clamav/clamav:1.5.4-debian13-slim` | `sha256:d7ede3cefeaf3b8a72ebcdee849d13f504bbca3f3b3cfade5bc7b6ee06a4f62c` | [Docker tag](https://hub.docker.com/v2/repositories/clamav/clamav/tags/1.5.4-debian13-slim) |

**Rationale:** exact version + immutable index digest avoids floating build inputs. Python/Node/PostgreSQL are Docker Official Images; Mailpit/ClamAV are upstream publishers. API and worker share the Python build input/package source. Debian slim plus binary wheels avoids Alpine musl/compiler complexity. ClamAV's Debian variant supports amd64/arm64, unlike the observed plain patch tag; scanner signatures are mutable security data, not permission to float the executable image. Scan is optional/inactive now and production scanning policies remain open.

**Alternatives considered:** `latest`, `stable`, major/minor tags, substituting missing Python patches, custom runtime builds, extra CI helper images (reject). Record any necessary later helper/build input under the same approval/identity rules.

## R5. Minimal closed auth adapter and development mail — F-07

**Decision:** initialize the supported Better Auth PostgreSQL adapter configuration with an explicit database, no secondary cache, `session.cookieCache.enabled = false`, `emailAndPassword.enabled = false`, and `emailAndPassword.disableSignUp = true`; no social providers/plugins. Do not mount `toNextJsHandler` as an operational public catch-all. Keep `/api/backend/*` deny-only, verify service-only API denial, and demonstrate web→API via private health rather than a fake signed business request.

**Rationale:** Better Auth supports Next App Router handlers, but D-02 owns its supported schema and B-01/B-02 with D-09 own the complete identity/replay boundary. Exact-version source shows omission of database configuration can enable stateless defaults; missing DB/schema is therefore a denial/unavailable state, never a fallback. F-07's persistent-session configuration is inspectable without claiming operational auth tables/login. No later-Wave dependency is added to F-07. Reserved invitation/reset templates and isolated SMTP transport are exercised through a private synthetic harness; durable jobs, real tokens and account workflows remain deferred.

**Alternatives considered:** generated auth schema in F-07, custom sessions, mock login, service-key-only identity passthrough and request-time durable mail workflows (reject).

Sources: [exact PostgreSQL adapter docs](https://github.com/better-auth/better-auth/blob/v1.7.12/docs/content/docs/adapters/postgresql.mdx), [Next integration source](https://github.com/better-auth/better-auth/blob/v1.7.12/packages/better-auth/src/integrations/next-js.ts), [initialization source](https://github.com/better-auth/better-auth/blob/v1.7.12/packages/better-auth/src/context/create-context.ts), [session lookup source](https://github.com/better-auth/better-auth/blob/v1.7.12/packages/better-auth/src/api/routes/session.ts).

## R6. Small operational and verification design — F-02–F-06

**Decision:** private app/database Compose networks, only loopback web host publication, file secrets, named DB/file volumes, optional mail/scan profiles. A baseline migration and restricted DB roles precede truthfully sanitized live/ready checks. Worker uses a process-owned status file/probe, not another HTTP server. One `ops/check.ps1` entrypoint compatible with existing Windows PowerShell **5.1** locally and PowerShell **7** in CI, and one CI workflow with local-capable and Compose integration jobs, are sufficient. No local `pwsh` installation is required. Use 5.1-compatible syntax/cmdlets and explicit `$LASTEXITCODE` checks after native commands, terminating nonzero on failure; do not rely on PowerShell 7-only native-error preferences or `$ErrorActionPreference` alone.

**Rationale:** `internal: true` and absent host publication enforce the intended service topology, but do not stop a privileged Docker-host administrator reaching container IPs. Prove the actual boundary, not an impossible host-admin claim. One real PostgreSQL topology supplies migration, role/context, health/network and F-07 smoke evidence. No clinical schema, simulated SQLite substitute, extra test framework, production proxy or extra infrastructure. Production code precedes focused tests; all runtime evidence is future work. Docker never runs or gets installed on this workstation.

**Alternatives considered:** local Docker installation, mandatory local PostgreSQL, mock-only isolation, multiple CI matrices, full E2E clinic journeys and premature production deployment (reject).

Sources: [Compose networks](https://docs.docker.com/compose/how-tos/networking/), [secrets](https://docs.docker.com/compose/how-tos/use-secrets/), [profiles](https://docs.docker.com/compose/how-tos/profiles/), [Windows PowerShell invocation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1), [native exit codes](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_automatic_variables#lastexitcode). See [contracts](contracts/foundation.md), [data model](data-model.md) and [validation guide](quickstart.md) rather than duplicating interface details here.

## R7. Security findings and implementation approval gates

| Gate | Evidence / disposition | Effect |
|---|---|---|
| Exact candidate approval | Product Owner approved exact R1–R4 runtimes, direct dependencies/tooling/types and five image references on 2026-10-21 for synthetic-data development | **APPROVED inputs, UNVERIFIED compatibility/install/lock/build/CI evidence.** Material substitution requires separate approval; no production approval. |
| Python secure bootstrap | Python 3.13.16 requires the exact URL/hash-verified pip 26.2.2 remediation in R1 before any dependency download; Product Owner approved this approach | **AUTHORIZED approach, UNVERIFIED execution.** Installation fails closed if hash/secure bootstrap fails; do not use the old installer unremediated. Independently ready work can proceed. |
| Unsupported ESLint | npm marks 9.39.5 unsupported; matching Next 15.5.28 config excludes ESLint 10. Product Owner approved a **temporary, synthetic-development/CI-only ESLint 9.39.5 exception on 2026-10-21**, without peer overrides or Next substitution | **NOT a development planning blocker.** Keep exact lint checks and document the risk; complete Security/Operations disposition before any real-user/production release, revisiting the exception at Wave 0 review. An actual exploitable critical issue affecting this environment must be escalated immediately, not deferred or ignored. |
| Two Next advisory checks — not blockers at the approved pin | Official GitHub reviewed advisories [GHSA-4jqv-mc3x-m676](https://github.com/advisories/GHSA-4jqv-mc3x-m676) and [GHSA-mcj8-r9mp-w47p](https://github.com/advisories/GHSA-mcj8-r9mp-w47p) identify the affected Next.js 15 range as `>=15.0.0, <15.5.27` and first patched version **15.5.27**; approved **15.5.28** is outside both affected ranges | These two advisories do not block 15.5.28. This corrects the earlier conclusion based on incomplete repository-advisory metadata; GitHub's reviewed advisory API records verify the bounded ranges. No blanket security clearance: all other unresolved security/advisory checks and the full locked transitive review remain required. |
| Full dependency/advisory resolution | Next [15.5.28 release](https://github.com/vercel/next.js/releases/tag/v15.5.28), React [RSC advisory](https://github.com/react/react/security/advisories/GHSA-xqc2-xf4c-x3h3), Better Auth [1.7.12 release](https://github.com/better-auth/better-auth/releases/tag/v1.7.12) include security fixes; React/DOM pins alone do not prove Next's bundled RSC clear | Keep F-01/F-06 frozen/hash install/build and full locked-transitive advisory review. Record findings, including actual installed `sharp` security applicability, and require remediation of an applicable exploitable critical issue before affected operation. Unresolved non-exploitable/deployment-only risk disposition may remain a **pre-deployment gate**, not a blanket block on synthetic development. No silent upgrades. |
| Production policies/release | SECURITY-POLICY-NUMERICS, OPEN-OPS-01/02/03, OPEN-FILES-01 and production mail/provider/release approvals remain OPEN | Synthetic-only timing variance is authorized; real-user/patient operation and production upload gates stay closed. |

Product Owner approval of the exact candidates and conditional synthetic-only recommendations is recorded on 2026-10-21. **No Product Owner input/version decision is pending as a blanket development blocker**; actual secure installer bootstrapping, dependency locking, compatibility, vulnerability evidence and all task-specific checks are still unexecuted, and failures must be reported truthfully. Production-only risk acceptances, numerical policies and release approvals are deferred until before deployment, never waived. Planning approval does **not** authorize implementation, and no task/Wave is reported complete.
