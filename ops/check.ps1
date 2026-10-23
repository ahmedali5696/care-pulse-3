param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('inputs', 'python-quality', 'unit', 'web', 'imports', 'integration')]
    [string]$Check
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
Set-Location (Split-Path $PSScriptRoot -Parent)

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed (exit $LASTEXITCODE)" }
}

try {
    switch ($Check) {
        'inputs' {
            # Standard-library validation runs before installers or network access.
            $verify = @'
import hashlib, importlib.metadata as metadata, json, platform, re, sys
from pathlib import Path

def require(condition, message):
    if not condition:
        raise SystemExit('FAIL inputs: ' + message)

require(platform.python_version() == '3.13.16', 'CPython version')
import sysconfig
require(not sysconfig.get_config_var('Py_GIL_DISABLED'), 'standard GIL')
require(metadata.version('pip') == '26.2.2', 'pip version')
root = json.loads(Path('package.json').read_text())
require(root['packageManager'] == 'pnpm@10.34.7', 'packageManager')
require(root['engines'] == {'node': '24.22.1', 'pnpm': '10.34.7'}, 'runtime pins')
require(root['private'] is True, 'private workspace')
web = json.loads(Path('apps/web/package.json').read_text())
dependencies = {'better-auth':'1.7.12','next':'15.5.28','nodemailer':'10.1.7','pg':'8.23.1','react':'19.3.1','react-dom':'19.3.1'}
tooling = {'@eslint/eslintrc':'3.3.1','@types/node':'24.19.2','@types/pg':'8.23.1','@types/react':'19.3.0','@types/react-dom':'19.3.0','eslint':'9.39.5','eslint-config-next':'15.5.28','typescript':'5.9.3'}
require(web['dependencies'] == dependencies and web['devDependencies'] == tooling, 'approved manifest pins')
require(web['private'] is True, 'private web package')
npmrc = dict(line.split('=', 1) for line in Path('.npmrc').read_text().splitlines() if line.strip())
require(npmrc == {'engine-strict':'true','package-manager-strict':'true','package-manager-strict-version':'true','manage-package-manager-versions':'false','strict-peer-dependencies':'true','auto-install-peers':'false'}, 'strict engines/peers')
require(Path('pnpm-workspace.yaml').read_text().split() == ['packages:', '-', 'apps/web'], 'workspace membership')
approved_locks = {'pnpm-lock.yaml':'e81b48c7ab50811e42a3eee46e58071a243d4f4c0ee873e0d91aa089285911a4','requirements.lock':'f819c9728ebcb2298fbfe5f06022a359b13e8eeed3b4e46a8c7a55f159e23d47'}
for filename, approved in approved_locks.items():
    raw = Path(filename).read_bytes()
    canonical = hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()
    require(canonical == approved, filename + ' drift (review lock changes, never regenerate automatically)')
    print(filename + ' SHA256 bytes=' + hashlib.sha256(raw).hexdigest() + ' LF=' + canonical)
direct = {}
for line in Path('requirements.in').read_text().splitlines():
    if not line.strip() or line.startswith('#'):
        continue
    match = re.fullmatch(r'([A-Za-z0-9_\[\]-]+)==([0-9.]+)', line)
    require(match is not None, 'exact Python direct pins')
    direct[match[1].lower()] = match[2]
require(direct == {'fastapi':'0.158.1','starlette':'1.9.0','uvicorn':'0.54.0','sqlalchemy':'2.0.55','psycopg[binary]':'3.3.6','alembic':'1.20.0','pytest':'9.1.1','ruff':'0.17.1'}, 'approved Python direct pins')
for line in Path('requirements.lock').read_text().splitlines():
    if not line.strip() or line.startswith('#'):
        continue
    match = re.fullmatch(r'([a-z0-9-]+)(?:\[binary\])?==([0-9.]+)(?:; sys_platform == "win32")?(?: --hash=sha256:[a-f0-9]{64})+', line)
    require(match is not None, 'complete exact SHA256 lock entries')
    if '; sys_platform' not in line or sys.platform == 'win32':
        require(metadata.version(match[1]) == match[2], 'installed distribution ' + match[1])
        print(match[1] + '==' + match[2])
images = re.findall(r'^\s*(?:FROM|image:)\s+(\S+)', Path('compose.yaml').read_text(), re.M)
require(images == ['postgres:17.11-bookworm@sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27','axllent/mailpit:v1.31.6@sha256:23fe39e81bde596a918000b3135c0f85dc8bb4c9ea6be274b051143f3e300387','clamav/clamav:1.5.4-debian13-slim@sha256:d7ede3cefeaf3b8a72ebcdee849d13f504bbca3f3b3cfade5bc7b6ee06a4f62c'], 'compose.yaml approved image references')
# Fingerprint whole files so added commands cannot evade line-prefix checks.
approved_bootstrap_files = {
    '.github/workflows/foundation.yml': '771e6f25275b16e95287d98e64afc9f883d77ed493c0cb12c1c3e10acf61a4dd',
    'apps/api/Dockerfile': '293af5bb74f261da9d70fe314241b897b4452e116617758889a9b2dbecfc942a',
    'apps/web/Dockerfile': '611bacc8415759957ecf813a220303aa7adc6d2f12eade10cb64e9f99b7560b1',
}
for filename, approved in approved_bootstrap_files.items():
    canonical = Path(filename).read_bytes().replace(b'\r\n', b'\n')
    require(hashlib.sha256(canonical).hexdigest() == approved, filename + ' executable input drift')
print('PASS approved manifests, complete locks, installed Python closure, private workspace and image inputs')
'@
            $verify | & python -
            if ($LASTEXITCODE -ne 0) { throw "Input validation failed (exit $LASTEXITCODE)" }
            $nodeVersion = & node --version
            if ($LASTEXITCODE -ne 0) { throw 'node version command failed' }
            if ($nodeVersion -ne 'v24.22.1') { throw 'Unapproved Node version' }
            $pnpmVersion = & pnpm --version
            if ($LASTEXITCODE -ne 0) { throw 'pnpm version command failed' }
            if ($pnpmVersion -ne '10.34.7') { throw 'Unapproved pnpm version' }
            Invoke-Checked python @('-m', 'pip', 'check')
            Invoke-Checked pnpm @('install', '--frozen-lockfile')
            Invoke-Checked node @('-e', "const fs=require('node:fs'); const p=JSON.parse(fs.readFileSync('apps/web/package.json')); for(const [name,version] of Object.entries({...p.dependencies,...p.devDependencies})){if(require('./apps/web/node_modules/'+name+'/package.json').version!==version)throw Error('Installed package mismatch: '+name);} console.log('PASS installed web pins and frozen strict peers')")
        }
        'python-quality' {
            Invoke-Checked python @('-m', 'ruff', 'format', '--check', 'apps/api', 'apps/worker', 'db', 'tests')
            Invoke-Checked python @('-m', 'ruff', 'check', 'apps/api', 'apps/worker', 'db', 'tests')
        }
        'unit' { Invoke-Checked python @('-m', 'pytest', '-q', 'tests/unit') }
        'web' {
            Invoke-Checked pnpm @('typecheck')
            Invoke-Checked pnpm @('lint')
            Invoke-Checked pnpm @('build')
        }
        'imports' {
            $env:PYTHONPATH = (Join-Path $PWD 'apps/api') + [IO.Path]::PathSeparator + $PWD
            Invoke-Checked python @('-c', "from carepulse.main import app; import apps.worker.main; assert set(app.openapi()['paths']) == {'/health/live', '/health/ready'}; assert app.openapi_url is None; print('PASS shared API/worker imports, health-only schema, no jobs')")
            Invoke-Checked python @('-m', 'alembic', 'heads')
        }
        'integration' {
            if (-not (Test-Path -LiteralPath 'tests/integration/test_auth_mail.py' -PathType Leaf)) {
                throw 'Missing required F-07 integration test: tests/integration/test_auth_mail.py'
            }
            if (-not (Test-Path -LiteralPath 'tests/integration/test_tenant_isolation.py' -PathType Leaf)) {
                throw 'Missing required D-01 integration test: tests/integration/test_tenant_isolation.py'
            }
            if ($env:CI -ne 'true' -or $env:RUNNER_OS -ne 'Linux' -or $PSVersionTable.PSVersion.Major -lt 7) {
                throw 'Integration requires Docker-capable Linux CI and PowerShell 7; no local Docker probe performed'
            }
            if ($env:CP_CI_PROJECT -notmatch '^carepulse-ci-[0-9]+-[0-9]+$' -or $env:COMPOSE_PROJECT_NAME -ne $env:CP_CI_PROJECT) {
                throw 'Explicit matching disposable CI project required'
            }
            # Linux CI retains the accepted Bash probes; PowerShell checks their exit immediately.
            $integration = @'
set -euo pipefail
umask 077
private="$RUNNER_TEMP/foundation-ci-private"
test ! -e "$private"
mkdir -m 0700 "$private"
cleanup() {
  code=$?
  trap - EXIT
  if [[ "$COMPOSE_PROJECT_NAME" =~ ^carepulse-ci-[0-9]+-[0-9]+$ && "$COMPOSE_PROJECT_NAME" = "$CP_CI_PROJECT" ]]; then
    if ! docker compose --profile mail --profile test down --remove-orphans --volumes; then code=1; fi
    rm -f "$private"/{postgres-password,runtime-db-password,migration-password,operator-password,service-key,auth-secret,file-key,compose.json,roles.log,web-denial.log,worker-denial.log,denials.log,runtime-logs,canaries,old-worker-status.json,health-logs,auth-logs,mail-denial.log}
    rm -rf "$private/image-scan"
    rmdir "$private" || code=1
  else
    echo 'FAIL unsafe cleanup project'; code=1
  fi
  exit "$code"
}
trap cleanup EXIT
for file in "$CP_DB_PASSWORD_FILE" "$CP_RUNTIME_DB_PASSWORD_FILE" "$CP_MIGRATION_SECRET_FILE" "$CP_OPERATOR_SECRET_FILE" "$CP_SERVICE_KEY_SOURCE_FILE" "$CP_AUTH_SECRET_SOURCE_FILE" "$CP_FILE_ENCRYPTION_KEY_SOURCE_FILE"; do
  test "$(dirname "$file")" = "$private"
  openssl rand -hex 32 > "$file"
  chmod 0444 "$file"
done
docker compose --profile mail --profile scan --profile test config --quiet
docker compose --profile mail --profile scan --profile test config --format json > "$private/compose.json"
chmod 0444 "$private/compose.json"
for image in \
  'node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205' \
  'python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d' \
  'postgres:17.11-bookworm@sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27' \
  'axllent/mailpit:v1.31.6@sha256:23fe39e81bde596a918000b3135c0f85dc8bb4c9ea6be274b051143f3e300387' \
  'clamav/clamav:1.5.4-debian13-slim@sha256:d7ede3cefeaf3b8a72ebcdee849d13f504bbca3f3b3cfade5bc7b6ee06a4f62c'
do
  docker pull --platform linux/amd64 --quiet "$image" > /dev/null
  test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" = linux/amd64
  docker image inspect --format '{{json .RepoDigests}} {{.Id}} {{.Os}}/{{.Architecture}}' "$image"
done
docker compose --profile test build --pull
docker compose up -d --no-build db
for attempt in $(seq 1 40); do
  if docker compose exec -T db pg_isready -U carepulse_bootstrap -d carepulse > /dev/null 2>&1; then break; fi
  if [ "$attempt" = 40 ]; then echo 'FAIL disposable database startup'; exit 1; fi
  sleep 2
done
export CP_MIGRATION_PASSWORD="$(cat "$CP_MIGRATION_SECRET_FILE")"
export CP_RUNTIME_PASSWORD="$(cat "$CP_RUNTIME_DB_PASSWORD_FILE")"
export CP_OPERATOR_PASSWORD="$(cat "$CP_OPERATOR_SECRET_FILE")"
if ! docker compose exec -T -e CP_MIGRATION_PASSWORD -e CP_RUNTIME_PASSWORD -e CP_OPERATOR_PASSWORD \
  db psql --no-psqlrc -U carepulse_bootstrap -d carepulse < db/roles.sql > "$private/roles.log" 2>&1; then
  echo 'FAIL controlled roles (credential-bearing diagnostics withheld)'; exit 1
fi
unset CP_MIGRATION_PASSWORD CP_RUNTIME_PASSWORD CP_OPERATOR_PASSWORD
docker compose --profile test run --rm --no-deps \
  -e CP_DB_HOST=db -e CP_DB_PORT=5432 -e CP_DB_NAME=carepulse \
  -e CP_MIGRATION_PASSWORD_FILE=/run/secrets/migration_password \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m alembic -c alembic.ini upgrade head
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider tests/integration/test_database.py tests/integration/test_tenant_isolation.py
docker compose up -d --no-build db api worker web
for attempt in $(seq 1 40); do
  if curl --silent --show-error --fail --max-time 2 http://127.0.0.1:3000/ > /dev/null 2>&1; then break; fi
  if [ "$attempt" = 40 ]; then echo 'FAIL runner-host web endpoint'; exit 1; fi
  sleep 2
done
echo 'PASS runner-host web http://127.0.0.1:3000/'
for port in 8000 5432; do
  if timeout 3 bash -c "echo > /dev/tcp/127.0.0.1/$port" 2>/dev/null; then
    echo "FAIL runner-host unexpected exposed port $port"; exit 1
  fi
  echo "PASS runner-host denied port $port"
done
docker compose exec -T web node -e 'fetch("http://api:8000/__foundation_network_probe__").then(r => process.exit(r.status === 404 ? 0 : 1)).catch(() => process.exit(1))'
echo 'PASS web-container -> private API HTTP 404'
docker compose exec -T db postgres --version | grep -F '17.11'
docker compose exec -T web node --version | grep -Fx 'v24.22.1'
docker compose exec -T api python --version | grep -F '3.13.16'
docker compose exec -T api python -m pip --version | grep -F '26.2.2'
docker compose exec -T api python -c 'from carepulse.config import load_config; load_config("api")'
docker compose exec -T worker python -c 'from carepulse.config import load_config; load_config("worker")'
for phase in seed verify; do
  if [ "$phase" = verify ]; then docker compose up -d --force-recreate --no-deps db api; fi
  docker compose --profile test run --rm --no-deps \
    -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_PERSIST_PHASE="$phase" \
    -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
    -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
    test python -m pytest -q -ra -p no:cacheprovider tests/integration/test_topology.py
  echo "PASS test-container topology, API and persistent DB/file markers: $phase"
done
docker compose exec -T web node -e 'fetch("http://api:8000/__foundation_network_probe__").then(r => process.exit(r.status === 404 ? 0 : 1)).catch(() => process.exit(1))'
docker compose --profile test run --rm --no-deps -e CI=true \
  -v "$GITHUB_WORKSPACE/tests:/srv/tests:ro" api python -m pytest -q -ra -p no:cacheprovider tests/integration/test_secrets.py
: > "$private/denials.log"
for key in CP_POLICY_FILE CP_DB_PASSWORD_FILE CP_SERVICE_KEY_FILE CP_AUTH_SECRET_FILE; do
  set +e
  timeout 15s docker compose run --rm --no-deps -e "$key=/run/secrets/missing" web > "$private/web-denial.log" 2>&1
  code=$?
  set -e
  cat "$private/web-denial.log" >> "$private/denials.log"
  if [ "$code" -eq 0 ] || [ "$code" -eq 124 ] || ! grep -Fq "$key" "$private/web-denial.log"; then
    echo "FAIL web protected startup missing $key (exit $code)"; exit 1
  fi
  echo "PASS web protected startup denies missing $key"
done
test "$(docker inspect --format '{{.State.Running}}' "$(docker compose ps -q worker)")" = true
for key in CP_POLICY_FILE CP_DB_PASSWORD_FILE CP_FILE_ENCRYPTION_KEY_FILE; do
  set +e
  timeout 10s docker compose run --rm --no-deps -e "$key=/run/secrets/missing" worker > "$private/worker-denial.log" 2>&1
  code=$?
  set -e
  cat "$private/worker-denial.log" >> "$private/denials.log"
  if [ "$code" -eq 0 ] || [ "$code" -eq 124 ] || ! grep -Fq "$key" "$private/worker-denial.log"; then
    echo "FAIL worker protected startup missing $key (exit $code)"; exit 1
  fi
  echo "PASS worker protected startup denies missing $key"
done
cat "$CP_DB_PASSWORD_FILE" "$CP_RUNTIME_DB_PASSWORD_FILE" "$CP_SERVICE_KEY_SOURCE_FILE" "$CP_AUTH_SECRET_SOURCE_FILE" \
  "$CP_FILE_ENCRYPTION_KEY_SOURCE_FILE" "$CP_MIGRATION_SECRET_FILE" "$CP_OPERATOR_SECRET_FILE" > "$private/canaries"
chmod 0444 "$private/canaries"
awk 'length($0) != 64 || $0 !~ /^[0-9a-f]+$/ { invalid=1 } END { if (NR != 7 || invalid) exit 1 }' "$private/canaries"
if git grep -I -q -f "$private/canaries" -- apps tests ops compose.yaml; then
  echo 'FAIL synthetic key in tracked source'; exit 1
else
  code=$?; if [ "$code" -ne 1 ]; then echo 'FAIL tracked-source scan error'; exit 1; fi
fi
docker compose run --rm --no-deps -v "$private/canaries:/tmp/canaries:ro" web sh -c \
  'test -r /tmp/canaries || exit 2
   result=0
   grep -aRF -f /tmp/canaries /workspace/apps/web/.next /workspace/apps/web/app > /dev/null || result=$?
   case "$result" in 1) echo "PASS web source/build canary scan";; *) echo "FAIL web source/build scan"; exit 1;; esac'
images=()
for service in web api worker; do
  image="$(docker compose images -q "$service")"
  test -n "$image"
  images+=("$image")
  docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image"
  if docker image history --no-trunc "$image" | grep -aF -f "$private/canaries" > /dev/null; then
    echo "FAIL synthetic key in $service image history"; exit 1
  else
    code=$?; if [ "$code" -ne 1 ]; then echo 'FAIL image-history scan error'; exit 1; fi
  fi
done
mkdir -m 0700 "$private/image-scan"
docker image save "${images[@]}" | tar -xf - -C "$private/image-scan"
if [ ! -f "$private/image-scan/manifest.json" ] && [ ! -f "$private/image-scan/index.json" ]; then
  echo 'FAIL saved-image archive manifest'; exit 1
fi
test "$(find "$private/image-scan" -type f | wc -l)" -ge 3
if grep -aRF -f "$private/canaries" "$private/image-scan" > /dev/null; then
  echo 'FAIL synthetic key in image layers'; exit 1
else
  code=$?; if [ "$code" -ne 1 ]; then echo 'FAIL image-layer scan error'; exit 1; fi
fi
rm -rf "$private/image-scan"
health_case() {
  scenario="$1"; shift
  docker compose --profile test run --rm --no-deps \
    -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_HEALTH_SCENARIO="$scenario" \
    -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
    -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
    test python -m pytest -q -ra --tb=short -p no:cacheprovider "$@"
}
worker_state() {
  expected="$1"
  for attempt in $(seq 1 25); do
    if docker compose exec -T worker python apps/worker/status.py live; then
      if docker compose exec -T worker python apps/worker/status.py ready; then actual=0; else actual=$?; fi
      if [ "$actual" = "$expected" ]; then return 0; fi
    fi
    sleep 1
  done
  echo 'FAIL worker live/readiness distinction'; return 1
}
export CP_MAIL_ENABLED=true CP_SMTP_HOST=mail CP_SMTP_PORT=1025
docker compose --profile mail up -d --no-build --no-deps --force-recreate mail web
health_case healthy tests/integration/test_health.py
worker_state 0
docker compose stop db
health_case database_down tests/integration/test_health.py::test_running_service_state
worker_state 1
docker compose start db
health_case healthy tests/integration/test_health.py::test_running_service_state
worker_state 0
docker compose --profile mail stop mail
health_case mail_down tests/integration/test_health.py::test_running_service_state
worker_state 0
export CP_MAIL_ENABLED=false
docker compose up -d --no-build --no-deps --force-recreate web
health_case mail_disabled tests/integration/test_health.py::test_running_service_state
export CP_MAIL_ENABLED=true
docker compose --profile mail start mail
docker compose up -d --no-build --no-deps --force-recreate web
health_case healthy tests/integration/test_health.py::test_running_service_state
docker compose exec -T worker cat /tmp/carepulse-worker-status.json > "$private/old-worker-status.json"
docker compose restart api worker
health_case healthy tests/integration/test_health.py::test_running_service_state
worker_state 0
docker compose exec -T worker python -c 'import json,sys,time; from pathlib import Path; old=json.load(sys.stdin); old["heartbeat"]=time.monotonic(); p=Path("/tmp/previous-worker-status.json"); p.write_text(json.dumps(old)); p.chmod(0o600)' < "$private/old-worker-status.json"
if docker compose exec -T worker python apps/worker/status.py live /tmp/previous-worker-status.json; then
  echo 'FAIL restarted worker accepted old identity'; exit 1
fi
for service in api web worker; do
  for attempt in $(seq 1 25); do
    state="$(docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q "$service")")"
    if [ "$state" = healthy ]; then break; fi
    if [ "$attempt" = 25 ]; then echo "FAIL Compose $service health"; exit 1; fi
    sleep 1
  done
done
# F-07 is independently executable without D-02 tables or a fake identity.
docker compose exec -T web node --experimental-strip-types mail/smoke.ts auth
docker compose exec -T web node --experimental-strip-types mail/smoke.ts mail
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_SERVICE_KEY_SOURCE_FILE:/run/secrets/service_key:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider tests/integration/test_auth_mail.py
docker compose logs --no-color api > "$private/auth-logs"
if grep -Fq '__foundation_bff_must_not_forward__' "$private/auth-logs"; then
  echo 'FAIL BFF forwarded a protected request'; exit 1
else
  code=$?; if [ "$code" -ne 1 ]; then echo 'FAIL API no-passthrough log scan'; exit 1; fi
fi
# Demonstrate that the same real transport fails, rather than selecting a relay.
docker compose --profile mail stop mail
set +e
timeout 15s docker compose exec -T web node --experimental-strip-types mail/smoke.ts mail > "$private/mail-denial.log" 2>&1
code=$?
set -e
cat "$private/mail-denial.log" >> "$private/denials.log"
if [ "$code" -eq 0 ] || [ "$code" -eq 124 ]; then echo 'FAIL missing Mailpit did not fail bounded smoke'; exit 1; fi
docker compose --profile mail start mail
health_case healthy tests/integration/test_health.py::test_running_service_state
docker compose logs --no-color web api worker > "$private/runtime-logs"
if grep -aF -f "$private/canaries" "$private/runtime-logs" "$private/denials.log" > /dev/null; then
  echo 'FAIL synthetic key in runtime/denial logs'; exit 1
else
  code=$?; if [ "$code" -ne 1 ]; then echo 'FAIL runtime-log scan error'; exit 1; fi
fi
{
  echo '### Wave 0 combined foundation checks'
  echo "- Run: https://github.com/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID} (attempt ${GITHUB_RUN_ATTEMPT}, SHA ${GITHUB_SHA})"
  echo '- Pinned Linux amd64 images, builds and runtime versions: verified above'
  echo '- F-02 topology/persistence: 3 seed + 3 verify; runner/web/test vantage probes passed'
  echo '- F-03 process denials, source/build/image/history/runtime secret scans: passed'
  echo '- F-04 sole baseline, reset/rollback/privilege and same-connection context cases: passed'
  echo '- F-05 bounded status/failure/recovery/restart and fresh worker identity: passed'
  echo '- F-07 explicit PostgreSQL/no-cache auth, closed BFF/API, two Mailpit messages and mail-loss denial: passed'
  echo '- Synthetic development only; auth persistence, production and later Waves remain closed'
} >> "$GITHUB_STEP_SUMMARY"
'@
            & bash -e -c $integration
            if ($LASTEXITCODE -ne 0) { throw "Integration failed (exit $LASTEXITCODE)" }
        }
    }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
exit 0
