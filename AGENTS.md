# Deferred Findings

Track material unresolved findings and production-readiness obligations in GitHub Issues. Search for an existing Issue before creating one.

Record the risk, impact, evidence, responsible role, review trigger, and verifiable closure criteria. Link relevant Issues from existing task evidence when appropriate.

Before declaring an authorized scope complete, report relevant open Issues and distinguish immediate blockers from deferred or production-only work.

Do not implement deferred work, modify approved decisions, or close Issues without verified evidence and appropriate authorization.

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
