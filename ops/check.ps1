param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('inputs', 'python-quality', 'unit', 'web', 'imports', 'integration', 'integration-diagnostic', 'compat-diagnostic', 'compat-full')]
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

function Assert-JUnit {
    param([string]$Report)
    $validate = @'
import sys
from xml.etree import ElementTree
root = ElementTree.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == 'testsuite' else list(root)
if not suites or any(suite.tag != 'testsuite' for suite in suites):
    raise SystemExit('FAIL incomplete unit JUnit suite')
counts = [{field: int(suite.attrib[field]) for field in ('tests', 'failures', 'errors', 'skipped')} for suite in suites]
if sum(row['tests'] for row in counts) <= 0 or any(row['failures'] or row['errors'] or row['skipped'] for row in counts):
    raise SystemExit(f'FAIL incomplete unit JUnit coverage: {counts}')
print('PASS mandatory JUnit cases, zero skips:', sum(row['tests'] for row in counts))
'@
    & python -c $validate $Report
    if ($LASTEXITCODE -ne 0) { throw "Mandatory JUnit validation failed: $Report" }
}

function Assert-Tap {
    param([string]$Report)
    $validate = @'
import re, sys
from pathlib import Path
values = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    match = re.fullmatch(r'# (tests|pass|fail|cancelled|skipped|todo) ([0-9]+)', line)
    if match:
        if match.group(1) in values:
            raise SystemExit('FAIL repeated TAP summary')
        values[match.group(1)] = int(match.group(2))
if set(values) != {'tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo'}:
    raise SystemExit(f'FAIL missing TAP summary: {values}')
if values['tests'] <= 0 or values['pass'] != values['tests'] or any(values[field] for field in ('fail', 'cancelled', 'skipped', 'todo')):
    raise SystemExit(f'FAIL incomplete TAP coverage: {values}')
print('PASS mandatory TAP cases, zero skips:', values['tests'])
'@
    & python -c $validate $Report
    if ($LASTEXITCODE -ne 0) { throw "Mandatory TAP validation failed: $Report" }
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
    '.github/workflows/foundation.yml': '9a0f2dba53b47e4dca7b2b094e2b3c85bce3c86e80180e823ce5f8ef46dabe56',
    '.github/workflows/fast-feedback.yml': '814f40f9f2a09d6337e5d4e14f1d4851e88301e64c1f28d5e538d3549dccce8b',
    'compose.yaml': '7e23f17480731ba1544db9943c9a2e9b8d34942df04ba2f3663d9948a57b363e',
    'compose.compat.yaml': 'e88fe73556a5ff585a08f84f459ba3cd1fd56b5b28dd2fb75ebbc7d932efa0e0',
    'apps/api/Dockerfile': '8465e11d5b90d94704ec63072180f10c56dcf4becf471da84258ea21de5208c8',
    'apps/web/Dockerfile': 'f4c68d5acdb939573661e8d79b1e08c901fa54cf87993464942eedab8f8df93b',
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
        'unit' {
            $unitReport = Join-Path $(if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }) 'foundation-unit.xml'
            Invoke-Checked python @('-m', 'pytest', '-q', "--junitxml=$unitReport", 'tests/unit')
            Assert-JUnit $unitReport
        }
        'web' {
            Invoke-Checked pnpm @('typecheck')
            foreach ($webSuite in @('apps/web/auth/signing.test.ts', 'apps/web/mail/delivery-material.test.ts')) {
                $tapReport = Join-Path $(if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }) ((Split-Path $webSuite -Leaf) + '.tap')
                & node --no-warnings --experimental-strip-types --test --test-reporter=tap $webSuite | Tee-Object -FilePath $tapReport
                if ($LASTEXITCODE -ne 0) { throw "Node tests failed: $webSuite (exit $LASTEXITCODE)" }
                Assert-Tap $tapReport
            }
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
        { $_ -in @('integration', 'integration-diagnostic') } {
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
            foreach ($suite in @('tests/integration/test_followups.py', 'tests/integration/test_followups_denial.py', 'tests/integration/test_followup_decisions.py', 'tests/integration/test_followups_races.py', 'tests/integration/test_b10_writer_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required B-10 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_visit_persistence.py', 'tests/integration/test_visit_revisions.py', 'tests/integration/test_visit_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required D-07 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_followup_persistence.py', 'tests/integration/test_followup_races.py', 'tests/integration/test_followup_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required D-08 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_t133_migration.py', 'tests/integration/test_t133_seed.py', 'tests/integration/test_t133_runtime.py', 'tests/integration/test_t133_observer.py', 'ops/t133-ci.sh')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required T133 acceptance component: $suite" }
            }
            foreach ($suite in @('tests/integration/test_queue_persistence.py', 'tests/integration/test_queue_capacity.py', 'tests/integration/test_queue_races.py', 'tests/integration/test_queue_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required D-06 verification: $suite" }
            }
            foreach ($suite in @('tests/integration/test_day_review_persistence.py', 'tests/integration/test_report_query_plans.py', 'tests/integration/test_day_review_races.py', 'tests/integration/test_day_review_migration.py')) {
                if (-not (Test-Path -LiteralPath $suite -PathType Leaf)) { throw "Missing required D-11 verification: $suite" }
            }
            # Linux CI retains the accepted Bash probes; PowerShell checks their exit immediately.
            $integration = @'
set -euo pipefail
umask 077
focused_target=
foundation_lane="${CP_FOUNDATION_LANE:-standard}"
foundation_full=false
foundation_coverage_verified=false
foundation_tests=0
case "$foundation_lane" in ordinary|historical|standard) ;; *) echo 'FAIL unknown Foundation lane'; exit 1 ;; esac
if [[ "${CP_FOUNDATION_MODE:-}" = full ]]; then
  if [[ "$1" != integration || "$foundation_lane" = standard || -z "${CP_FOUNDATION_RECEIPT_PATH:-}" || "${CP_B04_SPIKE:-false}" != false || "${CP_T133_MODE:-false}" != false || -n "${CP_T133_SHA:-}" ]]; then
    echo 'FAIL full Foundation lane invocation'; exit 1
  fi
  foundation_full=true
elif [[ "$foundation_lane" != standard ]]; then
  echo 'FAIL Foundation lanes require full acceptance mode'; exit 1
fi
if [[ "${CP_T133_MODE:-false}" = true || -n "${CP_T133_SHA:-}" ]]; then
  if [[ "${CP_T133_MODE:-false}" != true || "${CP_B04_SPIKE:-false}" != false ||
        "${GITHUB_EVENT_NAME:-}" != workflow_dispatch ||
        "${GITHUB_REPOSITORY:-}" != ahmedali5696/care-pulse-3 ||
        "${GITHUB_REF:-}" != refs/heads/feature/wave-2-booking-and-scheduling ||
        "${CP_ENV:-}" != test || "${CP_SYNTHETIC_TEST_PROFILE:-}" != true ||
        ! "${CP_T133_SHA:-}" =~ ^[0-9a-f]{40}$ || "$CP_T133_SHA" != "$GITHUB_SHA" ||
        "$(git rev-parse HEAD)" != "$GITHUB_SHA" ||
        "$CP_CI_PROJECT" != "carepulse-ci-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" ||
        "$(realpath "$(git rev-parse --show-toplevel)")" != "$(realpath "$GITHUB_WORKSPACE")" ]]; then
    echo 'T133 DENIED: unauthorized execution context'; exit 1
  fi
fi
if [ "$1" = integration-diagnostic ]; then
  if [[ "${GITHUB_EVENT_NAME:-}" != workflow_dispatch || "${CP_B04_SPIKE:-false}" != false || "${CP_T133_MODE:-false}" != false || -n "${CP_T133_SHA:-}" ]]; then
    echo 'FAIL focused diagnostics require a manual dispatch without experimental modes'; exit 1
  fi
  case "${CP_POSTGRES_DIAGNOSTIC:-}" in
    authorization-lock) focused_target='tests/integration/test_request_boundary.py::test_suspension_serializes_with_protected_mutation' ;;
    booking-races) focused_target='tests/integration/test_booking_races.py' ;;
    followup-races) focused_target='tests/integration/test_followups_races.py' ;;
    f01-cleanup) focused_target='tests/integration/test_f01_runtime_identity.py::test_session_cleanup_requires_committed_matching_revocation[committed]' ;;
    f01-followup-lock) focused_target='tests/integration/test_followup_persistence.py::test_v2_creation_exception_cannot_authorize_same_date_interval_change' ;;
    f01-visit-lock) focused_target='tests/integration/test_visit_persistence.py::test_invalid_admission_shell_rejected_without_a_visit[forged_actor]' ;;
    f01-followup-context) focused_target='tests/integration/test_followup_persistence.py::test_v2_writer_rejects_invalid_context_without_evidence[missing]' ;;
    f01-pricing) focused_target='tests/integration/test_pricing.py' ;;
    f01-pricing-races) focused_target='tests/integration/test_pricing_races.py' ;;
    *) echo 'FAIL unapproved PostgreSQL diagnostic selection'; exit 1 ;;
  esac
  printf 'Focused PostgreSQL diagnostic only: %s (SHA %s, project %s)\n' "$focused_target" "$GITHUB_SHA" "$CP_CI_PROJECT"
fi
private="$RUNNER_TEMP/foundation-ci-private"
test ! -e "$private"
mkdir -m 0700 "$private"
cleanup() {
  local code=$? cleanup_failed=0
  trap - EXIT
  if [[ -n "${host_pid:-}" ]]; then
    kill -TERM "$host_pid" 2>/dev/null || true
    if ! wait "$host_pid"; then
      echo 'FAIL independent compatibility host supervisor'; cleanup_failed=1
    fi
    host_pid=''
  fi
  if [[ -n "${host_pid:-}" ]]; then
    kill -TERM "$host_pid" 2>/dev/null || true
    wait "$host_pid" 2>/dev/null || true
    host_pid=''
  fi
  if [[ "$COMPOSE_PROJECT_NAME" =~ ^carepulse-ci-[0-9]+-[0-9]+$ && "$COMPOSE_PROJECT_NAME" = "$CP_CI_PROJECT" && "$private" = "$RUNNER_TEMP/foundation-ci-private" && -d "$private" && -O "$private" && ! -L "$private" ]]; then
    if ! docker compose --profile mail --profile test --profile sender --profile operator down --remove-orphans --volumes; then
      echo 'FAIL scoped Compose cleanup'; cleanup_failed=1
    fi
    if ! rm -rf -- "$private" || test -e "$private"; then
      echo 'FAIL synthetic secret cleanup'; cleanup_failed=1
    fi
    if [ "$foundation_full" = true ] || [ "${CP_B04_SPIKE:-false}" = true ]; then
      local project remaining
      for project in "$CP_CI_PROJECT" "carepulse-ci-${GITHUB_RUN_ID}-0${GITHUB_RUN_ATTEMPT}"; do
        for resource in containers volumes networks; do
          case "$resource" in
            containers) if ! remaining="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"; then cleanup_failed=1; continue; fi ;;
            volumes) if ! remaining="$(docker volume ls -q --filter "label=com.docker.compose.project=$project")"; then cleanup_failed=1; continue; fi ;;
            networks) if ! remaining="$(docker network ls -q --filter "label=com.docker.compose.project=$project")"; then cleanup_failed=1; continue; fi ;;
          esac
          if [ -n "$remaining" ]; then echo "FAIL residual $resource for $project"; cleanup_failed=1; fi
        done
      done
    fi
    if [ "$cleanup_failed" = 1 ]; then
      code=1
    elif [ -n "$focused_target" ] || [ "${CP_B04_SPIKE:-false}" = true ]; then
      echo 'PASS scoped PostgreSQL volume and synthetic secret cleanup'
    fi
  else
    echo 'FAIL unsafe cleanup project'; code=1
  fi
  if [ "$code" -eq 0 ] && [ "$foundation_full" = true ]; then
    if [[ "$foundation_coverage_verified" != true || "$foundation_tests" -le 0 || -e "$private" ]]; then
      echo 'FAIL incomplete Foundation evidence or private cleanup'; code=1
    else
      export CP_FOUNDATION_CLEANUP_VERIFIED=1 CP_FOUNDATION_COVERAGE_VERIFIED=1
      export CP_FOUNDATION_EXECUTION=PASS CP_FOUNDATION_TESTS=PASS CP_FOUNDATION_ZERO_SKIPS=true CP_FOUNDATION_CLEANUP=PASS
      export CP_FOUNDATION_COVERAGE="$(if [ "$foundation_lane" = ordinary ]; then echo current-head-excluding-extracted-historical; else echo immutable-0040-selected-nodes-on-isolated-postgres; fi)"
      export CP_FOUNDATION_CANDIDATE_SCHEMA=0046_f01_audit_integrity
      export CP_FOUNDATION_HISTORICAL_SHA=6cf2973c07f6e0ef2d8040d1eb3c235fd1598df7
      export CP_FOUNDATION_HISTORICAL_SCHEMA=0040_t173_numeric_money
      export CP_FOUNDATION_VERIFIED_TESTS="$foundation_tests" CP_FOUNDATION_LANE="$foundation_lane"
      if ! python3 -m ops.foundation_acceptance seal "$CP_FOUNDATION_RECEIPT_PATH"; then
        echo 'FAIL terminal Foundation receipt seal'; code=1
      fi
    fi
  fi
  exit "$code"
}
trap cleanup EXIT
mkdir -m 1777 "$private/foundation-results"
for file in "$CP_DB_PASSWORD_FILE" "$CP_RUNTIME_DB_PASSWORD_FILE" "$CP_API_DB_PASSWORD_SOURCE_FILE" "$CP_WEB_DB_PASSWORD_SOURCE_FILE" "$CP_WORKER_DB_PASSWORD_SOURCE_FILE" "$CP_GATE_DB_PASSWORD_SOURCE_FILE" "$CP_DB_IDENTITY_KEY_SOURCE_FILE" "$CP_AUTH_DB_PASSWORD_SOURCE_FILE" "$CP_MIGRATION_SECRET_FILE" "$CP_OPERATOR_SECRET_FILE" "$CP_OPERATOR_CREDENTIAL_SOURCE_FILE" "$CP_SENDER_DB_PASSWORD_SOURCE_FILE" "$CP_SERVICE_KEY_SOURCE_FILE" "$CP_AUTH_SECRET_SOURCE_FILE" "$CP_FILE_ENCRYPTION_KEY_SOURCE_FILE"; do
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
  if [ -n "$focused_target" ]; then
    # Focused PostgreSQL runs use only the pinned Python test image and DB.
    case "$image" in python:*|postgres:*) ;; *) continue ;; esac
  fi
  docker pull --platform linux/amd64 --quiet "$image" > /dev/null
  test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" = linux/amd64
  docker image inspect --format '{{json .RepoDigests}} {{.Id}} {{.Os}}/{{.Architecture}}' "$image"
done
if [ -n "$focused_target" ] || [ "$foundation_lane" = historical ]; then
  docker compose --profile test build --pull test
else
  docker compose --profile test --profile sender --profile operator build --pull
fi
docker compose up -d --no-build db
for attempt in $(seq 1 40); do
  # TCP readiness excludes the temporary Unix-socket-only bootstrap server.
  if docker compose exec -T db pg_isready -h 127.0.0.1 -U carepulse_bootstrap -d carepulse > /dev/null 2>&1; then break; fi
  if [ "$attempt" = 40 ]; then echo 'FAIL disposable database startup'; exit 1; fi
  sleep 2
done
export CP_MIGRATION_PASSWORD="$(cat "$CP_MIGRATION_SECRET_FILE")"
export CP_RUNTIME_PASSWORD="$(cat "$CP_RUNTIME_DB_PASSWORD_FILE")"
export CP_API_DB_PASSWORD="$(cat "$CP_API_DB_PASSWORD_SOURCE_FILE")"
export CP_WEB_DB_PASSWORD="$(cat "$CP_WEB_DB_PASSWORD_SOURCE_FILE")"
export CP_WORKER_DB_PASSWORD="$(cat "$CP_WORKER_DB_PASSWORD_SOURCE_FILE")"
export CP_GATE_DB_PASSWORD="$(cat "$CP_GATE_DB_PASSWORD_SOURCE_FILE")"
export CP_AUTH_DB_PASSWORD="$(cat "$CP_AUTH_DB_PASSWORD_SOURCE_FILE")"
export CP_OPERATOR_PASSWORD="$(cat "$CP_OPERATOR_SECRET_FILE")"
export CP_SENDER_PASSWORD="$(cat "$CP_SENDER_DB_PASSWORD_SOURCE_FILE")"
if ! docker compose exec -T -e CP_MIGRATION_PASSWORD -e CP_RUNTIME_PASSWORD -e CP_AUTH_DB_PASSWORD -e CP_OPERATOR_PASSWORD -e CP_SENDER_PASSWORD -e CP_API_DB_PASSWORD -e CP_WEB_DB_PASSWORD -e CP_WORKER_DB_PASSWORD -e CP_GATE_DB_PASSWORD \
  db psql --no-psqlrc -v VERBOSITY=sqlstate -U carepulse_bootstrap -d carepulse < db/roles.sql > "$private/roles.log" 2>&1; then
  roles_error="$(sed -nE 's/^psql:[^:]+:([0-9]+): ERROR: +([A-Z0-9]{5}).*$/roles.sql:\1 SQLSTATE \2/p' "$private/roles.log" | head -n 1)"
  echo "FAIL controlled roles (${roles_error:-SQLSTATE unavailable}; credential-bearing diagnostics withheld)"; exit 1
fi
unset CP_MIGRATION_PASSWORD CP_RUNTIME_PASSWORD CP_AUTH_DB_PASSWORD CP_OPERATOR_PASSWORD CP_SENDER_PASSWORD CP_API_DB_PASSWORD CP_WEB_DB_PASSWORD CP_WORKER_DB_PASSWORD CP_GATE_DB_PASSWORD
identity_fixture_args=(
  -e CP_DB_IDENTITY_KEY_ID
  -v "$CP_DB_IDENTITY_KEY_SOURCE_FILE:/run/secrets/db_identity_key:ro"
  -v "$CP_API_DB_PASSWORD_SOURCE_FILE:/run/secrets/api_db_password:ro"
  -v "$CP_GATE_DB_PASSWORD_SOURCE_FILE:/run/secrets/gate_db_password:ro"
)
upgrade_head() {
  docker compose --profile test run --rm --no-deps \
    -e CP_DB_HOST=db -e CP_DB_PORT=5432 -e CP_DB_NAME=carepulse \
    -e CP_MIGRATION_PASSWORD_FILE=/run/secrets/migration_password \
    -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
    test python -m alembic -c alembic.ini upgrade head
}

# Synthetic-only custody setup. The ordinary runtime roles have no key-table
# access. Provisioning is performed outside all application containers.
seed_identity_policy() {
  set +x
  export CP_F01_SYNTHETIC_KEY="$(cat "$CP_DB_IDENTITY_KEY_SOURCE_FILE")"
  export CP_F01_FRESHNESS="$(jq -er 'select(.profile == "synthetic") | .signed_request_freshness_seconds' tests/fixtures/foundation-policy.json)"
  export CP_F01_SKEW="$(jq -er 'select(.profile == "synthetic") | .signed_request_max_skew_seconds' tests/fixtures/foundation-policy.json)"
  export CP_F01_IDLE="$(jq -er 'select(.profile == "synthetic") | .session_idle_minutes' tests/fixtures/foundation-policy.json)"
  docker compose exec -T -e CP_F01_SYNTHETIC_KEY -e CP_DB_IDENTITY_KEY_ID -e CP_F01_FRESHNESS -e CP_F01_SKEW -e CP_F01_IDLE \
    db psql --no-psqlrc -v VERBOSITY=sqlstate -U carepulse_bootstrap -d carepulse < db/f01-synthetic-bootstrap.sql > "$private/f01-bootstrap.log" 2>&1 || {
      unset CP_F01_SYNTHETIC_KEY CP_F01_FRESHNESS CP_F01_SKEW CP_F01_IDLE
      echo 'FAIL F-01 synthetic identity provisioning'; return 1;
    }
  unset CP_F01_SYNTHETIC_KEY CP_F01_FRESHNESS CP_F01_SKEW CP_F01_IDLE
}

if [ -n "$focused_target" ]; then
  if ! upgrade_head > "$private/focused-migration.log" 2>&1; then
    echo 'FAIL focused PostgreSQL Alembic upgrade to head'; exit 1
  fi
  echo 'PASS focused PostgreSQL Alembic upgrade to head'
  seed_identity_policy
  docker compose exec -T db postgres --version | grep -F '17.11'
  docker compose --profile test run --rm --no-deps -T "${identity_fixture_args[@]}" \
    -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
    -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
    -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
    -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
    test python tests/integration/focused_postgres.py "$focused_target"
  exit 0
fi

# Historical migration assertions use the accepted 0040 graph and a different
# PostgreSQL cluster, never a re-enabled login in the current-head cluster.
historical_nodes=(
  'tests/integration/test_t133_migration.py::test_t133_populated_0032_upgrade_preserves_claim_and_new_exam'
  'tests/integration/test_t133_migration.py::test_t133_failed_upgrade_0032_rolls_back_every_new_object'
  'tests/integration/test_m1_migration.py'
  'tests/integration/test_queue_migration.py'
  'tests/integration/test_day_review_migration.py'
  'tests/integration/test_schedule_migration.py'
  'tests/integration/test_b10_writer_migration.py'
  'tests/integration/test_visit_migration.py::test_version_and_fresh_upgrade_preserve_predecessor_records'
  'tests/integration/test_visit_migration.py::test_existing_unbacked_admission_refuses_upgrade_atomically'
  'tests/integration/test_visit_migration.py::test_failed_later_revision_rolls_back_and_keeps_d07'
  'tests/integration/test_visit_migration.py::test_populated_0025_upgrade_preserves_legacy_clinical_history_and_receipts'
  'tests/integration/test_visit_migration.py::test_failed_0026_upgrade_reverts_objects_and_predecessor_version'
  'tests/integration/test_followup_migration.py::test_v2_populated_immediate_predecessor_preserves_legacy_evidence_and_denies_unattested_actions'
  'tests/integration/test_followup_migration.py::test_v2_upgrade_replays_correction_ancestry_and_never_invents_doctor_default'
  'tests/integration/test_followup_migration.py::test_v2_failed_populated_upgrade_rolls_back_and_repeat_upgrade_is_stable'
  'tests/integration/test_followup_migration.py::test_v2_first_extension_uses_verified_legacy_deadline_without_rewriting_it'
  'tests/integration/test_followup_migration.py::test_predecessor_data_and_security_survive_compatible_upgrade'
  'tests/integration/test_followup_migration.py::test_incompatible_unpaired_predecessor_claim_refuses_atomically'
  'tests/integration/test_followup_migration.py::test_fresh_and_failed_following_migration_keep_one_head'
  'tests/integration/test_followup_migration.py::test_populated_0023_followup_evidence_survives_0024_and_new_claims_use_live_policy'
  'tests/integration/test_pricing_migration.py::test_predecessor_legacy_numeric_null_snapshots_receipts_survive_upgrade'
  'tests/integration/test_pricing_migration.py::test_fresh_install_and_repeated_upgrade_are_no_ops'
  'tests/integration/test_pricing_migration.py::test_failed_following_revision_keeps_head_and_ddl_atomic'
  'tests/integration/test_pricing_migration.py::test_t145_upgrade_preserves_historical_numeric_percentage_and_receipt'
  'tests/integration/test_pricing_migration.py::test_t173_populated_0039_money_successor_and_atomic_failed_upgrade'
  'tests/integration/test_policy_persistence.py::test_older_clinics_backfilled_and_new_clinics_triggered_in_both_orders'
  'tests/integration/test_followup_persistence.py::test_v2_doctor_priority_blocks_receptionist_reset_exception_and_extension'
  'tests/integration/test_followup_persistence.py::test_v2_declared_new_date_without_event_rolls_back_all_creation_evidence'
  'tests/integration/test_followup_persistence.py::test_completed_new_examination_sources_create_active_claim'
  'tests/integration/test_followup_persistence.py::test_nonqualifying_source_cannot_claim'
  'tests/integration/test_followup_persistence.py::test_followup_target_must_match_source_tenant_patient_doctor'
  'tests/integration/test_followup_persistence.py::test_one_active_source_across_different_service_dates'
  'tests/integration/test_followup_persistence.py::test_claim_cannot_forge_frozen_authorization_evidence'
  'tests/integration/test_followup_persistence.py::test_consumed_source_cannot_regain_active_or_second_consumed_claim'
  'tests/integration/test_followup_persistence.py::test_released_prior_claim_cannot_become_second_consumption'
  'tests/integration/test_followup_persistence.py::test_source_authorization_and_revision_required'
  'tests/integration/test_followup_persistence.py::test_released_history_allows_fresh_eligibility_and_price_snapshot'
  'tests/integration/test_followup_persistence.py::test_default_claim_uses_live_followup_window_and_rebook_freezes_new_policy'
  'tests/integration/test_followup_persistence.py::test_zero_day_followup_policy_qualifies_and_confirms_only_completion_day'
  'tests/integration/test_followup_persistence.py::test_integral_decimal_followup_policy_payload_is_accepted'
  'tests/integration/test_followup_persistence.py::test_claim_and_preferential_appointment_must_both_exist_at_commit'
  'tests/integration/test_followup_persistence.py::test_cancel_no_show_and_exceptional_admission_release_in_same_commit'
  'tests/integration/test_followup_persistence.py::test_completed_followup_consumes_once_and_retains_immutable_history'
  'tests/integration/test_followup_persistence.py::test_injected_failure_after_appointment_write_rolls_back_both_sides'
  'tests/integration/test_followup_persistence.py::test_unpaired_terminal_transition_cannot_commit'
  'tests/integration/test_followup_persistence.py::test_tenant_rls_no_context_operator_denied_and_narrow_runtime_grants'
  'tests/integration/test_followup_races.py::test_v2_waiting_writer_refreshes_source_claim_and_empty_authorization_ledger[release]'
  'tests/integration/test_followup_races.py::test_v2_waiting_writer_refreshes_source_claim_and_empty_authorization_ledger[consume]'
  'tests/integration/test_followup_races.py::test_v2_extension_first_remains_historical_after_later_restriction_or_terminal[release]'
  'tests/integration/test_followup_races.py::test_v2_extension_first_remains_historical_after_later_restriction_or_terminal[consume]'
  'tests/integration/test_followup_races.py::test_v2_claim_waits_for_release_and_cannot_reuse_consumed_source'
  'tests/integration/test_followup_races.py::test_two_simultaneous_claims_leave_one_active_and_no_orphan_appointment'
  'tests/integration/test_followup_races.py::test_terminal_first_holds_claim_until_release_or_permanent_consumption'
  'tests/integration/test_followup_races.py::test_claim_attempt_first_respects_later_terminal_outcome'
  'tests/integration/test_followup_races.py::test_claim_first_staged_booking_contends_with_terminal_before_commit'
  'tests/integration/test_followup_races.py::test_extension_and_competing_claim_serialize_without_new_reservation'
  'tests/integration/test_followup_races.py::test_extension_rejects_unrelated_clinical_audit_evidence'
  'tests/integration/test_followup_races.py::test_deadline_day_is_inclusive_but_next_day_loses_parallel_race'
  # Pre-cutover writer contracts: the current API has no permitted Wave 3 writer path.
  'tests/integration/test_visit_persistence.py::test_new_completion_attests_actual_staff_and_history_without_clinical_revision'
  'tests/integration/test_visit_persistence.py::test_completion_rejects_invalid_assertion_actor_scope_and_revision'
  'tests/integration/test_visit_persistence.py::test_completion_duplicate_and_direct_evidence_forgery_are_denied'
  'tests/integration/test_visit_revisions.py::test_receptionist_completion_preserves_independent_doctor_corrections'
  'tests/integration/test_visit_revisions.py::test_terminal_doctor_correction_keeps_status_and_actual_revision'
  'tests/integration/test_visit_revisions.py::test_unauthorized_or_unjustified_correction_denied'
  'tests/integration/test_visit_revisions.py::test_disabled_doctor_cannot_edit_clinical_content'
  'tests/integration/test_visit_migration.py::test_competing_completions_consume_one_visit_revision'
  'tests/integration/test_visit_migration.py::test_concurrent_clinical_correction_and_completion_preserve_both_evidence'
  'tests/integration/test_day_review_persistence.py::test_all_six_states_and_retained_visit_survive_cairo_midnight_and_day_close'
  'tests/integration/test_day_review_persistence.py::test_review_amendments_append_history_and_stale_revision_cannot_overwrite'
  'tests/integration/test_day_review_persistence.py::test_verified_receptionist_can_record_initial_day_reviews_but_cannot_amend'
  'tests/integration/test_day_review_persistence.py::test_day_reviews_enforce_tenant_actor_context_and_operator_denial'
  'tests/integration/test_day_review_races.py::test_direct_waiting_transition_serializes_with_final_close'
  'tests/integration/test_day_review_races.py::test_first_queue_day_insert_waits_on_final_close_barrier_in_both_orders'
  'tests/integration/test_day_review_races.py::test_direct_active_queue_entry_update_uses_same_barrier'
  'tests/integration/test_day_review_races.py::test_direct_active_entry_insert_waits_for_barrier_before_rejecting_booked_source'
  'tests/integration/test_followup_races.py::test_current_head_doctor_clinical_correction_and_extension_preserve_independent_revisions'
  'tests/integration/test_followups_races.py::test_release_or_consume_races_fresh_claim_without_double_use'
  'tests/integration/test_followups_races.py::test_claim_confirmation_first_blocks_terminal_until_booking_commits'
  'tests/integration/test_queue_capacity.py::test_scoped_confirmation_consumes_once_in_rolled_back_sql_simulation'
  'tests/integration/test_queue_capacity.py::test_one_shared_doctor_control_across_lane_and_original_date'
  'tests/integration/test_queue_capacity.py::test_doctor_scoped_confirmation_validates_revision_and_one_use_transition'
  'tests/integration/test_queue_capacity.py::test_non_doctor_cannot_revoke_scoped_approval'
  'tests/integration/test_queue_races.py::test_shared_doctor_control_blocks_alternate_lane_and_original_date'
  'tests/integration/test_queue_races.py::test_cross_date_cross_lane_provisional_admission_serializes_then_rolls_back'
  'tests/integration/test_queue_races.py::test_provisional_confirmation_is_one_use_and_invisible_until_rollback'
  'tests/integration/test_followups.py::test_pre0029_preferential_v1_receipt_survives_upgrade_and_signed_replay'
  'tests/integration/test_followup_decisions.py::test_signed_pre0029_v1_extension_receipt_survives_upgrade_and_authority_checks'
  'tests/integration/test_followup_migration.py::test_retained_active_unattested_claim_conflicts_at_current_head'
)
historical_deselect=()
ordinary_core_migration_files=(tests/integration/test_t133_migration.py tests/integration/test_m1_migration.py)
ordinary_queue_migration_files=(tests/integration/test_queue_migration.py)
ordinary_schedule_migration_files=(tests/integration/test_schedule_migration.py)
ordinary_b10_migration_files=(tests/integration/test_b10_writer_migration.py)
ordinary_day_review_migration_files=(tests/integration/test_day_review_migration.py)
run_historical_migrations() (
  set -euo pipefail
  test "${CP_T133_MODE:-false}" = false
  test -z "${CP_T133_SHA:-}"
  local proof="${1:-migrations}"
  local historical_sha=6cf2973c07f6e0ef2d8040d1eb3c235fd1598df7
  local historical_head=0040_t173_numeric_money
  local historical_project="carepulse-ci-${GITHUB_RUN_ID}-0${GITHUB_RUN_ATTEMPT}"
  local historical_dir="$private/historical"
  if [ "$proof" = b04 ]; then
    test "${CP_B04_SPIKE:-false}" = true
    test "$foundation_full" = false
    # Original Issue #7 feasibility evidence, before 0013 revoked native account access.
    historical_sha=696a5969d12b3e7f3801eb86591fe65113ac9eef
    historical_head=0008_provisioning_admin
    historical_dir="$private/historical-b04"
  else
    test "$proof" = migrations
  fi
  local historical_root="$historical_dir/source"
  local test_image="$COMPOSE_PROJECT_NAME-test"
  [[ "$historical_project" =~ ^carepulse-ci-[0-9]+-[0-9]+$ ]]
  test "$historical_project" != "$COMPOSE_PROJECT_NAME"
  test ! -e "$historical_dir"
  mkdir -m 0700 "$historical_dir" "$historical_root"
  # Checkout is shallow; the public origin is fetched by immutable object ID only.
  if ! git cat-file -e "$historical_sha^{commit}" 2>/dev/null; then
    GIT_TERMINAL_PROMPT=0 git fetch --no-tags --depth=1 origin "$historical_sha"
  fi
  test "$(git rev-parse "$historical_sha^{commit}")" = "$historical_sha"
  local -a historical_graph_files
  mapfile -t historical_graph_files < <(git ls-tree -r --name-only "$historical_sha" -- db/migrations)
  if [ "$proof" = b04 ]; then
    test "${#historical_graph_files[@]}" -gt 8
  else
    test "${#historical_graph_files[@]}" -gt 40
  fi
  git diff --exit-code "$historical_sha" -- "${historical_graph_files[@]}"
  git archive "$historical_sha" -- alembic.ini pyproject.toml db apps/api/carepulse apps/worker tests/integration tests/fixtures | tar -x -C "$historical_root"
  mkdir "$historical_root/private-files"
  chmod -R a+rX "$historical_root"
  test "$(docker image inspect --format '{{.Id}}' "$test_image")" != ""
  printf 'services:\n  test:\n    image: %s\n' "$test_image" > "$historical_dir/compose-override.yaml"
  if [ "$proof" = b04 ]; then
    # Reuse the pinned images only with identical proof sources and dependency locks.
    git diff --exit-code "$historical_sha" -- apps/web/auth/b04-spike.test.ts apps/web/auth/schema.ts apps/web/mail/delivery-material.ts tests/integration/b04_spike_seed.py apps/web/package.json pnpm-lock.yaml requirements.lock
    test "$(docker image inspect --format '{{.Id}}' "$COMPOSE_PROJECT_NAME-web")" != ""
    printf '  web:\n    image: %s\n    secrets: !reset []\n' "$COMPOSE_PROJECT_NAME-web" >> "$historical_dir/compose-override.yaml"
  fi
  # Never share the active cluster's credentials, volumes or network.
  export CP_DB_PASSWORD_FILE="$historical_dir/postgres-password"
  export CP_RUNTIME_DB_PASSWORD_FILE="$historical_dir/runtime-password"
  export CP_MIGRATION_SECRET_FILE="$historical_dir/migration-password"
  export CP_AUTH_DB_PASSWORD_SOURCE_FILE="$historical_dir/auth-password"
  export CP_OPERATOR_SECRET_FILE="$historical_dir/operator-password"
  export CP_OPERATOR_DB_PASSWORD_SOURCE_FILE="$CP_OPERATOR_SECRET_FILE"
  export CP_SENDER_DB_PASSWORD_SOURCE_FILE="$historical_dir/sender-password"
  for file in "$CP_DB_PASSWORD_FILE" "$CP_RUNTIME_DB_PASSWORD_FILE" "$CP_MIGRATION_SECRET_FILE" "$CP_AUTH_DB_PASSWORD_SOURCE_FILE" "$CP_OPERATOR_SECRET_FILE" "$CP_SENDER_DB_PASSWORD_SOURCE_FILE"; do
    openssl rand -hex 32 > "$file"
    chmod 0444 "$file"
  done
  historical_compose() {
    docker compose -p "$historical_project" -f "$PWD/compose.yaml" -f "$historical_dir/compose-override.yaml" "$@"
  }
  historical_compose --profile test config --format json > "$historical_dir/compose.json"
  chmod 0444 "$historical_dir/compose.json"
  jq -e --arg project "$historical_project" '
    .name == $project and .services.db.environment.POSTGRES_DB == "carepulse" and
    (.services.db.ports // [] | length == 0) and
    .volumes.db_data.name == ($project + "_db_data") and
    .networks.database.name == ($project + "_database") and .networks.database.internal == true
  ' "$historical_dir/compose.json" > /dev/null
  if [ "$proof" = b04 ]; then
    jq -e '(.services.web.secrets // [] | length) == 0' "$historical_dir/compose.json" > /dev/null
  fi
  test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$historical_project")"
  test -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$historical_project")"
  test -z "$(docker network ls -q --filter "label=com.docker.compose.project=$historical_project")"
  historical_cleanup() {
    local code=$?
    trap - EXIT
    if ! historical_compose --profile test down --remove-orphans --volumes; then
      echo 'FAIL historical Compose cleanup'; code=1
    fi
    for resource in containers volumes networks; do
      case "$resource" in
        containers) remaining="$(docker ps -aq --filter "label=com.docker.compose.project=$historical_project")" || code=1 ;;
        volumes) remaining="$(docker volume ls -q --filter "label=com.docker.compose.project=$historical_project")" || code=1 ;;
        networks) remaining="$(docker network ls -q --filter "label=com.docker.compose.project=$historical_project")" || code=1 ;;
      esac
      if [[ -n "$remaining" ]]; then echo "FAIL historical $resource remain"; code=1; fi
    done
    exit "$code"
  }
  trap historical_cleanup EXIT
  historical_compose up -d --no-build db
  for attempt in $(seq 1 40); do
    if historical_compose exec -T db pg_isready -h 127.0.0.1 -U carepulse_bootstrap -d carepulse > /dev/null 2>&1; then break; fi
    if [ "$attempt" = 40 ]; then echo 'FAIL historical database startup'; exit 1; fi
    sleep 2
  done
  export CP_MIGRATION_PASSWORD="$(cat "$CP_MIGRATION_SECRET_FILE")"
  export CP_RUNTIME_PASSWORD="$(cat "$CP_RUNTIME_DB_PASSWORD_FILE")"
  export CP_AUTH_DB_PASSWORD="$(cat "$CP_AUTH_DB_PASSWORD_SOURCE_FILE")"
  export CP_OPERATOR_PASSWORD="$(cat "$CP_OPERATOR_SECRET_FILE")"
  export CP_SENDER_PASSWORD="$(cat "$CP_SENDER_DB_PASSWORD_SOURCE_FILE")"
  if ! historical_compose exec -T -e CP_MIGRATION_PASSWORD -e CP_RUNTIME_PASSWORD -e CP_AUTH_DB_PASSWORD -e CP_OPERATOR_PASSWORD -e CP_SENDER_PASSWORD \
    db psql --no-psqlrc -v VERBOSITY=sqlstate -U carepulse_bootstrap -d carepulse < "$historical_root/db/roles.sql" > "$historical_dir/roles.log" 2>&1; then
    echo 'FAIL historical controlled roles (credential-bearing diagnostics withheld)'; exit 1
  fi
  unset CP_MIGRATION_PASSWORD CP_RUNTIME_PASSWORD CP_AUTH_DB_PASSWORD CP_OPERATOR_PASSWORD CP_SENDER_PASSWORD
  local -a historical_test_args=(
    --profile test run --rm --no-deps -T
    -e CP_CI_PROJECT="$historical_project" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json
    -v "$historical_root:/srv:ro"
    -v "$historical_dir/compose.json:/tmp/foundation-compose.json:ro"
    -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro"
    -v "$CP_AUTH_DB_PASSWORD_SOURCE_FILE:/run/secrets/auth_db_password:ro"
    -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro"
    -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro"
    -v "$CP_SENDER_DB_PASSWORD_SOURCE_FILE:/run/secrets/sender_password:ro"
  )
  test "$(historical_compose "${historical_test_args[@]}" test python -m alembic -c alembic.ini heads)" = "$historical_head (head)"
  historical_compose "${historical_test_args[@]}" \
    -e CP_DB_HOST=db -e CP_DB_PORT=5432 -e CP_DB_NAME=carepulse \
    -e CP_MIGRATION_PASSWORD_FILE=/run/secrets/migration_password \
    test python -m alembic -c alembic.ini upgrade "$historical_head"
  test "$(historical_compose exec -T db psql --no-psqlrc -At -U carepulse_bootstrap -d carepulse -c 'SELECT version_num FROM public.alembic_version')" = "$historical_head"
  echo "Historical migration evidence: $historical_sha ($historical_head), cluster $historical_project"
  mkdir -m 1777 "$historical_dir/results"
  if [ "$proof" = migrations ]; then
    historical_compose "${historical_test_args[@]}" -v "$historical_dir/results:/tmp/foundation-results:rw" test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/historical.xml "${historical_nodes[@]}" | tee "$historical_dir/tests.log"
    if grep -Eq '[0-9]+ skipped|no tests ran' "$historical_dir/tests.log"; then
      echo 'FAIL mandatory historical migration evidence skipped'; exit 1
    fi
    python3 - "$historical_dir/results/historical.xml" <<'PY'
import sys
from xml.etree import ElementTree
root = ElementTree.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == 'testsuite' else list(root)
if len(suites) != 1 or suites[0].tag != 'testsuite':
    raise SystemExit('FAIL ambiguous historical JUnit')
stats = {key: int(suites[0].attrib[key]) for key in ('tests', 'failures', 'errors', 'skipped')}
if stats != {'tests': 163, 'failures': 0, 'errors': 0, 'skipped': 0}:
    raise SystemExit(f'FAIL incomplete historical coverage: {stats}')
print('PASS 163 required historical tests, zero skips')
PY
  else
    historical_compose "${historical_test_args[@]}" \
      -e CP_B04_SEED_FILE=/tmp/b04-seed.json \
      -v "$historical_dir/results:/tmp/foundation-results:rw" \
      test sh -c 'python -m pytest -q -p no:cacheprovider --junitxml=/tmp/foundation-results/b04-seed.xml tests/integration/b04_spike_seed.py >&2 && cat /tmp/b04-seed.json' | \
    historical_compose run --rm --no-deps -T --entrypoint node \
      -e CI=true -e CP_CI_PROJECT="$historical_project" \
      -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
      -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
      web --experimental-strip-types auth/b04-spike.test.ts | tee "$historical_dir/b04-spike.log"
    python3 - "$historical_dir/results/b04-seed.xml" <<'PY'
import sys
from xml.etree import ElementTree
root = ElementTree.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == 'testsuite' else list(root)
if len(suites) != 1 or suites[0].tag != 'testsuite':
    raise SystemExit('FAIL B-04 seed JUnit missing or ambiguous')
stats = {field: int(suites[0].attrib[field]) for field in ('tests', 'failures', 'errors', 'skipped')}
if stats != {'tests': 1, 'failures': 0, 'errors': 0, 'skipped': 0}:
    raise SystemExit(f'FAIL B-04 seed mandatory test: {stats}')
PY
    grep -Fxq 'FEASIBILITY PROOFS PASSED; Issue #7 remains OPEN/BLOCKED pending architecture review' "$historical_dir/b04-spike.log"
  fi
)
if [ "$foundation_lane" = historical ]; then
  run_historical_migrations
  foundation_tests=163
  foundation_coverage_verified=true
  exit 0
fi
# Current-head B-04 uses the same split as ordinary acceptance; T133 retains its legacy selection.
if [ "${CP_T133_MODE:-false}" = false ] && [ -z "${CP_T133_SHA:-}" ]; then
  for node in "${historical_nodes[@]}"; do
    if [[ "$node" = *::* ]]; then historical_deselect+=("--deselect=$node"); fi
  done
  ordinary_core_migration_files=(tests/integration/test_t133_migration.py)
  ordinary_queue_migration_files=()
  ordinary_schedule_migration_files=()
  ordinary_b10_migration_files=()
  ordinary_day_review_migration_files=()
fi

upgrade_head
seed_identity_policy
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_WEB_DB_PASSWORD_SOURCE_FILE:/run/secrets/web_db_password:ro" \
  -v "$CP_WORKER_DB_PASSWORD_SOURCE_FILE:/run/secrets/worker_db_password:ro" \
  -v "$CP_AUTH_DB_PASSWORD_SOURCE_FILE:/run/secrets/auth_db_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  -v "$CP_SENDER_DB_PASSWORD_SOURCE_FILE:/run/secrets/sender_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_DB_PASSWORD_FILE:/run/secrets/postgres_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/f01.xml tests/integration/test_f01_runtime_identity.py tests/integration/test_f01_audit_integrity.py tests/integration/test_f01_grants_migration.py | tee "$private/f01-runtime-identity.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/f01-runtime-identity.log"; then
  echo 'FAIL mandatory F-01 runtime identity evidence skipped'; exit 1
fi
if [ "${CP_T133_MODE:-false}" = false ] && [ -z "${CP_T133_SHA:-}" ] && [ "${CP_B04_SPIKE:-false}" = false ] && [ "$foundation_lane" != ordinary ]; then
  run_historical_migrations
fi
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_AUTH_DB_PASSWORD_SOURCE_FILE:/run/secrets/auth_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  -v "$CP_SENDER_DB_PASSWORD_SOURCE_FILE:/run/secrets/sender_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/d08.xml "${historical_deselect[@]}" tests/integration/test_database.py tests/integration/test_tenant_isolation.py tests/integration/test_patient_persistence.py tests/integration/test_policy_persistence.py tests/integration/test_identity_persistence.py tests/integration/test_evidence_persistence.py tests/integration/test_request_boundary.py tests/integration/test_provisioning.py tests/integration/test_appointment_persistence.py tests/integration/test_appointment_races.py tests/integration/test_visit_persistence.py tests/integration/test_visit_revisions.py tests/integration/test_visit_migration.py tests/integration/test_followup_persistence.py tests/integration/test_followup_races.py tests/integration/test_followup_migration.py "${ordinary_core_migration_files[@]}" | tee "$private/d08-core.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/d08-core.log"; then
  echo 'FAIL mandatory D-08 persistence evidence skipped'; exit 1
fi
# Independent D-06 suite must remain executable without D-07/Visit imports.
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  test python -m pytest -q -s -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/d06.xml "${historical_deselect[@]}" \
  tests/integration/test_queue_persistence.py tests/integration/test_queue_capacity.py \
  tests/integration/test_queue_races.py "${ordinary_queue_migration_files[@]}" | tee "$private/d06-queue.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/d06-queue.log"; then
  echo 'FAIL mandatory D-06 real-PostgreSQL evidence skipped'; exit 1
fi
# All B-06 suites run against disposable PostgreSQL; no silent skip counts.
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/b06.xml "${historical_deselect[@]}" \
  tests/integration/test_pricing.py tests/integration/test_pricing_races.py tests/integration/test_pricing_migration.py | tee "$private/b06-pricing.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b06-pricing.log"; then
  echo 'FAIL mandatory B-06 pricing evidence skipped'; exit 1
fi
# B-07 tests must run against disposable PostgreSQL, including preview races and migration.
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/b07.xml \
  tests/integration/test_schedule.py tests/integration/test_schedule_races.py "${ordinary_schedule_migration_files[@]}" | tee "$private/b07-schedule.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b07-schedule.log"; then
  echo 'FAIL mandatory B-07 schedule evidence skipped'; exit 1
fi
# B-08 tests exercise the real PostgreSQL booking-day lock and capacity races.
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/b08.xml \
  tests/integration/test_slots.py tests/integration/test_slot_races.py | tee "$private/b08-slots.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b08-slots.log"; then
  echo 'FAIL mandatory B-08 slot evidence skipped'; exit 1
fi
# B-09 confirms atomic Booked-only creation and real-PostgreSQL races.
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/b09.xml \
  tests/integration/test_booking.py tests/integration/test_booking_races.py | tee "$private/b09-booking.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b09-booking.log"; then
  echo 'FAIL mandatory B-09 booking evidence skipped'; exit 1
fi
# B-10 confirms Doctor decisions, exact extensions, bookings and denial/race evidence.
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/b10.xml "${historical_deselect[@]}" \
  tests/integration/test_followups.py tests/integration/test_followups_denial.py \
  tests/integration/test_followup_decisions.py tests/integration/test_followups_races.py \
  "${ordinary_b10_migration_files[@]}" | tee "$private/b10-followups.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b10-followups.log"; then
  echo 'FAIL mandatory B-10 follow-up evidence skipped'; exit 1
fi
# D-11 remains mandatory, but runs after the T145-owned pricing/booking/follow-up
# suites so an unrelated fail-fast plan assertion cannot mask their evidence.
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  test python -m pytest -q -s -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/d11.xml "${historical_deselect[@]}" \
  tests/integration/test_day_review_persistence.py tests/integration/test_report_query_plans.py \
  tests/integration/test_day_review_races.py "${ordinary_day_review_migration_files[@]}" | tee "$private/d11-day-review.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/d11-day-review.log"; then
  echo 'FAIL mandatory D-11 real-PostgreSQL evidence skipped'; exit 1
fi
# B-05 backend proof has no BFF/invitation prerequisite and cannot silently skip.
docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/b05-backend.xml -m 'not b05_browser' \
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
timeout 60s docker compose exec -T web node --experimental-strip-types --input-type=module <<'NODE'
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
    -v "$private/foundation-results:/tmp/foundation-results:rw" \
    -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
    test python -m pytest -q -ra -p no:cacheprovider "--junitxml=/tmp/foundation-results/topology-$phase.xml" tests/integration/test_topology.py
  echo "PASS test-container topology, API and persistent DB/file markers: $phase"
done
docker compose exec -T web node -e 'fetch("http://api:8000/__foundation_network_probe__").then(r => process.exit(r.status === 404 ? 0 : 1)).catch(() => process.exit(1))'
docker compose --profile test run --rm --no-deps -e CI=true \
  -v "$GITHUB_WORKSPACE/tests:/srv/tests:ro" \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  api python -m pytest -q -ra -p no:cacheprovider --junitxml=/tmp/foundation-results/secrets.xml tests/integration/test_secrets.py
: > "$private/denials.log"
for key in CP_POLICY_FILE CP_DB_PASSWORD_FILE CP_AUTH_DB_PASSWORD_FILE CP_SERVICE_KEY_FILE CP_AUTH_SECRET_FILE CP_DB_IDENTITY_KEY_FILE; do
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
cat "$CP_DB_PASSWORD_FILE" "$CP_RUNTIME_DB_PASSWORD_FILE" "$CP_API_DB_PASSWORD_SOURCE_FILE" "$CP_WEB_DB_PASSWORD_SOURCE_FILE" "$CP_WORKER_DB_PASSWORD_SOURCE_FILE" "$CP_GATE_DB_PASSWORD_SOURCE_FILE" "$CP_DB_IDENTITY_KEY_SOURCE_FILE" "$CP_AUTH_DB_PASSWORD_SOURCE_FILE" "$CP_SERVICE_KEY_SOURCE_FILE" "$CP_AUTH_SECRET_SOURCE_FILE" \
  "$CP_FILE_ENCRYPTION_KEY_SOURCE_FILE" "$CP_MIGRATION_SECRET_FILE" "$CP_OPERATOR_SECRET_FILE" "$CP_OPERATOR_CREDENTIAL_SOURCE_FILE" "$CP_SENDER_DB_PASSWORD_SOURCE_FILE" > "$private/canaries"
chmod 0444 "$private/canaries"
awk 'length($0) != 64 || $0 !~ /^[0-9a-f]+$/ || seen[$0]++ { invalid=1 } END { if (NR != 15 || invalid) exit 1 }' "$private/canaries"
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
health_case_count=0
health_case() {
  scenario="$1"; shift
  health_case_count=$((health_case_count + 1))
  docker compose --profile test run --rm --no-deps \
    -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_HEALTH_SCENARIO="$scenario" \
    -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
    -v "$private/foundation-results:/tmp/foundation-results:rw" \
    -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
    test python -m pytest -q -ra --tb=short -p no:cacheprovider "--junitxml=/tmp/foundation-results/health-$health_case_count.xml" "$@"
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
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_SERVICE_KEY_SOURCE_FILE:/run/secrets/service_key:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/auth-mail.xml tests/integration/test_auth_mail.py
if ! docker compose --profile test run --rm --no-deps "${identity_fixture_args[@]}" \
  -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" \
  -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/sessions.xml tests/integration/test_sessions.py tests/integration/test_staff.py tests/integration/test_identity_races.py; then
  docker compose logs --no-color web 2>&1 | grep -aE 'B04_SYNTHETIC_STAGE' || true
  exit 1
fi
# B-02/B-03/B-04 are integrated: both actually invited roles are mandatory now.
docker compose --profile test run --rm --no-deps \
  -e CP_CI_PROJECT="$CP_CI_PROJECT" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider --junitxml=/tmp/foundation-results/b05-invited.xml -m b05_browser \
  tests/integration/test_patients.py | tee "$private/b05-invited.log"
if grep -Eq '[0-9]+ skipped|no tests ran' "$private/b05-invited.log"; then
  echo 'FAIL mandatory B-05 invited-session evidence skipped'; exit 1
fi
docker compose exec -T web node --no-warnings --experimental-strip-types --test --test-reporter=tap auth/ingress.test.mjs | tee "$private/foundation-results/web-ingress.tap"
echo 'PASS verified source separation, spoof resistance and sole Web listener'
# The sender target alone has Node, Mailpit access, and the private delivery key.
# Migration credentials are mounted ONLY into the disposable test invocation.
docker compose --profile sender run --rm --no-deps -T --entrypoint python \
  -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" -e COMPOSE_PROJECT_NAME="$CP_CI_PROJECT" \
  -v "$PWD/tests:/srv/tests:ro" \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  sender -m pytest -q -ra --tb=short -p no:cacheprovider \
  --junitxml=/tmp/foundation-results/sender.xml \
  tests/integration/test_delivery_sender.py -k 'not missing_mailpit'
if ! docker compose --profile sender run --rm --no-deps -T --entrypoint python \
  -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" -e COMPOSE_PROJECT_NAME="$CP_CI_PROJECT" \
  -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -v "$PWD/tests:/srv/tests:ro" \
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$CP_DB_PASSWORD_FILE:/run/secrets/postgres_password:ro" \
  -v "$CP_OPERATOR_SECRET_FILE:/run/secrets/operator_password:ro" \
  sender -m pytest -q -ra --tb=short -p no:cacheprovider \
  --junitxml=/tmp/foundation-results/recovery.xml \
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
  PLAYWRIGHT_JUNIT_OUTPUT_FILE="$private/foundation-results/owner-invite.xml" \
  pnpm --filter @carepulse/web test:owner-invite --reporter=line,junit
echo 'PASS real restricted operator, post-commit one-shot sender and browser Owner activation/login'
docker compose exec -T -e CI=true -e CP_CI_PROJECT="$CP_CI_PROJECT" \
  -e CP_ADAPTER_TEST_CLINIC="$clinic_id" web \
  node --no-warnings --experimental-strip-types --test --test-reporter=tap auth/postgres-adapter.integration.test.ts | tee "$private/foundation-results/web-adapter.tap"
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
  -v "$private/foundation-results:/tmp/foundation-results:rw" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  sender -m pytest -q -ra --tb=short -p no:cacheprovider \
  --junitxml=/tmp/foundation-results/sender-mail-down.xml \
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
# The spike commits an ACTIVE synthetic Owner; run after empty-baseline assertions,
# using fresh, sequentially isolated historical clusters, never current-head grants.
if [ "${CP_B04_SPIKE:-false}" = true ]; then
  run_historical_migrations
  run_historical_migrations b04
fi
if [ "${CP_T133_MODE:-false}" = true ]; then
  bash ops/t133-ci.sh "$private" | tee "$private/t133-run.log"
  python3 - "$private/t133-run.log" <<'PY'
import re
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
summaries = re.findall(r'(?m)^([1-9][0-9]*) passed(?:, [0-9]+ warnings?)? in [0-9.]+s$', text)
if len(summaries) != 3 or not all(int(count) > 0 for count in summaries):
    raise SystemExit('FAIL T133 seed/runtime/observer mandatory pytest summaries')
if not all(marker in text for marker in ('T133 ACL DURING:', 'T133 ACL AFTER:', 'T133 scoped runtime acceptance complete:')):
    raise SystemExit('FAIL incomplete protected T133 evidence')
PY
fi
if [ "$foundation_full" = true ] || [ "${CP_B04_SPIKE:-false}" = true ]; then
  foundation_tests="$(python3 - "$private/foundation-results" "$health_case_count" <<'PY'
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

folder = Path(sys.argv[1])
health_count = int(sys.argv[2])
if health_count != 8:
    raise SystemExit(f'FAIL expected 8 health phases, got {health_count}')
expected = {
    'f01', 'd08', 'd06', 'b06', 'b07', 'b08', 'b09', 'b10', 'd11', 'b05-backend',
    'topology-seed', 'topology-verify', 'secrets', 'auth-mail', 'sessions',
    'b05-invited', 'sender', 'recovery', 'sender-mail-down', 'owner-invite',
} | {f'health-{i}' for i in range(1, health_count + 1)}
actual = {p.stem for p in folder.glob('*.xml')}
if actual != expected:
    raise SystemExit(f'FAIL ordinary JUnit inventory: missing={expected - actual}, unexpected={actual - expected}')
total = 0
for name in sorted(expected):
    root = ElementTree.parse(folder / (name + '.xml')).getroot()
    suites = [root] if root.tag == 'testsuite' else list(root)
    if not suites or any(suite.tag != 'testsuite' for suite in suites):
        raise SystemExit(f'FAIL ambiguous JUnit: {name}')
    for suite in suites:
        stats = {field: int(suite.attrib[field]) for field in ('tests', 'failures', 'errors', 'skipped')}
        if stats['tests'] <= 0 or any(stats[field] for field in ('failures', 'errors', 'skipped')):
            raise SystemExit(f'FAIL {name} JUnit: {stats}')
        total += stats['tests']
for name in ('web-ingress', 'web-adapter'):
    values = {}
    for line in (folder / (name + '.tap')).read_text().splitlines():
        match = re.fullmatch(r'# (tests|pass|fail|cancelled|skipped|todo) ([0-9]+)', line)
        if match:
            if match.group(1) in values:
                raise SystemExit(f'FAIL duplicate TAP summary: {name}')
            values[match.group(1)] = int(match.group(2))
    if set(values) != {'tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo'}:
        raise SystemExit(f'FAIL missing TAP summary: {name}')
    if values['tests'] <= 0 or values['pass'] != values['tests'] or any(values[k] for k in ('fail', 'cancelled', 'skipped', 'todo')):
        raise SystemExit(f'FAIL {name} TAP: {values}')
    total += values['tests']
if total <= 0:
    raise SystemExit('FAIL empty ordinary coverage')
print(f'PASS ordinary {total} mandatory JUnit/TAP tests, zero skips', file=sys.stderr)
print(total)
PY
)"
  test "$foundation_tests" -gt 0
  foundation_coverage_verified=true
fi
{
  echo '### Provisional Wave 0 and Wave 1 D-01/D-02 lane checks (before scoped cleanup)'
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
            & bash -e -c $integration -- $Check
            if ($LASTEXITCODE -ne 0) { throw "Integration failed (exit $LASTEXITCODE)" }
        }
        { $_ -in @('compat-diagnostic', 'compat-full') } {
            if ($env:CI -ne 'true' -or $env:RUNNER_OS -ne 'Linux' -or $PSVersionTable.PSVersion.Major -lt 7) {
                throw 'Compatibility verification requires Docker-capable Linux CI and PowerShell 7; no local Docker probe performed'
            }
            if ($env:CP_CI_PROJECT -notmatch '^carepulse-ci-[0-9]+-[0-9]+$' -or $env:COMPOSE_PROJECT_NAME -ne $env:CP_CI_PROJECT) {
                throw 'Explicit matching disposable CI project required'
            }
            $compatScript = @'
set -euo pipefail
umask 077
foundation_lane="${CP_FOUNDATION_LANE:-standard}"
foundation_full=false
foundation_coverage_verified=false
if [[ "${CP_FOUNDATION_MODE:-}" = full ]]; then
  if [[ "$1" != compat-full || "$foundation_lane" != compat || -z "${CP_FOUNDATION_RECEIPT_PATH:-}" ]]; then
    echo 'FAIL full compatibility lane invocation'; exit 1
  fi
  foundation_full=true
elif [[ "$foundation_lane" != standard ]]; then
  echo 'FAIL compatibility lane requires full acceptance mode'; exit 1
fi
if [[ "${CP_B04_SPIKE:-false}" != false || "${CP_T133_MODE:-false}" != false || -n "${CP_T133_SHA:-}" ]]; then
  echo 'FAIL compat runner cannot combine with experimental or protected modes'; exit 1
fi
if [[ "$1" = compat-diagnostic ]]; then
  if [[ "${GITHUB_EVENT_NAME:-}" != workflow_dispatch ]]; then
    echo 'FAIL compat diagnostics require a manual dispatch'; exit 1
  fi
  mode="${CP_COMPAT_DIAGNOSTIC:-}"
  case "$mode" in
    compat-us1|compat-us2|compat-us3) ;;
    *) echo "FAIL unknown compat diagnostic mode: $mode"; exit 1 ;;
  esac
elif [[ "$1" = compat-full && "${CP_COMPAT_DIAGNOSTIC:-}" = compat-full ]]; then
  mode=compat-full
else
  echo 'FAIL invalid ordinary compatibility selection'; exit 1
fi
private="$RUNNER_TEMP/foundation-ci-private"
test ! -e "$private"
mkdir -m 0700 "$private"
if [[ "$mode" = compat-full ]]; then
  mkdir -m 1777 "$private/compat-results"
fi
source_dir="$RUNNER_TEMP/compat-source-0040"
if [ -e "$source_dir" ] || [ -L "$source_dir" ]; then
  echo 'FAIL existing private compatibility source directory'; exit 1
fi
publish_host_diagnostics() {
  # Publish only the host's structured diagnostic records, never its raw stderr.
  if [[ -f "$private/ci-host.log" ]]; then
    grep '^CI_HOST_DIAGNOSTIC {' "$private/ci-host.log" 2>/dev/null || true
  fi
}
cleanup() {
  local code=$? cleanup_failed=0
  trap - EXIT
  if [[ -n "${host_pid:-}" ]]; then
    kill -TERM "$host_pid" 2>/dev/null || true
    if ! wait "$host_pid"; then
      echo 'FAIL independent compatibility host supervisor'; cleanup_failed=1
    fi
    if [[ "$code" -ne 0 || "$cleanup_failed" -ne 0 ]]; then
      publish_host_diagnostics
    fi
    host_pid=''
  fi
  if [[ "$COMPOSE_PROJECT_NAME" =~ ^carepulse-ci-[0-9]+-[0-9]+$ && "$COMPOSE_PROJECT_NAME" = "$CP_CI_PROJECT" && "$private" = "$RUNNER_TEMP/foundation-ci-private" && -d "$private" && -O "$private" && ! -L "$private" && "$source_dir" = "$RUNNER_TEMP/compat-source-0040" && ! -L "$source_dir" && ( ! -e "$source_dir" || ( -d "$source_dir" && -O "$source_dir" ) ) ]]; then
    if ! docker compose -f compose.compat.yaml --profile mail --profile source --profile source-sender --profile candidate --profile candidate-sender --profile controller --profile observer down --remove-orphans --volumes; then
      echo 'FAIL scoped compat Compose cleanup'; cleanup_failed=1
    fi
    if ! rm -rf -- "$private" "$source_dir" || [ -e "$private" ] || [ -e "$source_dir" ]; then
      echo 'FAIL synthetic compat secret/source cleanup'; cleanup_failed=1
    fi
    if [ "$foundation_full" = true ]; then
      local remaining resource
      for resource in containers volumes networks; do
        case "$resource" in
          containers) if ! remaining="$(docker ps -aq --filter "label=com.docker.compose.project=$CP_CI_PROJECT")"; then cleanup_failed=1; continue; fi ;;
          volumes) if ! remaining="$(docker volume ls -q --filter "label=com.docker.compose.project=$CP_CI_PROJECT")"; then cleanup_failed=1; continue; fi ;;
          networks) if ! remaining="$(docker network ls -q --filter "label=com.docker.compose.project=$CP_CI_PROJECT")"; then cleanup_failed=1; continue; fi ;;
        esac
        if [ -n "$remaining" ]; then echo "FAIL residual compat $resource"; cleanup_failed=1; fi
      done
    fi
    if [ "$cleanup_failed" = 1 ]; then
      code=1
    else
      echo 'PASS scoped compat Compose volume and synthetic secret cleanup'
    fi
  else
    echo 'FAIL unsafe compat cleanup project'; code=1
  fi
  if [ "$code" -eq 0 ] && [ "$foundation_full" = true ]; then
    if [[ "$foundation_coverage_verified" != true || -e "$private" || -e "$source_dir" ]]; then
      echo 'FAIL incomplete compatibility evidence or source cleanup'; code=1
    else
      export CP_FOUNDATION_CLEANUP_VERIFIED=1 CP_FOUNDATION_COVERAGE_VERIFIED=1
      export CP_FOUNDATION_EXECUTION=PASS CP_FOUNDATION_TESTS=PASS CP_FOUNDATION_ZERO_SKIPS=true CP_FOUNDATION_CLEANUP=PASS
      export CP_FOUNDATION_COVERAGE=full-US1-US2-US3-six-successors-zero-skips
      export CP_FOUNDATION_SOURCE_SHA=6cf2973c07f6e0ef2d8040d1eb3c235fd1598df7 CP_FOUNDATION_SOURCE_SCHEMA=0040_t173_numeric_money
      export CP_FOUNDATION_CANDIDATE_SCHEMA=0046_f01_audit_integrity CP_FOUNDATION_LANE=compat
      if ! python3 -m ops.foundation_acceptance seal "$CP_FOUNDATION_RECEIPT_PATH"; then
        echo 'FAIL terminal compatibility receipt seal'; code=1
      fi
    fi
  fi
  exit "$code"
}
trap cleanup EXIT

source_sha='6cf2973c07f6e0ef2d8040d1eb3c235fd1598df7'
if ! git cat-file -e "${source_sha}^{commit}" 2>/dev/null; then
  git fetch --no-tags --depth=1 origin "$source_sha"
fi
git cat-file -e "${source_sha}^{commit}"
mkdir -m 0755 "$source_dir"
git archive "$source_sha" | tar -x -C "$source_dir"
echo "Verified immutable source commit $source_sha in $source_dir"
export CP_SOURCE_CONTEXT="$source_dir"
export CP_COMPAT_ADAPTERS_DIR="$PWD/ops/compat"
export CP_COMPAT_MANIFEST_DIR="$private/manifests"
mkdir -m 0755 "$CP_COMPAT_MANIFEST_DIR"

export CP_SOURCE_RUNTIME_DB_PASSWORD_FILE="$private/source-runtime-db-password"
export CP_SOURCE_AUTH_DB_PASSWORD_FILE="$private/source-auth-db-password"
export CP_SOURCE_SENDER_DB_PASSWORD_FILE="$private/source-sender-db-password"
export CP_SOURCE_SERVICE_KEY_FILE="$private/source-service-key"
export CP_SOURCE_AUTH_SECRET_FILE="$private/source-auth-secret"
export CP_SOURCE_FILE_ENCRYPTION_KEY_FILE="$private/source-file-key"
export CP_SOURCE_DELIVERY_PRIVATE_KEY_FILE="$private/source-delivery-private-key"
export CP_SOURCE_DELIVERY_PUBLIC_KEY_FILE="$private/source-delivery-public-key"

for file in "$CP_DB_PASSWORD_FILE" "$CP_RUNTIME_DB_PASSWORD_FILE" "$CP_API_DB_PASSWORD_SOURCE_FILE" "$CP_WEB_DB_PASSWORD_SOURCE_FILE" "$CP_WORKER_DB_PASSWORD_SOURCE_FILE" "$CP_GATE_DB_PASSWORD_SOURCE_FILE" "$CP_DB_IDENTITY_KEY_SOURCE_FILE" "$CP_AUTH_DB_PASSWORD_SOURCE_FILE" "$CP_MIGRATION_SECRET_FILE" "$CP_OPERATOR_SECRET_FILE" "$CP_OPERATOR_CREDENTIAL_SOURCE_FILE" "$CP_SENDER_DB_PASSWORD_SOURCE_FILE" "$CP_SERVICE_KEY_SOURCE_FILE" "$CP_AUTH_SECRET_SOURCE_FILE" "$CP_FILE_ENCRYPTION_KEY_SOURCE_FILE" "$CP_SOURCE_RUNTIME_DB_PASSWORD_FILE" "$CP_SOURCE_AUTH_DB_PASSWORD_FILE" "$CP_SOURCE_SENDER_DB_PASSWORD_FILE" "$CP_SOURCE_SERVICE_KEY_FILE" "$CP_SOURCE_AUTH_SECRET_FILE" "$CP_SOURCE_FILE_ENCRYPTION_KEY_FILE"; do
  test "$(dirname "$file")" = "$private"
  openssl rand -hex 32 > "$file"
  chmod 0444 "$file"
done
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$CP_DELIVERY_PRIVATE_KEY_SOURCE_FILE" > /dev/null 2>&1
openssl pkey -in "$CP_DELIVERY_PRIVATE_KEY_SOURCE_FILE" -pubout -out "$CP_DELIVERY_PUBLIC_KEY_SOURCE_FILE" > /dev/null 2>&1
chmod 0444 "$CP_DELIVERY_PRIVATE_KEY_SOURCE_FILE" "$CP_DELIVERY_PUBLIC_KEY_SOURCE_FILE"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$CP_SOURCE_DELIVERY_PRIVATE_KEY_FILE" > /dev/null 2>&1
openssl pkey -in "$CP_SOURCE_DELIVERY_PRIVATE_KEY_FILE" -pubout -out "$CP_SOURCE_DELIVERY_PUBLIC_KEY_FILE" > /dev/null 2>&1
chmod 0444 "$CP_SOURCE_DELIVERY_PRIVATE_KEY_FILE" "$CP_SOURCE_DELIVERY_PUBLIC_KEY_FILE"
! cmp -s "$CP_SOURCE_DELIVERY_PRIVATE_KEY_FILE" "$CP_DELIVERY_PRIVATE_KEY_SOURCE_FILE"

docker compose -f compose.compat.yaml --profile mail --profile source --profile source-sender --profile candidate --profile candidate-sender --profile controller --profile observer config --quiet
docker compose -f compose.compat.yaml --profile mail --profile source --profile source-sender --profile candidate --profile candidate-sender --profile controller --profile observer config --format json > "$private/compose.compat.json"
chmod 0444 "$private/compose.compat.json"
docker compose -f compose.compat.yaml --profile mail --profile source --profile source-sender --profile candidate --profile candidate-sender --profile controller --profile observer build --pull
docker compose -f compose.compat.yaml --profile mail up -d --no-build db mail
for attempt in $(seq 1 40); do
  if docker compose -f compose.compat.yaml exec -T db pg_isready -h 127.0.0.1 -U carepulse_bootstrap -d carepulse > /dev/null 2>&1; then break; fi
  if [ "$attempt" = 40 ]; then echo 'FAIL disposable compat database startup'; exit 1; fi
  sleep 2
done

for svc in source-api source-web source-worker source-sender candidate-api candidate-web candidate-worker candidate-sender; do
  img_tag="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["services"][sys.argv[2]]["image"])' "$private/compose.compat.json" "$svc")"
  img="$(docker image inspect --format '{{.Id}}' "$img_tag")"
  [[ "$img" == sha256:* ]] || { echo "FAIL missing built compat image for $svc"; exit 1; }
  echo "COMPAT_IMAGE $svc=$img"
done
sha256sum ops/compat/source-web.cjs ops/compat/source-api.py ops/compat/source-jobs.py apps/api/carepulse/compat/admission.py apps/web/compat/admission.ts

assert_full_compat_count() {
  python3 - "$1" "$2" "$3" <<'PY'
import sys
import xml.etree.ElementTree as ET

group, expected, filename = sys.argv[1], int(sys.argv[2]), sys.argv[3]
root = ET.parse(filename).getroot()
suites = [root] if root.tag == 'testsuite' else list(root)
if len(suites) != 1 or suites[0].tag != 'testsuite':
    raise SystemExit(f'FAIL {group}: missing or ambiguous JUnit test suite')
suite = suites[0]
stats = {key: int(suite.attrib[key]) for key in ('tests', 'failures', 'errors', 'skipped')}
if stats != {'tests': expected, 'failures': 0, 'errors': 0, 'skipped': 0}:
    raise SystemExit(f'FAIL {group}: expected {expected} tests, zero failures/skips; got {stats}')
print(f'PASS {group}: {expected} mandatory Python tests, zero skips')
PY
}
run_compat_observer() {
  local group="$1" expected="$2"
  shift 2
  local ipc_args=()
  if [[ "$group" = compat-us1-source-web-runtime ]]; then
    test -n "${CP_SOURCE_WEB_DB_IP:-}"
    ipc_args=(-e CP_SOURCE_WEB_DB_IP)
  fi
  if [[ "$group" = compat-us2 ]]; then
    ipc_args=(-e CP_COMPAT_T051_IPC_DIR=/tmp/compat-host -e CP_COMPAT_T051_REPLY_DIR=/tmp/compat-host-replies -v "$private/handshake:/tmp/compat-host:rw" -v "$private/host-replies:/tmp/compat-host-replies:ro")
  fi
  if [[ "$mode" = compat-full ]]; then
    timeout 120s docker compose -f compose.compat.yaml --profile observer run --rm --no-deps -T -e CP_COMPAT_COMPOSE_CONFIG_PATH=/tmp/compat-compose.json -v "$private/compose.compat.json:/tmp/compat-compose.json:ro" -v "$private/compat-results:/tmp/compat-results:rw" "${ipc_args[@]}" observer python -u -m pytest -v -s -ra -o faulthandler_timeout=25 --tb=short -p no:cacheprovider "--junitxml=/tmp/compat-results/$group.xml" "$@"
    assert_full_compat_count "$group" "$expected" "$private/compat-results/$group.xml"
  else
    timeout 120s docker compose -f compose.compat.yaml --profile observer run --rm --no-deps -T -e CP_COMPAT_COMPOSE_CONFIG_PATH=/tmp/compat-compose.json -v "$private/compose.compat.json:/tmp/compat-compose.json:ro" "${ipc_args[@]}" observer python -u -m pytest -v -s -ra -o faulthandler_timeout=25 --tb=short -p no:cacheprovider "$@"
  fi
}
if [[ "$mode" = compat-us1 || "$mode" = compat-full ]]; then
    required_suites=(tests/unit/test_compat_admission.py tests/integration/test_compat_matrix.py tests/integration/test_compat_entry_denials.py tests/integration/test_compat_source_privileges.py)
    for f in "${required_suites[@]}"; do test -f "$f" || { echo "FAIL missing $f"; exit 1; }; done
    run_compat_observer compat-us1 25 "${required_suites[@]}"
    docker compose -f compose.compat.yaml --profile candidate run --rm --no-deps -T --entrypoint node candidate-web --no-warnings --experimental-strip-types --test --test-reporter=tap auth/compat.test.ts | tee "$private/compat-us1-web.log"
    test -f tests/unit/source-web.test.cjs || { echo 'FAIL missing source Web privilege regression'; exit 1; }
    docker compose -f compose.compat.yaml --profile source run --rm --no-deps -T --entrypoint node -e CP_SOURCE_WEB_ADAPTER_PATH=/run/compat-adapters/source-web.cjs -v "$PWD/tests/unit/source-web.test.cjs:/tmp/source-web.test.cjs:ro" source-web --test --test-reporter=tap /tmp/source-web.test.cjs | tee "$private/compat-us1-source-web.log"
    # Hosted immutable-source Web: genuine Node preload, Next HTTP and both real 0040 logins.
    docker compose -f compose.compat.yaml --profile observer run --rm --no-deps -T observer python -c 'from tests.integration.test_compat_source_web_runtime import prepare_source_web_http_lab; prepare_source_web_http_lab()'
    docker compose -f compose.compat.yaml --profile source --profile candidate up -d --no-build --no-deps source-api source-web candidate-web
    docker compose -f compose.compat.yaml --profile source run -d --no-deps --name "${CP_CI_PROJECT}-source-peer" -e CP_COMPAT_EXPECTED_API_PEER=http://candidate-api:8000 source-web
    source_web_id="$(docker compose -f compose.compat.yaml --profile source ps -q source-web)"
    if [[ -z "$source_web_id" ]]; then
      echo 'FAIL immutable source Web stopped during HTTP test startup'
      docker compose -f compose.compat.yaml --profile source --profile candidate ps -a source-api source-web candidate-web
      docker compose -f compose.compat.yaml --profile source logs --no-color --tail 30 source-web
      exit 1
    fi
    echo 'PASS immutable source Web process running before HTTP test'
    source_web_host_pid="$(docker inspect --format '{{.State.Pid}}' "$source_web_id")"
    export CP_SOURCE_WEB_DB_IP="$(docker inspect "$source_web_id" | python3 -c 'import json,sys; nets=json.load(sys.stdin)[0]["NetworkSettings"]["Networks"]; ips=[n["IPAddress"] for k,n in nets.items() if k.endswith("_compat_db")]; assert len(ips)==1; print(ips[0])')"
    test -n "$CP_SOURCE_WEB_DB_IP"
    run_compat_observer compat-us1-source-web-runtime 1 tests/integration/test_compat_source_web_runtime.py
    test "$(docker inspect --format '{{.State.Pid}}' "$source_web_id")" = "$source_web_host_pid"
    docker compose -f compose.compat.yaml --profile source --profile candidate stop source-api source-web candidate-web
    docker rm -f "${CP_CI_PROJECT}-source-peer" >/dev/null
    if [[ "$mode" = compat-full ]]; then
      for result in '# tests 9' '# pass 9' '# fail 0' '# cancelled 0' '# skipped 0' '# todo 0'; do
        grep -Fxq "$result" "$private/compat-us1-web.log" || { echo "FAIL mandatory compat Web test count: $result"; exit 1; }
      done
      echo 'PASS compat-us1-web: 9 mandatory Web tests, zero skips'
      for result in '# tests 2' '# pass 2' '# fail 0' '# cancelled 0' '# skipped 0' '# todo 0'; do
        grep -Fxq "$result" "$private/compat-us1-source-web.log" || { echo "FAIL mandatory source Web test count: $result"; exit 1; }
      done
      echo 'PASS compat-us1-source-web: 2 mandatory source image tests, zero skips'
    fi
fi
# The independent Docker/PG supervisor attests every stopped-consumer proof
# for US2 drain/migration and US3 recovery; observer never receives Docker.
if [[ "$mode" = compat-us2 || "$mode" = compat-us3 || "$mode" = compat-full ]]; then
    python3 - "$private/compose.compat.json" "$private/observed.json" "$GITHUB_SHA" <<'PY'
import json, re, subprocess, sys

config_path, output_path, sha = sys.argv[1:]
assert re.fullmatch(r'[0-9a-f]{40}', sha)
services = json.load(open(config_path, encoding='utf-8'))['services']
observed = {'candidate_sha': sha, 'services': {}}
for generation in ('source', 'candidate'):
    for service in ('api', 'web', 'worker', 'sender'):
        name = f'{generation}-{service}'
        spec = services[name]
        image = json.loads(subprocess.check_output(
            ['docker', 'image', 'inspect', spec['image']], text=True))[0]
        image_id = image['Id']
        assert re.fullmatch(r'sha256:[0-9a-f]{64}', image_id)
        entry = spec.get('entrypoint')
        command = spec.get('command')
        if entry is None:
            entry = image['Config'].get('Entrypoint') or []
        if command is None:
            command = image['Config'].get('Cmd') or []
        assert isinstance(entry, list) and isinstance(command, list)
        volumes = spec.get('volumes', [])
        observed['services'][name] = {
            'image_id': image_id,
            'entry': entry + command,
            'adapter_readonly': any(v.get('target') == '/run/compat-adapters'
                                    and v.get('read_only') is True for v in volumes),
            'api_peer': spec.get('environment', {}).get('CP_COMPAT_EXPECTED_API_PEER', ''),
            'smtp_peer': spec.get('environment', {}).get('CP_SMTP_HOST', ''),
            'db_name': spec.get('environment', {}).get('CP_DB_NAME', ''),
            'target_id': spec.get('environment', {}).get('CP_COMPAT_TARGET_ID', ''),
            'secrets': sorted(s.get('source', '') if isinstance(s, dict) else s
                              for s in spec.get('secrets', [])),
        }
with open(output_path, 'w', encoding='utf-8') as stream:
    json.dump(observed, stream, sort_keys=True)
PY
    chmod 0444 "$private/observed.json"
    mkdir -m 1777 "$private/handshake"
    mkdir -m 0755 "$private/host-replies"
    # Only the supervisor's structured stdout is retained; never publish raw stderr.
    python3 -u ops/compat/ci_host.py "$private/handshake" "$private/observed.json" "$private/host-replies" > "$private/ci-host.log" 2>/dev/null &
    host_pid=$!
fi
if [[ "$mode" = compat-us2 || "$mode" = compat-full ]]; then
    required_suites=(tests/integration/test_compat_drain.py tests/integration/test_compat_delivery.py tests/integration/test_compat_upgrade.py)
    for f in "${required_suites[@]}"; do test -f "$f" || { echo "FAIL missing $f"; exit 1; }; done
    run_compat_observer compat-us2 8 "${required_suites[@]}"
    if [[ "$mode" = compat-us2 ]]; then
      kill -TERM "$host_pid" 2>/dev/null || true
      if ! wait "$host_pid"; then
        echo 'FAIL independent compatibility host supervisor'
        exit 1
      fi
      host_pid=''
    fi
fi
if [[ "$mode" = compat-us3 || "$mode" = compat-full ]]; then
    required_suites=(tests/integration/test_compat_recovery_source.py tests/integration/test_compat_recovery_intermediate.py tests/integration/test_compat_recovery_target.py tests/integration/test_compat_preservation.py)
    for f in "${required_suites[@]}"; do test -f "$f" || { echo "FAIL missing $f"; exit 1; }; done
    set +e
    if [[ "$mode" = compat-full ]]; then
      timeout 300s docker compose -f compose.compat.yaml --profile observer run --rm --no-deps -T -e CP_COMPAT_COMPOSE_CONFIG_PATH=/tmp/compat-compose.json -e CP_COMPAT_T051_IPC_DIR=/tmp/compat-host -e CP_COMPAT_T051_REPLY_DIR=/tmp/compat-host-replies -v "$private/compose.compat.json:/tmp/compat-compose.json:ro" -v "$private/observed.json:/tmp/compat-observed.json:ro" -v "$private/handshake:/tmp/compat-host:rw" -v "$private/host-replies:/tmp/compat-host-replies:ro" -v "$private/compat-results:/tmp/compat-results:rw" observer python -u -m pytest -v -s -ra -o faulthandler_timeout=35 --tb=short -p no:cacheprovider --junitxml=/tmp/compat-results/compat-us3.xml "${required_suites[@]}"
      test_code=$?
    else
      timeout 300s docker compose -f compose.compat.yaml --profile observer run --rm --no-deps -T -e CP_COMPAT_COMPOSE_CONFIG_PATH=/tmp/compat-compose.json -e CP_COMPAT_T051_IPC_DIR=/tmp/compat-host -e CP_COMPAT_T051_REPLY_DIR=/tmp/compat-host-replies -v "$private/compose.compat.json:/tmp/compat-compose.json:ro" -v "$private/observed.json:/tmp/compat-observed.json:ro" -v "$private/handshake:/tmp/compat-host:rw" -v "$private/host-replies:/tmp/compat-host-replies:ro" observer python -u -m pytest -v -s -ra -o faulthandler_timeout=35 --tb=short -p no:cacheprovider "${required_suites[@]}"
      test_code=$?
    fi
    kill -TERM "$host_pid" 2>/dev/null || true
    wait "$host_pid"
    host_code=$?
    host_pid=''
    set -e
    if [[ "$test_code" -ne 0 || "$host_code" -ne 0 ]]; then
      echo 'FAIL real compatibility scenario or bounded host supervisor'
      publish_host_diagnostics
      exit 1
    fi
    if [[ "$mode" = compat-full ]]; then
      assert_full_compat_count compat-us3 10 "$private/compat-results/compat-us3.xml"
    fi
fi
if [[ "$mode" = compat-full ]]; then
  echo 'PASS mandatory compatibility: 44 Python cases (US1 26, US2 8, US3 10), 11 Web cases, zero skips'
  if [ "$foundation_full" = true ]; then foundation_coverage_verified=true; fi
fi
'@
            & bash -e -c $compatScript -- $Check
            if ($LASTEXITCODE -ne 0) { throw "Compatibility verification failed (exit $LASTEXITCODE)" }
        }
    }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
exit 0
