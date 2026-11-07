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
tooling = {'@eslint/eslintrc':'3.3.1','@playwright/test':'1.64.1','@types/node':'24.19.2','@types/pg':'8.23.1','@types/react':'19.3.0','@types/react-dom':'19.3.0','eslint':'9.39.5','eslint-config-next':'15.5.28','typescript':'5.9.3'}
require(web['dependencies'] == dependencies and web['devDependencies'] == tooling, 'approved manifest pins')
require(web['private'] is True, 'private web package')
npmrc = dict(line.split('=', 1) for line in Path('.npmrc').read_text().splitlines() if line.strip())
require(npmrc == {'engine-strict':'true','package-manager-strict':'true','package-manager-strict-version':'true','manage-package-manager-versions':'false','strict-peer-dependencies':'true','auto-install-peers':'false'}, 'strict engines/peers')
require(Path('pnpm-workspace.yaml').read_text().split() == ['packages:', '-', 'apps/web'], 'workspace membership')
approved_locks = {'pnpm-lock.yaml':'cc80cf7fb9ae5ea3ab0cc671ede25203d06312421687692f826548fde646274b','requirements.lock':'38e65d6ee9a37ee944eef73e772984cdccbf82d797f3a1c64f9fb917289af9b6'}
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
require(direct == {'fastapi':'0.158.1','starlette':'1.9.0','uvicorn':'0.54.0','sqlalchemy':'2.0.55','psycopg[binary]':'3.3.6','alembic':'1.20.0','pytest':'9.1.1','ruff':'0.17.1','cryptography':'50.0.2'}, 'approved Python direct pins')
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
    '.github/workflows/foundation.yml': '308fedbe2bc27d690ae0ca84eeaf2c4e954cf31c169c27c22b335a18fc0fc992',
    'apps/api/Dockerfile': '8465e11d5b90d94704ec63072180f10c56dcf4becf471da84258ea21de5208c8',
    'apps/web/Dockerfile': '933af52609f2a4eb638d048605ec555e2a3443cb25b3ba5d64d5a36ec585f02a',
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
            Invoke-Checked node @('--no-warnings', '--experimental-strip-types', '--test', 'apps/web/auth/signing.test.ts')
            Invoke-Checked node @('--no-warnings', '--experimental-strip-types', '--test', 'apps/web/mail/delivery-material.test.ts')
            Invoke-Checked pnpm @('lint')
            Invoke-Checked pnpm @('build')
        }
        'imports' {
            $env:PYTHONPATH = (Join-Path $PWD 'apps/api') + [IO.Path]::PathSeparator + $PWD
            $imports = @'
from carepulse.main import app
from carepulse.identity.boundary import ClosedServiceBoundary
import apps.worker.main

expected_paths = {
    '/health/live', '/health/ready', '/v1/provisioning/owner-activate',
    '/v1/staff/receptionists', '/v1/staff/receptionists/{staff_id}',
    '/v1/staff/receptionists/{staff_id}/disable',
    '/v1/staff/receptionists/{staff_id}/enable',
    '/v1/staff/receptionists/{staff_id}/permissions',
    '/v1/staff/receptionists/{staff_id}/invite',
    '/v1/staff/receptionists/{staff_id}/revoke-invite',
    '/v1/staff/receptionists/invite-accept',
    '/v1/staff/recovery/reserve', '/v1/staff/recovery/finalize',
    '/v1/patients', '/v1/patients/{patient_id}',
    '/v1/patients/{patient_id}/archive', '/v1/patients/{patient_id}/unarchive',
    '/v1/settings/pricing', '/v1/settings/schedule',
    '/v1/settings/schedule/preview', '/v1/settings/schedule/activate',
    '/v1/slots', '/v1/appointments', '/v1/appointments/{appointment_id}',
    '/v1/visits/{visit_id}', '/v1/visits/{visit_id}/extend-followup',
}
assert set(app.openapi()['paths']) == expected_paths
assert app.openapi_url is None and len(app.user_middleware) == 1
boundary = app.user_middleware[0]
assert boundary.cls is ClosedServiceBoundary
assert boundary.kwargs['routes'] == {
    ('POST', '/v1/provisioning/owner-activate'): 'OWNER_ACTIVATION',
    ('GET', '/v1/staff/receptionists'): 'STAFF_MANAGEMENT',
    ('POST', '/v1/staff/receptionists'): 'STAFF_MANAGEMENT',
    ('POST', '/v1/staff/receptionists/invite-accept'): 'RECEPTIONIST_ACCEPT',
    ('POST', '/v1/staff/recovery/reserve'): 'OWNER_RECOVERY',
    ('POST', '/v1/staff/recovery/finalize'): 'OWNER_RECOVERY',
    ('GET', '/v1/patients'): 'PATIENT_REGISTRY',
    ('POST', '/v1/patients'): 'PATIENT_REGISTRY',
    ('GET', '/v1/appointments'): 'APPOINTMENT_BOOKING',
    ('POST', '/v1/appointments'): 'APPOINTMENT_BOOKING',
    ('GET', '/v1/settings/pricing'): 'SETTINGS_PRICING',
    ('PATCH', '/v1/settings/pricing'): 'SETTINGS_PRICING',
    ('GET', '/v1/settings/schedule'): 'SETTINGS_SCHEDULE',
    ('PATCH', '/v1/settings/schedule'): 'SETTINGS_SCHEDULE',
    ('POST', '/v1/settings/schedule/preview'): 'SETTINGS_SCHEDULE',
    ('POST', '/v1/settings/schedule/activate'): 'SETTINGS_SCHEDULE',
    ('GET', '/v1/slots'): 'SLOTS',
}
uuid = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
assert {(method, pattern.pattern, purpose) for method, pattern, purpose in boundary.kwargs['route_patterns']} == {
    ('GET', f'/v1/staff/receptionists/{uuid}', 'STAFF_MANAGEMENT'),
    ('GET', f'/v1/patients/{uuid}', 'PATIENT_REGISTRY'),
    ('GET', f'/v1/appointments/{uuid}', 'APPOINTMENT_BOOKING'),
    ('PATCH', f'/v1/visits/{uuid}', 'VISIT_FOLLOWUP'),
    ('POST', f'/v1/visits/{uuid}/extend-followup', 'VISIT_FOLLOWUP'),
    ('PATCH', f'/v1/patients/{uuid}', 'PATIENT_REGISTRY'),
    ('POST', f'/v1/patients/{uuid}/(?:archive|unarchive)', 'PATIENT_REGISTRY'),
    ('POST', f'/v1/staff/receptionists/{uuid}/(?:enable|disable|invite|revoke-invite)', 'STAFF_MANAGEMENT'),
    ('PATCH', f'/v1/staff/receptionists/{uuid}/permissions', 'STAFF_MANAGEMENT'),
}
print('PASS approved signed Owner, Receptionist and recovery routes')
'@
            $imports | & python -
            if ($LASTEXITCODE -ne 0) { throw "Import and boundary validation failed (exit $LASTEXITCODE)" }
            Invoke-Checked python @('-m', 'alembic', 'heads')
        }
        'integration' {
            if (-not (Test-Path -LiteralPath 'tests/integration/test_auth_mail.py' -PathType Leaf)) {
                throw 'Missing required F-07 integration test: tests/integration/test_auth_mail.py'
            }
            if (-not (Test-Path -LiteralPath 'tests/integration/test_tenant_isolation.py' -PathType Leaf)) {
                throw 'Missing required D-01 integration test: tests/integration/test_tenant_isolation.py'
            }
            if (-not (Test-Path -LiteralPath 'tests/integration/test_identity_persistence.py' -PathType Leaf)) {
                throw 'Missing required D-02 integration test: tests/integration/test_identity_persistence.py'
            }
            if (-not (Test-Path -LiteralPath 'tests/integration/test_evidence_persistence.py' -PathType Leaf)) {
                throw 'Missing required D-09 integration test: tests/integration/test_evidence_persistence.py'
            }
            if (-not (Test-Path -LiteralPath 'tests/integration/test_patient_persistence.py' -PathType Leaf)) {
                throw 'Missing required D-03 integration test: tests/integration/test_patient_persistence.py'
            }
            if (-not (Test-Path -LiteralPath 'tests/integration/test_policy_persistence.py' -PathType Leaf)) {
                throw 'Missing required D-04 integration test: tests/integration/test_policy_persistence.py'
            }
            foreach ($suite in @('tests/integration/test_appointment_persistence.py', 'tests/integration/test_appointment_races.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required D-05 integration test: $suite" }
            }
            if (-not (Test-Path -LiteralPath 'tests/integration/test_request_boundary.py' -PathType Leaf)) {
                throw 'Missing required B-01 integration test: tests/integration/test_request_boundary.py'
            }
            if (-not (Test-Path -LiteralPath 'tests/integration/test_sessions.py' -PathType Leaf)) {
                throw 'Missing required B-02 integration test: tests/integration/test_sessions.py'
            }
            foreach ($suite in @('tests/integration/test_provisioning.py', 'tests/integration/test_staff.py', 'tests/integration/test_recovery.py', 'tests/integration/test_identity_races.py', 'tests/e2e/owner-invite.spec.ts', 'tests/e2e/extract_mailpit_invitation.py', 'apps/web/mail/delivery-material.test.ts', 'tests/unit/test_delivery_material.py', 'apps/web/auth/postgres-adapter.integration.test.ts')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required B-03 verification: $suite" }
            }
            if ($env:CI -ne 'true' -or $env:RUNNER_OS -ne 'Linux' -or $PSVersionTable.PSVersion.Major -lt 7) {
                throw 'Integration requires Docker-capable Linux CI and PowerShell 7; no local Docker probe performed'
            }
            if ($env:CP_CI_PROJECT -notmatch '^carepulse-ci-[0-9]+-[0-9]+$' -or $env:COMPOSE_PROJECT_NAME -ne $env:CP_CI_PROJECT) {
                throw 'Explicit matching disposable CI project required'
            }
            foreach ($suite in @('tests/integration/test_patients.py', 'tests/integration/test_patient_races.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required B-05 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_pricing.py', 'tests/integration/test_pricing_races.py', 'tests/integration/test_pricing_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required B-06 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_schedule.py', 'tests/integration/test_schedule_races.py', 'tests/integration/test_schedule_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required B-07 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_slots.py', 'tests/integration/test_slot_races.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required B-08 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_booking.py', 'tests/integration/test_booking_races.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required B-09 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_followups.py', 'tests/integration/test_followups_denial.py', 'tests/integration/test_followup_decisions.py', 'tests/integration/test_followups_races.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required B-10 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_visit_persistence.py', 'tests/integration/test_visit_revisions.py', 'tests/integration/test_visit_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required D-07 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_followup_persistence.py', 'tests/integration/test_followup_races.py', 'tests/integration/test_followup_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required D-08 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_queue_persistence.py', 'tests/integration/test_queue_capacity.py', 'tests/integration/test_queue_races.py', 'tests/integration/test_queue_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required D-06 verification: $suite" }
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
  if [[ "$COMPOSE_PROJECT_NAME" =~ ^carepulse-ci-[0-9]+-[0-9]+$ && "$COMPOSE_PROJECT_NAME" = "$CP_CI_PROJECT" && "$private" = "$RUNNER_TEMP/foundation-ci-private" && -d "$private" && -O "$private" && ! -L "$private" ]]; then
    if ! docker compose --profile mail --profile test --profile sender --profile operator down --remove-orphans --volumes; then code=1; fi
    if ! rm -rf -- "$private" || test -e "$private"; then code=1; fi
  else
    echo 'FAIL unsafe cleanup project'; code=1
  fi
  exit "$code"
}
trap cleanup EXIT
for file in "$CP_DB_PASSWORD_FILE" "$CP_RUNTIME_DB_PASSWORD_FILE" "$CP_AUTH_DB_PASSWORD_SOURCE_FILE" "$CP_MIGRATION_SECRET_FILE" "$CP_OPERATOR_SECRET_FILE" "$CP_OPERATOR_CREDENTIAL_SOURCE_FILE" "$CP_SENDER_DB_PASSWORD_SOURCE_FILE" "$CP_SERVICE_KEY_SOURCE_FILE" "$CP_AUTH_SECRET_SOURCE_FILE" "$CP_FILE_ENCRYPTION_KEY_SOURCE_FILE"; do
  test "$(dirname "$file")" = "$private"
  openssl rand -hex 32 > "$file"
  chmod 0444 "$file"
done
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$CP_DELIVERY_PRIVATE_KEY_SOURCE_FILE" > /dev/null 2>&1
openssl pkey -in "$CP_DELIVERY_PRIVATE_KEY_SOURCE_FILE" -pubout -out "$CP_DELIVERY_PUBLIC_KEY_SOURCE_FILE" > /dev/null 2>&1
chmod 0444 "$CP_DELIVERY_PRIVATE_KEY_SOURCE_FILE" "$CP_DELIVERY_PUBLIC_KEY_SOURCE_FILE"
docker compose --profile mail --profile scan --profile test --profile sender --profile operator config --quiet
docker compose --profile mail --profile scan --profile test --profile sender --profile operator config --format json > "$private/compose.json"
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
docker compose --profile test --profile sender --profile operator build --pull
docker compose up -d --no-build db
for attempt in $(seq 1 40); do
  # TCP readiness excludes the temporary Unix-socket-only bootstrap server.
  if docker compose exec -T db pg_isready -h 127.0.0.1 -U carepulse_bootstrap -d carepulse > /dev/null 2>&1; then break; fi
  if [ "$attempt" = 40 ]; then echo 'FAIL disposable database startup'; exit 1; fi
  sleep 2
done
export CP_MIGRATION_PASSWORD="$(cat "$CP_MIGRATION_SECRET_FILE")"
export CP_RUNTIME_PASSWORD="$(cat "$CP_RUNTIME_DB_PASSWORD_FILE")"
export CP_AUTH_DB_PASSWORD="$(cat "$CP_AUTH_DB_PASSWORD_SOURCE_FILE")"
export CP_OPERATOR_PASSWORD="$(cat "$CP_OPERATOR_SECRET_FILE")"
export CP_SENDER_PASSWORD="$(cat "$CP_SENDER_DB_PASSWORD_SOURCE_FILE")"
if ! docker compose exec -T -e CP_MIGRATION_PASSWORD -e CP_RUNTIME_PASSWORD -e CP_AUTH_DB_PASSWORD -e CP_OPERATOR_PASSWORD -e CP_SENDER_PASSWORD \
  db psql --no-psqlrc -v VERBOSITY=sqlstate -U carepulse_bootstrap -d carepulse < db/roles.sql > "$private/roles.log" 2>&1; then
  roles_error="$(sed -nE 's/^psql:[^:]+:([0-9]+): ERROR: +([A-Z0-9]{5}).*$/roles.sql:\1 SQLSTATE \2/p' "$private/roles.log" | head -n 1)"
  echo "FAIL controlled roles (${roles_error:-SQLSTATE unavailable}; credential-bearing diagnostics withheld)"; exit 1
fi
unset CP_MIGRATION_PASSWORD CP_RUNTIME_PASSWORD CP_AUTH_DB_PASSWORD CP_OPERATOR_PASSWORD CP_SENDER_PASSWORD
docker compose --profile test run --rm --no-deps \
  -e CP_DB_HOST=db -e CP_DB_PORT=5432 -e CP_DB_NAME=carepulse \
  -e CP_MIGRATION_PASSWORD_FILE=/run/secrets/migration_password \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m alembic -c alembic.ini upgrade head

docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_AUTH_DB_PASSWORD_SOURCE_FILE:/run/secrets/auth_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  -v "$CP_SENDER_DB_PASSWORD_SOURCE_FILE:/run/secrets/sender_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider tests/integration/test_database.py tests/integration/test_tenant_isolation.py tests/integration/test_patient_persistence.py tests/integration/test_policy_persistence.py tests/integration/test_identity_persistence.py tests/integration/test_evidence_persistence.py tests/integration/test_request_boundary.py tests/integration/test_provisioning.py tests/integration/test_appointment_persistence.py tests/integration/test_appointment_races.py tests/integration/test_visit_persistence.py tests/integration/test_visit_revisions.py tests/integration/test_visit_migration.py tests/integration/test_followup_persistence.py tests/integration/test_followup_races.py tests/integration/test_followup_migration.py tests/integration/test_m1_migration.py | tee "$private/d08-core.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/d08-core.log"; then
  echo 'FAIL mandatory D-08 persistence evidence skipped'; exit 1
fi
# Independent D-06 suite must remain executable without D-07/Visit imports.
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  test python -m pytest -q -s -ra --tb=short -p no:cacheprovider \
  tests/integration/test_queue_persistence.py tests/integration/test_queue_capacity.py \
  tests/integration/test_queue_races.py tests/integration/test_queue_migration.py | tee "$private/d06-queue.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/d06-queue.log"; then
  echo 'FAIL mandatory D-06 real-PostgreSQL evidence skipped'; exit 1
fi
# All B-06 suites run against disposable PostgreSQL; no silent skip counts.
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_pricing.py tests/integration/test_pricing_races.py tests/integration/test_pricing_migration.py | tee "$private/b06-pricing.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b06-pricing.log"; then
  echo 'FAIL mandatory B-06 pricing evidence skipped'; exit 1
fi
# B-07 tests must run against disposable PostgreSQL, including preview races and migration.
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_schedule.py tests/integration/test_schedule_races.py tests/integration/test_schedule_migration.py | tee "$private/b07-schedule.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b07-schedule.log"; then
  echo 'FAIL mandatory B-07 schedule evidence skipped'; exit 1
fi
# B-08 tests exercise the real PostgreSQL booking-day lock and capacity races.
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_slots.py tests/integration/test_slot_races.py | tee "$private/b08-slots.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b08-slots.log"; then
  echo 'FAIL mandatory B-08 slot evidence skipped'; exit 1
fi
# B-09 confirms atomic Booked-only creation and real-PostgreSQL races.
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_booking.py tests/integration/test_booking_races.py | tee "$private/b09-booking.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b09-booking.log"; then
  echo 'FAIL mandatory B-09 booking evidence skipped'; exit 1
fi
# B-10 confirms Doctor decisions, exact extensions, bookings and denial/race evidence.
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_followups.py tests/integration/test_followups_denial.py \
  tests/integration/test_followup_decisions.py tests/integration/test_followups_races.py | tee "$private/b10-followups.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b10-followups.log"; then
  echo 'FAIL mandatory B-10 follow-up evidence skipped'; exit 1
fi
# B-05 backend proof has no BFF/invitation prerequisite and cannot silently skip.
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider -m 'not b05_browser' \
  tests/integration/test_patients.py tests/integration/test_patient_races.py | tee "$private/b05-backend.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b05-backend.log"; then
  echo 'FAIL mandatory B-05 backend evidence skipped'; exit 1
fi
docker compose up -d --no-build db api worker web
for attempt in $(seq 1 40); do
  if curl --silent --show-error --fail --max-time 2 http://127.0.0.1:3000/ > /dev/null 2>&1; then break; fi
  if [ "$attempt" = 40 ]; then echo 'FAIL runner-host web endpoint'; exit 1; fi
  sleep 2
done
echo 'PASS runner-host web http://127.0.0.1:3000/'
docker compose exec -T web node --experimental-strip-types --input-type=module <<'NODE'
import { getSchema } from 'better-auth/db';
import { getMigrations } from 'better-auth/db/migration';
import { betterAuth } from 'better-auth';
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { getAuth } from './auth.ts';
import { loadWebConfig } from './config.ts';
import { authDatabase } from './db.ts';

const auth = getAuth();
const options = auth.options;
await auth.$context;
if (options.verification?.storeIdentifier !== 'hashed' ||
    options.verification?.storeInDatabase !== true ||
    options.emailAndPassword?.revokeSessionsOnPasswordReset !== true) {
  throw new Error('FAIL native auth verification/revocation configuration');
}
const models = getSchema(options);
const expectedTables = ['user', 'session', 'account', 'verification',
  'recovery_operations', 'auth_recovery_completions'];
if (expectedTables.some(table => !models[table]) || Object.keys(models).length !== 6) {
  throw new Error('FAIL supported native auth model set');
}
for (const field of ['securityRevision', 'absoluteDeadline', 'lastActivity']) {
  if (models.session.fields[field]?.input !== false || models.session.fields[field]?.required !== true) {
    throw new Error('FAIL native session authority fields');
  }
}
const pool = authDatabase(loadWebConfig());
try {
  for (const table of expectedTables) {
    const found = await pool.query(
      "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = $1",
      [table],
    );
    const actual = new Set(found.rows.map(row => row.column_name));
    const expected = new Set(['id', ...Object.keys(models[table].fields)]);
    if (actual.size !== expected.size || [...expected].some(column => !actual.has(column))) {
      throw new Error(`FAIL supported native schema drift in ${table}`);
    }
  }
  // Better Auth 1.7.12 schema introspection only supports its Kysely bridge.
  // Inspect the same live schema using the original pool; runtime auth uses
  // our public custom adapter and must not invoke its migration generator.
  const plan = await getMigrations({ ...options, database: pool }, { throwOnUnsafe: false });
  if (plan.schemaProblems.length || plan.unsafeChanges.length ||
      plan.toBeCreated.length || plan.toBeAdded.length || plan.toBeAddedIndexes.length) {
    throw new Error('FAIL supported Better Auth migration schema/index compatibility');
  }

  // A test-only native API instance exercises digest storage; no public route
  // is enabled and the reset callback makes no SMTP/network call.
  const nativeUser = `synthetic-native-${randomUUID()}`;
  const nativeEmail = `synthetic-native-${randomUUID()}@example.invalid`;
  let proof;
  await pool.query(
    'INSERT INTO public."user" (id, name, email, "emailVerified", "createdAt", "updatedAt") VALUES ($1, $2, $3, true, clock_timestamp(), clock_timestamp())',
    [nativeUser, 'Synthetic native verification', nativeEmail],
  );
  try {
    const testAuth = betterAuth({
      baseURL: 'http://localhost:3000',
      secret: randomBytes(32).toString('hex'),
      database: pool,
      advanced: { database: { validateSchema: false } },
      verification: { storeInDatabase: true, storeIdentifier: 'hashed' },
      emailAndPassword: {
        enabled: true, disableSignUp: true,
        sendResetPassword: ({ token }) => { proof = token; },
      },
      logger: { disabled: true },
    });
    const response = await testAuth.api.requestPasswordReset({
      body: { email: nativeEmail }, headers: new Headers({ origin: 'http://localhost:3000' }),
    });
    if (response.status !== true || !proof) {
      throw new Error('FAIL native password-reset proof issuance');
    }
    const stored = await pool.query(
      'SELECT identifier FROM public."verification" WHERE value = $1', [nativeUser],
    );
    const digest = createHash('sha256').update(`reset-password:${proof}`).digest('base64url');
    if (stored.rows.length !== 1 || stored.rows[0].identifier !== digest ||
        stored.rows[0].identifier === `reset-password:${proof}`) {
      throw new Error('FAIL native proof identifier is not SHA-256 digest stored in database');
    }
    const nativeAdapter = (await testAuth.$context).internalAdapter;
    const consumed = await nativeAdapter.consumeVerificationValue(`reset-password:${proof}`);
    if (consumed?.value !== nativeUser ||
        await nativeAdapter.consumeVerificationValue(`reset-password:${proof}`) !== null) {
      throw new Error('FAIL native reset proof did not consume exactly once');
    }
    const remaining = await pool.query(
      'SELECT count(*)::int AS count FROM public."verification" WHERE value = $1', [nativeUser],
    );
    if (remaining.rows[0].count !== 0) {
      throw new Error('FAIL consumed native proof remained persisted');
    }
  } finally {
    await pool.query('DELETE FROM public."verification" WHERE value = $1', [nativeUser]);
    await pool.query('DELETE FROM public."user" WHERE id = $1', [nativeUser]);
  }
  console.log('PASS supported native Better Auth schema/index compatibility, hashed reset-proof issuance and server-only session fields');
} finally {
  await pool.end();
}
NODE
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
for key in CP_POLICY_FILE CP_DB_PASSWORD_FILE CP_AUTH_DB_PASSWORD_FILE CP_SERVICE_KEY_FILE CP_AUTH_SECRET_FILE; do
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
cat "$CP_DB_PASSWORD_FILE" "$CP_RUNTIME_DB_PASSWORD_FILE" "$CP_AUTH_DB_PASSWORD_SOURCE_FILE" "$CP_SERVICE_KEY_SOURCE_FILE" "$CP_AUTH_SECRET_SOURCE_FILE" \
  "$CP_FILE_ENCRYPTION_KEY_SOURCE_FILE" "$CP_MIGRATION_SECRET_FILE" "$CP_OPERATOR_SECRET_FILE" "$CP_OPERATOR_CREDENTIAL_SOURCE_FILE" "$CP_SENDER_DB_PASSWORD_SOURCE_FILE" > "$private/canaries"
chmod 0444 "$private/canaries"
awk 'length($0) != 64 || $0 !~ /^[0-9a-f]+$/ { invalid=1 } END { if (NR != 10 || invalid) exit 1 }' "$private/canaries"
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
# F-07 remains closed to unauthenticated callers with D-02 native storage present.
docker compose exec -T web node --experimental-strip-types mail/smoke.ts auth
docker compose exec -T web node --experimental-strip-types mail/smoke.ts mail
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_SERVICE_KEY_SOURCE_FILE:/run/secrets/service_key:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider tests/integration/test_auth_mail.py
if ! docker compose --profile test run --rm --no-deps \
  -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" \
  -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider tests/integration/test_sessions.py tests/integration/test_staff.py tests/integration/test_identity_races.py; then
  docker compose logs --no-color web 2>&1 | grep -aE 'B04_SYNTHETIC_STAGE' || true
  exit 1
fi
# B-02/B-03/B-04 are integrated: both actually invited roles are mandatory now.
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider -m b05_browser \
  tests/integration/test_patients.py | tee "$private/b05-invited.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b05-invited.log"; then
  echo 'FAIL mandatory B-05 invited-session evidence skipped'; exit 1
fi
docker compose exec -T web node --no-warnings --experimental-strip-types --test auth/ingress.test.mjs
echo 'PASS verified source separation, spoof resistance and sole Web listener'
# The sender target alone has Node, Mailpit access, and the private delivery key.
# Migration credentials are mounted ONLY into the disposable test invocation.
docker compose --profile sender run --rm --no-deps -T --entrypoint python \
  -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" -e COMPOSE_PROJECT_NAME="$CP_CI_PROJECT" \
  -v "$PWD/tests:/srv/tests:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  sender -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_delivery_sender.py -k 'not missing_mailpit'
if ! docker compose --profile sender run --rm --no-deps -T --entrypoint python \
  -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" -e COMPOSE_PROJECT_NAME="$CP_CI_PROJECT" \
  -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$PWD/tests:/srv/tests:ro" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_DB_PASSWORD_FILE:/run/secrets/postgres_password:ro" \
  sender -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_recovery.py; then
  docker compose logs --no-color web 2>&1 | grep -aE 'B04_SYNTHETIC_ISSUE' || true
  exit 1
fi
# The operator's identity is issued only in the disposable CI DB; the one-shot
# CLI authenticates with its own secret file, then a separate sender delivers.
docker compose --profile test run --rm --no-deps -T \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_OPERATOR_CREDENTIAL_SOURCE_FILE:/run/secrets/operator_credential:ro" \
  test python - <<'PY' > "$private/operator-id"
import hashlib
from pathlib import Path
from uuid import uuid4
import psycopg
password = Path('/run/secrets/migration_password').read_text().strip()
credential = Path('/run/secrets/operator_credential').read_text().strip()
actor = uuid4()
with psycopg.connect(host='db', dbname='carepulse', user='carepulse_migration', password=password) as connection:
    connection.execute('''INSERT INTO public.operator_identities
        (id, credential_digest, issued_by_reference, issued_at)
        VALUES (%s, %s, 'synthetic-B-03-CI', clock_timestamp())''',
        (actor, hashlib.sha256(credential.encode()).hexdigest()))
print(actor)
PY
operator_id="$(tail -n 1 "$private/operator-id")"
if ! [[ "$operator_id" =~ ^[a-f0-9-]{36}$ ]]; then echo 'FAIL synthetic operator identity'; exit 1; fi
docker compose --profile operator run --rm --no-deps -T operator clinics create \
  --operator-id "$operator_id" --reason synthetic-ci-acceptance \
  --branch 'Synthetic B-03 clinic' --owner-email first-owner@example.invalid \
  --idempotency-key synthetic-ci-create > "$private/clinic-create.json"
clinic_id="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["organization_id"])' "$private/clinic-create.json")"
if ! [[ "$clinic_id" =~ ^[a-f0-9-]{36}$ ]]; then echo 'FAIL synthetic clinic create'; exit 1; fi
set +e
sender_result="$(docker compose --profile sender run --rm --no-deps -T sender --organization-id "$clinic_id")"
sender_status=$?
set -e
if [ "$sender_status" -ne 0 ]; then
  case "$sender_result" in
    'Delivery unavailable'|READY|FAILED|STALE|NOT_DUE)
      echo "FAIL explicit first-Owner sender: $sender_result";;
    *) echo 'FAIL explicit first-Owner sender: process/error output unavailable';;
  esac
  exit 1
fi
if [ "$sender_result" != SENT ]; then
  case "$sender_result" in
    READY|FAILED|STALE|NOT_DUE) echo "FAIL explicit first-Owner sender outcome: $sender_result";;
    *) echo 'FAIL unexpected sender output';;
  esac
  exit 1
fi
docker compose --profile test run --rm --no-deps -T \
  test python tests/e2e/extract_mailpit_invitation.py > "$private/invite-link"
if ! grep -Eq '^http://127\.0\.0\.1:3000/auth/invite-accept#' "$private/invite-link"; then
  echo 'FAIL private Mailpit invitation artifact'; exit 1
fi
CP_INVITE_LINK_FILE="$private/invite-link" CP_INVITE_OWNER_EMAIL=first-owner@example.invalid \
  pnpm --filter @carepulse/web test:owner-invite
echo 'PASS real restricted operator, post-commit one-shot sender and browser Owner activation/login'
docker compose exec -T -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" \
  -e CP_ADAPTER_TEST_CLINIC="$clinic_id" web \
  node --no-warnings --experimental-strip-types --test auth/postgres-adapter.integration.test.ts
echo 'PASS real PostgreSQL Stage-1 Better Auth adapter parity and transaction isolation'
docker compose logs --no-color api > "$private/auth-logs"
if grep -Fq '__foundation_bff_must_not_forward__' "$private/auth-logs"; then
  echo 'FAIL BFF forwarded a protected request'; exit 1
else
  code=$?; if [ "$code" -ne 1 ]; then echo 'FAIL API no-passthrough log scan'; exit 1; fi
fi
# Demonstrate that the same real transport fails, rather than selecting a relay.
docker compose --profile mail stop mail
docker compose --profile sender run --rm --no-deps -T --entrypoint python \
  -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" -e COMPOSE_PROJECT_NAME="$CP_CI_PROJECT" \
  -e CP_EXPECT_MAILPIT_DOWN=true \
  -v "$PWD/tests:/srv/tests:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  sender -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_delivery_sender.py -k missing_mailpit
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
# The spike commits an ACTIVE synthetic Owner; run after empty-baseline assertions.
if [ "${CP_B04_SPIKE:-false}" = true ]; then
  docker compose --profile test run --rm --no-deps -T \
    -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
    -e CP_B04_SEED_FILE=/tmp/b04-seed.json \
    -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
    test sh -c 'python -m pytest -q -p no:cacheprovider tests/integration/b04_spike_seed.py >&2 && cat /tmp/b04-seed.json' | \
  docker compose run --rm --no-deps -T --entrypoint node \
    -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" \
    -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
    web --experimental-strip-types auth/b04-spike.test.ts
fi
{
  echo '### Wave 0 and Wave 1 D-01/D-02 foundation checks'
  echo "- Run: https://github.com/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID} (attempt ${GITHUB_RUN_ATTEMPT}, SHA ${GITHUB_SHA})"
  echo '- Pinned Linux amd64 images, builds and runtime versions: verified above'
  echo '- F-02 topology/persistence: 3 seed + 3 verify; runner/web/test vantage probes passed'
  echo '- F-03 process denials, source/build/image/history/runtime secret scans: passed'
  echo '- F-04 sole baseline, reset/rollback/privilege and same-connection context cases: passed'
  echo '- F-05 bounded status/failure/recovery/restart and fresh worker identity: passed'
  echo '- F-07 explicit PostgreSQL/no-cache auth, closed BFF/API, two Mailpit messages and mail-loss denial: passed'
  echo '- D-01 tenant/role isolation and migration rollback/privilege cases: passed'
  echo '- D-02 PostgreSQL identity races, immutable session authority, Better Auth migration compatibility and ephemeral native hashed reset-proof issuance: passed'
  echo '- D-07 admission-linked Visit, immutable clinical revisions, and safe predecessor migration checks: passed'
  echo '- B-10 Doctor Follow-up decisions, confirmed extensions, receipt recovery and booking races: passed'
  echo '- B-01 signed request, durable nonce replay, live authority/revocation, RLS and idempotent receipt boundary tests: passed'
  echo '- B-03 restricted operator and owner activation, separate committed sender and real Playwright invite/login: passed'
  echo '- Synthetic development only; production and later Waves remain closed'
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
