# Data model — PRE-W3-COMPAT

This design adds cutover control information for one synthetic target. The existing clinic, identity, receipt and job schemas remain the authority for their records. The published Alembic history ends at `0046_f01_audit_integrity`.

## Application generation and service identity

A reviewed launch manifest binds the source SHA, actual image digest, service kind, admission adapter digest, expected peer image(s), credential set and allowed database identity for a generation. It lives with the trusted launcher. The process cannot promote itself by reporting a label. A newly built compatibility executable gets its own SHA/digest even when it uses the 0046 schema.

| Participant | 0040 authority | 0046 authority and scope |
| --- | --- | --- |
| Web/BFF and native auth | Historical runtime and restricted native auth login | `carepulse_web` and restricted `carepulse_auth`. Forward only to the paired API, with approved signing credentials. |
| Private API | Historical runtime under the predecessor signed/live path | `carepulse_api` for the product transaction and `carepulse_gate` for the separate proof/binding. |
| Worker | Historical runtime for its supported worker duties | `carepulse_worker` for its existing duties. Its heartbeat attests to the process. |
| One-shot sender | Restricted `carepulse_sender` | Same narrowly scoped role name, generation-specific credential delivery and network confinement. It owns real delivery claims and transport. |
| Maintenance and restricted operator | Separate bootstrap, migrator and operator duties | Keep these duties separate from product admission. Hold affected provisioning, cleanup and scheduled invocations during the fence. |

Validate the actual login and effective role on the target. A checkout cannot inherit a previous request's compatibility or F-01 authorization. Missing peer identity, an unexpected role, inherited/SET access or an unverified dependency denies protected work. Preserve live actor/role, tenant and resource checks within the product transaction.

## Target control and ownership

One private, maintenance-owned record per managed database contains its provisioned target identifier, phase, monotonically increasing ownership epoch, and the reviewed source/target manifest identifiers. Bind the target identifier to trusted connection configuration and the expected cluster/database identity during provisioning and recovery. An isolated copy gets a distinct target binding. Never select the target from user input or the first reachable database. The admitted generation derives from the phase and pinned manifest. The committed schema revision is read independently from `public.alembic_version`.

A product login has no right to set the phase, choose a permitted digest, change an epoch or acquire migration authority. Admission adapters have only the nonclinical rights to check admission and record their own unfinished work through a narrow interface. The designated controller uses its migration connection for schema advancement. Credential administration stays with its existing separately privileged duty. The control schema and its privileges are provisioned explicitly outside the immutable product revision chain. A missing control record never implies an open predecessor.

| Durable phase | Allowed new work | Transition condition |
| --- | --- | --- |
| `CLOSED` (initial or interrupted verification) | None | The current owner verifies the database, manifest, role, evidence and outstanding work to open an eligible endpoint. An existing cutover keeps its 0040-origin identity. |
| `SOURCE_OPEN` | Verified predecessor only at 0040 | Owner commits closure under the admission row lock. |
| `DRAINING` | None. Previously admitted operations may settle. | Account for transactions, streams, leases and delivery outcomes, stop affected consumers and seal stale authority. Recheck preflight under the fence. |
| `MIGRATING` | None | Advance a single named successor at a time under the current epoch. A new owner can only resume the same event from independently verified state. |
| `VERIFYING` | None | Verify exact target and effective authority, data, compatible applications, absence of stale access and outstanding-work disposition. |
| `TARGET_OPEN` | Verified candidate only at 0046 | Reopening commits once under the current owner. A new closure starts with fresh evidence. |

An interruption preserves the committed phase. A lost acknowledgement of reopening requires closing new ingress and resolving any intervening admissions before a repeat decision. A failed unit with **zero** durable migration commits can return to `SOURCE_OPEN` only after the full verification and still-valid original credentials. After any durable advance, the only routine migration transition is forward. Unknown or contradictory evidence leaves admission closed. The [cutover contract](contracts/cutover.md) defines how permits and ownership order transitions.

## Admitted work and unresolved outcomes

A non-reusable permit belongs to an invocation, target, generation, service and epoch. It spans the handler/response or the whole job attempt. It grants no clinical scope. Short shared/exclusive row locks order initial admission against closure. A separate session lock tracks lifetime without holding a clinical transaction during network I/O.

For effects spanning transactions or an external system, keep a private durable pending reference before dispatch. Include an opaque attempt ID, target/epoch, service and invocation class, correlation with the existing receipt or job/lease generation when available, and the independently verified disposition. References can be access-restricted on the target. Public status and CI reports expose only sanitized results. A lost acknowledgement, missing correlation or expired lease cannot transition a pending attempt to settled. For a generic poll whose claim is initially unknown, preserve the poll reference and correlate the resulting attempt under the fence. If that is impossible, block recovery. Routine cleanup cannot erase pending evidence.

## Committed revision and retained evidence

Exactly one visible, expected revision on the intended target is required. Missing metadata, zero rows, multiple rows, unreadable state, unknown or newer revisions fail closed. A committed intermediate belongs to its 0040-origin event. The approved path/role changes are enumerated in [research](research.md). A journal entry or client return value cannot advance the observed revision.

| Evidence set | Retained keys, relationships and comparisons |
| --- | --- |
| Clinical and operational records | Organization-qualified keys/FKs, distinct staff roles and live state, Patients, Appointments, numbering/counters and history, exact EGP snapshots, Visits and positive actual-actor completion attestations. Compare values and links in both clinics. |
| Follow-up and policies | Source Visit and actor/role, decision and policy revision, confirmed claim, dated exception/extension and its audit. Preserve unknown historical provenance, frozen prices, Doctor precedence and the original receipt result. |
| Command effects | Receipt tenant/actor/operation scope as defined by that command, original idempotency key/digest/result, matching audit/history and post-commit outbox. Check authorized replay, changed input and live denial independently. |
| Lease and transport | Pending and claimed job identifiers, tenant links, attempts, state, lease owner/generation, material presence, retry state and independently observed synthetic delivery count. Confirm known outcomes before destructive cleanup or new delivery. |
| F-01 evidence where present | Preserve legacy key/nonce pairs and their 0041 tombstones. Preserve private request, operation/resource, binding to database/login/backend/transaction/stage, activation and actual withdrawal evidence from the revision where each exists. A registered request and its consumed nonce are distinct from an as-yet unregistered envelope. Check original expiry and disposition without exposing keys. |
| Effective security | Compare expected before/after role graph, ACLs, owners, RLS/FORCE RLS, policy/trigger definitions and scoped behaviors against each published revision. Expected grant transfers count as intended changes. Unexpected drift blocks progress. |

A committed receipt keeps its original result and live replay restrictions. A rolled-back domain command has no successful domain, audit or notification effects. Independently committed nonce evidence can legitimately outlive it. An unknown binding remains retained, with no derived authority to clean up or impersonate a staff withdrawal.
