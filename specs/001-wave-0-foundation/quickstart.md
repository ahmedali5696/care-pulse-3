# Wave 0 validation guide

**Date:** 2026-10-21 | **Scope:** F-01–F-07 | **State:** Wave 0 implementation started; commands not yet validated unless recorded below

This guide originated in planning; its prospective commands are not proof of a successful run. No Docker installation or local Docker command is required or permitted on the Product Owner's workstation.

## T001 — approved input baseline (2026-10-22)

The Product Owner approved these exact versions on 2026-10-21. **Input selection** is separate from lock/install/build/CI proof.

| Consumer | Approved direct inputs |
|---|---|
| Windows / CI / database | Node `24.22.1`, pnpm `10.34.7`, standard-GIL CPython `3.13.16`, pip `26.2.2`, PostgreSQL `17.11` |
| Web | Next `15.5.28`, React and React DOM `19.3.1` |
| Web tooling | TypeScript `5.9.3`, ESLint `9.39.5`, eslint-config-next `15.5.28`, @eslint/eslintrc `3.3.1` |
| Types | @types/node `24.19.2`, @types/react `19.3.0`, @types/react-dom `19.3.0`, @types/pg `8.23.1` |
| Browser server/auth/mail | better-auth `1.7.12`, pg `8.23.1`, nodemailer `10.1.7` (bundled types) |
| API / shared worker | fastapi `0.158.1`, uvicorn `0.54.0`, SQLAlchemy `2.0.55`, psycopg[binary] `3.3.6`, alembic `1.20.0` |
| Python checks | pytest `9.1.1`, ruff `0.17.1` |

The secured pip bootstrap, **before other dependency downloads in each fresh Python environment**, is the exact wheel `https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl`, SHA-256 `fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70`; verify pip reports `26.2.2` before installing the SHA-256-locked dependencies.

| Image consumer | Approved tag with index digest (Linux amd64 identity still requires CI proof) |
|---|---|
| Web | `node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205` |
| API / worker / tests | `python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d` |
| PostgreSQL | `postgres:17.11-bookworm@sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27` |
| Optional mail | `axllent/mailpit:v1.31.6@sha256:23fe39e81bde596a918000b3135c0f85dc8bb4c9ea6be274b051143f3e300387` |
| Optional scan | `clamav/clamav:1.5.4-debian13-slim@sha256:d7ede3cefeaf3b8a72ebcdee849d13f504bbca3f3b3cfade5bc7b6ee06a4f62c` |

Version/peer release metadata was reviewed in [research R1–R4](research.md) against upstream release, npm/PyPI and Docker registry records on 2026-10-21; the implementation additionally queried npm exact-version `next@15.5.28`, `react-dom@19.3.1`, `eslint-config-next@15.5.28` and `better-auth@1.7.12` on 2026-10-22. Peer ranges admit the approved React 19/TypeScript 5.9/ESLint 9/Next 15/pg 8 baseline. Actual Node/pnpm installs, Python hash resolution, final transitive graph, Linux platform pulls and PostgreSQL runtime remain independent evidence steps; source metadata is not runtime proof.

**Temporary risk exception:** ESLint `9.39.5` with Next `15.5.28` is approved for synthetic development and CI only. Keep real lint checks; obtain Security/Operations production disposition before deployment and review the installed transitive graph including `sharp` and bundled RSC. No production exception has been granted.

**CI-only input approval:** the Product Owner approved the exact T010 checkout SHA, runner label, PowerShell archive/hash, Docker Engine, Buildx and Compose plugin pins/installation method on 2026-10-22 (full record below). Do not use floating action refs or substitute unapproved CI tools. T010 must still verify the actual runner, Linux amd64 image identities and integration runtime; approval is not execution evidence.

### Local implementation evidence — T002/T004

- The workstation's `py -V:3.13` resolved **Python 3.13.16**. An isolated `.venv-wave0` was created without modifying global runtimes; the exact SHA-256 wheel bootstrap installed **pip 26.2.2** successfully. No other dependencies were downloaded before the secure bootstrap.
- `requirements.in` has the seven approved direct Python pins; `requirements.lock` contains **29 exact resolved distributions** with SHA-256 wheel hashes from the Windows amd64 and Linux amd64 CPython 3.13 resolutions. `psycopg[binary]` is included and `colorama` is Windows-gated. `pydantic` is a locked transitive dependency only; it has no direct source import yet. Wheel downloads and the Python environment are ignored under `.tools/` and `.venv-wave0/`.
- `python -m pip install --require-hashes --only-binary=:all: --no-index --find-links .tools/wheels/win -r requirements.lock` completed with exit 0 using the isolated interpreter; `python -m pip check` returned `No broken requirements found.` and the approved runtime imported FastAPI, Uvicorn, SQLAlchemy, psycopg, Alembic and pytest. `python -m ruff check apps/api apps/worker` passed; format check found all 13 Python files formatted. Offline wheels are a local verification aid, not a committed artifact; normal installs must use the same hash lock from the approved index.
- `PYTHONPATH=apps/api` supplies the shared `carepulse` package to both `uvicorn carepulse.main:app` and `python apps/worker/main.py` without copying it or creating a second installed package. FastAPI has zero API routes prior to the later Wave 0 health/auth owners; the worker has only an idle entrypoint. Linux real-runtime evidence remains T010/F-06 CI-only.

### Local web implementation evidence — T003/T005

- Isolated Node **24.22.1** was downloaded into ignored `.tools/node-v24.22.1-win-x64/` and its archive hash verified against the official `SHASUMS256.txt`: `d2046d28c2c3dcd211ac5145c0bf30e2047ef9dfb306693c8c6e2eccd8b95fa5`. Isolated pnpm **10.34.7** was used; no global Node/pnpm version was changed.
- `pnpm-lock.yaml` SHA-256 `e81b48c7ab50811e42a3eee46e58071a243d4f4c0ee873e0d91aa089285911a4`. The 14 direct dependency/tool/type pins match the approved exact input table. With strict peer/engine/package-manager rules, `pnpm install --lockfile-only` and `pnpm install --frozen-lockfile` both returned exit 0. The frozen install reported one ignored optional `unrs-resolver@1.12.2` build script; all required checks succeeded.
- `pnpm typecheck`, `pnpm lint` and `pnpm build` all returned exit 0 on the exact runtime; the build generated the minimal `/` route (4/4 static stages). ESLint's flat config excludes only Next-generated `.next/**`/`next-env.d.ts` files and retains Next core-web-vitals/TypeScript lint rules.
- Installed transitive `sharp@0.35.5` was confirmed. Production-dependency audit found **0 critical, 2 high, 2 moderate** advisories on transitive `postcss@8.4.31` through Next; the boot page processes no untrusted CSS. Treat these as pending Security/Operations risk evaluation before production, alongside the approved synthetic-only ESLint 9 EOL exception; do not auto-substitute approved pins. Bundled React Server Components require separate security review at F-06.

### F-01 local acceptance — T006 (2026-10-22)

**IMPLEMENTED and LOCALLY TESTED** (six of six F-01 tasks); **INTEGRATION VERIFIED: pending T010/F-06**; **PRODUCTION READY: no**. Clean locked installs were performed into freshly created isolated local environments, with no reliance on existing global Node/pnpm or Python dependency folders. `requirements.lock` SHA-256 `1e0d0bd292857372ba61269de4a76ed6ae20d7129817e4a9ff6b689a840ebafb`. Commands actually executed from the repository root in Windows PowerShell 5.1, with exit 0:

```powershell
& 'C:\Program Files\PyManager\py.exe' -V:3.13 -m venv .venv-wave0
& .\.venv-wave0\Scripts\python.exe -m pip install --no-cache-dir 'pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70'
& .\.venv-wave0\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: --no-index --find-links .tools/wheels/win -r requirements.lock
& .\.venv-wave0\Scripts\python.exe -m pip check
$env:PYTHONPATH = (Join-Path (Get-Location) 'apps/api')
& .\.venv-wave0\Scripts\python.exe -c "from carepulse.main import app; import apps.worker.main; print(len(app.routes))"
& .\.venv-wave0\Scripts\python.exe -m ruff check apps/api apps/worker
& .\.venv-wave0\Scripts\python.exe -m ruff format --check apps/api apps/worker
$env:PATH = (Join-Path (Get-Location) '.tools/node-v24.22.1-win-x64') + ';' + $env:PATH
$node = '.\.tools\node-v24.22.1-win-x64\node.exe'
$pnpm = '.\.tools\pnpm\node_modules\pnpm\bin\pnpm.cjs'
& $node $pnpm install --frozen-lockfile
& $node $pnpm typecheck
& $node $pnpm lint
& $node $pnpm build
```

The actual local FastAPI process returned the expected 404 for `/private-probe`, and the separate Python worker process remained alive in idle mode. A fresh independent read-only Python lock/shared-package review found **0 BLOCKER / 0 MAJOR / 0 MINOR**; it checked SHA-256 binary wheel closure for both approved platforms and 29/29 transitive distributions with `pip --dry-run` and metadata. Linux installation, image digest/platform execution, PostgreSQL 17.11 runtime and network/CI evidence remain mandatory at T010/F-06, and none is claimed from the dry-run or published image metadata. No deployment or external CI run occurred during F-01.

### F-02 implementation evidence — T007–T009 (2026-10-22)

**IMPLEMENTED and STATICALLY CHECKED**; container/network/DB/integration verification **BLOCKED/UNVERIFIED** until T010 CI. `apps/web/Dockerfile` and `apps/api/Dockerfile` use the approved image tag+digest pins; the Python build verifies pip 26.2.2's published SHA-256 bootstrap before any dependency download, then installs only hash-checked wheels. The API, worker and CI test target use one Python source and lock; neither application runtime performs DDL or migrations. `.dockerignore` excludes local installations, caches, credentials, images of secrets and other unnecessary context files.

`compose.yaml` defines web, API, worker and PostgreSQL; `mail` and `scan` are optional profiles and `test` is the non-public CI target. Web alone publishes `127.0.0.1:3000:3000`; no database/API/worker/mail/scanner ports are published. `database` is internal; `application` is the private bridge, with IP masquerading disabled; a third, **scanner-only, opt-in** `signature_updates` bridge gives ClamAV update egress without attaching default application consumers to an outbound bridge. Compose does not enforce remote domain allowlists; any mandatory network destination policy still requires host rules. Named `db_data` and `private_files` volumes are mounted only on consumers; a real synthetic CI password file must be provided through `CP_DB_PASSWORD_FILE` for the DB service. The observed topology and host web connectivity are not proven by this static inspection.

The new `tests/integration/test_topology.py` checks the full resolved network/port/volume profile, the network-attached test container's API reachability (expected FastAPI 404), and a synthetic file/database marker across explicit CI database/API **container recreation**. The test service now sets `CI=true`, preventing missing CI inputs from silently skipping every test. It requires a disposable project name `CP_CI_PROJECT=carepulse-ci-<run>-<attempt>` (and matching actual Compose project name), a resolved JSON config available at `CP_COMPOSE_CONFIG_PATH`, and `CP_PERSIST_PHASE=seed` then `verify` in two CI test-container invocations. Its only privileged database credential is the synthetic `postgres_password` Compose secret at `/run/secrets/postgres_password`, mounted solely into disposable DB and test containers, never normal web/API/worker. Generate JSON with all three optional profiles enabled (`--profile mail --profile scan --profile test config --format json`) for structural coverage, and mount that file read-only into both test invocations; normal running services need only the requested profiles. Enforce **zero pytest skips** when running in CI. CI must recreate disposable DB/API containers between `seed` and `verify` without deleting named volumes, then capture the marker result before cleanup. Local check commands with observed outcomes:

```powershell
.\.venv-wave0\Scripts\python.exe -m ruff check tests/integration/test_topology.py # passed
.\.venv-wave0\Scripts\python.exe -m ruff format --check tests/integration/test_topology.py # passed
.\.venv-wave0\Scripts\python.exe -m pytest -q tests/integration/test_topology.py # 3 skipped: no local Docker
git diff --check # passed
```

Additional Python/PyYAML static assertions on the compose and Dockerfiles passed, covering approved image digests, pinned installers, valid file paths and build inputs, network/volume/profile boundaries, absent secret build ARGs and no automatic migrations. CI must independently execute: **runner-host vantage** loopback web response on 3000 and denial on API 8000/PostgreSQL 5432; **web-container vantage** ordinary HTTP GET of `http://api:8000/__foundation_network_probe__` returning 404; **test-container vantage** resolved config, private API and marker persistence in `seed` then `verify` after service restart. Unique disposable CI project/DB only; no shared-volume cleanup or privileged-host-isolation claim.

**Negative verification of the CI skip guard:** with `CI=true` locally, and without `CP_CI_PROJECT` and other CI inputs, `python -m pytest -q tests/integration/test_topology.py` reported **3 FAILURES** (each `CI topology probe missing CP_CI_PROJECT`) and a nonzero exit, as required. A normal workstation invocation still reports three intentional skips. CI success must consist of the actual passing cases, and the final T010 run must capture three passes on each `seed`/`verify` invocation, never accept skips as green. The independent T007–T009 review found the pre-correction missing-CI bypass and missing web-vantage evidence; the former is now guarded, while the latter remains a specifically required T010 command/evidence checkpoint. The static topology test additionally asserts restricted application masquerading and only the optional scanner's outbound network.

**T010 inputs — Product Owner approved 2026-10-22:** GitHub-hosted `ubuntu-24.04` Linux x64 runner (record actual rolling ImageOS/ImageVersion); immutable `actions/checkout` v7.0.1 SHA `3d3c42e5aac5ba805825da76410c181273ba90b1` with `persist-credentials:false`; PowerShell 7.6.7 pinned archive SHA-256 `6896f9e3a6b0a354bf470835a96da76d2f801cd50dcf047ad7c4563bf93bf431`; signed Docker official Ubuntu Noble amd64 apt packages Docker CE and CLI `5:29.9.2-1~ubuntu.24.04~noble`, containerd.io `2.4.2-3~ubuntu.24.04~noble`, Compose plugin `5.6.1-1~ubuntu.24.04~noble`, Buildx plugin `0.38.1-1~ubuntu.24.04~noble`. The hosted VM image is rolling and cannot be immutable through standard `runs-on`; CI logs its observed identity. Upstream reference URLs: [checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1), [PowerShell v7.6.7](https://github.com/PowerShell/PowerShell/releases/tag/v7.6.7), [Docker official Noble packages](https://download.docker.com/linux/ubuntu/dists/noble/pool/stable/amd64/), [Compose v5.6.1](https://github.com/docker/compose/releases/tag/v5.6.1), [Buildx v0.38.1](https://github.com/docker/buildx/releases/tag/v0.38.1). This is **input approval only**, not evidence that GitHub runners, image digests or CI tests have actually run.

### T010 CI workflow and acceptance (2026-10-22)

**IMPLEMENTED, TESTED AND CI INTEGRATION VERIFIED on published commit `c68f90d`.** Added the single `.github/workflows/foundation.yml` with two dependent jobs (`inputs-and-build`, `compose-integration`) on `ubuntu-24.04`, least-privilege `contents: read`, immutable checkout SHA, approved exact PowerShell/Docker/Compose/Buildx and immutable Python/Node archives. The Linux container-free job builds official CPython 3.13.16 source checked against its published SHA-256 (`f4b1bfb3c79b5bb11b8d228a12504163b4c0dab4d679828d8f5f26b6cb6ab35d`) and verifies the official Node Linux x64 archive SHA-256 (`6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6`); then securely bootstraps pip 26.2.2, verifies frozen Python/Node installs, web build, imports and lint without changing the workstation tools. The second job validates signed Docker apt toolpins and all approved Linux amd64 images, then builds and boots only the nonoptional services.

The CI workflow uses disposable `COMPOSE_PROJECT_NAME=CP_CI_PROJECT=carepulse-ci-<run-id>-<attempt>`, a freshly randomized synthetic PostgreSQL password file outside the Docker build context and a private all-profile rendered Compose JSON. Both are bind-mounted read-only as needed for the nonroot test target; runtime web/API/worker never receive the DB bootstrap password. Evidence from each distinct vantage point is required: runner-host successful loopback web HTTP plus denied API/DB ports; web-container successful private API 404; test-container three positive topology/API/DB-and-file marker checks with `CP_PERSIST_PHASE=seed`, DB/API **container recreation**, and another three passing `verify` checks. The unique disposable Compose project alone is cleaned with its named volumes after tests; no scanner startup or local Docker use. Sanitized step logs and GitHub Step Summary include run URL/attempt/SHA and checks on success; no downloadable uploaded artifact is claimed.

Local static verification: the existing approved Node 24.22.1 parsed the workflow using the already-installed `js-yaml@4.3.2` (three triggers and two jobs), `git diff --check` passed, isolated CPython/Ruff passed, and workstation `python -m pytest -q tests/integration/test_topology.py` reported **three expected skips** because no CI container exists. These local skips are distinct from the actual six passing Linux CI integration tests recorded below. No local Docker was installed or run.

**First published CI attempt:** Commit `9f562f3347752e39f48584f539fac83db948161c`, [GitHub run 39685998168](https://github.com/ahmedali5696/care-pulse-3/actions/runs/39685998168), event `push`, ended `failure` with **zero jobs**. GitHub CLI reported a workflow-file issue with no job logs. The workflow used a `runner.temp` expression in **job-level** `env`, where the `runner` expression context is unavailable. Narrow correction: pass `$RUNNER_TEMP` through `GITHUB_ENV` in an initial runner step instead; preserve all approved versions. The follow-up run created both jobs and succeeded, corroborating this diagnosis. No runtime/build/Compose tests executed in the failed first attempt.

**Accepted positive CI evidence:** [GitHub Actions run 39686480938](https://github.com/ahmedali5696/care-pulse-3/actions/runs/39686480938), event `push`, attempt 1, executable commit `c68f90dbb01bba6ae5c8101bc5892dd87096836c`, completed **SUCCESS** with both jobs green: `inputs-and-build` (4m43s, job `119521721162`) and `compose-integration` (2m15s, job `119523700011`). The GitHub logs confirm Linux runner `ubuntu24` image version `20261012.334.1` on x86_64; signed Docker install, approved client/server and plugin version assertions, pinned Linux amd64 pulls for **all five** approved images, exact Node/Python/pip/PostgreSQL runtime assertions and clean Docker builds/boot. Runtime PostgreSQL output was `17.11 (Debian 17.11-1.pgdg12+2)`; API `Python 3.13.16`, `pip 26.2.2`. No substitutions or local Docker runs.

**Observed vantage probes and persistence:** runner-host `http://127.0.0.1:3000/` **PASS**, runner-host TCP **8000 denied / 5432 denied**; web-container HTTP `http://api:8000/__foundation_network_probe__` returned required **404**; network-attached test-container `python -m pytest -q -ra -p no:cacheprovider tests/integration/test_topology.py` reported **3 passed (0 skipped) in seed** and **3 passed (0 skipped) in verify**, after `docker compose up -d --force-recreate --no-deps db api` without volume deletion. Both DB table marker and private-file marker survived container recreation and were read/removed on verify. All-profile resolved JSON was generated privately, normal service startup excluded optional scanner, and cleanup of **only** `carepulse-ci-39686480938-1` and its named volumes succeeded. Logs and GitHub step summary are the evidence; no separate uploaded artifact was produced. No privileged-host isolation claim, production deployment or F-03–F-07 acceptance is made.

**T010 = [X]; F-02 = CI INTEGRATION VERIFIED / ACCEPTED.** F-03 is now verified separately below. Mandatory later gates remain F-04 migration/role isolation, F-05 health, F-06 consolidated reproducibility and drift/security review, and independent F-07 auth/mail, with production Security/Operations approval still outstanding.

### F-03: fail-closed configuration and synthetic-only verification

F-03 requires an **explicit** environment (`CP_ENV=development` or `test`) together with `CP_SYNTHETIC_TEST_PROFILE=true` and the **complete** tagged JSON in `tests/fixtures/foundation-policy.json`. Its values (session/login/recovery/invitation/Doctor-confirmation/freshness/download limits and file classes/MIME/types/count/storage) are **synthetic test examples only**. They are not production defaults, approved operational security thresholds, or permission to process patient records. `CP_ENV=production` deliberately fails startup while SECURITY-POLICY-NUMERICS and applicable production approval gates remain OPEN.

Only the file **paths** are provided in process environment variables; the actual credentials are generated into ignored private files. `CP_DB_PASSWORD_FILE` is a disposable PostgreSQL **bootstrap** secret for the one-off DB and topology test, never the API/web/worker runtime credential. Compose mounts a distinct `runtime_db_password` to the runtime consumers, `service_key` only to web and API, `auth_secret` only to web and `file_encryption_key` only to API and worker. No operator or migration credential reaches a normal service. The synthetic policy file is mounted read-only. The optional mail transport is web-owned; no worker SMTP grant or network is added. File uploads/scanner activity remain inactive in this Wave.

Reproduce local checks **without Docker** after selecting the approved Python 3.13.16 and Node 24.22.1 toolchains:

```powershell
$env:PYTHONPATH = "apps/api"
.\.venv-wave0\Scripts\python.exe -m pytest -q tests/unit/test_config.py
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
.\.venv-wave0\Scripts\python.exe -m ruff check apps/api apps/worker tests
.\.venv-wave0\Scripts\python.exe -m ruff format --check apps/api apps/worker tests
pnpm typecheck
pnpm lint
pnpm build
```

All consuming-process, secret/canary/image-layer and PostgreSQL-backed checks **must run on the disposable Linux CI runner**, not locally. The single `foundation.yml` renders Compose only after generating distinct private files; it starts normal services with complete synthetic configuration, demands nonzero exits on deliberately removed process inputs, checks the separate runner-host/web/test vantage points and seed→recreate→verify persistent-volume evidence, and scans tracked source, Next build output, image contents/history and captured process logs against generated synthetic credentials. Runtime image preflight validates web policy **before** Next binds port 3000 (instrumentation is a secondary validation guard). Config tests prove error messages and returned object representations do not serialize secret values. Integration must report its exact GitHub run URL/SHA/pass counts before F-03 acceptance; local success or three CI-skipped workstation probes is insufficient.

**T011-T016 = [X]; F-03 = SYNTHETIC-DEVELOPMENT CI INTEGRATION VERIFIED / ACCEPTED (2026-10-22).** [GitHub Actions run 39700680578](https://github.com/ahmedali5696/care-pulse-3/actions/runs/39700680578), `push`, attempt **1**, executable SHA `e1df6d721585b890bfac0fc87ea7c0b3ed0202a6`, **completed SUCCESS**: `inputs-and-build` (job `119568862211`) and `compose-integration` (job `119570531079`) both green. The Linux job proved **395/395** deterministic configuration unit cases with approved Python 3.13.16, pinned/hash-locked Python inputs, Ruff and frozen pnpm/TypeScript/ESLint/Next build. Actual Docker Compose Linux amd64 service builds and startup, separate per-consumer file secrets and test-only full active policy, initial web response, closed runner-host API/DB ports, private Web-to-API HTTP 404, and named DB/file marker persistence after DB/API recreation all passed. Network-attached topology tests: **3/3 seed + 3/3 verify**, with no skipped CI cases. Actual API consuming-process configuration integration: **34/34 passed**. Separate negative container starts denied missing `CP_POLICY_FILE`, `CP_DB_PASSWORD_FILE`, `CP_SERVICE_KEY_FILE`, `CP_AUTH_SECRET_FILE` for Web and missing `CP_POLICY_FILE`, `CP_DB_PASSWORD_FILE`, `CP_FILE_ENCRYPTION_KEY_FILE` for Worker (**7/7 nonzero denials**). Nonroot Web source/build canary scan passed, all three service image-history scans passed, and the synthetic-key check reported **PASS** across tracked source, Next output/browser bundle, unpacked Docker-save image archives, and captured service/denial logs. The CI cleanup succeeded for only its disposable Compose project and private runner files. The CI logs/step summary are the evidence, with no separate uploaded artifact and no real operator secret used. Failed predecessor attempts `39693664344`, `39694691796`, `39694893037` and `39699774824` were diagnostic and **not** counted as acceptance; the final fixed executable SHA above is binding. F-03 acceptance is strictly for **synthetic development/testing**, not production policy approval, live authentication, medical uploads, release clearance, or F-04 through F-07 acceptance.

## F-04 local reproduction — T017–T021 (2026-10-22)

F-04 is **CI integration verified/accepted** on executable commit
`bfb3005ffd9290a05b035316bc9fc5a3b946cb19`, [run 39760438154](https://github.com/ahmedali5696/care-pulse-3/actions/runs/39760438154),
with 401 unit and 30 actual PostgreSQL cases passing. See T021 in
[tasks.md](tasks.md) for the exact evidence. F-05 started only after this gate.

These container-free commands were run from the repository root in Windows
PowerShell 5.1 using the existing approved CPython 3.13.16 environment:

```powershell
$env:PYTHONPATH = "apps/api"
.\.venv-wave0\Scripts\python.exe -m pytest -q tests/unit/test_config.py tests/unit/test_db.py
.\.venv-wave0\Scripts\python.exe -m ruff check apps/api apps/worker db tests
.\.venv-wave0\Scripts\python.exe -m ruff format --check apps/api apps/worker db tests
.\.venv-wave0\Scripts\python.exe -m alembic heads
.\.venv-wave0\Scripts\python.exe -m pytest --collect-only -q tests/integration/test_database.py
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
```

Collection does not execute PostgreSQL tests. The sole revision is
`0001_foundation`; no auth, clinical or job tables are added. The existing
workflow contains the proposed CI-only commands, including controlled
`db/roles.sql` provisioning, explicit `python -m alembic -c alembic.ini upgrade head`
with `CP_MIGRATION_PASSWORD_FILE` mounted only into the one-shot process, and
`python -m pytest -q -ra --tb=short -p no:cacheprovider tests/integration/test_database.py`.
Those CI commands passed on the pinned F-04 executable commit above. Never run
this suite against a shared database: reset tests create/drop only their generated
`carepulse_ci_<run>_<attempt>_f04_<suffix>` database inside the guarded disposable
Compose project. `db/roles.sql` is a fresh-cluster, explicit bootstrap operation;
normal application startup/readiness never provisions roles or runs migrations.
The script deliberately fails if these roles already exist instead of modifying
an existing cluster's principals.

## F-05 local reproduction — T022–T026 (2026-10-22)

API/web liveness is process-only; readiness requires the compatible baseline and
dependencies used by that consumer. Web additionally checks private API liveness
and SMTP when mail is enabled. Scanner/upload capabilities remain inactive.
Neither startup nor health creates schema or authenticates a clinic user.

```powershell
$env:PYTHONPATH = "apps/api;."
.\.venv-wave0\Scripts\python.exe -m pytest -q tests/unit
.\.venv-wave0\Scripts\ruff.exe check apps/api apps/worker db tests
.\.venv-wave0\Scripts\ruff.exe format --check apps/api apps/worker db tests
.\.venv-wave0\Scripts\python.exe -m pytest --collect-only -q tests/integration/test_health.py
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
# Select the approved Node 24.22.1/pnpm 10.34.7 first.
pnpm typecheck
rtk proxy pnpm lint
```

These local checks passed; integration collection is not execution. Local Next
build was not attempted under observed Windows virtual-memory pressure; clean
Linux/Next/container builds and all actual service scenarios remain required in
the existing CI workflow. F-05 passed on `2f25da0c55d09c4bad5a9611312f04f24da0c85e` in [run 39763868797](https://github.com/ahmedali5696/care-pulse-3/actions/runs/39763868797): 422 CI unit cases, 30 PostgreSQL cases and 9 running-service health cases across seven invocations, with all prior topology/secret checks and cleanup green. Final independent status-case fixtures bring the current local unit total to 424; that count does not replace the pinned run count. See T026 in [tasks.md](tasks.md) for evidence and the Phase 4 stop.

Private HTTP probes: `GET /health/live`, `GET /health/ready` (web and API);
exact bodies and `Cache-Control: no-store` are defined in the existing contract.
The worker writes `/tmp/carepulse-worker-status.json`; inside its container run
`python apps/worker/status.py live` or `python apps/worker/status.py ready`.
The probe requires PID 1 plus matching kernel boot/process-start identity and a
monotonic heartbeat age in `[0, 10]` seconds. A fresh live worker may be unready.
The 2-second heartbeat interval plus bounded 4-second dependency cycle stays
below that maximum age; Compose probes have a 6-second timeout. These are fixed
operational timings, not approved production security-policy values or an SLA.

The CI-only F-05 step in `.github/workflows/foundation.yml` contains the scoped
Compose stop/start/restart and direct `tests/integration/test_health.py` commands.
It enables Mailpit without sending messages, verifies mail-required versus
mail-disabled readiness, and rejects the actual old worker identity after restart.
Run those commands only in the generated disposable CI project; do not reproduce
DB shutdown, baseline mutation or restart against shared services.

## Prerequisites and safety

### Pending F-07/F-06 candidate (2026-10-22)

Both remaining lanes and T038 are now explicitly assigned, but the uncommitted
candidate is **not accepted**. Windows virtual-memory exhaustion prevented
meaningful web/check-runner validation; even Ruff later failed allocating 9,480
bytes. See the resumption record in `tasks.md` for exact failures and partial
results. Do not repeat memory workarounds, change approved tools or infer a pass.
Free workstation commit/virtual-memory capacity before retrying.

The candidate `ops/check.ps1` uses tools on `PATH`; select the existing approved
isolated tools without changing global installations:

```powershell
$env:PATH = "$PWD\.venv-wave0\Scripts;$PWD\.tools\node-v24.22.1-win-x64;$PWD\.tools\pnpm\node_modules\.bin;$env:PATH"
powershell.exe -NoProfile -File .\ops\check.ps1 inputs
powershell.exe -NoProfile -File .\ops\check.ps1 python-quality
powershell.exe -NoProfile -File .\ops\check.ps1 unit
powershell.exe -NoProfile -File .\ops\check.ps1 imports
powershell.exe -NoProfile -File .\ops\check.ps1 web
```

These commands are **pending successful reproduction**, not green evidence.
`integration` refuses workstation invocation before probing Docker; only pinned
Linux CI with an explicitly matching disposable project may run it. The revised
single workflow delegates its checks to the common entrypoint and retains the
accepted bootstrap/tool pins. Candidate HTTP/mail/PG checks have not run.
Native TypeScript private smoke commands inside CI's web container are
`node --experimental-strip-types mail/smoke.ts auth` and
`node --experimental-strip-types mail/smoke.ts mail`; no public auth/mail route,
working account or D-02 schema is introduced. Both commands are unverified.

The input checker prints raw and canonical-LF lock hashes; LF normalization makes
reviewed source bytes comparable across Windows checkout conversion and Linux.
It does not change package artifact SHA-256 verification. A direct import of
already-locked `starlette==1.9.0` is recorded in `requirements.in`; both complete
locks and all approved distribution versions remain unchanged.

**Local resumption update:** the five commands above now all pass under approved
Windows PowerShell 5.1/tool versions: **439 tests**, Ruff format/lint, shared
imports/sole Alembic head, frozen inputs and Next type/lint/build. An isolated
post-implementation fault returned runner exit 1 (1 failed/439 passed); removing
only the fault restored 439 passes/exit 0. See `tasks.md` for exact fault/advisory
evidence. This supersedes the earlier local-resource block, not pending CI.
The two private native-TypeScript/HTTP/mail smoke modes remain CI-unverified.

1. Product Owner **approved the exact R1–R4 inputs and conditional R7 synthetic-development recommendations on 2026-10-21**; obtain separate explicit assignment/authorization for each F task. Keep approved Next.js `15.5.28`; do not substitute a newer version to satisfy an advisory or peer conflict. GHSA-4jqv-mc3x-m676 and GHSA-mcj8-r9mp-w47p are patched from `15.5.27`, so `15.5.28` is outside their affected ranges and those two are not blockers. ESLint 9.39.5 is temporarily authorized **only for synthetic development/CI**, with Security/Operations production disposition deferred until before deployment. Other security/advisory checks and full locked transitive verification remain required during F-01/F-06.
2. Use the approved Python/Node/pnpm versions, not merely the globally installed ones. Current workstation tools are observations, not approved pins. Existing `.venv`/`node_modules` content is not reproducibility evidence; preserve it and use a clean isolated environment for later checks.
3. Use synthetic data only. Supply separate ignored test secret files and the full, explicitly labeled synthetic security-policy fixture through the F-03 configuration interface. Never copy fixtures to production, print keys/configuration, or use real operator credentials/email addresses.
4. CI needs a Docker-capable Linux runner, registry access, the recorded Compose tool version, and an isolated checkout/project/database. Do not execute this guide against a shared or production database. CI reports its run URL, Git revision, logs and sanitized evidence artifacts.
5. F-01's exact local tools and lockfiles have now been provisioned and tested as recorded above. All later steps must retain approved frozen/hash locks; migrations and CI-only integration are pending and must not be treated as complete.

## Container-free workstation checks

From repository root in the workstation's existing **Windows PowerShell 5.1**; do not install or require `pwsh` locally. Reproduce the full verified local command sequence under **T006** above. For a new workstation, resolve Python 3.13.16 explicitly before creating an isolated environment:

```powershell
py -V:3.13 -m venv .venv-wave0
.\.venv-wave0\Scripts\python.exe -m pip install "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
.\.venv-wave0\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.lock
.\.venv-wave0\Scripts\python.exe -m pip check
pnpm install --frozen-lockfile # after selecting Node 24.22.1 and pnpm 10.34.7
```

`requirements.lock` is the delivered single exact direct/transitive development/test lock for Wave 0; API/worker use the same pinned package set, without separate drifting locks. The pinned/hash-verified pip bootstrap follows Python's security instructions and must precede dependency downloads, including in CI/container builds; it is not a floating auto-upgrade. `packageManager` pins pnpm; the environment must provision that exact version before running it. Do not auto-upgrade pip, dependencies or Next.js. Exact installer/tool versions and selected platform artifacts are recorded by F-01/F-06.

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
