# Wave 1 Research

**Date**: 2026-10-23. **Scope**: the 11 master tasks in [plan.md](plan.md), refined for U1/I1/U2/U3. Initial research covered authorities, Wave 0 source/pins/tests and installed Better Auth 1.7.12. Two focused read-only research agents examined reset concurrency and end-to-end secure mail alternatives for this refinement; findings were checked against relevant installed source, packaging and configuration before consolidation. No application behavior was executed.

## 1. Authority, ambiguity and execution boundary

**Decision**: Preserve master plan §6 and its exact edges. Apply PRD → Domain → System Design → master plan precedence and Constitution v1.1.0. The current request permits planning beyond the previous specification-only stop; it does not edit the spec's historical authorization statement or authorize implementation.

**Rationale**: Dependency readiness and design completion are not task assignment. The 2026-10-21 timing variance already permits synthetic fixtures, but no production policy choice.

**Alternatives considered**: Replanning scope, reapproving the existing variance, filling numeric defaults, or treating future referenced AC/CON cases as fully delivered — rejected.

**Sources**: master §§2–4/6/12/14; constitution Authority/Approval Boundaries; spec Authority and Scope/Assumptions.

## 2. Existing architecture and pins

**Decision**: Keep SQLAlchemy Core synchronous READ COMMITTED transactions, PostgreSQL, Next/Better Auth BFF and Python domain/worker ownership. Reuse locks, secrets loader, private network, Nodemailer/Mailpit and CI. Preserve all existing versions/locks.

**Rationale**: Wave 0 already supplies these foundations. `apps/api/carepulse/db.py` sets transaction-local tenant context; `apps/web/auth.ts` is intentionally deny-only with schema preflight deferred. There are no operational auth or clinical tables yet. Both health implementations currently require `0001_foundation`, so readiness and baseline assertions must evolve with migrations.

**Alternatives considered**: ORM replacement, custom session engine, Redis, second mail channel, public backend, dependency upgrade or rewriting baseline migration — unnecessary and contrary to authorities.

**Sources**: master §2; `package.json`, `apps/web/package.json`, `requirements.in`, locks; `db/roles.sql`; `ops/check.ps1`.

## 3. Owner and tenant isolation

**Decision**: Partial unique enabled Owner plus deferred cross-table verified-Owner check, with Organization lifecycle locking on tenant and Owner mutations. Clinical FK pairs include tenant. FORCE RLS and non-owner NOBYPASSRLS apply to runtime; operator receives only metadata operations. D-01 keeps activation closed until the D-02 guard exists.

**Rationale**: A screen check or isolated unique index cannot prevent ownerless ACTIVE tenants or concurrent last-Owner removal/activation. Migration/superuser capabilities are not constrained by a claim that ordinary RLS protects them.

**Alternatives considered**: Owner count checked only in BFF; nullable clinical tenant columns; operator BYPASSRLS — rejected.

**Sources**: master §§2/6 D-01/D-02; Domain INV-01/19/20/28.

## 4. Supported Better Auth persistence and recovery

**Decision**: Derive auth tables with installed supported migration tooling and apply reviewed SQL through Alembic/migration credentials. Keep PostgreSQL sessions, no secondary cache, signup disabled and schema validation restored. Use supported auth/plugin schema extensions for nonclinical security metadata. Do not copy library session/credential logic.

**Rationale**: Installed `better-auth/dist/utils/password.mjs` wraps proof consumption, session deletion and credential write in `runWithTransaction`; the direct pg Pool adapter supports a real transaction. Enable `revokeSessionsOnPasswordReset`. `onPasswordReset` runs afterward and cannot establish atomic app revision/audit by itself. Recovery issuance currently creates proof before invoking its email callback, so a callback that merely queues mail is not sufficient atomic proof+job issuance.

**Selected integration**: A narrow supported auth extension wraps issuance in adapter transaction context and writes proof plus mapped D-09 delivery intent in that same context; the sender never runs there. Reset uses supported recovery plus an application block/revision operation and durable auth completion evidence, as detailed in [security-lifecycle.md](contracts/security-lifecycle.md). Use public exported transaction/plugin APIs, not imports from `dist` internals; installed source is evidence, not an integration API. If the required public adapter transaction/schema mapping cannot be proven in the pinned version, stop the affected task rather than ship an approximation.

**Digest storage**: Explicit hashed verification storage is necessary; installed default is plaintext. Installed reset identifiers use `reset-password:`; do not assume a documentation example's `password-reset` override matches. Check persisted proof rows directly.

**Alternatives considered**: Assume a Python transaction includes a Better Auth HTTP call; rely on after-hooks; delete sessions eventually and report successful reset; upgrade solely to obtain transactions — rejected. Existing supported transactional capability is sufficient to design against; real rollback/race evidence is still mandatory.

**Sources**: installed `apps/web/node_modules/better-auth` package: `dist/api/routes/password.mjs`, `dist/utils/password.mjs`, `dist/db/internal-adapter.mjs`, `dist/db/verification-token-storage.mjs`; installed `@better-auth/core` transaction context and `@better-auth/kysely-adapter`. Current [Context7 Better Auth transaction documentation](https://github.com/better-auth/better-auth/blob/main/packages/core/src/context/transaction.ts) corroborates nested transaction-context reuse; current docs are not a substitute for installed-version proof.

### Concurrency-safe recovery operation (U1)

**Decision**: Bind one durable operation to each proof digest and allow only one unresolved operation per native auth identity. Matching proof+exact-password retries reuse that operation and its single revision bump; changed passwords conflict via a purpose-keyed request fingerprint, never a stored password/unkeyed hash. The auth transaction takes a guarded operation-row write fence before native consumption, records completion atomically with credential/session effects, and never acquires domain locks afterward. Domain revocations invalidate that same gate; finalization compares owned block, exact evidence and captured revisions under the existing lock order. The [normative protocol and failure matrix](contracts/security-lifecycle.md#5-recovery-issuance-and-durable-reset-coordination) define retries, two proofs, expiry, crashes and stale finalizers.

**Rationale**: An arbitrary new operation UUID on each request lets competitors replace each other's blocks. A transaction alone does not define reservation identity or finalization idempotence. Supported PostgreSQL atomic consumption is necessary, but application fencing also prevents stale reset/finalization after revocation. A keyed fingerprint distinguishes competing desired credentials without creating a database-only password-guessing oracle. Completion evidence permits a lost-response retry after consumption/expiry without repeating credential effects.

**Alternatives considered**: In-memory mutexes, proof-only reservation that treats two different passwords as identical, client-chosen request IDs, unkeyed password hashes, lease-based takeover of uncertain auth work, callback-only success audit or blanket unblocking after timeout — rejected. A real auth transaction rolls back its fence and effects on failure; explicit reconciliation fences abandonment without opening an access gap.

**Installed-source checks**: `resetCredentialPassword` encloses consume/delete-sessions/update-password in `runWithTransaction`; the PostgreSQL adapter's `consumeOne` uses atomic deletion/returning and supports a guarded real-write fence through mapped models. Nested supported adapter contexts reuse the active transaction. Native reset checks `expiresAt < now`, so the wrapper must enforce strict `now < expires_at`, including a conservative pre-completion recheck. An after-reset callback alone supplies none of the application reservation/finalization guarantees. Public mapping/transaction support and cross-process rollback/fence behavior remain required implementation evidence, not assumed runtime success.

## 5. Session lifetime and revision binding

**Decision**: Better Auth remains session authority. Supported server-controlled additional session fields retain immutable absolute expiry, last authenticated activity and the security revision captured before credential verification. Both BFF and API enforce current staff/clinic authority; deadline equality is expired. Activity cannot extend absolute expiry, recreate a deleted session or upgrade its revision.

**Rationale**: Native `expiresIn`/`updateAge` provide sliding expiry, not two independent idle/absolute limits. `freshAge` is not absolute lifetime, and periodic `updatedAt` is not exact last activity. A login that verifies an old password before reset must not create a usable session with the new revision afterward. Raw direct `auth.api` calls also do not automatically execute HTTP rate limiting/Origin checks.

**Alternatives considered**: Map policy names to nonexistent native `idle_timeout` options; use sliding `expiresIn` as absolute; trust a cookie cache; stamp the latest revision after login finishes — rejected.

**Sources**: installed `dist/api/routes/session.mjs`, `sign-in.mjs`, internal adapter; master §§2/11.2; spec SEC-03/RACE-04. Public wrapper routes must explicitly enforce Origin/CSRF, configured throttle and sanitized DTOs, including removal of library-returned bearer tokens.

## 6. Replay versus idempotency

**Decision**: Commit an authenticated transport nonce in a short protected transaction before domain work; reuse is denied even when domain work fails. Store key ID and authenticated time and retain until the entire configured acceptance window has elapsed. Recheck live authority in the domain transaction before any receipt lookup. D-09 owns one tenant/actor/operation/key receipt store; receipt and successful business result commit together.

**Rationale**: Rolling back the nonce with domain failure would reauthorize replay. Committing a receipt independently would return nonexistent business success. A transport retry must use a fresh signature/nonce even when its business key stays the same.

**Alternatives considered**: In-memory nonce cache, a second D-05 receipt table, or cached-result lookup before revocation check — rejected.

**Sources**: master §§2/11.1; spec RACE-08/09.

## 7. Committed mail and delivery secrets

**Decision (U3)**: Keep one D-09 nonclinical queue and one Python-owned post-commit claim/send path using the existing Node/Nodemailer transport. Encrypt **only the small proof token** with RSA-OAEP/SHA-256, with context bound through the OAEP label; omit the AES content key/wrapping envelope. Python issuers use reviewed, exactly pinned PyCA `cryptography`; Node recovery issuance/decryption uses native `node:crypto`. A single bounded Node child remains at delivery, not at every Python issuance. Issuers get public material only; the trusted sender invocation/container (including its Python launcher) alone receives the private key. Digest verification remains separate. Full encoding and boundaries are in [security lifecycle §6](contracts/security-lifecycle.md#6-shared-post-commit-nonclinical-sender).

**Rationale**: A digest cannot reconstruct a retryable link, and plaintext JSON/DB backups would defeat digest-only verification. Public-only encryption preserves issuer/sender key separation. A small token fits direct OAEP (RSA-3072/SHA-256 maximum 318 bytes); the current native reset token and a 32-random-byte base64url invitation fit without hybrid machinery. The one new direct Python dependency removes issuance subprocess framing/timeouts/errors and Node packaging from API/operator images. Existing pins are not upgraded; exact compatible pin, binary-wheel availability, hash-locked closure and advisory review are required during implementation, with no lock change in this design pass. No hand-written RSA is acceptable.

**Complete alternatives considered** (including packaging and custody, not just cipher choice):

| Option | Security / total operational cost | Disposition |
| --- | --- | --- |
| Existing proposed AES-GCM content + RSA-OAEP wrapping, Node codec called by Python | Preserves separation, but adds content key, nonce, tag, wrapping/format validation and issuance subprocess handling; Node is needed in every Python issuer image, including API, not only worker/operator. | Reject unnecessary envelope for bounded tokens. |
| Direct RSA-OAEP, Node-only crypto via Python subprocess | Removes hybrid envelope and avoids a dependency, but keeps issuance child-process protocol/failure handling and Node in all issuer images. | Secure runner-up; greater packaging/process surface than a reviewed Python crypto dependency. |
| **Direct RSA-OAEP in Python + native Node** | One new reviewed Python dependency and small cross-language format proof; no issuance subprocess or Node issuer runtime. Retains only the Node mail child already justified by transport reuse. | **Selected least-complex complete solution.** |
| Python/Node shared symmetric AEAD | Short crypto code, but issuers gain decryption capability; asymmetric wrapping to restore separation recreates the hybrid design. | Reject weaker key separation/unneeded wrapping. |
| All-Python encryption + SMTP | Removes Node from worker but replaces/duplicates approved F-07 Nodemailer transport and its existing mail checks. | Reject parallel/replacement sender. |
| Plaintext job + ACL/encrypted DB volume, or digest-only job | Plaintext leaks usable proofs to database readers/backups; digest-only cannot deliver the link later. | Reject lost security/delivery guarantee. |
| Deterministic proof regeneration | Adds derivation-secret binding/rotation/revocation rules, retains secret custody and changes proof generation without eliminating delivery work. | Reject unnecessary security protocol. |
| Request-time SMTP / send before commit | Cannot atomically commit SMTP and DB; permits missing mail or delivery for rolled-back proof and bypasses the required sender. | Reject. |
| Database encryption/decryption | Extension/format work; private-key DB custody violates sender-only trust boundary. Public-key DB encryption still needs interoperability and Node delivery. | Reject broader DB trust/extension surface. |
| New Node mail/crypto listener or move issuance business rules into BFF | Avoids local subprocesses by adding a service or duplicating FastAPI-owned rules. | Reject architecture change. |

**Key/format guarantee**: OAEP is randomized, context-bound public-key encryption, not AEAD or proof of issuer identity; the proposed public-key hybrid envelope also cannot authenticate an issuer. Committed job/proof binding, restricted database writes and authoritative digest consumption establish issuer authority. A dedicated delivery key pair is separate from auth/signing/clinical-file keys. No medical-file AES-GCM contract changes. Oversize tokens, malformed/wrong-context ciphertext or missing key fail closed; never fall back to plaintext or invent a second format. Private keys are secret mounts scoped to the sender execution; a same-UID Python parent cannot honestly be excluded from that trust boundary.

**Packaging/handoff**: Keep Python API/operator images Node-free. Package the existing exact Node runtime, narrow mail modules and locked Nodemailer closure only with the existing worker/sender target, not the full Next app. Extract mail-only configuration from today's `loadWebConfig()` dependency so the sender does not require web auth/service secrets. The one-shot worker invocation needs explicit private SMTP connectivity: today worker is database-network-only and Mailpit is application-network-only. Give sender execution narrowly scoped nonclinical DB credentials and its private key; issuer/operator/web executions never mount that key. Authorized CLI/CI orchestration invokes it separately after commit, without a Docker socket in application containers. No new listener, daemon or service; B-25 later owns lifetime and scheduling of this exact path.

**Cost/evidence limit**: Direct RSA removes symmetric envelope fields and issuance child processes; it does not remove Python/Node interoperability or sender packaging. No image-size/latency improvement is claimed without measurement. SMTP still has an accepted-message/acknowledgment crash window, so retries can duplicate messages but not account/credential effects.

**Sources**: master D-09/B-03/B-04/B-25 and §12; spec RACE-08; existing Dockerfiles, `requirements.in`/lock, `mail/{transport,templates}.ts`, `config.ts` and `compose.yaml`; current [Node 24 crypto](https://nodejs.org/docs/latest-v24.x/api/crypto.html) and [PyCA RSA/OAEP](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/rsa/) docs retrieved through Context7. Both APIs support SHA-256 and the same OAEP label; Node's SHA-1 default must not be used. The former task-file envelope/codec wording was a superseded design candidate; the completed U1/I1/U2/U3 reconciliation aligns `tasks.md` with the direct-token contract.

### Abandoned claims and stale owners (U2)

**Decision**: Persist an expiring lease and strictly increasing claim generation on the existing job. The same explicit one-shot invocation claims due work or reclaims expired ownership under `FOR UPDATE SKIP LOCKED`; all completion/retry/material updates compare state, owner, generation and still-valid lease using database time. Use a bounded attempt deadline shorter than the configured lease, without a renewal loop. Details and failure outcomes are normative in [security lifecycle §6](contracts/security-lifecycle.md#6-shared-post-commit-nonclinical-sender).

**Rationale**: A claim flag without expiry strands jobs after process death. Ownership alone without a generation/expiry check lets a resumed sender overwrite a replacement. READ COMMITTED rechecks a conditional update against the row version left by a competing updater. Database fencing cannot retract an SMTP message, so duplicates after lease loss or acknowledgment failure remain possible without duplicate logical effects.

**Alternatives considered**: Permanent claims, unconditional completion, process-ID ownership, a separate sweeper/periodic scheduler, long transactions spanning SMTP and a heartbeat service — rejected. Recovery is demand-driven; without another explicit invocation an abandoned job waits. B-25 alone owns future scheduling.

**Sources**: PostgreSQL 17 [queue locking](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE) and [READ COMMITTED conditional-update semantics](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-READ-COMMITTED), retrieved through Context7; master D-09/B-03/B-04/B-25; spec RACE-08.

## 8. Patient interpretation and policy/Appointment substrate

**Decision**: Follow spec's explicit source interpretation: optional phone per PRD §2; if provided preserve raw and normalize when possible (Egypt default, international supported). Require DOB or declared age+reference date. Keep shared phone/name nonunique. Archive blocks new IDs, not pre-existing-ID continuation. D-04 keeps immutable versions with independent current/effective revisions; D-05 implements six-state vocabulary, counted uniqueness and historical numbering/snapshot guards only.

**Rationale**: B-05's lower-priority “optional DOB” shorthand cannot waive age evidence. The broad operator paragraph cannot remove the runtime clinical grants required by explicit RLS design. A status CHECK does not implement a clinical state machine. Defaults invent neither price nor working calendar/hours. Final-net EGP rounds once with ROUND_HALF_UP; preserve creation-time standard base for Urgent comparison.

**Alternatives considered**: Treat phone as identity, fabricate DOB, expose all history under Patient, infer midnight cutoff, use a counter per lane, or add booking APIs to demonstrate schema — rejected.

**Sources**: PRD §§2/5.1/7; master §§2/6/11.1; Domain INV-02/03/09/18/24/26/31/33/38.

### Independent B-05 delivery (I1)

**Decision**: Separate private backend delivery/evidence (D-03+B-01 only) from deferred browser forwarding (B-02 available) and real invited-session integration (B-03 available). Keep all master dependencies unchanged. The browser names in the HTTP contract describe the eventual surface, not prerequisites for patient backend acceptance.

**Rationale**: Requiring B-02's proxy in the same backend completion check creates a hidden edge even if a dependency table says otherwise. Legitimately persisted supported auth/session fixtures can exercise the actual B-01 security boundary without implementing a browser login bypass. Independent backend acceptance does not claim browser integration or waive final Wave evidence.

**Alternatives considered**: Add B-02 to B-05's master dependencies, duplicate its proxy in B-05, ship unsigned fixture routes, or permanently omit real-session checks — rejected. The reconciled `tasks.md` now separates B-05's independently testable backend from deferred guarded browser/invited-session integration, without changing its master dependencies.

**Sources**: master §6 B-05 and §12 dependency rules; spec dependency table and Wave exit; existing task wording around B-05's private/browser surfaces.

### Provisioning and policy-order consistency (I1 / pre-tenant U1 / defaults U2)

**Decision**: Authorized SUSPENDED → ACTIVE applies only to formerly ACTIVE clinics with a currently valid verified enabled Owner; a suspended, never-activated bootstrap clinic cannot be activated by reactivation or invitation acceptance. D-09's existing receipt store distinguishes pre-tenant operator clinic-create receipts from tenant/staff receipts by checked scope and a durable unique operator/operation/key identity. B-03 revalidates operator authority before receipt lookup and atomically commits the safe original clinic result with invitation/job/audit, so concurrent and lost-response retries cannot mint a second clinic. Same key with different normalized input conflicts. Operators cannot read tenant-scoped clinical receipts.

**D-04 ordering**: D-04 installs versioned policy storage, backfills any existing organizations and installs an AFTER INSERT Organization trigger in one migration transaction. The trigger uses a narrowly scoped non-login SECURITY DEFINER role with only initial-policy insert privileges and corresponding FORCE RLS policy, fixed safe search path and no PUBLIC execute; it derives the clinic only from the inserted row. No direct operator policy grants or BYPASSRLS. Unique initial-version constraints and insert-if-missing behavior make backfill and future creation idempotent, preserve customized/older versions and avoid adding D-04 to B-03's master dependencies. Test both migration/creation orders and rollback. These are technical delivery mechanisms, not new product policies.

**Alternatives considered**: Tenant-only lookup for an as-yet unallocated clinic, a second operator receipt table, non-atomic post-create result recording, B-03 calling policy seeding directly, a new inter-master edge or an unrestricted privileged trigger — rejected.

## 9. Evidence and unresolved decisions

**Decision**: Documentation checks now; implementation owners later run focused unit/static and CI real-DB/Compose/auth/E2E evidence. Existing `ops/check.ps1 integration` names explicit files, so adding tests without wiring them is insufficient. Reviewed manifest/workflow fingerprints must evolve together with genuine changes.

**Rationale**: Local Docker is prohibited and unavailable. No runtime result follows from research, source presence or a delegated assertion.

**Alternatives considered**: SQLite/mock race proof, loosening integrity hashes, silently skipping absent CI suites — rejected.

**Clarification closure**: No remaining synthetic product choice is filled by assumption. Selected technical designs have explicit failure gates; executable validation belongs to their existing task owners. Production decisions cannot be resolved by research: SECURITY-POLICY-NUMERICS (Product Owner + Security/Operations), OPEN-OPS-01/02/03 and OPEN-FILES-01 retain the owners and blocking consequences in master §14 and [plan.md](plan.md). No numeric policies, provider, legal conclusions or release approval selected.
