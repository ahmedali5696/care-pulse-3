# Deferred Findings

Track material unresolved findings and production-readiness obligations in GitHub Issues. Search for an existing Issue before creating one.

Record the risk, impact, evidence, responsible role, review trigger, and verifiable closure criteria. Link relevant Issues from existing task evidence when appropriate.

Before declaring an authorized scope complete, report relevant open Issues and distinguish immediate blockers from deferred or production-only work.

Do not implement deferred work, modify approved decisions, or close Issues without verified evidence and appropriate authorization.

## Mandatory Code Quality & Testing

- MUST load and follow `clean-code-guard` and
  `test-guard` before implementing or modifying code.
- Apply both skills throughout implementation and perform
  a final self-check before delivery or any authorized commit/push.
- Report verification results and unresolved findings.

## CI workflow selection

- Use Fast Feedback for routine development checkpoints.
- Require Full Foundation for integration acceptance at the exact tested SHA.
- Never substitute Fast Feedback or diagnostic success for Full Foundation acceptance.
- Rerun all Full Foundation jobs when required; never combine evidence from different attempts.
- Verify mandatory tests actually execute with zero skips.
- Follow docs/postgresql-ci.md for workflow selection, acceptance evidence, and diagnostic procedures.
- MUST read and follow the permanent scalability rules in `docs/full-foundation-scalability.md` when planning, implementing, assigning, or reviewing integration tests and CI evidence for every Master.

## CI failure triage

- Record the failing test or step, run, and exact SHA. Classify the failure as application, test, or environment, and establish the cause from logs, a reproduction, or observed transaction state before changing code.
- Run the smallest supported check first. For PostgreSQL, dispatch Foundation with a listed `postgres_diagnostic` selection, then run full Foundation at the final executable SHA for acceptance. See [PostgreSQL CI diagnostics](docs/postgresql-ci.md).
- After two speculative corrections for the same failure, stop guessing. Require new root-cause evidence before another correction. Continue justified diagnosis, scoped corrections supported by that evidence, and required CI retries.
- Prove ordered races with transaction checkpoints and PostgreSQL lock observations. Never mask failures with retries, skips, relaxed assertions, or arbitrary timeout increases.

<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->
