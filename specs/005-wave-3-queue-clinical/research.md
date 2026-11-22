# Wave 3 Research

Phase 0, 2026-11-22. Source baseline `79d6a2775d8b20a27b8df3e6ca4a64415c8eb8ec`. Proposed engineering decisions within the [spec](spec.md), master plan §§2/8/11.4/12 and approved authorities. Source research is not executable acceptance; settled product policy is not reopened.

## Decisions

### Reuse F-01, extending only the implemented operation

- **Decision:** Keep existing signed BFF/API transport, independent database proof, actual login/backend/transaction binding, live authorization and protected nonce lifecycle. Add exact method/path/purpose, signed resource extraction, permitted stage/recovery, RLS/column/function grants, receipt namespace and audit/outbox scope for each owning operation through reviewed forward-only evolution.
- **Rationale:** `apps/web/auth/proxy.ts:39–73`, API `main.py:91–174`, `identity/boundary.py:228–290`, `identity/authorization.py:136–178` and `identity/runtime_identity.py:33–80` already implement the boundary. `0043_f01_writer_boundary.py:373–386` fences unsupported queue/terminal/day-review writers. [F-01](../../docs/f01-runtime-identity.md) and master §8 require operation-specific current-head proof; a valid proof does not validate arbitrary business arguments.
- **Alternatives considered:** Another identity layer, broad tenant activation, direct browser/private-API access or restored shared-runtime/GUC authority would duplicate or weaken established contracts. Do not weaken 0046 resource/audit isolation.

### Reuse domain substrate and transaction-taking primitives

- **Decision:** Extend existing Queue/Visit/DayReview metadata and use patient lifecycle, booking date/slot/number/price, policy heads, B-10 qualification/claim commands, tenant receipts and audit/outbox helpers. Individual and bulk changes call the same domain primitives in one caller transaction.
- **Rationale:** `queue/tables.py:20–119`, `visits/tables.py:20–128`, migrations 0025/0027 and `notifications/evidence.py:13–87` supply substrate. They do not establish protected product routes. B-12 precedes B-11; admission alone creates Visit. B-16 owns completion and In Examination exceptional cancel.
- **Alternatives considered:** Parallel intake booking logic, an entitlement engine, generic repository or new queue/worker service increases scope without a need. No dependency or infrastructure added.

### Preserve B-10 while adding Doctor clinical PATCH

- **Decision:** One strict Visit PATCH dispatcher selects exactly one closed source-decision or Doctor clinical payload family; BFF, API and database scope must agree. Preserve existing legacy/v2 B-10 schemas, operation/receipt family and live field-safe replay. Deny mixed/duplicate/unknown fields before execution; never widen VISIT_FOLLOWUP authority to clinical content.
- **Rationale:** `main.py:112–124`, `visits/routes.py:94–114` and the BFF Visit catch-all currently bind PATCH to VISIT_FOLLOWUP only. Two independent handlers cannot select themselves by body. Operational source-decision revision is distinct from clinical revision.
- **Alternatives considered:** A broad permissive Visit DTO silently grants clinical scope; a second conflicting PATCH handler cannot dispatch safely. A separate unapproved clinical route is unnecessary when the established PATCH can strictly dispatch.

### Persist bulk preview through D-09 receipts

- **Decision:** Store an immutable same-actor/tenant bulk preview in the existing receipt JSONB result, with server receipt UUID as preview_id, original day, exact eligible selection/destination, exclusions and all compared revisions. Add a narrow preview-ID read capability only for the matching bulk-commit scope, plus preview receipt-ID recovery. Commit freshly validates selection, eligibility and every relevant revision. No new preview table or invented TTL.
- **Rationale:** `0004_evidence.py:222–275` supplies immutable receipt rows; `identity/receipts.py:41–46,86–128` returns generated receipt ID. Existing lookup `:154–185` and 0042 RLS permit only current operation/key. Cross-operation preview access is a required scoped extension, not an existing capability. A single result_revision cannot establish all record freshness.
- **Alternatives considered:** Browser-supplied eligibility, a second preview database/table, or treating the preview as an authorization grant adds risk or unnecessary machinery. Generic receipt access must remain closed.

### Preserve ordered transactions, receipts and honest recovery

- **Decision:** Retain master §2 lock classes and the detailed applicable [Wave 2 transaction order](../003-wave-2-booking-scheduling/contracts/transactions.md), including policy-head ordering and existing-Appointment-before-source rules. Deterministically lock multi-record/date work, fresh-read after waiting, compare revisions inside commit. Domain result, history/audit/outbox/receipt commit together. Every retry has fresh signed/live authorization and nonce; unknown outcome is reconciled under the original key.
- **Rationale:** Capacity, lane head, number, slot and source/claim writers must compose with actual triggers. Real PostgreSQL lock observations must prove critical interleavings. Nonce consumption may be separately durable even when the business transaction rolls back.
- **Alternatives considered:** Separate replacement/bulk commits, unguarded preview-write, arbitrary sleep/retry, new-key replay after lost response or stale receipt disclosure violates integrity or confidentiality.

### Configuration and later delivery remain bounded

- **Decision:** Confirmation TTL is configured, persisted on issue and checked at consumption; absent/invalid policy denies issuing/using confirmations. Only labeled synthetic fixture values are chosen during authorized testing. Commit minimal operational events now; B-23/B-24 deliver/filter realtime/push and B-25 owns scheduling/retries.
- **Rationale:** Spec clarification and master §2 already defer production numerical approval. Ordinary one-capacity admission needs no extra-case confirmation. B-16 owns Doctor In Examination cancellation review events, B-17 Doctor Booked/Waiting cancellation, B-18 Doctor individual reschedule. Source reasons/clinical narrative are excluded from operational delivery.
- **Alternatives considered:** Fixed TTL, new settings control, production defaults, automatic midnight carry, network calls during a transaction or implementing a scheduler now would invent policy or transfer ownership.

### Bind receipt identity to the complete command target

- **Decision:** New command digests include normalized operation/resource identifiers from path/query, including target/day/preview identity, plus canonical typed body. Verify the saved result's target relationship before replay success. Preserve existing B-10 digest compatibility.
- **Rationale:** A body-only hash permits same actor/key/body reuse across visible URL targets. B-10 already hashes Visit ID plus normalized body (`visits/eligibility.py:504–508`). Transport raw-body signature and canonical command digest have different jobs.
- **Alternatives considered:** Body-only digests or checking merely that the original result is visible cannot distinguish two authorized resources.

### Preserve DayReview management authority

- **Decision:** Authorized staff may create initial review/close. Any existing-review revision update, including amendment/reopen or later close, remains Owner-only. Original same-key recovery reads the committed result rather than making another amendment.
- **Rationale:** PRD BR-049 separates management amendment from initial review; BR-036 does not delegate it. `0027_d11_day_review.py:440–449` already restricts existing-review updates to DOCTOR_OWNER.
- **Alternatives considered:** Treating amendment as a universal staff action broadens established permissions without authority.

## Upstream prerequisite register

All consumed current-head acceptance below remains **UNVERIFIED by this planning run**; source presence and historical evidence are identified separately. This is not a declaration that upstream work is unimplemented. Wave 2 T128–T139 are individually checked and its quickstart records synthetic acceptance, despite older pending wording. Preserve those records. Engineering supplies exact-revision evidence; Security/Clinical independently review applicable boundaries; PO retains acceptance/disposition. Existing [#24](https://github.com/ahmedali5696/care-pulse-3/issues/24)/[#25](https://github.com/ahmedali5696/care-pulse-3/issues/25) track these obligations; no duplicate Issue is needed.

| Consumed contract / observed limit | Existing upstream owner | Affected consumer / trigger | Closure check before affected implementation |
| --- | --- | --- | --- |
| Live actual actor/tenant/security revision, fixed Manager allowlist, revocation and operational projections. Source exists; new-operation evidence does not follow from historical CI. | D-02/B-01/B-04, Wave 1; F-01 boundary owner | Each new operation; B-13/B-14 delegated settings specifically | Exact-revision signed/real-login success plus wrong actor/operation/resource/tenant/field denial, grant/revoke ordering, Manager capacity/account denial |
| Archive command still Doctor-only in `patients/service.py:182–183` and `0042_f01_effective_grants.py:242–244`, contrary to amended BR-038. `patients/lifecycle.py:35–48` already separates new-ID rejection from existing-ID continuation. | B-05, Wave 1; F-03 permission alignment | B-11/B-17 new IDs; B-12/B-15/B-18/B-19 existing IDs | Upstream owner supplies all-staff archive/revocation/booking-race proof; consumers prove correct lifecycle behavior. Archive-command mismatch alone does not block all consumers |
| Current policy/price/calendar publication, slots, counted patient/day uniqueness, frozen money and day numbering. Existing upstream implementation/evidence is not re-run here. | D-04/D-05/B-06–B-09, Waves 1–2 | B-11/B-17 creation, B-18/B-19 destination validation; applicable settings consumers | Current-boundary consumed-helper evidence, publication versus creation/move, immutable prices and unique never-reused committed assignments |
| Actual-actor source DEFAULT/OPTED_OUT/DEADLINE, reason/date, Receptionist-origin revision and genuine Doctor precedence. Implemented in `visits/eligibility.py:497–574`; historical B-10 acceptance in Wave 2 quickstart. | D-07/D-08/B-10, Wave 2 | B-16 source outcomes; B-18/B-19 authority; other preferential paths as applicable | Current signed actor success/denial, immutable source/clinical revision split, Doctor-revision/claim races and fail-closed unknown legacy provenance |
| Separate reasoned dated non-shortening confirmed extension and frozen evidence/price. `eligibility.py:577–715` implements ACTIVE claim/revision/Doctor guards and ordered writer. | D-08/B-10, Wave 2 | B-18/B-19 moves; B-16/B-17/B-20 terminal claim interactions | Independently verify current extension provenance, reason/date/stale/Doctor denial, extension versus restriction/release/move, no inherited extension after rebook |
| Positive completion provenance and tenant/Visit/Appointment/actor/assertion/history/audit links exist in `visits/tables.py:89–128`; terminal protected proof includes historical 0040 evidence. | D-07/D-08 substrate, Wave 2; B-10 source consumer | B-16 consumes storage and implements its new completion command | Verify consumed schema/provenance compatibility; then B-16 proves its own F-01 completion/cancel/claim behavior. Do not transfer the new workflow upstream |
| Queue/control/confirmation/DayReview persistence exists; F-01 intentionally denies unsupported product writers. | D-06/D-11, Wave 2 | B-12/B-14/B-15/B-20 and dependent consumers | Review schema/grants/trigger compatibility and preserved rows. New operational paths and their current-head positive/negative/race proof remain each Wave 3 owner's work |
| Compatible runtime/schema and authentic receipt replay source baseline | F-01 / PRE-W3-COMPAT engineering and independent reviewers | Changed schema/grants/receipt or runtime enablement | Reuse recorded master §12 synthetic baseline; prove affected immediate-predecessor→new-head compatibility at final executable SHA; preserve #28 disposition ownership |

Master §8 applies **before affected implementation**: when the exact consumed upstream contract lacks independently verified executable evidence, report **that affected Master BLOCKED for implementation** and name it. F-01's per-operation gate separately applies **before enablement**. An absent Wave 3 command is its owner's work, not an upstream defect. Neither gate blocks design, adds dependencies, reopens accepted tasks or supplies execution permission.

## Evidence and production decisions

- Master §12 records PR #29 merged at `4dbf08fbd84a6c328c07be174d223c71f930aa2f`, full Foundation [#44943156088](https://github.com/ahmedali5696/care-pulse-3/actions/runs/44943156088), 44/44 Python and 11/11 Web mandatory compatibility cases and T057 synthetic engineering PASS. These are documentary citations here, not newly inspected CI or new Wave 3 acceptance.
- Live GitHub search on 2026-11-22 confirmed #24/#25/#26/#28 OPEN. #26 retains independent Security/Clinical and PO risk disposition. Master §12 records later evidence addressing #28's historical synthetic findings; open status alone does not invalidate that evidence or require repetition.
- #2/#4/#5/#13/#18 and SECURITY-POLICY-NUMERICS / OPEN-OPS-01/02/03 / OPEN-FILES-01 retain master §§12/14 production owners/triggers/closure criteria. #13's wider historical upgrade obligation is distinct from mandatory preservation proof for any new immediate-predecessor migration. Custody, production TTL, legal basis and release approval are not Phase 0 unknowns or planning blockers.

No unresolved design clarification remains. Executable prerequisites remain explicitly unverified; use [quickstart](quickstart.md) under separate authorization.
