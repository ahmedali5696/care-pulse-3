# Wave 3 Data Model

Phase 1 proposed design, 2026-11-22; not DDL or runtime acceptance. Reuse [Wave 2 model](../003-wave-2-booking-scheduling/data-model.md), subject to the current [F-01 boundary](../../docs/f01-runtime-identity.md), approved amendments and [research prerequisites](research.md#upstream-prerequisite-register). Do not copy historical shared-runtime/GUC authorization assumptions into new work. [Transactions](contracts/transactions.md) defines atomic changes; [HTTP](contracts/http.md) defines projections.

## Shared representation

Existing UUID identities and non-null organization_id remain stable. Tenant-qualified foreign keys must validate Patient/Appointment/Visit/Doctor relationships together. FORCE RLS and effective column/function permissions apply under actual service logins; migration authority alone owns DDL. No cascading clinical/history deletion.

UTC timestamptz stores arrival/admission/completion/audit/expiry; Cairo DATE stores service day and deadline. Appointment labels remain exactly Booked, Waiting, In Examination, Completed, Cancelled, No-show. Review flags, queue disposition, confirmation state and claim lifecycle introduce no Appointment state. EGP is Decimal/NUMERIC with final-net ROUND_HALF_UP and decimal-string wire values; snapshots never reprice on move.

Expected revisions are separate typed tokens, not client authority: patient lifecycle, Appointment/history, QueueDay/entry, Doctor control, each policy head, Visit clinical, source decision, claim and claim-authorization revision. Only applicable tokens are required by each command; the complete compared set is retained for bulk previews and case confirmations. A clinical correction does not stand in for an operational source decision.

## Reused records and planned owner changes

| Record | Fields and relationships retained / required | Owner and validation |
| --- | --- | --- |
| Organization / staff / session | Tenant lifecycle; single Doctor; staff role/status/security revision; optional live operational_manager; authenticated session | D-02/B-01/B-04 upstream. Manager remains RECEPTIONIST. New scopes bind actual actor to service/backend/transaction; operator has no clinical access |
| Patient | Existing ID, archive/lifecycle revision, permitted demographic/intake fields | B-05 upstream. Archive blocks new intake/replacement IDs, not lawful existing-ID check-in/admission/reschedule. No implicit cancellation |
| Policy heads/versions and dated service guard | Immutable PRICE/SCHEDULE/QUEUE/ADMISSION/CAPACITY/FOLLOWUP versions, revisions and effective date; configured no_show_cutoff enabled/local_time | D-04/B-06/B-07 upstream; B-13 QUEUE, B-14 admission mode/capacity controls. Preserve active-day snapshots and shared schedule barriers |
| Appointment | Tenant/Patient/Doctor; service date, optional interval, booking mode, type/class/lane, status, number/revision; linked replacement IDs | Reuse D-05. B-11 creates Waiting via distinct intake; B-12 Booked→Waiting; B-15 admits; B-16 terminal clinical transitions; B-17–20 lawful changes. Server derives classification/status |
| Price snapshot / number ledger / history | 1:1 immutable price and provenance; clinic/day number counter; never-reused committed assignments across both lanes; append-only old/new status/date/number/actor/reason/time | Reuse existing allocation/persistence. Cross-day move receives fresh destination number and retains historical assignments. Replacement creates new snapshot/ID; same-ID move preserves snapshot |
| QueueDay | organization_id, doctor_staff_id, service_date, lane, revision, queue_policy_version, arrival_counter, routine_cursor | Existing queue_days; B-12 snapshots latest applicable dated policy on creation. Unique dated lane. B-13 future policy never replaces active snapshot |
| QueueEntry | Appointment/Patient/Doctor/day/lane, server arrival_sequence, attested_by_staff_id/attested_at, routine_rank, revision/disposition and immutable closure references | Existing queue_entries. Exactly one active matching entry iff Waiting; absent before attendance, closed on admit/cancel/cross-day move. Urgent/Emergency FIFO, only Routine has mutable Routine rank |
| Doctor control / confirmation | Shared Doctor revision/capacity policy; exact Patient/Appointment/original day/lane, issuing Doctor, issued/expires, Appointment/QueueDay/control/capacity/admission revisions, state and consume/revoke evidence | Existing doctor_controls / doctor_admission_confirmations. B-14 issue/revoke; B-15 consume. Required issuer security/authorization freshness must be verified from protected identity, with minimal forward-only storage evolution if existing fields cannot preserve it |
| Visit | Tenant/Appointment/Patient/treating Doctor/original service day, admission history revision/instant, clinical_revision, Doctor examination/findings/diagnosis/medical_notes/treatment/tests/recommendation | Existing visits, unique per Appointment. B-15 creates once on admission. B-16 clinical authoring/correction only by Doctor. Treating Doctor is not every action's author; recommendation does not book or become a source decision |
| Clinical revision | Visit/revision, old_values/new_values, actual actor/role, occurred_at and applicable reason | Existing clinical_revisions. B-16 preserves immutable previous/new values and second-precision time after Completed/exceptional Cancelled without reopening outcome |
| Completion attestation | Tenant/Appointment/Visit, completion_history_revision, examination_occurred=true, actual actor_staff_id/actor_role, completed_at, sensitive_audit_id | Existing completion_attestations, unique per Appointment and matching audit/history. D-07 owns substrate; B-16 creates with Completed/claim consumption/event/receipt. Missing/false assertion cannot produce a row or completion |
| Source/claim/evidence | Completed New Examination; actual source actor/provenance and source-decision revision; frozen claim/policy/price; separate NEW-date exception and append-only dated confirmed extension | D-07/D-08/B-10 upstream. No synthetic Doctor DEFAULT/clinical revision; no recursive source. B-16–20 preserve release/consumption and consume genuine upstream authority |
| DayReview | Organization/Doctor/original date, review revision/state, actual actor/time/reason and immutable amendments | Existing D-11 day_reviews substrate. Staff may create initial review/close; any existing-review update remains Owner-only. B-20 close checks zero Waiting; amendment/reopen never reopens terminal Appointments or ends examinations |
| Receipt / audit / outbox | Tenant/actual actor/operation/key/command digest/result/revision; normalized target/day/preview identity plus typed body in digest; actual action/resource/reason/time facts; minimal event | D-09 stores and append helpers. Preserve existing digest compatibility; verify replay target relationship/fields. Owning command adds scoped grants; rollback leaves no successful domain/evidence/event/receipt effects |

### Bulk preview: reuse receipts

B-19 preview stores an immutable D-09 receipt with a server UUID returned as preview_id, actual tenant/actor/security revision, original cohort day, exact eligible selected IDs, exclusions/reasons, destination and per-record/policy/queue/source/claim/authorization revisions. Retain only operational projection fields, never clinical narrative. Do not add a bulk_reschedule grant or reuse schedule-policy preview rows with different ownership semantics.

Existing receipt lookup/RLS is scoped to the current operation/key and cannot already load another operation by UUID. Add only a preview-specific reader under authenticated bulk-commit scope, bound to signed preview_id, same tenant/actor and exact preview operation. If privileged SQL is necessary, fixed safe search_path and narrow EXECUTE/projection are mandatory. Generic receipt reads remain closed. Preview replay must recover the original UUID/result, not create another preview. Full comparison plus fresh live eligibility invalidates stale previews; no new time limit is selected.

## State and evidence transitions

| Command | Appointment / related transition | Required invariant |
| --- | --- | --- |
| Check-in | Booked→Waiting; create one active entry | Positive physical presence, original date/number/price retained, no Visit |
| Immediate intake | New ID→Waiting plus snapshot/number/entry | Routine toggle/date/slot; Emergency independent lane; Urgent Receptionist-only onsite same-day premium/work-period guards |
| Admission | Waiting→In Examination; close entry, create Visit; consume confirmation if active count already ≥1 | Shared capacity across dates/lanes, current mode/unpaused, ordinary head or reasoned exception, fresh old-day presence; only ordinary MAIN Routine head advances cursor |
| Complete | In Examination→Completed; retain Visit, append positive attestation, consume active claim once | Actual staff identity, no invented Doctor content/decision; qualifying New Examination becomes source under upstream rules |
| Exceptional cancel | In Examination→Cancelled; retain Visit/content, release active claim | Doctor only, reason, no completed value/source, one Doctor-origin review event |
| Ordinary cancel / No-show | Booked/Waiting→Cancelled, or only unarrived Booked→No-show; close queue if applicable, release claim | Retain original/history; No-show needs configured cutoff or verified absence/reason, never calendar age alone |
| Replace Routine with Urgent | Old Booked/Waiting→Cancelled plus linked new Urgent ID→Waiting | One transaction, reason/typed confirmation, current Urgent/new-ID eligibility, new number/price; failure preserves old effects |
| Reschedule / bulk | Same-ID eligible Routine move; cross-day Waiting→Booked; detach old queue/fresh number/attendance | Frozen price/entitlement, distinct prior extension when required, current Doctor precedence, no cross-day same-ID Urgent; all selected bulk moves or none |
| Doctor correction | Retained terminal outcome, next clinical revision | Immutable old/new actual-Doctor evidence, no claim/outcome reopening |
| Review/close | Review record only | Zero unresolved Waiting for close; Booked unchanged, In Examination continues on original day |

Confirmation ISSUED may become CONSUMED once in successful admission or REVOKED by Doctor while unused. Expiry and revision/role/scope changes make it ineligible without granting another actor revocation power. Persist expiry from configured TTL; consuming at or after expiry denies. Missing/invalid TTL denies issue/use, not unrelated admission that needs no confirmation.

## Evolution and legacy provenance

This plan reserves no migration number and changes no published migration. At authorized implementation, inspect the actual immediate predecessor and compose minimal forward-only changes for operation scope, required projections/guards and only genuinely missing fields. Do not duplicate existing completion, queue, claim or review tables. Preserve tenant keys, IDs, histories, frozen amounts and authentic source ancestry on representative populated upgrade; unknown historical authorship remains unverified/fail-closed, never repaired by inventing an attestation or Doctor action.

Required fields above express accepted outcomes and proposed representation; executable grant/trigger/lock proof remains with the owners. A missing upstream representation/evidence is handled by the [prerequisite register](research.md#upstream-prerequisite-register), not a silent Wave 3 reassignment.
