# F-01 runtime identity hardening (#26)

Status: implementation checkpoint; not accepted or authorized for operational cutover.

## Boundary and dependencies

The implementation targets the approved refined Option C and takes the feasibility experiment at `03a84e1f7853fcec553257646a9ae1969f323c59` (CI `43629974397`) as evidence of feasibility, not acceptance of this implementation. The experiment's files and secrets are not merged.

API, web, worker and gate use distinct database logins. The independent database HMAC proof binds the signed request, while the gate binds the validated identity to a particular database login, backend and transaction. Protected keys and nonce records belong to a dedicated non-login authority owner. The legacy shared runtime's clinical grants are revoked; it is not an application fallback. Existing privileged nonclinical roles must remain confined to their existing narrowly scoped duties.

The intended guarantee is identity and fixed operation/resource isolation, not full SQL-to-HTTP command equivalence. A valid signed operation does not establish that arbitrary command arguments are legally or clinically valid. Existing domain constraints and signed application validation continue to apply.

Migrations 0041–0044 are forward-only. Revision 0044 preserves the restricted delivery-job projection, including denial of sender lease ownership to the API, and retains expired requests with bindings whose transaction outcome cannot be established. Existing readiness revision checks identify the new migration head; general schema compatibility and cross-version deployment fencing remain F-02. Permission matrix alignment remains F-03; F-01 does not authorize new product permissions. Neither dependency may be bypassed by restoring legacy GUC authorization or enabling the retired runtime credential.

## Custody and operational boundaries

The synthetic bootstrap provisions a generated identity key and policy from test configuration. Do not use it for production. Production provisioning, rotation, issuance cutoffs, secret distribution and access review require an approved custody procedure and policy values. Retiring keys cannot authorize issuance beyond their cutoff. Nonce tombstones are retained to prevent replay after request cleanup, with storage growth requiring operational review. Clock regression, expiry and cleanup must be verified against PostgreSQL before acceptance.

## Acceptance

This checkpoint is not evidence of executable acceptance. Require ordinary Foundation CI on the exact reviewed SHA, including real-login ACL and impersonation denials, valid signed commands, live revocation, transaction/pool isolation, replay tests, and fresh/repeated/populated/fault-injected migration checks. Legacy runtime fixtures must be migrated to the new boundary, or historical-only migration evidence must be isolated at the actual historical revision without weakening current-head gates. The protected T133 procedure remains outside this authorization.

The ordinary runner uses an immutable 0040 source archive in a separate disposable cluster for explicitly selected predecessor writer contracts. Current-head fixtures may seed or inspect schema state as migration maintenance; supported product operations must exercise signed runtime transactions. Current-head denials cover unsupported queue/admission, terminal Visit, clinical mutation, and day-review writer paths while their original predecessor assertions remain mandatory. This separation does not establish current-head functionality for writers whose product routes are outside the present scope.

Independent Security/Clinical review and Product Owner acceptance are required after exact-SHA evidence; no issue closure, merge, operational cutover, deployment, release or Wave 3 is authorized by this checkpoint.
