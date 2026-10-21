# Wave 0 validation guide

**Date:** 2026-10-21 | **Scope:** F-01–F-07 | **State:** prospective run guide; commands require separately authorized implementation

No application, lockfiles, Compose configuration or check runner is delivered by planning. The commands below define the intended interface, **not checks run during planning**. No Docker installation or local Docker command is required or permitted on the Product Owner's workstation.

## Prerequisites and safety

1. Product Owner **approved the exact R1–R4 inputs and conditional R7 synthetic-development recommendations on 2026-10-21**; obtain separate explicit assignment/authorization for each F task. Keep approved Next.js `15.5.28`; do not substitute a newer version to satisfy an advisory or peer conflict. GHSA-4jqv-mc3x-m676 and GHSA-mcj8-r9mp-w47p are patched from `15.5.27`, so `15.5.28` is outside their affected ranges and those two are not blockers. ESLint 9.39.5 is temporarily authorized **only for synthetic development/CI**, with Security/Operations production disposition deferred until before deployment. Other security/advisory checks and full locked transitive verification remain required during F-01/F-06.
2. Use the approved Python/Node/pnpm versions, not merely the globally installed ones. Current workstation tools are observations, not approved pins. Existing `.venv`/`node_modules` content is not reproducibility evidence; preserve it and use a clean isolated environment for later checks.
3. Use synthetic data only. Supply separate ignored test secret files and the full, explicitly labeled synthetic security-policy fixture through the F-03 configuration interface. Never copy fixtures to production, print keys/configuration, or use real operator credentials/email addresses.
4. CI needs a Docker-capable Linux runner, registry access, the recorded Compose tool version, and an isolated checkout/project/database. Do not execute this guide against a shared or production database. CI reports its run URL, Git revision, logs and sanitized evidence artifacts.
5. Install and configure tools only in the later authorized implementation; this planning session performs documentation checks only. Runtime installs must use approved frozen/hash locks; lock generation and migration execution are not part of planning.

## Container-free workstation checks

From repository root in the workstation's existing **Windows PowerShell 5.1**, after implementation supplies the files; do not install or require `pwsh` locally:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.lock
.\.venv\Scripts\python.exe -m pip check
pnpm install --frozen-lockfile
```

`requirements.lock` is the proposed single exact direct/transitive development/test lock for Wave 0; API/worker use the same pinned package set, without separate drifting locks. The pinned/hash-verified pip bootstrap follows Python's security instructions and must precede dependency downloads, including in CI/container builds; it is not a floating auto-upgrade. `packageManager` pins pnpm; the environment must provision that exact version before running it. Do not auto-upgrade pip, dependencies or Next.js. Exact installer/tool versions and selected platform artifacts are recorded by F-01/F-06.

The later F-06 implementation exposes **one small `ops/check.ps1` entrypoint**, compatible with Windows PowerShell **5.1** locally and PowerShell **7** in Linux CI, with one command per check; do not add duplicate wrapper layers. Keep syntax/cmdlets compatible with 5.1 and check `$LASTEXITCODE` immediately after every native command, terminating nonzero on failure. `$ErrorActionPreference` alone does not propagate native failures in both shells; do not depend on PowerShell 7-only native-error preferences.

| Command (prospective) | Expected local outcome |
|---|---|
| `powershell.exe -NoProfile -File .\ops\check.ps1 inputs` | Runtime/tool versions and exact direct pins match the approved input record; lock integrity/peer checks pass; reject floating/unapproved inputs. Does not need Docker locally. |
| `powershell.exe -NoProfile -File .\ops\check.ps1 python-quality` | Ruff formatting check and lint pass; no redundant formatter/linter stack. |
| `powershell.exe -NoProfile -File .\ops\check.ps1 unit` | Focused deterministic Python configuration/transaction-lifecycle unit checks pass without PostgreSQL; no mocked test claims database isolation proof. |
| `powershell.exe -NoProfile -File .\ops\check.ps1 web` | Next.js exact pin, TypeScript no-emit, ESLint and production build pass using frozen inputs. Build uses no operational secrets or DB mutation. |
| `powershell.exe -NoProfile -File .\ops\check.ps1 imports` | API import and worker module import/startup smoke pass without launching job processing; configuration validation is tested separately, never bypassed for a protected runtime. |

If a required local interpreter/tool is unavailable, report that check blocked; do not require Docker as a workaround. Linux-only build/platform proof remains CI-owned. Unit tests are added **after production implementation**, not a red-first prerequisite.

### Intentional fault evidence — F-06

In a disposable checkout after the focused tests exist, introduce one known failing assertion into a single unit test, run `powershell.exe -NoProfile -File .\ops\check.ps1 unit`, and record its nonzero exit and named failing case. Remove only that intentional change, rerun, and show green. Never weaken an existing assertion, inject a production bypass or commit the fault. This tests check failure propagation after implementation; it is not TDD.

## Docker-capable CI only

T010 creates the initial workflow using direct commands for delivered F-01/F-02 functionality and supplies the first mandatory Linux/container build and runtime/platform/image evidence. F-01 acceptance uses verified inputs and container-free local checks; this staging preserves every original master acceptance requirement, with Linux/container proof still mandatory at T010 and reverified by F-06. T016/T021/T026/T031 extend the same workflow only with delivered owner checks; no earlier acceptance waits for the future F-06 runner, migration or health endpoint.

Once F-06 supplies the runner, in CI's **PowerShell 7**, use `pwsh -NoProfile -File ops/check.ps1 <check>` for the same local-capable check names above, followed by one integration invocation. `pwsh` is a CI runner tool, not a workstation prerequisite:

```powershell
pwsh -NoProfile -File ops/check.ps1 integration
```

F-06's integration mode runs one Compose topology with disposable real PostgreSQL, the approved container pins and a test target containing the existing pytest tooling. It activates `mail` for F-07 checks; `scan` remains optional. Keep one workflow with a container-free job and one dependent Compose integration job; no OS/database/version matrix, parallel database harness, extra frontend test framework or production deployment job.

The integration command must provide these observable stages (standard underlying commands shown for review; **CI only**):

```text
docker compose --profile mail config --quiet
docker compose --profile mail build
docker compose up -d db
docker compose run --rm api alembic upgrade head
docker compose --profile mail up -d --wait
docker compose run --rm api python -m pytest tests/integration
```

Migration invocation uses the explicitly scoped migration credential, **not** the API runtime service credential. The check runner supplies a narrowly scoped one-shot test/migration override; normal API/web/worker containers never receive DDL credentials. No migration-on-web-startup hook. Compose commands use a unique CI project name and ignored/CI-mounted secret files. Execute host-published web-port response and localhost API/DB denial checks directly on the CI runner; execute private service connectivity checks from the network-attached test container, using the expected FastAPI 404 before F-05 health exists. Record each probe's vantage point. These checks prove absent host-published API/DB endpoints, not that Docker bridge networking prevents privileged host access. Readiness checks execute from the appropriate private network. Never echo fully interpolated config, environment, connection URLs or secret files.

### Required scenarios and acceptance evidence

| Owner / prerequisite | Scenario | Required result and retained evidence |
|---|---|---|
| F-01 / none | Verified inputs; clean container-free frozen frontend and hash-locked backend install; build/import smoke | Verified approved interpreter/DB/image selections, exact Next/React/pnpm/Python/dependency inputs, strict peer checks, local resolved versions/lock hashes and type/lint/build/import results retained. Acceptance does not wait for CI or claim container integration; mandatory Linux/container build/runtime/platform/image evidence follows at T010 and F-06 revalidation. |
| F-02 / F-01 | Clean boot and private connectivity; initial Linux/container proof at T010 | CI runner probes the host-published web entrypoint and denied localhost API/DB endpoints; network-attached test container verifies private web→API connectivity using expected FastAPI 404 before F-05, not a nonexistent health endpoint. Retain each probe's vantage, resolved port/network membership, named DB/file persistence and initial Linux build/runtime/DB/platform/image identities. No claim that Docker bridge networking prevents privileged host access. |
| F-03 / F-01, F-02 | Omit each required key/policy; inject test fixture into production config | Protected process refuses startup with sanitized diagnostic/nonzero exit, no credential value. Source, browser bundle, build context/image layers and logs pass targeted secret-canary checks; use synthetic canaries, never real secrets. Positive complete synthetic config starts. |
| F-04 / F-02, F-03 | Upgrade empty DB; prove transactional failure; reset a disposable empty DB and upgrade again | One expected Alembic head; failed migration does not advance version or leave partial objects; both fresh upgrades agree. Disposable reset is explicitly scoped. Runtime and operator metadata roles cannot DDL or acquire privileged role membership. |
| F-04 / F-02, F-03 | Reuse one pooled connection across commit, rollback and SQL error | New transaction has no effective previous organization context; no session-wide SET. No clinical table or full RLS proof claimed. |
| F-05 / F-02, F-03, F-04 | Healthy, DB unavailable, wrong migration revision, enabled dependency failure, probe timeout, recovery/restart | Liveness remains process-only; finite readiness dependency deadlines return 503/nonzero on failure or timeout and recover when prerequisites recover. Reject missing/stale/mismatched worker identity/heartbeat state, reject old-process status after restart and accept fresh matching state; check timing boundaries with controlled time where practical per [operational timing rules](data-model.md#worker-status--f-05). Disabled optional services do not fail unrelated readiness. All status output satisfies [health contract](contracts/foundation.md#2-health--f-05). |
| F-06 / F-01–F-05 | Run each check; mutate a dependency/image input in disposable checkout | Valid approved inputs pass; unapproved/floating/lock-inconsistent inputs fail. Evidence includes reproducible install/container/real-DB run and deliberate fault failure. Advisory report cannot silently update approved pins. |
| F-07 / F-02, F-03 | No-session request and forged headers/cookie to browser boundary; private API calls without/with invalid service key | BFF 401 with zero passthrough; API 401 for absent/invalid key. Correct service key alone still cannot perform business work. Unsupported candidate sessions cannot open a route. Signup absent, no operational login bypass; auth DB/cache configuration inspected without generating D-02 schema. |
| F-07 / F-02, F-03 | Exercise reserved invitation/reset template transport with private smoke harness | Both synthetic messages visible via internal Mailpit API; correct kind/recipient, non-operational links; no production relay fallback or public send route. Mail failure fails mail check. |

Lifecycle checks follow their actual prerequisites. F-07's dependency list is **only F-02/F-03**: validate its configuration/denials/mail independently; the final all-Wave CI run may run later without imposing F-04/F-06 as new implementation dependencies.

Persist a marker in a synthetic named database/file volume and restart services without volume deletion to prove development persistence. Any final volume cleanup may target **only the unique disposable CI project**, after evidence capture. Do not use broad/pruning commands or `down -v` against a developer/shared environment.

## Reporting and stop conditions

For each F task report files, applicable refs/migration IDs, exact commands, positive/negative results, CI run URL/artifacts and limitations. Record `IMPLEMENTED`, `TESTED` and `INTEGRATION VERIFIED` independently; no CI evidence means integration **BLOCKED/UNVERIFIED**, not passed. After two failed corrections of the same issue, stop with reproduction and likely cause.

Wave 0 is complete only with **all seven** task evidence sets plus upstream §5 exit/§13.3 W0 checks. D-01 is merely dependency-ready after F-04, not authorized. OPEN-OPS-01/02/03, OPEN-FILES-01, SECURITY-POLICY-NUMERICS and production mail/release approvals remain open. No production or later-Wave workflow follows this guide automatically.
