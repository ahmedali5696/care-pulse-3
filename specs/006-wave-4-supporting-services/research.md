# Research: Wave 4 Supporting Services

**Date**: 2026-11-28 | **Baseline**: `f52738dc0f4a33a1c8798df704134f690b9463c2` | **Status**: Technical planning decisions; no runtime acceptance.

The [specification](spec.md) and approved authorities settle product scope. Research was split between file/export and delivery/worker/reporting investigations, then reconciled with the Constitution and permanent CI rules. All technical unknowns raised during planning are resolved below. No new Product Owner decision is required to write this design. Evidence and production gates stay open as recorded in [plan.md](plan.md#open-issues-and-execution-gates).

## Existing Substrate and Missing Work

CodeGraph was consulted before code discovery. References below identify the inspected current substrate; new Wave 4 names in the other artifacts are proposed implementation contracts.

| Source | Observation and consequence |
| --- | --- |
| `apps/api/carepulse/identity/authorization.py:136` | `protected_transaction` rechecks staff/session/tenant under the F-01 bound transaction. Reuse it for new staff operations. |
| `apps/api/carepulse/identity/runtime_identity.py:33` | Binding is one-use and tied to physical transaction/stage. A long stream cannot reuse one PRIMARY proof for repeated transactions. |
| `apps/api/carepulse/identity/contracts.py:152`; `identity/receipts.py:248` | Reuse normalized command digests and visibility/projection-checked immutable receipt replay. New URL targets and artifact selectors must be bound. |
| `apps/api/carepulse/notifications/evidence.py:45,81` | Existing sensitive audit and transactional outbox; use original committed events rather than repeating transitions. |
| `apps/api/carepulse/notifications/tables.py:94` | Outbox has UUID/time/payload, but no safe commit-ordered publication cursor or recipient notifications. Add these under B-23. |
| `db/migrations/versions/0021_d07_visits.py:55` | Visit uniqueness covers tenant/id and tenant/appointment, not tenant/patient/id. Add the triple unique key and attachment FK in a successor. |
| `apps/api/carepulse/files/__init__.py` | File package is a stub; D-10/B-21/B-22 schema and behavior are planned work. |
| `apps/api/carepulse/config.py:36,85,335` | Existing file policy and grant lifetime configuration; production fails closed. Validate actual AES key bytes, not just secret-text length. |
| `compose.yaml:63,119` | API/worker mount protected `private_files`; Web has no such mount. Do not give Web retained clinical-storage access for request spooling. |
| `apps/web/auth/proxy.ts:79`; `identity/boundary.py:215` | Generic proxy is bounded JSON with a fetch timeout; body replay reports disconnect after its body. Dedicated binary/SSE adapters are necessary. |
| `apps/api/carepulse/notifications/email_sender.py:157` | Shared one-shot sender already claims, sends and reconciles finish; its compatibility uncertainty guard is conditional. B-25 schedules this path. |
| `apps/worker/main.py:17` | Current process is an idle readiness loop; scheduling is B-25 work, not already implemented. |
| `db/migrations/versions/0072_b20_worker.py:74`; `appointments/cutoff.py:25,60` | Existing actual-worker-login cutoff capabilities and transition/DST rules. Reuse them. |
| `apps/api/carepulse/reporting/tables.py:19` | Existing reporting package supplies day reviews; metric routes/queries are new B-26 work. |

## 1. Schema and Identity Boundary

**Decision**: Forward-only migrations after `0072_b20_worker`, tenant-qualified relationships, FORCE RLS and narrow table/column/function grants. Register every new route/purpose/target/stage consistently in BFF, API and F-01 database mappings. B-23 worker delivery gets a real-login operation capability and owns its nonclinical-ID-only tenant enumeration capability; B-25 consumes it without duplication, preserving B-20 cutoff capabilities.

**Rationale**: `SET LOCAL app.organization_id` scopes data but does not authenticate an actor or authorize an operation. Add `(organization_id, patient_id, id)` uniqueness to Visits and the matching attachment FK to enforce same-patient binding even when IDs are otherwise valid.

**Alternatives considered**: Shared runtime grants, arbitrary tenant GUCs and route-only checks violate the existing boundary. Editing published Visit/F-01 migrations would destroy historical evidence.

## 2. Private File Pipeline and External Atomicity

**Decision**: Reuse protected volume, configured type/class/size/count/storage policy, ClamAV `INSTREAM`, and AES-256-GCM from existing cryptography. Stage bytes privately; inspect extension/declared MIME/detected type, scan, encrypt and authenticate metadata outside critical transactions. Finalize only with fresh authority and matching immutable digest/generation; expose only committed CLEAN objects. Never release quarantine without a separately authorized policy/workflow.

**Rationale**: Database rollback cannot undo a filesystem write. State/generation fencing plus an immutable encrypted candidate object makes failure leave an unreadable orphan rather than a successful clinical object. Cleaner first claims only unreferenced non-clean generations; it cannot delete a committed object. A definitive scanner clean response is required; timeout/error/unavailable is not clean. See official [ClamD protocol](https://docs.clamav.net/manual/Usage/ClamdProtocol.html).

**Alternatives considered**: Public object links, plaintext finalized files, scan-after-disclosure, daemon-visible user paths, holding database locks during scan or treating an error as clean all break approved protection. No new scanner service is needed beyond existing Compose ClamAV.

## 3. Binary BFF and One-use Retrieval

**Decision**: A dedicated authenticated binary adapter privately spools the policy-bounded complete file, then forwards contiguous raw chunks of at most the existing 1,048,576-byte request-body bound. Each chunk gets fresh signed canonical target/query/raw-body digest and independent API plus PostgreSQL exact-byte verification. API independently verifies whole assembled length/digest against BFF-observed totals before scanning/finalization. Keep JSON and database proof bounds unchanged; file policy governs total size/type/count/storage. Web uses protected ephemeral storage, without mounting retained objects. See the [complete protocol](contracts/http.md#dedicated-binary-adapter). Each chunk has independently signed stage, publish and status phases with actual raw-byte proof on stage, immutable fence on publication and read-only recovery; both BFF/API validate compatible server-only CP_POLICY_FILE before consuming upload resources.

Retrieval grants store a bearer digest only and bind actor/session/security revision, tenant, patient, resource and artifact. Transmit bearer in a protected same-origin request header, never URLs/audit/logs/immutable receipts. Fully verify GCM authentication before any plaintext. Immediately before serving, fresh live authorization and atomic grant consumption/audit establish one authorized redemption. No external file/network work occurs while holding clinical locks. Interrupted transfer burns that grant; another grant requires fresh authorization.

**Rationale**: Binding byte digest prevents metadata-only signing from accepting different bytes; exact target/artifact and CAS consumption prevent cross-resource/replay races. Revocation ordered before redemption denies. Already transmitted bytes cannot be recalled.

**Alternatives considered**: Public/presigned storage URLs, reusable links, bearer tokens retained in receipt projections, or streaming unauthenticated GCM plaintext would weaken required controls.

## 4. Full Export, Snapshot and Handoff

**Decision**: One ExportRecord with separate encrypted RECORD (PDF or CSV) and MANIFEST artifacts. Each gets an independent artifact-bound one-use grant through the same export download route. Both share patient, purpose, intended recipient, snapshot and audit identity. Full clinical extraction uses one composed READ COMMITTED SQL statement, not ordinary UI DTOs or multiple reads labeled with one timestamp. Generation/render/encryption runs outside database locks; final commit reauthorizes and records artifacts/audit/receipt. Creation verification is preparatory only; require positive matching recipient identity/authority confirmation by actual acting staff at handoff before issuing each disclosure grant, and recheck its applicability at redemption. Actual external handoff requires staff verification then; server redemption proves only exporter disclosure. OPEN-OPS-03 remains open for production procedures.

**Rationale**: Every active authorized staff variant can read the full downloaded record, including archived history, while original-file and ordinary clinical UI/edit permissions remain separate. A record download must not consume manifest access. No raw attachment files are embedded. Immutable minimal verification evidence records method/outcome and verifier; no retained ID document or invented consent form is required.

**Alternatives considered**: Redacted Receptionist export, Owner-only export, multi-patient export and automatically including originals contradict the approved amendment. A ZIP bundle adds unnecessary format complexity. Multiple READ COMMITTED queries do not establish one snapshot.

## 5. PDF and CSV Generation

**Decision**: Plan ReportLab with its Arabic bidi/shaping support and a bundled licensed Arabic TrueType font; use server-owned layouts and escaped untrusted text. Pin validated exact packages/fonts during implementation. Accept only after visual/content proof for Arabic, English, mixed digits, long clinical narrative and page breaks. CSV uses a documented fixed schema/encoding, correct quoting and formula neutralization for untrusted text, including control/whitespace prefixes before formula markers; retain typed dates/decimal amounts faithfully.

**Rationale**: No existing renderer was found. The official [ReportLab Arabic guide](https://docs.reportlab.com/rl-arabic/) describes shaping/bidi support and also calls it experimental, so successful rendering is an explicit B-22 acceptance gate, not a claim made here.

**Alternatives considered**: Browser screenshots/PDF require another runtime path; untrusted HTML rendering expands resource-fetch risk. WeasyPrint's [documented CSS limitations](https://doc.courtbouillon.org/weasyprint/stable/api_reference.html#css-level-2-revision-1) include RTL/bidi, making it a weaker default. No lockfile change or claim about a future package release is made.

## 6. Notification Publication and Cursor

**Decision**: B-23 owns a reusable bounded per-tenant handler with claim owner/generation/lease, safe operational projection, recipient/type uniqueness and atomic notification/delivery result. Allocate increasing publication position while holding the tenant stream-state row lock until publication commit. Cursor is opaque and bound to tenant/recipient/stream context; gaps return an authorized snapshot plus watermark from one statement snapshot.

**Rationale**: UUID/time and sequence allocation alone do not establish commit order. Publication order need not pretend to be original domain commit order. Source identity remains auditable. Map B-17 `DOCTOR_CANCELLED_APPOINTMENT`, B-18 `DOCTOR_RESCHEDULED_APPOINTMENT` and Doctor-origin B-16 `VISIT_EXAMINATION_CANCELLED`; ordinary companion events must not duplicate the review notification or impersonate Doctor origin.

**Alternatives considered**: Global clinical polling, transport-time business mutation, UUID/time cursors and naive `nextval` replay can leak data, repeat effects or skip late commits. No Redis/broker is authorized.

## 7. SSE Through Current F-01

**Decision**: Public BFF `GET /events/stream` uses dedicated SSE framing and fresh signed bounded private reads, each with a new nonce and physical transaction binding. Subscribe, configured periodic checks (maximum recheck interval including idle), each read and event emission recheck live authority. Require explicit valid poll/batch/recheck bounds; missing/invalid policy or failed authorization check refuses or closes the stream and drops buffered protected data. Release database transactions before waiting/writing network output; abort on disconnect/revocation. Snapshot recovery uses `GET /events/snapshot` and current role projection. Both adapters consume compatible validated server-only CP_POLICY_FILE before opening/polling; absence, invalidity or mismatch fails closed.

**Rationale**: Current F-01 is request/transaction-bound and the generic proxy is JSON-oriented. A BFF stream adapter preserves the existing proof boundary without a connection-long database transaction or identity bypass.

**Alternatives considered**: Reusing one PRIMARY binding, holding authorization locks for the stream, cached role claims and changing generic proxy timeout alone are incorrect. Native API SSE would require additional boundary lifecycle changes without a demonstrated need.

## 8. Doctor Device Push

**Decision**: Approved native Web Push/VAPID through planned pywebpush under B-24, pinned during implementation. Its [official project documentation](https://github.com/web-push-libs/pywebpush) documents Python payload encryption and VAPID sending, fitting the existing Python worker without another service. Store Doctor/device/session-bound consent, separate explicit Doctor name-inclusion consent (default off), and protected endpoint/key material. Fresh send-time access and both consent checks are mandatory; logout/reset/disable/suspension/revocation suppress subsequent sends. Only committed Doctor attendance alerts are push-eligible; generic payload has no patient identifier, name requires current explicit device name consent. Reject Receptionist/operator subscriptions. Sound is role-safe metadata for the Doctor attendance, Receptionist admission and Doctor-origin Receptionist review alerts only, for user-interaction-gated Wave 5 playback. See HTTP routing table. Final authorization/handoff is serialized against withdrawal, and transport begins immediately outside domain locks; prior withdrawal denies or removes name, prior handoff retains honest uncertain outcome and cannot recall transmission. See transactions.md.

**Rationale**: Per-device explicit consent is independent of in-app reception. Protected endpoints must never become an arbitrary outbound URL facility: validate Web Push subscription shape and restrict transport destinations against loopback/private/internal targets and redirect bypass. Actual network send is outside clinical locks with fenced attempts; no transport exactly-once claim.

**Alternatives considered**: Tenant-wide silent subscription, Receptionist push, PHI-rich payloads and another messaging service exceed authority. Do not add patient outbound messaging.

## 9. Worker, Cutoff and Mail Recovery

**Decision**: B-25 extends existing worker lifetime/scheduling, invoking B-23 handler, B-20 selected-day cutoff and B-03/B-04 shared `send_one_job`. Keep metadata sender identity separate from clinical worker authority. Require explicit valid retry/lease/disposal policies; missing policy disables affected claims/retries/cleanup. Stale generations cannot finish/retry/clean. Unknown database commit is read back under authorized matching identity before mutation; ordinary unknown SMTP acknowledgment requires the durable T046 handoff fence, not blind retry. Definitive in-app publication failure at the retry bound records terminal failure for authorized operational diagnostics without manufacturing recipient notifications or retrying automatically. Cleanup requires non-clean fenced generation, no active owner or committed reference and resolved commit outcome. No new retention period or production numerical value.

**Rationale**: Email sender already commits claim before network, checks material and reads back finish. B-20 already owns disabled/default cutoff, due/day/policy checks and Cairo DST resolution (later repeated instant; first valid instant after a gap). Midnight/restart is not implicit activation. Request-layer throttling/recovery stays synchronous at its authoritative boundary.

**Alternatives considered**: A second sender, generic retry of uncertain SMTP, GUC-only worker authority and duplicated cutoff eligibility/transition create inconsistent or unsafe effects.

## 10. Metrics and Consistency

**Decision**: Direct tenant-scoped SQL aggregates and an explicit versioned Metric Dictionary. One composed statement/CTEs produces a response's values, bucket boundaries and `as_of`. Sources are current appointments, retained number assignments, frozen price snapshots, unique cancellation action history/audit and Visit admission IDs/times. Use Cairo service/event dates exactly as plan §11.3 defines; EGP is a decimal string. Calendar months run first local day inclusive to first local day of the next month exclusive; the explicit PO-approved CHK019 decision (2026-11-28) defines Saturday-through-Friday weeks as Cairo-local `[Saturday, next Saturday)`. Compare service dates directly and map event instants to Africa/Cairo local dates; equivalent instant boundaries must preserve local-date membership across DST, without fixed 168-hour weeks or locale defaults. PO-approved own-tenant completed-value aggregate read includes all three staff variants, with no pricing mutation or settings elevation.

**Rationale**: F-01 activation requires READ COMMITTED (`0041_f01_runtime_identity.py:488`), so do not silently switch to REPEATABLE READ. Count retained replacement originals correctly and avoid multiplication by joined history/outbox rows. No cache/warehouse/metric ledger is needed.

**Alternatives considered**: Multiple independently snapshotted reads, UTC-day grouping, payment interpretations, notification counts as cancellation activity and UI-inferred metric definitions fail required semantics.

## 11. Integrated Evidence and Production Boundaries

**Decision**: Use all six permanent scalability rules, map owner evidence to precise requirement/actors/boundary/conditions/effects, and add only distinct integration gaps/interactions. Preserve current mandatory inventory, five lanes, cleanup and same-attempt exact-SHA Full acceptance. D-12 retains representative populated-upgrade proof and Issue #13 review; immutable Historical/bounded Compatibility evidence stays separately scoped. Record actual per-Master timing/cost from required runs.

**Rationale**: Mapping a historical pass does not replace mandatory current execution. Missing independent review or production policy cannot become PASS. Current planning needs document validation, not an application/Compose run.

**Alternatives considered**: Duplicating every owner test, dropping mandatory identities under evidence reuse, adding lanes automatically, inventing duration gates or treating Fast Feedback/diagnostic success as acceptance violates current rules.

## Resolution

### Review Finding 1 — Effective F-01 Lifecycle Verification

Static source verification at `f52738dc0f4a33a1c8798df704134f690b9463c2` covered all 32 migrations from `0041` through head `0072`, including replacement definitions, chained wrappers and textual rewrites. A source-only reconstruction applied the `0049` bind replacement and every subsequent bind rewrite (`0051/0055/0057/0059/0060/0061/0063/0066/0071`), verifying unique replacement markers and preserved gate/login/PID/xid, expiry, first-stage and conditional recovery guards. This is source evidence, not a live PostgreSQL `pg_get_functiondef` observation or runtime acceptance.

- `0041_f01_runtime_identity.py:412,473,636` defines bind/activate/context. `0049_b12_checkin_provenance.py:259–320` replaces bind: gate-only, locked request, unexpired/monotonic clock, correct API login/database/PID/in-progress xid; first binding PRIMARY except explicit invitation preparation. Binding is independently committed by `identity/runtime_identity.py:33–80`, so business rollback does not free PRIMARY.
- `0049` permits only the proven invitation continuation, allowlisted aborted PRIMARY + nonempty idempotency key → RECEIPT_RECOVERY, or committed/activated PRIMARY + cleanup evidence → SESSION_CLEANUP. Recovery eligibility is subsequently extended by `0051:242` (intake), `0055:192` (B-14), `0057:152` (admission), `0059:186` (queue policy/placement), `0060:231` (Visit), `0061:206` (cancel/replace), `0063:167` (reschedule), `0066:177` (bulk) and `0071:179` (reconciliation/day review). These insert operation/digest guards into recovery eligibility; none allows repeated PRIMARY or arbitrary file/export/download continuation. `0044/0045` cleanup evidence and `0046` audit/context changes do not add such permission; `0072` adds separate actual-worker cutoff capability, not staff continuation.
- Registration at `0049:226` sets expiry from issued time plus configured freshness. `0041:488–517,601–658` requires READ COMMITTED and live keys/tenant/staff/session; activation horizon is capped by proof/session limits and context checks wall-clock expiry on use. A delayed scan/render/decrypt cannot keep old authority alive.
- The chosen correction uses [fresh per-phase requests](contracts/transactions.md#multi-transaction-f-01-authorization-lifecycle), not new `f01_bind` stages. Persisted operation/generation/phase fences and newly authorized status readback resolve crashes and unknown commits. Every new route/purpose/body scope remains future forward-only implementation, without broader recovery/identity grants or duration changes.

### Review Finding 2 — Effective Binary and Proof Limits

`identity/boundary.py:50,88–114,274–285` bounds actual received body at 1 MiB, computes its digest and passes the actual bytes to PostgreSQL. `apps/web/auth/proxy.ts:8` retains the same existing bound. PostgreSQL `0049_b12_checkin_provenance.py:30–48` retains 1,048,576-byte body and 65,536-byte envelope bounds and verifies `sha256(body)` against the signed envelope. Bounded envelope fields and canonical route/target verification also remain required.

Successor registration wrappers call their predecessor with the same raw `payload,key,mac,body`: `0051` intake → `0055` B-14 → `0057` admission → `0059` queue → `0060` Visit → `0061` cancel/replace → `0063` reschedule → `0066` bulk → `0071` reconciliation. The renamed base is `f01_register_proof_before_intake`. Reviewed targeted rewrites `0052` (Routine JSON parsing), `0056` (B-14 target qualification), `0060` (base clinical-route compatibility) and `0062` (typed B-17 body) do not remove its size/hash checks. The remaining successors through `0072` do not supersede those checks. Thus a single larger signed upload would still fail even if Web spooled it successfully.

The corrected design keeps per-request limits and independent actual-byte hashing at both layers, using [bounded chunks plus independent assembly verification](contracts/http.md#dedicated-binary-adapter). The 1 MiB transport bound is not a production file limit. Cumulative policy/quota enforcement and no-read staging prevent chunking from weakening file controls. No limit, wrapper, proof function or runtime code was changed during this correction.

No unresolved technical clarification remains in this design. The planned renderer/protocol-client compatibility checks, new F-01 registrations, actual migration IDs and focused runtime proofs are implementation work. Production approvals and independent Issue dispositions are explicit execution/release gates in the plan, not missing decisions silently assigned by this research.
