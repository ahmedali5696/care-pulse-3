# CarePulse restricted operator and email delivery runbook

This procedure is for isolated **synthetic-data development and CI**. There is no approved production security-policy timing, credential custody, key rotation/backup, SMTP provider, or clinical-data release gate. Never run these commands against real clinic/patient records.

## Prepare restricted credentials and keys

Operations provision an individually issued high-entropy operator credential and enable its digest-bound identity in the restricted `operator_identities` authority. The operator DB password and the person's credential are **different** files; operator CLI checks both. The separate `carepulse_sender` login receives only nonclinical claim and delivery metadata grants, enforced by RLS. The migration/bootstrap login and passwords do not belong in operator, sender, web, API or worker containers.

Create ignored, access-controlled files outside source control and set their **host paths** in local Compose variables. Required filenames/variables are shown in `.env.example`; active synthetic runs also require `CP_OPERATOR_DB_PASSWORD_SOURCE_FILE`, `CP_OPERATOR_CREDENTIAL_SOURCE_FILE`, `CP_SENDER_DB_PASSWORD_SOURCE_FILE`, `CP_DELIVERY_PUBLIC_KEY_SOURCE_FILE`, `CP_DELIVERY_PRIVATE_KEY_SOURCE_FILE`, `CP_DELIVERY_KEY_ID`, `CP_DELIVERY_PRIVATE_KEY_FILES`, `CP_MAIL_BASE_URL`, `CP_DELIVERY_LEASE_SECONDS`, `CP_DELIVERY_ATTEMPT_TIMEOUT_SECONDS`, `CP_DELIVERY_MAX_ATTEMPTS` and `CP_DELIVERY_RETRY_SECONDS`.

`CP_DELIVERY_PRIVATE_KEY_FILES` is a private sender-only key-ID mapping, for example `synthetic-v1=/run/secrets/delivery_private_key`; `CP_DELIVERY_KEY_ID` selects the corresponding public issuer key. Public RSA-3072 PEM is exposed only to authenticated issuers; private PEM is mounted only into the disposable sender container (both its Python launcher and Node adapter have access). Publish the new public key first and retain old private key IDs/mounts until their queued jobs can no longer be delivered. The key ring can be expanded explicitly during rotation; never store PEM in environment variables, image layers, CLI arguments or application output.

Synthetic sender values must be explicit: positive `CP_DELIVERY_LEASE_SECONDS`, shorter positive `CP_DELIVERY_ATTEMPT_TIMEOUT_SECONDS`, positive `CP_DELIVERY_MAX_ATTEMPTS`, and positive `CP_DELIVERY_RETRY_SECONDS`. All values and retries need production owner sign-off later. `CP_MAIL_BASE_URL=http://127.0.0.1:3000` supplies only the controlled synthetic origin; SMTP is restricted to Mailpit `mail:1025`. The only external host port remains loopback-bound web port 3000. The sender connects to private DB and Mailpit networks; the operator connects to DB only.

## One-shot operator commands

Run from the repository root in a prepared synthetic Compose project, substituting **only metadata** arguments such as UUIDs, ordinary names, revisions, reasons and idempotency keys. Never put operator credentials, proof tokens, private keys or passwords into command arguments/shell history. `ops/carepulse-admin` is the direct one-shot CLI entrypoint; its Docker-equivalent commands are:

```sh
docker compose --profile operator run --rm operator clinics create --operator-id <operator-uuid> --branch <branch-name> --owner-email first-owner@example.invalid --idempotency-key <request-uuid> --reason <reason>
docker compose --profile operator run --rm operator clinics list --operator-id <operator-uuid> --reason <reason>
docker compose --profile operator run --rm operator clinics invite --operator-id <operator-uuid> --clinic-id <clinic-uuid> --invitation-id <prior-invitation-uuid> --expected-lifecycle-revision <revision> --expected-invitation-revision <revision> --reason <reason>
docker compose --profile operator run --rm operator clinics revoke-invite --operator-id <operator-uuid> --clinic-id <clinic-uuid> --invitation-id <invitation-uuid> --expected-lifecycle-revision <revision> --expected-invitation-revision <revision> --reason <reason>
docker compose --profile operator run --rm operator clinics suspend --operator-id <operator-uuid> --clinic-id <clinic-uuid> --expected-lifecycle-revision <revision> --reason <reason>
docker compose --profile operator run --rm operator clinics reactivate --operator-id <operator-uuid> --clinic-id <clinic-uuid> --expected-lifecycle-revision <revision> --reason <reason>
```

An authenticated `create` commits a **PROVISIONING** tenant, first-Owner invitation digest, encrypted delivery material, audit, delivery job and operator-scoped idempotency receipt in one transaction. Same valid key/body returns the committed safe metadata. Changed-body retry conflicts. Reissue atomically revokes/supersedes the previous invitation, requiring both expected revisions; revoke makes its proof unusable. `list` and all actions require an authenticated, enabled operator identity and reason. Output contains permitted identifiers, state and revisions, not proofs, ciphertext, passwords, auth sessions or patient data. Operator metadata-only role cannot SELECT patient records or other clinical tables.

After commit, take the resulting `organization_id`; invoke the **same** one-shot D-09 sender used by future B-04 recovery and B-25 scheduling. A command without a job ID claims at most one eligible due/reclaimable job in that tenant. For authorized integrations which already know the committed job ID, pass optional `--job-id <delivery-job-uuid>` to target it:

```sh
docker compose --profile sender run --rm sender --organization-id <clinic-uuid>
```

Start the optional Mailpit profile when sending synthetic messages (`docker compose --profile mail up -d mail`). Sender claims under a transaction and commits its fresh owner/generation/lease before reading protected delivery material and calling Node. Node decrypts in memory, compares the committed verifier digest, creates an allowlisted same-origin invite/reset URL with the proof in its fragment, and sends through Mailpit. Sender stdout is only a sanitized outcome such as `SENT`, `READY`, `FAILED`, `STALE` or `NOT_DUE`, never a link. Mailpit synthetic mailbox message contains the intended proof URL for browser invitation acceptance; use the verified-email BFF acceptance and credential setup, then test normal login. Only the single successful first-Owner acceptance activates the clinic. Suspension revokes effective access immediately; reactivation requires an already ACTIVE-before-suspension clinic with a currently valid verified Owner and its latest expected revision. Suspension and reactivation preserve historical clinic data.

## Rollback, failures, and explicit reclamation

Failure or rollback during creation/reissue leaves **no new committed delivery job** and must never send email. Failure after commit retains the same logical job/proof; never mint/reissue proof solely to retry SMTP. A rejected/missing key, policy, DB or Mailpit configuration fails closed. A usable proof can be sent only after current claim, owner/generation, unexpired lease and proof state have been rechecked. Retryable failure returns job to `READY` with an explicit not-before delay; exhausted/unusable jobs are marked `FAILED`/`CANCELLED` and ciphertext is disposed after terminal transition. `SENT` also disposes ciphertext. Any stale finish/retry/disposal affects zero rows.

There is no active scheduler, heartbeat or implicit sweep. A crash **before** claim commit leaves a due `READY` job. A crash **after** claim commit but **before** send leaves `CLAIMED`: invocation **before** its lease expires returns `NOT_DUE`, invocation **at or after** expiry may reclaim a fresh owner/generation. A paused old sender must not finalize, reschedule, delete material or send another message after detecting lost ownership; only the live generation can finish. Crash/lost SMTP acknowledgement **after** remote acceptance may cause a later explicit reclaimer to send another email using the **same** job/proof; SMTP delivery is at least once under those conditions, while invitation/reset consumption remains single-use. Expired/revoked/consumed proofs are cancelled without SMTP. Confirm synthetic evidence and Mailpit mailbox, using no request-body/secret dumps or delivery-link logs.

Once the production security policy, custody/rotation, retention, transport and disaster recovery choices are approved, update release-gate evidence and procedures before any real-user activation. No production defaults are implied by this runbook.
