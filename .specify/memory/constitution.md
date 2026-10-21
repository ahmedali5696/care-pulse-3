# CarePulse 3 Constitution

## Core Principles

### I. Simplicity and Maintainability

Engineering MUST use the smallest correct, secure, maintainable solution. Apply KISS and YAGNI:
reuse existing project patterns and platform capabilities before adding dependencies, services,
frameworks, layers, or abstractions. Additional complexity MUST be justified by a concrete approved
requirement; speculative flexibility and duplicated business logic are prohibited.

Code MUST be readable, cohesive, strongly typed, clearly named, and separated by responsibility.
Preserve the approved Modular Monolith and domain module ownership: FastAPI owns clinical business
logic; the Next.js BFF/UI and worker MUST respect the approved contracts and reuse that logic.
Reduce indirection without weakening module boundaries or required behavior.

### II. Security and Data Integrity

Protected operations MUST enforce default-deny authorization, live account/role/tenant checks,
least privilege, and Organization isolation across APIs, database access, files, exports, jobs,
and realtime delivery. Validate untrusted input at authoritative boundaries. Enforce approved
field permissions, RLS, tenant-qualified relationships, and immediate access revocation.

Secrets MUST remain outside source control, logs, and client-visible output; development and
production credentials/configuration MUST be separate. Clinical data MUST remain private,
with authorized access, protected storage, safe logging, and the approved file-security controls.

Critical commands MUST preserve approved state machines, transaction atomicity, concurrency
guards, idempotency, immutable history, and durable audit. Failures MUST leave no partial domain
changes or uncommitted notifications; publish through the approved post-commit mechanism.
Missing required authorization or security policy MUST fail closed. Simplicity and performance
MUST NOT weaken security, integrity, validation, or error handling that prevents data loss.

### III. Pragmatic Performance

Implementation MUST use efficient database access and avoid unnecessary queries, data transfer,
processing, and lock duration. Keep external network/file work outside critical transactions.
Preserve responsive, accessible workflows and the approved Arabic RTL/English LTR behavior.
Optimization complexity MUST address a measured or otherwise demonstrated meaningful bottleneck,
with evidence of improvement and preserved correctness; do not invent performance targets or
add speculative caches, services, or infrastructure.

### IV. Code-First Testing and Evidence

Functional work MUST implement production functionality first, then add focused tests and run
the relevant checks. TDD, test-first development, and red-green-refactor cycles are not required
and MUST NOT be imposed by agent workflows. Tests MUST cover essential behavior, critical
regressions, security boundaries, and concurrency risks using a small, deterministic,
maintainable suite. Use real PostgreSQL for persistence, RLS, and races as required by the plan;
avoid excessive mocks, redundant coverage, and unnecessary test infrastructure. Valid tests
MUST NOT be weakened merely to pass checks.

Before reporting a task complete, the executing agent MUST independently inspect the actual
changes against authorized scope, dependencies, affected consumers, and acceptance criteria;
run relevant targeted verification; and inspect failures and unintended changes. Required
task/Wave integration checks remain binding. Documentation-only changes need document checks,
not unrelated application tests. Report the task ID where applicable, changed files, commands,
concrete results, and limitations. Distinguish IMPLEMENTED, TESTED, INTEGRATION VERIFIED, BLOCKED, and
PRODUCTION READY using the plan's evidence requirements; never infer completion or readiness
from code presence, delegated claims, or unexecuted checks.

Docker is unavailable on the Product Owner's workstation and MUST NOT be installed or required
locally. Run available container-free checks locally; required Compose, real-PostgreSQL and other
container-dependent verification MUST run in Docker-capable CI with recorded evidence. Distinguish
INTEGRATION VERIFIED from local-only checks; missing CI evidence remains BLOCKED/UNVERIFIED.

### V. Focused Agent Execution

Agents MUST minimize steps, touched files, and tool usage while completing the authorized work
and its verification. Temporary, scratch, duplicate, or auxiliary files require a genuine task
need; unnecessary documentation and artifacts outside established workflows are prohibited.
Preserve unrelated work. Fix shared causes and affected consumers without expanding scope.

After two unsuccessful correction attempts on the same issue, STOP and report the failing
command or reproduction, expected versus actual result, evidence, likely cause, and recommended
action. Do not continue speculative rewrites or weaken checks to escape the blocker.

## Authority and Scope

The approved authorities govern in this descending precedence; all paths are repository-relative:

1. `docs/CarePulse_PRD_v1_APPROVED.md` — product decisions and acceptance outcomes.
2. `docs/CarePulse_Domain_Model_State_Machines_v1_2_APPROVED.md` — domain invariants and ownership.
3. `docs/CarePulse_System_Design_v1_APPROVED(1).md` — architecture and technical constraints.
4. `docs/implementation-plan.md` — task IDs, dependencies, owning Waves, contracts, and evidence.

This Constitution governs engineering practice within those authorities. It MUST NOT override
approved product decisions, architecture, or execution scope. Agents MUST follow authorized task
IDs, prerequisites, Wave ownership/boundaries, and explicit Product Owner assignments; dependency
readiness alone grants no execution permission. Do not expand scope, implement future Waves,
alter approved authorities, or turn unresolved decisions into requirements. Escalate conflicts
that block authorized work instead of resolving them by assumption.

## Approval Boundaries

The Product Owner explicitly approved **SECURITY-POLICY-TIMING-VARIANCE on 2026-10-21** for
synthetic-data development/testing with clearly labeled test-only values. This records the
authorization supplied with this Constitution, superseding only the plan's earlier pending
status for that variance; it neither edits the plan nor approves production policy values.
Test values MUST NOT become recommended thresholds, production defaults, or authorization for
real-user/real-patient operation. Production MUST fail closed when required approved policy is
absent; framework defaults are not a substitute.

**SECURITY-POLICY-NUMERICS, OPEN-OPS-01, OPEN-OPS-02, OPEN-OPS-03, and OPEN-FILES-01 remain OPEN.**
Their required owners, approvals, evidence, and capability-specific production restrictions
remain as defined in the authorities. Non-billed pilots do not bypass these gates. No production
security numerics, legal conclusions, or release approvals may be invented.

Constitution adoption authorizes no specification, planning, implementation, migration,
deployment, or future commercial scope. Each requires its applicable explicit authorization.

## Governance

Changes to this Constitution require an explicit Product Owner-approved amendment with its
reason, affected principles, and compatibility impact recorded in the existing review workflow.
An amendment cannot change a higher authority indirectly; obtain that authority's separate
approval where needed. Use semantic versioning: MAJOR for incompatible principle changes or
removals, MINOR for added or materially expanded guidance, PATCH for non-semantic clarification.
Preserve the original ratification date and update the amendment date when content changes.

Task and change reviews MUST check these principles, justified complexity, scope, and evidence
before completion; use existing task/PR records rather than creating duplicate governance
artifacts. Unresolved violations MUST be reported as blockers, not self-approved exceptions.

**Version**: 1.1.0 | **Ratified**: 2026-10-21 | **Last Amended**: 2026-10-21
