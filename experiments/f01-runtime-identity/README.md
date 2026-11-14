# F-01: isolated runtime identity feasibility proof

Baseline: `development@6cf2973c07f6e0ef2d8040d1eb3c235fd1598df7`.
Experiment branch: `experiment/f01-runtime-identity`.

This is a disposable Option C database experiment, not an application migration,
production policy, or Foundation acceptance run. It uses synthetic identities,
credentials, key bytes, and a 60-second freshness / 5-second future allowance.
There is no real clinic data or deployment. Do not run setup against any existing
CarePulse database.

`setup.sql` installs a protected verifier and gate, separate actual database logins,
transaction binding/activation, minimal live authority, FORCE RLS, and narrow
effects. `node_vector.cjs` creates a Node-signed vector; the Python checks verify
the identical bytes in PostgreSQL and exercise the nine criteria. Ordered races
require an actual `pg_blocking_pids` edge before releasing the blocker. The pool
check uses an actual SQLAlchemy pool with one physical connection.

## Reproduction and evidence

The isolated workflow `.github/workflows/f01-runtime-identity.yml` runs on pushes
to this experiment branch only. For an existing run, use `gh run rerun RUN_ID` to
reproduce the same commit and `gh run view RUN_ID --log` to inspect its evidence.
The job verifies the exact checkout SHA and baseline ancestry, rejects changes
outside its own paths, checks the PostgreSQL digest against `compose.yaml`, prints
the image identity, runs both test scripts using the existing locked Python
dependencies and pinned Node/Python images, and verifies disposable cleanup.
The required evidence is the actual CI result/log (not merely the presence of
these files).

## Boundaries

The database proof signs the canonical 16-field UTF-8 envelope with a separate
DB key and domain separator. The HTTP key ID remains the replay namespace through
DB-key rotation. A gate-committed stage record binds database/login OIDs, backend
PID and top-level `xid8`; live activation happens inside the business transaction.
Retiring keys accept only authenticated issuance timestamps strictly before the
cutoff, at both registration and activation, within the existing freshness window.
The verifier cannot establish the physical signing time of a backdated claim;
the trusted signer must stop issuing with the retiring key. Rolling back to a
pre-activation savepoint permits reactivation of the same binding only.
Neither caller GUCs nor SET ROLE establish identity. A synthetic provider-invite
credential and session rows stand in for external Better Auth/email flows; no
production auth adapter, existing writer or migration is modified. The dummy
patient note and idempotency scope are intentionally small single-operation
fixtures. Proof-key provisioning, BFF signer rollout, real session/email
integration, all production data/operation mappings, workload costs, rollout
and G3 acceptance remain outside this experiment.
