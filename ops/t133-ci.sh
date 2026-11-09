#!/usr/bin/env bash
# Disposable T133 grant window. Invoked only by Foundation's exact-SHA mode.
set -euo pipefail
umask 077

private="${1:?runner-scoped private directory required}"
repo='ahmedali5696/care-pulse-3'
ref='refs/heads/feature/wave-2-booking-and-scheduling'
signature='public.transition_terminal_appointment(uuid,uuid,bigint,jsonb)'
project="${CP_CI_PROJECT:?}"
network="${project}_database"
volume="${project}_db_data"
worker="${project}-t133-worker"
grant_open=false
db_identity=''

fail() { echo "T133 DENIED: $*" >&2; exit 1; }
sql() {
  docker compose exec -T db psql --no-psqlrc -X -qAt \
    -v ON_ERROR_STOP=1 -U carepulse_bootstrap -d carepulse "$@"
}

test "${GITHUB_EVENT_NAME:-}" = workflow_dispatch || fail 'manual dispatch required'
test "${GITHUB_REPOSITORY:-}" = "$repo" || fail 'repository mismatch'
test "${GITHUB_REF:-}" = "$ref" || fail 'feature ref mismatch'
test "${RUNNER_OS:-}" = Linux || fail 'Linux runner required'
test "${CP_ENV:-}" = test || fail 'test environment required'
test "${CP_SYNTHETIC_TEST_PROFILE:-}" = true || fail 'synthetic only'
test "${CP_T133_MODE:-}" = true || fail 'explicit T133 mode required'
test "${CI:-}" = true || fail 'CI required'
test "$project" = "carepulse-ci-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}" ||
  fail 'runner job project mismatch'
[[ "$project" =~ ^carepulse-ci-[0-9]+-[0-9]+$ ]] || fail 'unsafe project name'
test "${COMPOSE_PROJECT_NAME:-}" = "$project" || fail 'Compose project mismatch'
[[ "${CP_T133_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || fail 'missing exact SHA'
test "$CP_T133_SHA" = "$GITHUB_SHA" || fail 'SHA input mismatch'
test "$(git rev-parse HEAD)" = "$GITHUB_SHA" || fail 'checkout SHA mismatch'
test "$(realpath "$(git rev-parse --show-toplevel)")" = "$(realpath "$GITHUB_WORKSPACE")" ||
  fail 'checkout workspace mismatch'
python - <<'PY' || exit 1
import json
import os

with open(os.environ['GITHUB_EVENT_PATH'], encoding='utf-8') as file:
    event = json.load(file)
assert event['repository']['full_name'] == 'ahmedali5696/care-pulse-3'
assert event['ref'] in {
    'refs/heads/feature/wave-2-booking-and-scheduling',
    'feature/wave-2-booking-and-scheduling',
}
assert event['inputs']['t133_terminal'] == 'true'
assert event['inputs']['t133_sha'] == os.environ['GITHUB_SHA']
PY

test "$private" = "$RUNNER_TEMP/foundation-ci-private" ||
  fail 'private directory mismatch'
test -d "$private" && test -O "$private" && test ! -L "$private" ||
  fail 'unowned or linked private directory'
test "$(docker network inspect "$network" \
  --format '{{index .Labels "com.docker.compose.project"}}')" = "$project" ||
  fail 'unowned database network'
test "$(docker volume inspect "$volume" \
  --format '{{index .Labels "com.docker.compose.project"}}')" = "$project" ||
  fail 'unowned database volume'
db_container="$(docker compose ps -q db)"
test -n "$db_container" || fail 'missing owned PostgreSQL container'
test "$(docker inspect "$db_container" \
  --format '{{index .Config.Labels "com.docker.compose.project"}}')" = "$project" ||
  fail 'PostgreSQL container project mismatch'
test "$(docker inspect "$db_container" \
  --format '{{index .Config.Labels "com.docker.compose.service"}}')" = db ||
  fail 'wrong PostgreSQL Compose service'
test "$(docker inspect "$db_container" --format '{{.State.Running}}')" = true ||
  fail 'PostgreSQL container not running'
test "$(docker inspect "$db_container" \
  --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}')" = "$volume" ||
  fail 'unexpected database data volume'
test "$(sql -c 'SELECT version_num FROM public.alembic_version')" = 0033_d08_terminal_writer ||
  fail 'schema head mismatch'

# The PostgreSQL system identifier is read from the actual running server;
# it is never synthesized from environment flags or Compose metadata.
db_identity="$(sql -c 'SELECT system_identifier FROM pg_catalog.pg_control_system()')"
[[ "$db_identity" =~ ^[0-9]{10,}$ ]] || fail 'unverified PostgreSQL identity'
test "$(sql -c 'SELECT current_user || chr(58) || session_user')" = 'carepulse_bootstrap:carepulse_bootstrap' ||
  fail 'unexpected privileged SQL login'
test "$(sql -c "
  SELECT CASE WHEN p.proowner = (SELECT oid FROM pg_roles WHERE rolname='carepulse_migration')
       AND NOT has_function_privilege('carepulse_runtime', p.oid, 'EXECUTE')
       AND NOT EXISTS (SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee=0)
       AND NOT (SELECT rolbypassrls FROM pg_roles WHERE rolname='carepulse_runtime')
       AND NOT pg_has_role('carepulse_runtime','carepulse_migration','MEMBER')
       AND NOT has_function_privilege(
           'carepulse_runtime',
           'public.d07_complete_examined_visit_raw(uuid,uuid,bigint,boolean)',
           'EXECUTE')
    THEN 'DENY' ELSE 'FAIL' END
  FROM pg_proc p
  WHERE p.oid = '$signature'::regprocedure")" = DENY ||
  fail 'default effective ACL or role graph invalid'
echo "T133 ACL BEFORE: DENY (migration owner, no PUBLIC/runtime inheritance)"

test_image="$(docker image inspect "${project}-test:latest" --format '{{.Id}}' 2>/dev/null || true)"
if test -z "$test_image"; then
  test_image="$(docker compose images -q test)"
fi
test -n "$test_image" || fail 'pinned test image unavailable'
test "$(docker image inspect "$test_image" --format '{{.Os}}/{{.Architecture}}')" = 'linux/amd64' ||
  fail 'wrong test image architecture'
mkdir -m 0700 "$private/t133"

# Only this seed controller receives migration credentials. It exits before
# opening the grant window; runtime test workers never receive those secrets.
docker compose --profile test run --rm --no-deps -T \
  --user "$(id -u):$(id -g)" \
  -e CP_CI_PROJECT="$project" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -e CP_T133_FIXTURE_PATH=/tmp/t133/fixtures.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$private/t133:/tmp/t133" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_t133_seed.py
test -s "$private/t133/fixtures.json" || fail 'durable synthetic fixture missing'

scoped_assets() {
  test "${COMPOSE_PROJECT_NAME:-}" = "$project" &&
    test "$private" = "$RUNNER_TEMP/foundation-ci-private" &&
    test -d "$private" && test -O "$private" && test ! -L "$private" &&
    test "$(docker inspect "$db_container" \
      --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null)" = "$project" &&
    test "$(docker inspect "$db_container" \
      --format '{{index .Config.Labels "com.docker.compose.service"}}' 2>/dev/null)" = db &&
    test "$(docker volume inspect "$volume" \
      --format '{{index .Labels "com.docker.compose.project"}}' 2>/dev/null)" = "$project" &&
    test "$(docker network inspect "$network" \
      --format '{{index .Labels "com.docker.compose.project"}}' 2>/dev/null)" = "$project"
}
scoped() {
  scoped_assets &&
    test "$(sql -c 'SELECT system_identifier FROM pg_catalog.pg_control_system()' 2>/dev/null)" = "$db_identity"
}

runtime_denial() {
  docker run --rm --network "$network" --read-only \
    --cap-drop ALL --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" \
    -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
    "$test_image" python -c '
from pathlib import Path
import psycopg
p = Path("/run/secrets/runtime_db_password").read_text().strip()
with psycopg.connect(host="db",dbname="carepulse",user="carepulse_runtime",
                     password=p,connect_timeout=5) as c:
    assert c.execute("SELECT session_user").fetchone()[0] == "carepulse_runtime"
    assert c.execute("SELECT has_function_privilege(current_user,%s,%s)",
                     ("public.transition_terminal_appointment(uuid,uuid,bigint,jsonb)",
                      "EXECUTE")).fetchone()[0] is False
    try:
        c.execute("SELECT public.transition_terminal_appointment(%s,%s,%s,%s::jsonb)",
                  (None, None, 0, "{}"))
    except psycopg.Error as error:
        assert error.sqlstate == "42501"
    else:
        raise AssertionError("Terminal writer callable after revoke")
'
}

destroy_verified_project() {
  if scoped_assets; then
    docker compose --profile mail --profile test --profile sender --profile operator \
      down --remove-orphans --volumes || true
    echo 'T133 FAIL CLOSED: destroyed verified disposable project and volumes' >&2
  else
    echo 'T133 BLOCKER: cannot independently verify safe scoped teardown' >&2
  fi
}

cleanup() {
  result=$?
  trap - EXIT INT TERM
  if $grant_open; then
    # Never leave a running test worker with EXECUTE while revoking.
    if docker container inspect "$worker" >/dev/null 2>&1; then
      if test "$(docker inspect "$worker" \
        --format '{{index .Config.Labels "com.carepulse.t133.project"}}')" = "$project"; then
        docker stop -t 2 "$worker" >/dev/null 2>&1 || true
      else
        result=1
      fi
    fi
    if scoped && sql -c "
      REVOKE ALL ON FUNCTION $signature FROM carepulse_runtime, PUBLIC" &&
       test "$(sql -c "SELECT CASE WHEN has_function_privilege(
         'carepulse_runtime','$signature','EXECUTE') THEN 'FAIL' ELSE 'DENY' END")" = DENY &&
       runtime_denial; then
      echo 'T133 ACL AFTER: DENY via fresh runtime login'
    else
      destroy_verified_project
      result=1
    fi
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

scoped || fail 'PostgreSQL identity changed before grant'
sql -c "GRANT EXECUTE ON FUNCTION $signature TO carepulse_runtime"
grant_open=true
test "$(sql -c "
  SELECT CASE WHEN has_function_privilege('carepulse_runtime',
             '$signature','EXECUTE')
       AND EXISTS (
         SELECT 1 FROM aclexplode(p.proacl) acl
         WHERE acl.grantee = (SELECT oid FROM pg_roles WHERE rolname='carepulse_runtime')
           AND acl.privilege_type = 'EXECUTE' AND NOT acl.is_grantable
       )
       AND NOT EXISTS (
         SELECT 1 FROM aclexplode(p.proacl) acl WHERE acl.grantee=0
       )
    THEN 'ALLOW' ELSE 'FAIL' END
  FROM pg_proc p WHERE p.oid='$signature'::regprocedure")" = ALLOW ||
  fail 'grant privilege not narrowly effective'
echo 'T133 ACL DURING: runtime EXECUTE only on exact signature, no grant option'

docker run --rm --name "$worker" --network "$network" --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --cap-drop ALL --security-opt no-new-privileges \
  --label "com.carepulse.t133.project=$project" \
  --label "com.carepulse.t133.sha=$GITHUB_SHA" \
  --user "$(id -u):$(id -g)" \
  -e CI=true -e CP_T133_ACTIVE=true -e CP_T133_FIXTURE_PATH=/tmp/t133/fixtures.json \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$private/t133:/tmp/t133:ro" \
  -v "$CP_RUNTIME_DB_PASSWORD_FILE:/run/secrets/runtime_db_password:ro" \
  "$test_image" python -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_t133_runtime.py | tee "$private/t133/runtime.log"
test "$(sql -c "SELECT count(*) FROM pg_stat_activity
  WHERE application_name='t133-runtime'")" = 0 ||
  fail 'T133 runtime session still connected'
scoped || fail 'PostgreSQL identity changed before final revoke'
sql -c "REVOKE ALL ON FUNCTION $signature FROM carepulse_runtime, PUBLIC"
test "$(sql -c "SELECT CASE WHEN has_function_privilege(
  'carepulse_runtime','$signature','EXECUTE') THEN 'FAIL' ELSE 'DENY' END")" = DENY ||
  fail 'T133 revoke not effective'
runtime_denial || fail 'T133 fresh runtime login can still execute terminal writer'
grant_open=false
echo 'T133 ACL AFTER: DENY via fresh runtime login'
docker compose --profile test run --rm --no-deps -T \
  --user "$(id -u):$(id -g)" \
  -e CI=true -e CP_T133_OBSERVER=true \
  -e CP_CI_PROJECT="$project" -e CP_COMPOSE_CONFIG_PATH=/tmp/foundation-compose.json \
  -e CP_T133_FIXTURE_PATH=/tmp/t133/fixtures.json \
  -v "$private/compose.json:/tmp/foundation-compose.json:ro" \
  -v "$private/t133:/tmp/t133:ro" \
  -v "$CP_MIGRATION_SECRET_FILE:/run/secrets/migration_password:ro" \
  test python -m pytest -q -ra --tb=short -p no:cacheprovider \
  tests/integration/test_t133_observer.py | tee "$private/t133/observer.log"
echo "T133 scoped runtime acceptance complete: $repo $ref $GITHUB_SHA"
