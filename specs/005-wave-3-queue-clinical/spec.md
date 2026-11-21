# Feature Specification: CarePulse 3 — Wave 3: Queue & Clinical Operations

**Feature Branch**: `feature/wave-3-queue-and-clinical-operations`

**Created**: 2026-11-21

**Status**: Draft — approved-source specification; implementation acceptance is not asserted.

**Input**: Specify complete approved B-11–B-20 scope from implementation-plan §8 under PRD, Domain Model and System Design precedence and §12 cross-wave constraints. Preserve business rules, permissions, transitions, Master ownership/dependencies and existing identifiers. Describe observable F-01 and November-amendment outcomes, distinguish requirements from unverified implementation, and stop after specification.

## Clarifications

### Session 2026-11-22

- Q: How long should a Doctor's extra-case admission confirmation stay valid before it expires unused? → A: Configurable; production TTL pending approval

## User Scenarios & Testing *(mandatory)*

These are acceptance scenarios, not executed tests. Priorities order user value without changing dependencies. Each journey is independently assessable with valid upstream fixtures and its prerequisite operations. Doctor means the single Doctor/Owner. Manager is a Receptionist with live Owner-granted `operational_manager`, not a third role. “Authorized staff” includes Doctor, Manager and ordinary Receptionist subject to each operation's restrictions.

### User Story 1 - Check in and view dated queues (Priority: P1)

As staff, I confirm a booked patient's presence and see their actual Waiting position separately from their booking number. **Owner: B-12.**

**Why this priority**: Attendance and dated queues precede admission and immediate-intake reuse.

**Independent Test**: Check in existing bookings under booked-order/FIFO policies, including an archived patient's booking, and inspect dates, ordering and absence of Visits.

**Acceptance Scenarios**:

1. **Given** valid pre-existing Booked, including a booking made today or whose patient was later archived, **When** Doctor or Receptionist confirms physical presence, **Then** it becomes Waiting with exactly one dated entry, server arrival and committed Doctor-alert event, preserving number/price and creating no Visit. Concurrent/repeated check-in produces no duplicate entry/alert.
2. **Given** Waiting Routine/Urgent/Emergency and unarrived bookings, **When** a day is selected, **Then** only Waiting occupies positions: MAIN Urgent FIFO ahead of Routine under that day's policy, and independent Emergency FIFO. Booking number is not position.
3. **Given** booked-order Routine, **When** a lower-numbered patient arrives, **Then** normal number order applies until their turn genuinely passed; late arrival goes after the first `min(late_depth, Routine Waiting count)` Routine entries. Midnight never moves old Waiting to today's queue or changes its status/date.

**Trace**: FR-018–021b/062/108; BR-008–011c/048; INV-04/06/17/21/31/36/38; AC-08/15/42; CON-05/13/19/40/43.

### User Story 2 - Register immediate Routine, Emergency or Paid Urgent (Priority: P1)

As permitted staff, I use distinct intake operations for present patients. **Owner: B-11.**

**Why this priority**: Service, queue, presence and pricing must agree.

**Independent Test**: Exercise each intake with valid/invalid actors, toggles, prices, patient state and work periods; inspect all-or-none creation.

**Acceptance Scenarios**:

1. **Given** Routine walk-in OFF, **When** staff attempts direct unbooked Routine Waiting or a fused booking/check-in shortcut, **Then** it fails unchanged. Independent booking creates Booked only; physical check-in of a real existing booking remains allowed without invented lead time. With walk-in ON, valid present-patient intake under ordinary date/slot rules creates one Routine Waiting appointment/entry together.
2. **Given** present same-day active patient without counted conflict, **When** Doctor, Manager or ordinary Receptionist registers Emergency New Examination, **Then** separate FIFO, standard non-Urgent price or authorized reasoned discount/free, and Doctor-alert event result. Routine/Urgent toggles and Paid Urgent work-period restrictions do not block Emergency or change MAIN ranks.
3. **Given** authorized Receptionist, present same-day patient, enabled valid premium and valid current work period outside breaks/holidays, **When** Urgent commits, **Then** new MAIN/Urgent Waiting, number and frozen premium appear together. Doctor-created, remote, future-day, holiday, break, missing-hours, out-of-hours, free or insufficient-premium Urgent fails; current policy is rechecked at commitment.
4. **Given** archive or counted same-day conflict, **When** new intake is attempted, **Then** new IDs are blocked as applicable. Routine Booked/Waiting replacement requires Story 7; Completed/In Examination cannot be bypassed.

**Trace**: FR-018/021a/021c/059/059a/061; BR-011a/016a/024/038; INV-02/09/21/25/31/33/34/36; AC-14/16/20/30/32; CON-01/19/25/26/33/35/39/43/44.

### User Story 3 - Control admission and extra-case approval (Priority: P1)

As Doctor/Owner, I control Pause, capacity and case approvals; as Owner/live Manager, I configure admission mode. **Owner: B-14.**

**Why this priority**: Every admission depends on these controls.

**Independent Test**: Exercise mode/Pause/capacity and confirmation lifecycle, then consumption through Story 4.

**Acceptance Scenarios**:

1. **Given** Both mode and capacity one by default, **When** Owner/live Manager selects Doctor-only/Receptionist-only/Both, **Then** current admissions use the audited setting. Ordinary/revoked Receptionist settings changes fail; capacity is Owner-only.
2. **Given** Waiting in either lane, **When** Doctor pauses, **Then** all admissions stop while permitted check-in/intake/booking continue. Receptionists observe but cannot change Pause. Pause/capacity reduction never terminates existing examinations.
3. **Given** capacity above one and exact Waiting patient/appointment/original day/lane, **When** Doctor issues a confirmation, **Then** it is time-bounded, revision-bound, one-case authority and admits nobody itself. Doctor can issue it in Receptionist-only mode. Expired/used/Doctor-revoked/mismatched/stale approval cannot authorize admission; Receptionist/Manager/operator cannot issue or explicitly revoke it, and competing consumers cannot use it twice.

**Trace**: FR-023/024/030/031/063/064/071/091; BR-013–015/036; INV-07/20/22/35; AC-02/17/26; CON-14/20/21/42.

### User Story 4 - Admit from either lane (Priority: P1)

As staff permitted by current mode, I choose day/lane/target, ordinarily its head or an explicit justified within-lane exception. **Owner: B-15.**

**Why this priority**: Admission alone creates a Visit and must prevent duplicate/unsafe allocation.

**Independent Test**: Prepare E1/E2 and MAIN U1/R1; assess each staff actor's ordinary/exceptional admission and conflicting controls.

**Acceptance Scenarios**:

1. **Given** both lanes populated, **When** either head is selected, **Then** no cross-lane priority or exception justification applies. E2 or R1 behind U1 fails as an ordinary head, but Doctor/Manager/ordinary Receptionist may explicitly justify a non-head exception within the selected lane under all safeguards.
2. **Given** valid presence/current revisions/unpaused capacity, **When** admitted, **Then** exactly one Visit, In Examination, closed queue entry, actual actor/role/day/lane/head/exception audit and event result together. Only ordinary MAIN Routine-head admission advances cursor. All dates/lanes share capacity; each admission above one concurrent examination needs its own fresh Doctor confirmation, even if Doctor acts.
3. **Given** prior-day Waiting, **When** admitted, **Then** fresh explicit continuing-presence evidence with actor/time is recorded as part of admission; stale separate reconfirmation fails. Original date/lane remain.
4. **Given** missing justification/presence, wrong day/lane, stale head/revision, forged actor/approval, revoked access or Pause, **When** admission is attempted, **Then** no successful Visit/status/queue/cursor/claim/audit/event effect occurs. Archive alone does not disqualify valid existing appointments.

**Trace**: FR-023–029/108; BR-011c/013–015/047/048; INV-05–08/22/30/38; AC-08/15/17/26/38/42; CON-02–07/13/20/21/29/32/40/42.

### User Story 5 - Place Routine patients and change future queue policy (Priority: P2)

As Receptionist, I justify Routine placement; as Owner/live Manager, I change future queue policy. **Owner: B-13.**

**Why this priority**: These controls build on safe admission without disrupting active queues.

**Independent Test**: Reorder Routine and change future policy while admission competes.

**Acceptance Scenarios**:

1. **Given** MAIN/Routine/Waiting entries, **When** Receptionist supplies reason/current revision, **Then** only Routine positions change with old/new rank and actor/reason audit. Urgent/Emergency/cursor remain unchanged; missing reason, wrong date or non-Routine/non-Waiting targets fail.
2. **Given** active QueueDay, **When** Owner/live Manager changes booked-order/FIFO/late-depth, **Then** its snapshot/ranks remain; latest applicable policy governs next operating day. Ordinary/revoked Receptionist changes fail.
3. **Given** placement racing old-head admission, **When** both execute, **Then** a coherent current order governs; stale ordinary admission cannot bypass the new head. Manual movement never advances missed turns.

**Trace**: FR-021b/062/097; BR-011b/034/036; INV-08/26/27; AC-02/08/25; CON-28/29.

### User Story 6 - Document, complete or exceptionally cancel care (Priority: P1)

As Doctor, I document/correct care; as staff, I explicitly attest actual examination under my identity before completion. **Owner: B-16.**

**Why this priority**: Clinical outcome, provenance and entitlement must agree.

**Independent Test**: Exercise clinical fields, each staff actor's completion, Doctor cancellation/correction and competing outcomes.

**Acceptance Scenarios**:

1. **Given** admission-created Visit, **When** Doctor records examination/findings/diagnosis/notes/treatment/tests/recommendation, **Then** content is retained; recommendation creates no booking. Receptionists cannot author it or read narrative via operational views, nested history/search/audit/events.
2. **Given** In Examination after actual care, **When** Doctor/Manager/ordinary Receptionist positively asserts examination occurred, **Then** Completed, retained Visit, actual actor ID/role/tenant/appointment/Visit/server-time attestation, applicable claim consumption, value eligibility and event are recorded together. No Doctor signature/notes/source decision is fabricated. Missing/false assertion, wrong state or stale/competing outcome produces no partial effects or duplicate consumption.
3. **Given** In Examination preferential Follow-up, **When** Doctor exceptionally cancels with reason, **Then** Cancelled retains Visit/content, atomically releases claim, contributes no completed value/source eligibility and records one Doctor-origin Receptionist-review event. Receptionist cancellation fails; rollback creates no release/event.
4. **Given** Completed or Doctor-exceptionally-Cancelled Visit, **When** Doctor corrects content, **Then** immutable previous/new values, modifying actor/role and second-precision time remain without reopening/changing outcome. Receptionist correction fails.
5. **Given** administratively completed New Examination without Doctor source decision, **When** B-10 assesses eligibility, **Then** current policy applies without fabricated Doctor authorship. Source decisions, NEW-date exceptions and confirmed extensions remain separate upstream actions.

**Trace**: FR-009a–009c/032–034/067/069/074/080/081; BR-001/002a–002c/005/007/017/030; INV-05/10/15/23; AC-02/04–07/27; CON-11/15/18/22.

### User Story 7 - Cancel or atomically replace Routine with Urgent (Priority: P1)

As staff, I retain reasoned cancellation history; as Receptionist, I explicitly replace eligible Routine with new same-day Paid Urgent. **Owner: B-17.**

**Why this priority**: Failed replacement must not lose the original appointment.

**Independent Test**: Cancel across actors and inject failure/competing changes into typed-confirmed replacement.

**Acceptance Scenarios**:

1. **Given** Booked/Waiting, **When** staff cancels with reason, **Then** terminal original/history remain, queue closes and claim releases together. Doctor-origin records one Receptionist-review event; Receptionist-origin never impersonates Doctor.
2. **Given** eligible same-day Routine Booked/Waiting and present active patient, **When** Receptionist supplies reason/typed confirmation, **Then** old cancellation/queue closure/claim release and linked new ID/number/current premium/Waiting succeed together; no in-place priority/price conversion.
3. **Given** invalid presence/price/schedule, archive, lost eligibility or competing admission/reschedule, **When** replacement fails, **Then** original status/queue/claim/price/history remain, with no successful replacement/event. Completed/In Examination cannot be replaced; Cancelled/No-show permits only separately eligible new IDs.

**Trace**: FR-011/012/021c/061; BR-004–006/016a/018/019/038; INV-02/13/23/25/33; AC-06/16/30; CON-09/15/25/35/41.

### User Story 8 - Reschedule without losing attendance or entitlement (Priority: P1)

As staff, I move eligible existing appointments preserving identity, price and genuine Follow-up authority. **Owner: B-18.**

**Why this priority**: Moves cannot create ghosts, reprice care or bypass Doctor restrictions.

**Independent Test**: Move Routine across dates, adjust eligible same-day Urgent and assess outside-deadline confirmed Follow-up under races.

**Acceptance Scenarios**:

1. **Given** eligible Routine Booked/Waiting, **When** reason/current revision/valid destination are supplied, **Then** ID/price remain; cross-day move assigns fresh number, preserves old number/attendance, returns Waiting to Booked and requires fresh attendance without old queue ghost. Archive alone does not block same-ID moves; permitted same-day Waiting adjustments never silently reset attendance.
2. **Given** confirmed Follow-up moving outside frozen deadline, **When** requested, **Then** distinct prior B-10 extension must authorize it with mandatory reason, specific later/non-shortening Cairo date and append-only actual actor/source/claim/policy/revision evidence. All staff may extend within boundaries, but Receptionists cannot revise/bypass genuine Doctor OPTED_OUT/DEADLINE, including later restrictions. NEW-booking exception cannot substitute; original price/evidence remain.
3. **Given** existing eligible Paid Urgent, **When** permitted same-day adjustment commits, **Then** current onsite/same-Cairo-day, enabled work period, break/holiday, target interval/schedule and revision guards hold while original premium/lane remain. Invalid/stale eligibility fails unchanged; cross-day same-ID Urgent always fails without conversion/repricing.
4. **Given** Doctor's valid individual move, **When** committed, **Then** one minimal Receptionist-review event commits with it; failed moves produce none. Terminal/In Examination, uniqueness/slot/holiday/entitlement conflicts fail atomically. Separate cancellation/new booking uses fresh rules without inherited released extension/price.

**Trace**: FR-009c/011/012; BR-002a–002c/003/004/006/019/025/032/038; INV-10/12/18/24/37/38; AC-06/18/23/27/30; CON-16/24/37–41/45.

### User Story 9 - Move an eligible same-day cohort together (Priority: P2)

As Doctor/Manager/ordinary Receptionist, I preview a same-original-day Routine cohort and confirm an atomic move. **Owner: B-19.**

**Why this priority**: Bulk convenience retains individual safeguards.

**Independent Test**: Preview mixed eligibility, introduce one conflict and verify no selected movement.

**Acceptance Scenarios**:

1. **Given** same-original-day Routine Booked/Waiting and other records, **When** previewed, **Then** eligibility/exclusion counts and reasons are explicit; In Examination, terminal, Urgent Waiting and other ineligibles are excluded. No Manager designation or separate bulk grant is needed.
2. **Given** current preview and typed `RESCHEDULE`, **When** every selected record/destination remains eligible, **Then** all moves commit with frozen prices, valid claims, new-day numbers and queue/history changes. Bulk cancellation remains absent.
3. **Given** newly admitted selection, stale preview, revoked/foreign actor or destination/claim/Doctor-precedence conflict, **When** commit is attempted, **Then** the whole batch fails with no partial move/event. Changed selection needs current valid preview/confirmation.

**Trace**: FR-041a/071/091; BR-019a/046; INV-14; AC-09/34/43; CON-12/16.

### User Story 10 - Reconcile overdue work without forced close (Priority: P2)

As staff, I review a chosen day, confirm absence and optionally close its review without terminating care. **Owner: B-20.**

**Why this priority**: Midnight must not manufacture absence or completion.

**Independent Test**: Cross midnight with Booked/Waiting/In Examination; assess cutoff/manual absence/close against races.

**Acceptance Scenarios**:

1. **Given** defaults, **When** midnight passes, **Then** no close/cutoff/status change/reassignment occurs; old Booked/Waiting are flagged for dated review without seventh status and ongoing Visits retain date.
2. **Given** explicit enabled Cairo cutoff, **When** applied for its day, **Then** only still-Booked unarrived records become No-show with claim release/audit. Concurrent check-in yields Waiting or No-show, never both. Without cutoff, Doctor/Receptionist may manually mark overdue Booked No-show only with absence confirmation/reason/actor/time; age alone or Waiting/In Examination is insufficient.
3. **Given** chosen-day Booked/Waiting/In Examination, **When** close finalizes, **Then** zero Waiting remains after lawful admission/reason-cancel/reschedule. Review alone leaves Booked unchanged; In Examination may finish later on original date. Audited review amendment/reopening never reopens terminal appointments.
4. **Given** prior-day Waiting still present, **When** admitted, **Then** fresh presence/shared-capacity safeguards apply; no insertion into today's ranks or false No-show to close yesterday.

**Trace**: FR-035/092/106–108; BR-027/048/049; INV-11/17/30; AC-19/33/41/42; CON-08/13/32.

### Edge Cases

- Dates/lanes compete for shared capacity; filtered views never hide active examinations from capacity accounting.
- New Urgent changes MAIN head; Emergency cannot displace MAIN. Reorder/cancel/revisions invalidate stale targets/confirmations.
- Late Routine with zero/fewer-than-depth entries, an unpassed lower number and next-day policy creation preserve their distinct rules.
- Pause/mode/Manager revocation/confirmation expiry/tenant suspension race protected work; stale screens cannot authorize it.
- Midnight preserves service day for value; Cairo completion date separately supplies Follow-up day zero.
- Archive blocks new intake/replacement IDs but preserves lawful existing-ID check-in/admission/reschedule.
- Touching exact intervals do not overlap; manual precedence, breaks, date-specific hours and overlap capacity apply. Holiday permission never permits holiday Urgent; Open invents no hours/day cap.
- Walk-in OFF cannot be bypassed by fused booking/check-in; real same-day bookings need no arbitrary lead time.
- Later Doctor restriction does not retroactively cancel frozen claims but constrains later Receptionist actions; release/rebook inherits no extension.
- Completion/cancel/release/new-claim races preserve one outcome/Visit and at most one completed preferential use.
- Lost acknowledgements require authorized reconciliation, not blind duplicate mutations.
- General Receptionist nested history/errors/audit/events never expose Doctor narrative; future full export is not general clinical access.

## Requirements *(mandatory)*

### Functional Requirements

#### Authority, scope and identifiers

Descending precedence: [approved PRD](../../docs/CarePulse_PRD_v1_APPROVED.md), [approved Domain Model](../../docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md), [approved System Design](../../docs/CarePulse_System_Design_v1_APPROVED(1).md), then [implementation plan](../../docs/implementation-plan.md), especially §§2/8/11.4/12. The [Constitution](../../.specify/memory/constitution.md) governs practice within them. November amendments supersede conflicting historical workbook/completed-task interpretations. This spec adds no canonical FR/BR/AC/INV/CON IDs and renumbers none; ranges include only existing source identifiers.

Complete scope is **B-11–B-20**, all Wave 3-owned, retaining W3-M1 (check-in/intake/lanes/admission) and W3-M2 (Visit/individual changes/bulk/cutoff/review). Exact dependencies remain:

| Master | Approved scope | Dependencies, unchanged | Acceptance trace |
| --- | --- | --- | --- |
| B-12 | Independent physical check-in and dated queue projections | D-06, B-09 | Story 1; FR-018–021b/062/108; AC-08/15/42 |
| B-11 | Distinct Routine/Emergency/Urgent intake using B-12 queue outcomes | B-06–09, B-12, D-06 | Story 2; FR-018/021a/021c/059/059a/061; AC-14/16/20 |
| B-14 | Doctor Pause/confirmations, Owner capacity, Owner/live Manager mode | D-06/09, B-01/12 | Story 3; FR-023/024/030/031/063/064; AC-02/26 |
| B-15 | Day/lane/head or justified exception, old-day presence, shared capacity, unique Visit | B-12/14, D-07/09 | Story 4; FR-023–029/108; AC-08/15/17/26/38/42 |
| B-13 | Routine placement, future queue policy, placement/admission proof | B-12/15, D-04/06 | Story 5; FR-021b/062/097; AC-02/08/25 |
| B-16 | Doctor clinical work/corrections, attested completion, exceptional In Examination cancel | B-15, D-07/08, B-10 | Story 6; FR-032–034/067/069/074/080/081; AC-02/04–06 |
| B-17 | Booked/Waiting cancellation, atomic Routine-to-Urgent replacement | B-10–12/15 | Story 7; FR-011/012/021c; AC-06/16 |
| B-18 | Existing-ID reschedule, frozen claims/prices, same-day Urgent guards | B-07–10/12/17 | Story 8; FR-011/012; AC-18 plus amendment AC-06/27 |
| B-19 | All-staff eligible-only same-original-day Routine bulk reschedule | B-18, D-06 | Story 9; FR-041a/071/091; AC-09/34/43 |
| B-20 | Optional cutoff transition, manual No-show, overdue review/close | B-12, B-15–18, D-11 | Story 10; FR-035/092/106–108; AC-19/33/41/42 |

#### Actor and information boundaries

- **PRD §3; FR-003/069–072/075/080/091; BR-016/017/035/036; INV-01/19/20/21/28**: Each protected operation MUST enforce actual authenticated identity and current active tenant/account/operation permissions. Patients have no authenticated clinic account and provisioning operators have no clinical authority. Foreign resources, suspended clinics, disabled actors and revoked grants yield neither disclosure nor successful changes.
- **FR-023/024/026/029/034/041a; BR-007/011c/036**: Doctor/Manager/ordinary Receptionist MUST have permitted completion/bulk without special delegation; admission stays mode-bound and non-head selection requires explicit justification. Admission mode never transfers authorship. General staff authority does not permit Doctor Paid Urgent intake or manual Routine placement reserved to Receptionists.
- **FR-071/091; BR-036**: Manager settings authority MUST remain fixed: prices/Follow-up policy; work days/periods, breaks, holidays, holiday booking/slots; queue/late-depth/No-show; Routine/Urgent toggles; admission mode. Ordinary Receptionist has no settings authority. Owner alone manages all Receptionist accounts/delegation/capacity; Doctor alone controls Pause, case confirmation issue/revoke, clinical content/corrections and In Examination cancel. Wave 3 owns only its listed controls; other settings/account work stays upstream.
- **FR-032/069/074/080/081; BR-030; plan §2**: Receptionist reads MUST remain allowed intake and minimal operational appointment/Visit linkage, day/status, upload context and own permitted operational evidence. Doctor examination/findings/diagnosis/notes/treatment/tests/recommendations and clinical correction narrative MUST NOT appear in general Receptionist reads/writable inputs. Preliminary history/tests cannot become copied Doctor-narrative shortcuts; forbidden/unknown clinical inputs MUST be rejected.
- **FR-079/080/096/104; BR-039–041**: Five universal staff operations (justified non-head, source decisions/extensions, archive/unarchive, full one-patient export, eligible bulk) MUST NOT become Manager-only or grant clinical authorship. Archive/Follow-up stay upstream; files/full export stay later-Wave-owned. Full export disclosure never broadens general queue/Visit/history or original attachment rights.

#### State, attendance, queue and clinical integrity

- **FR-010/017/034/106; BR-001/003–009; INV-03–05**: Only Booked, Waiting, In Examination, Completed, Cancelled and No-show are Appointment states. Review flags, queue phases, approvals and day reviews add no state. Booking/check-in creates no Visit; admission creates one; completion/exceptional cancel retains it. Terminal appointments never reopen/reschedule.
- **FR-020/061; BR-010/016a; INV-02/31**: Preliminary numbers MUST be server-issued, unique and never reused per clinic/day across all lanes/types and historical allocations. At most one counted Booked/Waiting/In Examination/Completed exists per patient/day. Cross-day moves preserve old assignments and receive fresh destination numbers. Cancelled/No-show remains retained history allowing separately eligible new IDs; Completed stays counted.
- **FR-018–021b/062/097/108; BR-009–011c/047/048; INV-04/06/08/17/26/27/30**: MAIN Urgent FIFO precedes Routine; independent Emergency FIFO has no global precedence. Routine uses dated booked-order default or arrival FIFO, late-depth default five and reasoned Receptionist placement. Only ordinary MAIN Routine-head admission advances cursor; cancellation/reorder/Pause/Urgent/Emergency/exceptional admission never do. Settings apply next operating day without replacing active policy. Old Waiting retains date/lane with fresh presence on admission.
- **FR-023/024/030/063/064; BR-013–015; INV-07/22/35**: Default capacity one and mode Both apply. Active examinations across all dates/lanes share capacity. Each admission above one needs fresh Doctor-issued one-use authority bound to Doctor/patient/appointment/original day/lane/revisions and cannot exceed capacity. Expiry/reuse/mismatch/revocation/staleness fails; reductions never evict existing examinations. Confirmation lifetime MUST be a configured policy value, not a fixed constant, and not a Wave 3 user-facing settings control; this spec selects no numerical value and the production value is pending Product Owner and Security/Operations approval under implementation-plan.md §2. A missing or invalid value MUST fail closed (no confirmation can be issued or used), and any non-production test value is not a production default. Acceptance MUST show expiry at the configured boundary makes the confirmation unusable.
- **FR-011/012/034/035/092/107; BR-004–007/027/049; INV-11–14/23**: Lawful actor/reason/attendance/status and related queue/claim/Visit/evidence MUST succeed together. Booked/Waiting cancel closes queue/releases claim; Doctor-only In Examination cancel retains Visit/releases claim. Completion requires positive examination attestation and consumes entitlement once. Cross-day Routine Waiting becomes Booked needing new attendance. No-show is only unarrived Booked; close requires no unresolved Waiting without silently changing other states.
- **FR-013–015a/065/066; BR-023–026; INV-09/25**: New services MUST have server-calculated immutable EGP snapshots with authorized reasoned discounts, valid nonnegative amounts and approved final-net rounding. Emergency uses standard non-Urgent price, including permitted free; Urgent base/net must strictly exceed standard New Examination base and never be free. Reschedule never reprices; replacement freezes new current premium. Completed qualifying service value counts once on original day, never payment collection; cancelled retained Visits contribute zero. Reporting stays B-26.
- **FR-011/053–060/099; BR-006/032–034; INV-24/25/34/36/37/38**: Intake/reschedule MUST preserve ordinary date/holiday/slot guards and independent Urgent onsite/same-day/current-work-period guards at commitment. Open has no invented time/day cap; exact intervals respect manual precedence, breaks, overlap/capacity and date-specific hours. Holiday permission alone cannot invent hours or permit Urgent. Routine cannot evade walk-in toggle; archive blocks new IDs/replacements but not lawful existing attendance/admission/amendment.

#### Follow-up contracts consumed without transferring ownership

- **FR-009a–009c/067; BR-002a–002c; INV-10/23; AC-06/27/28**: D-07/D-08/B-10 retain source/claim substrate and source-decision/NEW-date-exception/confirmed-extension ownership. B-16–B-20 MUST preserve their outcomes without parallel entitlement policy.
- Sources MUST be Completed New Examinations for the same patient/clinic/Doctor, including Routine/Emergency/Urgent and valid administrative completion. Cairo completion day is day zero; current policy (initially through day seven inclusive) governs fresh claims without Doctor decision and after release. Preferential Follow-up cannot recursively source another; unavailable/consumed/nonqualifying eligibility means New Examination, not standalone standard-price Follow-up.
- Actual-actor `DEFAULT` needs no reason; `OPTED_OUT`/`DEADLINE` need reasons, with a specific Cairo DEADLINE no earlier than completion. Receptionists may revise Receptionist-origin decisions with immutable before/after but MUST NOT revise/bypass genuine Doctor restrictions. Doctor DEADLINE replaces the default window, shorter or longer; it is not a one-date exception. Only genuinely authenticated revision-checked Doctor action revises their restriction.
- NEW-booking exception MUST remain distinct: Doctor reason mandatory, Receptionist reason optional, date-specific actual-actor audit always mandatory. Confirmed extension requires reason, specific later/non-shortening Cairo date/deadline, append-only actor/role/tenant/source/claim/time/policy/source/claim revisions and before/after authorization evidence before outside-deadline movement. Both retain completion lower bound, Doctor precedence and ordinary safeguards.
- Original confirmed entitlement/source/policy/actor evidence and price MUST stay frozen. Later source/policy changes do not retroactively cancel/reprice, but constrain later Receptionist actions through Doctor precedence. At most one active claim and one Completed consumption per source; cancel/No-show releases and rebooking uses current policy/price without inherited extension. Races cannot produce stale authority, duplicate use or fabricated Doctor authorship.

#### Observable security, evidence and failure outcomes

- **F-01, plan §§2/8/12; FR-072/074/075; INV-01/19/20/28**: Caller-asserted actor/tenant/role MUST NOT authorize another identity/operation/resource. Valid authority for one operation cannot grant arbitrary clinical reads/writes or audit fabrication. Verified request authority must remain bound to its actual service identity and execution context; reuse in a different context, alteration of the authorized action or resource, and caller-supplied approval cannot broaden it. Missing/forged/replayed/mismatched authority and revoked access fail closed. Existing F-01 protection MUST remain effective for each new operation; retired shared runtime authority cannot be fallback. These are observable obligations, not a new identity design.
- **FR-012/033a/034/074; BR-007/030; INV-15/16**: Sensitive evidence MUST preserve actual actor ID/role, tenant, action/resource, required reason, server time and meaningful previous/new facts without caller-forged Doctor identity. Clinical corrections preserve immutable before/after values and second-precision time separately from Receptionist evidence. Domain changes and required history/audit/events MUST succeed or fail together.
- **FR-025/027/028/036/039; BR-018/019; INV-16; plan B-16–18/B-23**: Wave 3 MUST retain committed attendance/admission/completion/Pause/change events. B-17 owns Doctor-origin Booked/Waiting cancellation review events; B-16 In Examination cancellation; B-18 individual reschedule. Each committed action records one minimal operational Receptionist-review event; Receptionist-origin never impersonates Doctor. Events contain permitted identifiers/status/date, no Doctor narrative; rollback yields none. Actual delivery/realtime/sound/push stays B-23/B-24 and UI owners.
- **Plan §11.4; Domain §§4/10/11**: Critical commands MUST reject stale/invalid work and prevent partial domain/success-audit/event effects under races/failures. Authorized repeated requests/response recovery MUST NOT duplicate appointment/number/queue/Visit/claim/approval/correction/event effects. Unknown commit outcome requires authorized reconciliation against existing result/current state before retry, not blind replay or permission bypass.
- **Plan §11.4; Constitution II–IV**: Malformed/excessive input or unavailable dependency MUST fail predictably with sanitized errors, no sensitive logs or unbounded access; no new volume/transport/retention/timing limits are chosen. Required evolution preserves identifiers, clinical/history links, tenancy and authentic evidence without exposed partial state or fabricated success. Published migrations/historical evidence stay immutable. Technical design and executable proof remain later work.

### Key Entities *(include if feature involves data)*

- **Clinic/staff authority**: Tenant, single Doctor/Owner, Receptionists and optional Manager capability/current permissions.
- **Patient lifecycle**: Stable tenant identity/archive state, retained appointment/Visit history without merge/deletion.
- **Appointment/assignments**: Six-state service, lane/class, Cairo day/optional interval, frozen EGP, numbers/history/replacement links.
- **QueueDay/policy/entry**: Original day/lane, arrival, Routine rank/cursor, dated policy and historical attendance; only Waiting has active membership.
- **Admission control/confirmation**: Shared Pause/capacity/mode and exact-case time-bounded revision-bound one-use Doctor approval.
- **Visit/attestation/correction**: Single admission-created clinical record, actual-examination evidence and Doctor corrections; treating Doctor is not every action's author.
- **Follow-up source/claim/evidence**: Completed New Examination, actual-actor or policy-derived eligibility, frozen claim/price, distinct exception/extension, release/consumption.
- **Bulk preview/selection**: Same-original-day Routine cohort, eligibility/reasons, current evidence and typed confirmation.
- **Day review/cutoff**: Optional selected-day review/close/amendment and separately enabled Cairo cutoff, neither an Appointment state.
- **Audit/operational event**: Durable actual-actor facts and minimal information for later authorized delivery, separate from clinical narrative.

## Success Criteria *(mandatory)*

### Measurable Outcomes

These are future acceptance targets, not measured runtime results. Counts/zero-effect outcomes express existing invariants; no latency, throughput, satisfaction or production-security targets are invented.

- **Scope and task completion**: All **10** Masters retain ownership/dependencies and testable journeys. Each authorized actor can complete every permitted journey while prohibited combinations deny. Reviewers can distinguish booking number/position, service/event date, clinical/administrative authorship and the three Follow-up actions. (PRD §3; AC-02/04/08/14/34/42.)
- **Attendance/ordering**: Each Waiting has exactly **one** active dated entry and **zero** Visits before admission. Emergency causes **zero** MAIN rank/cursor change; only ordinary MAIN Routine-head admission advances cursor. Policy edits cause **zero** active-day reranking. (AC-04/08/15/20/25/38; CON-03–05/28/29.)
- **Admission**: Each success creates **one** Visit; capacity-one competition allows at most **one** success. Each extra case uses its own confirmation at most once; every old-day admission has fresh actual-actor presence/original date. (AC-17/26/42; CON-02/06/07/20/21/32.)
- **Clinical provenance**: Every completion has positive attestation tied to actual actor/role/tenant/appointment/Visit/server time; false/missing assertion yields **zero** completions. Every post-terminal correction retains before/after and second-precision actor/time. Receptionist work produces **zero** forged Doctor signatures/decisions/revisions. (AC-02/04–06.)
- **Atomic changes/claims**: Replacement/bulk yields **all** effects or **none**. Cross-day Routine has **zero** old-date active ghosts and requires attendance. Each source has at most **one** active claim and **one** Completed use with **zero** silent snapshot rewrites. (AC-06/09/16/18/27/34/43; CON-09–12/16/22.)
- **Date flexibility**: Default midnight causes **zero** status changes/migrations. Each manual No-show proves Booked/absence/reason; each finalized close has **zero** Waiting and **zero** forced examination endings/implicit No-show. (AC-19/33/41/42.)
- **Security/events**: Unauthorized/stale/rolled-back actions have **zero** successful domain/audit/event effects or forbidden disclosure. Each Doctor-origin committed individual cancel/reschedule has **one** review event; recovery adds **zero** duplicates. Historical baseline alone proves **none** of the new-operation claims. (AC-01/12/17/22/40; F-01; CON-14/15; plan §§8/11.4/12.)

## Assumptions

### Approved boundaries, not new defaults

- This invocation authorizes specification/checklist/active-feature metadata only, not planning/tasks/implementation/migration/CI dispatch/Issue disposition/merge/cutover/deployment/release. Earlier-Wave artifacts and authorities are unchanged.
- One Doctor/Owner, one branch, Egypt, Cairo/EGP and patients-as-records remain. Defaults: Open day-only, Routine walk-in OFF, Urgent OFF pending configuration, booked-order Routine, late-depth five, capacity one, Both admission, no default close/cutoff. No security/legal/performance/retention values are inferred.
- Manager scope, five universal operations, source reasons/dates/Doctor precedence, distinct confirmed extension and positive examination attestation are approved. Technical representation/proof remains future work, not unanswered product policy.

### Cross-wave ownership and readiness

- **Upstream**: Waves 0–2 retain identity/session/field authorization, staff/Manager lifecycle, patients/archive, pricing/scheduling/booking, queue/Visit/claim/day-review substrate and B-10 actions. B-12 does not depend on B-11; B-11 reuses it. B-15 does not depend on B-13; B-13 owns placement/admission proof. B-16 alone owns In Examination cancel; B-17 Booked/Waiting cancel/replacement. No new Master/dependency or automatic reopening of historical tasks.
- **November gate before affected implementation**: Independently verify exact consumed upstream actual-actor decisions/revisions, Doctor restrictions, dated reasoned extensions, completion provenance, operational/clinical boundaries, security/source/policy/claim revisions and required forward-only compatibility. Missing executable prerequisite evidence makes **that affected Master BLOCKED for implementation**, naming the missing contract; it does not block specification or declare every Master blocked. B-16 still owns its new completion behavior; upstream provenance readiness does not transfer ownership.
- **F-01 before enabling each operation**: Plan §8 requires current executable-revision evidence for actual participating service identities/effective least privilege/tenant isolation, verified actual actor with exact operation/resource scope, live revocation/replay/idempotency/revisions and positive/negative/critical concurrency outcomes at the real protected boundary. Historical 0040-only writer tests do not establish current Queue/Admission/Visit/DayReview. Existing evidence obligations are referenced, not redesigned.
- **Later Waves**: Wave 3 records audit/events before delivery exists. B-23 owns delivery/filtered realtime; B-24 consented Doctor push/sound events; B-25 durable worker invocation/retries including B-20 cutoff. B-20 does not own scheduling/retries. D-10/B-21/B-22 own files/full export; B-26 reports; D-12/B-27 broader assurance without replacing focused evidence. Wave 5 owns final UI/localization/accessibility/E2E. Ready overlap requires existing dependencies and separate authorization.
- **Excluded**: bulk cancellation, patient messaging/login/payments, SaaS public signup/billing/admin dashboard, generated electronic prescriptions, deletion/merge, bulk patient export, more Doctors/branches/currencies/timezones or speculative integrations.

### Evidence and open obligations at specification time

Sources inspected **2026-11-21** at repository revision `479db8b72ca543ad751a0deeca40382884e607cb`. Documentary handoff is distinguished from verification here:

- Plan §12 records [PR #29](https://github.com/ahmedali5696/care-pulse-3/pull/29) merged at `4dbf08fbd84a6c328c07be174d223c71f930aa2f`, full exact-SHA [Foundation #44943156088](https://github.com/ahmedali5696/care-pulse-3/actions/runs/44943156088) PASS, **44/44 Python and 11/11 Web** mandatory compatibility cases with zero mandatory skips, and independent T057 synthetic engineering PASS. This F-01/PRE-W3-COMPAT recorded baseline covers synthetic 0040→0046 compatibility/authentic historical receipt replay, not new Wave 3 behavior or production adoption. This specification did not rerun/reassess that CI.
- Live GitHub open-Issue search confirmed **#24/#25/#26/#28 remain open**. [#24](https://github.com/ahmedali5696/care-pulse-3/issues/24) and [#25](https://github.com/ahmedali5696/care-pulse-3/issues/25) track approved amendment alignment, separately authorized implementation, exact-revision security/concurrency/clinical proof, independent review and PO acceptance. Engineering and Security/Clinical/PO reviewers retain those gates before affected Master acceptance. Missing consumed proof blocks affected implementation, not this specification or settled policy.
- [#26](https://github.com/ahmedali5696/care-pulse-3/issues/26) retains independent Security/Clinical and PO risk disposition for runtime authority/actor attribution. Merged engineering does not close it or demonstrate a newly verified exploit here. Actual-identity/effective-access evidence and named disposition remain necessary.
- [#28](https://github.com/ahmedali5696/care-pulse-3/issues/28) retains historical compatibility findings; plan §12 records later executable/independent-review evidence addressing its two synthetic observations, but it remains open. Compatibility engineering, Security/Clinical and Operations retain evidence/disposition responsibilities. Neither operational approval nor a need to repeat accepted engineering is inferred from open status alone.
- **Production/deployment-only**: [#2](https://github.com/ahmedali5696/care-pulse-3/issues/2) ingress/throttling; [#4](https://github.com/ahmedali5696/care-pulse-3/issues/4) dependency security; [#5](https://github.com/ahmedali5696/care-pulse-3/issues/5) operator/delivery custody/policy; [#13](https://github.com/ahmedali5696/care-pulse-3/issues/13) applicable populated historical upgrades; [#18](https://github.com/ahmedali5696/care-pulse-3/issues/18) security numerics and OPEN-OPS-01/02/03 / OPEN-FILES-01 remain open. Existing Engineering, Security/Operations, Clinical, legal/privacy and PO owners retain Issue-specific triggers/closure criteria. These do not block specification; required missing production policy fails closed and synthetic values are not production defaults.

Checklist completion is specification quality only, not implementation, independent Security/Clinical acceptance or production readiness. Focused real-persistence/concurrency and full required CI proof remains unverified for new Wave 3 operations; container-dependent checks belong in approved CI, not local Docker installation.
