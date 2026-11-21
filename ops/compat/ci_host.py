"""CI-only, bounded Docker supervisor for disposable compatibility evidence.

The observer can request two fixed operations through a private directory; it
never receives a Docker socket or chooses commands, images, networks or secrets.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

SOURCE_HEAD = "0040_t173_numeric_money"
SUCCESSORS = (
    "0041_f01_runtime_identity",
    "0042_f01_effective_grants",
    "0043_f01_writer_boundary",
    "0044_f01_delivery_acl",
    "0045_f01_revocation_evidence",
    "0046_f01_audit_integrity",
)
COMPOSE = ("docker", "compose", "-f", "compose.compat.yaml")
IDENT = re.compile(r"[a-z][a-z0-9_]{2,75}\Z")
TOKEN = re.compile(r"[0-9a-f]{12,32}\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
RUN_TIMEOUT = 20


def trusted_foundation_event() -> bool:
    """Only first-party ordinary Foundation events may use the Docker supervisor."""
    if (
        os.environ.get("CI") != "true"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_OS") != "Linux"
        or os.environ.get("GITHUB_REPOSITORY") != "ahmedali5696/care-pulse-3"
        or os.environ.get("GITHUB_SERVER_URL") != "https://github.com"
    ):
        return False
    event = os.environ.get("GITHUB_EVENT_NAME")
    if event == "workflow_dispatch":
        return os.environ.get("GITHUB_REF", "").startswith("refs/heads/")
    if event == "push":
        return os.environ.get("GITHUB_REF") == "refs/heads/development"
    if event == "pull_request":
        try:
            payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            return (
                payload["repository"]["full_name"] == "ahmedali5696/care-pulse-3"
                and payload["pull_request"]["head"]["repo"]["full_name"]
                == "ahmedali5696/care-pulse-3"
                and payload["pull_request"]["base"]["repo"]["full_name"]
                == "ahmedali5696/care-pulse-3"
            )
        except (KeyError, OSError, TypeError, ValueError):
            return False
    return False


def command(args: list[str] | tuple[str, ...], *, timeout: int = RUN_TIMEOUT):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def required(args: list[str] | tuple[str, ...], *, timeout: int = RUN_TIMEOUT) -> str:
    result = command(args, timeout=timeout)
    if result.returncode:
        raise RuntimeError("CI-host command failed")
    return result.stdout.strip()


def compose(*args: str, timeout: int = RUN_TIMEOUT) -> str:
    return required((*COMPOSE, *args), timeout=timeout)


def emit(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(payload, output, separators=(",", ":"), sort_keys=True)
    os.chmod(temporary, 0o644)  # Readable to the unprivileged observer UID 65532.
    os.replace(temporary, path)


class Supervisor:
    def __init__(
        self, directory: Path, observed_path: Path, response_directory: Path
    ) -> None:
        if (
            not trusted_foundation_event()
            or not re.fullmatch(
                r"carepulse-ci-[0-9]+-[0-9]+", os.environ.get("CP_CI_PROJECT", "")
            )
            or os.environ.get("COMPOSE_PROJECT_NAME") != os.environ.get("CP_CI_PROJECT")
        ):
            raise RuntimeError("CI-host requires a matched disposable runner")
        if (
            not directory.is_dir()
            or not response_directory.is_dir()
            or directory.is_symlink()
            or response_directory.is_symlink()
            or directory.resolve() == response_directory.resolve()
        ):
            raise RuntimeError(
                "CI-host request and response directories must be separate"
            )
        self.directory = directory
        self.response_directory = response_directory
        self.observed = json.loads(observed_path.read_text(encoding="utf-8"))
        if self.observed["candidate_sha"] != os.environ.get("GITHUB_SHA"):
            raise RuntimeError(
                "CI-host observed image provenance differs from checkout"
            )
        self.active: dict[str, tuple[str, dict]] = {}
        self.sealed_source_targets: set[str] = set()
        self.stopped_source_ids: dict[str, str] = {}
        self.processed: set[str] = set()
        self.stopped = False

    def validate(self, request: dict) -> None:
        if (
            request.get("kind") not in ("booking_source", "intermediate")
            or request.get("version") != 1
        ):
            raise RuntimeError("Unrecognized CI-host operation")
        db = request.get("db_name", "")
        target = request.get("target_id")
        if (
            not isinstance(db, str)
            or not IDENT.fullmatch(db)
            or not db.startswith("compat_")
        ):
            raise RuntimeError("Invalid disposable database")
        if target != f"t-{db}" or db == "carepulse":
            raise RuntimeError("Invalid target identity")
        source = request.get("source_manifest_id")
        if source != "source-0040":
            raise RuntimeError("Unrecognized source manifest")
        if request["kind"] == "intermediate" and (
            request.get("step") not in range(6)
            or request.get("head") != SUCCESSORS[request["step"]]
            or request.get("candidate_manifest_id") != "candidate-0046"
            or type(request.get("owner_epoch")) is not int
            or request["owner_epoch"] <= 0
            or not UUID.fullmatch(request.get("tenant_id", ""))
            or not UUID.fullmatch(request.get("job_id", ""))
            or not request.get("cluster_id", "").isdigit()
        ):
            raise RuntimeError("Invalid intermediate checkpoint")

        # PostgreSQL is queried by the HOST, not trusted from the observer.
        # Identifiers are narrowly validated before entering the SQL literal.
        head = SOURCE_HEAD if request["kind"] == "booking_source" else request["head"]
        phase = "SOURCE_OPEN" if request["kind"] == "booking_source" else "MIGRATING"
        generation = "source" if request["kind"] == "booking_source" else "none"
        sql = (
            "SELECT concat_ws('|',t.target_id,t.cluster_id,t.db_name,t.phase,"
            "t.owner_epoch,t.source_manifest_id,t.target_manifest_id,t.active_generation,"
            "CASE WHEN t.source_sealed THEN 'true' ELSE 'false' END,"
            "(SELECT version_num FROM public.alembic_version)) "
            "FROM compat_control.target_control t WHERE singleton_id=true"
        )
        record = compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "carepulse_bootstrap",
            "-d",
            db,
            "-c",
            sql,
        ).split("|")
        if (
            len(record) != 10
            or record[0] != target
            or record[2] != db
            or record[3] != phase
            or record[5] != source
            or record[7] != generation
            or record[9] != head
        ):
            raise RuntimeError("CI-host target/head/phase mismatch")
        if request["kind"] == "intermediate":
            if record[1] != request["cluster_id"]:
                raise RuntimeError("CI-host cluster identity mismatch")
            if int(record[4]) != request["owner_epoch"]:
                raise RuntimeError("CI-host owner epoch mismatch")
            if record[6] != request["candidate_manifest_id"]:
                raise RuntimeError("CI-host candidate manifest mismatch")
            if record[8] != "true":
                raise RuntimeError("CI-host source-seal mismatch")
        elif record[8] != "false":
            raise RuntimeError("CI-host source already sealed")

    def launch(
        self, service: str, request: dict, *, name: str, args: tuple[str, ...] = ()
    ) -> str:
        if (
            service in ("source-api", "source-web")
            and request["target_id"] in self.sealed_source_targets
        ):
            raise RuntimeError("Source ingress launch is sealed")
        generation = service.split("-", 1)[0]
        manifest = (
            request["source_manifest_id"]
            if generation == "source"
            else request["candidate_manifest_id"]
        )
        cid = compose(
            "run",
            "-d",
            "--no-deps",
            "--use-aliases",
            "--name",
            name,
            "-e",
            f"CP_DB_NAME={request['db_name']}",
            "-e",
            f"CP_COMPAT_TARGET_ID={request['target_id']}",
            "-e",
            f"CP_COMPAT_MANIFEST_ID={manifest}",
            service,
            *args,
            timeout=30,
        )
        actual = required(("docker", "inspect", "--format", "{{.Image}}", cid))
        if actual != self.observed["services"][service]["image_id"]:
            self.remove(cid)
            raise RuntimeError("CI-host runtime image mismatches observed digest")
        return cid

    @staticmethod
    def remove(cid: str) -> None:
        required(("docker", "rm", "-f", cid), timeout=10)

    def mail_count(self) -> int:
        script = (
            "import json,urllib.request; "
            "print(json.load(urllib.request.urlopen('http://mail:8025/api/v1/messages',timeout=5))['total'])"
        )
        return int(
            compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "python",
                "observer",
                "-c",
                script,
                timeout=15,
            )
        )

    def worker(self, service: str, request: dict, token: str) -> dict:
        starts: list[tuple[str, str, float]] = []
        for sequence in (0, 1):
            name = f"{os.environ['CP_CI_PROJECT']}-{token[:12]}-{service}-{sequence}"
            cid = self.launch(service, request, name=name)
            try:
                deadline = time.monotonic() + 10
                status = None
                while time.monotonic() < deadline:
                    observed = command(
                        (
                            "docker",
                            "exec",
                            cid,
                            "cat",
                            "/tmp/carepulse-worker-status.json",
                        ),
                        timeout=3,
                    )
                    if observed.returncode == 0:
                        try:
                            status = json.loads(observed.stdout)
                        except ValueError:
                            status = None
                        if (
                            status
                            and status.get("alive") is True
                            and status.get("ready") is False
                        ):
                            break
                    time.sleep(0.15)
                if status is None or status.get("ready") is not False:
                    raise RuntimeError("Worker did not produce fenced heartbeat")
                if (
                    command(
                        (
                            "docker",
                            "exec",
                            cid,
                            "python",
                            "-m",
                            "apps.worker.status",
                            "live",
                        )
                    ).returncode
                    != 0
                ):
                    raise RuntimeError("Actual worker not live")
                if (
                    command(
                        (
                            "docker",
                            "exec",
                            cid,
                            "python",
                            "-m",
                            "apps.worker.status",
                            "ready",
                        )
                    ).returncode
                    == 0
                ):
                    raise RuntimeError("Fenced worker unexpectedly ready")
                first_beat = status["heartbeat"]
                updated = False
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    latest = json.loads(
                        required(
                            (
                                "docker",
                                "exec",
                                cid,
                                "cat",
                                "/tmp/carepulse-worker-status.json",
                            ),
                            timeout=3,
                        )
                    )
                    if latest.get("heartbeat", 0) > first_beat:
                        updated = True
                        break
                    time.sleep(0.25)
                if not updated:
                    raise RuntimeError("Worker heartbeat did not advance")
                starts.append((cid, status["started"], first_beat))
            finally:
                self.remove(cid)
        if starts[0][0] == starts[1][0] or starts[0][1] == starts[1][1]:
            raise RuntimeError("Worker restart failed to refresh process identity")
        return {
            "image_id": self.observed["services"][service]["image_id"],
            "first_container_id": starts[0][0],
            "restart_container_id": starts[1][0],
            "first_start": starts[0][1],
            "restart_start": starts[1][1],
            "heartbeat_advanced": True,
            "live": True,
            "ready": False,
        }

    def sender(self, service: str, request: dict, token: str) -> dict:
        name = f"{os.environ['CP_CI_PROJECT']}-{token[:12]}-{service}"
        generation = service.split("-", 1)[0]
        manifest = (
            request["source_manifest_id"]
            if generation == "source"
            else request["candidate_manifest_id"]
        )
        spec = self.observed["services"][service]
        args = (
            *COMPOSE,
            "run",
            "--no-deps",
            "-T",
            "--name",
            name,
            "-e",
            f"CP_DB_NAME={request['db_name']}",
            "-e",
            f"CP_COMPAT_TARGET_ID={request['target_id']}",
            "-e",
            f"CP_COMPAT_MANIFEST_ID={manifest}",
            service,
            "--organization-id",
            request["tenant_id"],
            "--job-id",
            request["job_id"],
        )
        try:
            response = command(args, timeout=20)
            actual = required(
                ("docker", "inspect", "--format", "{{.Id}}|{{.Image}}", name)
            ).split("|")
            if (
                len(actual) != 2
                or not re.fullmatch(r"[0-9a-f]{64}", actual[0])
                or actual[1] != spec["image_id"]
            ):
                raise RuntimeError("Sender container image mismatches inspected build")
            if (
                response.returncode != 1
                or response.stdout.strip() != "Delivery unavailable"
            ):
                # Do not expose sender output, which could contain sensitive material.
                raise RuntimeError(
                    f"{service} sender denial mismatch: exit={response.returncode}, "
                    f"expected_output={response.stdout.strip() == 'Delivery unavailable'}, "
                    f"stdout_bytes={len(response.stdout)}, stderr_bytes={len(response.stderr)}"
                )
        finally:
            self.remove(name)
        return {
            "image_id": spec["image_id"],
            "container_id": actual[0],
            "invoked": True,
            "exit_code": 1,
            "denial": "Delivery unavailable",
        }

    def intermediate(self, request: dict) -> dict:
        self.validate(request)
        token = request["id"]
        before = self.mail_count()
        workers = {}
        senders = {}
        for generation in ("source", "candidate"):
            self.validate(request)
            workers[generation] = self.worker(f"{generation}-worker", request, token)
            self.validate(request)
            senders[generation] = self.sender(f"{generation}-sender", request, token)
        self.validate(request)
        after = self.mail_count()
        if before != after:
            raise RuntimeError("SMTP synthetic sink changed during fenced recovery")
        return {
            **request,
            "status": "ok",
            "ok": True,
            "workers": workers,
            "senders": senders,
            "mail_before": before,
            "mail_after": after,
        }

    def book_source(self, request: dict) -> dict:
        self.validate(request)
        if self.active or request["target_id"] in self.sealed_source_targets:
            raise RuntimeError("Source runtime already running")
        name = f"{os.environ['CP_CI_PROJECT']}-{request['id'][:12]}-source-api"
        cid = self.launch("source-api", request, name=name)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                check = command(
                    (
                        "docker",
                        "exec",
                        cid,
                        "python",
                        "-c",
                        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=2)",
                    ),
                    timeout=4,
                )
                if check.returncode == 0:
                    self.active[request["id"]] = (cid, request)
                    return {
                        **request,
                        "status": "ready",
                        "endpoint": "http://source-api:8000",
                    }
                time.sleep(0.2)
            raise RuntimeError("Source image did not become live")
        finally:
            if request["id"] not in self.active:
                self.remove(cid)

    def stop_source(self, request: dict) -> dict:
        item = self.active.get(request["id"])
        if item is None or any(
            item[1].get(k) != request.get(k)
            for k in ("id", "kind", "db_name", "target_id", "source_manifest_id")
        ):
            raise RuntimeError("Source stop does not match launched generation")
        db = request["db_name"]
        target = request["target_id"]
        if (
            not IDENT.fullmatch(db)
            or not db.startswith("compat_")
            or target != f"t-{db}"
        ):
            raise RuntimeError("Source stop target identity mismatch")
        record = compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "carepulse_bootstrap",
            "-d",
            db,
            "-c",
            "SELECT concat_ws('|', target_id,db_name,phase,owner_epoch,"
            "source_manifest_id,active_generation,"
            "CASE WHEN source_sealed THEN 'true' ELSE 'false' END) "
            "FROM compat_control.target_control WHERE singleton_id=true",
        ).split("|")
        if (
            len(record) != 7
            or record[0] != target
            or record[1] != db
            or record[4] != request["source_manifest_id"]
            or record[2] not in ("SOURCE_OPEN", "DRAINING")
            or record[5] != ("source" if record[2] == "SOURCE_OPEN" else "none")
            or record[6] != "false"
        ):
            raise RuntimeError("Source stop control/phase mismatch")
        epoch = int(record[3])
        if request.get("owner_epoch") is not None and (
            type(request["owner_epoch"]) is not int or request["owner_epoch"] != epoch
        ):
            raise RuntimeError("Source stop owner epoch mismatch")
        if record[2] == "DRAINING" and request.get("owner_epoch") != epoch:
            raise RuntimeError("Fenced source stop requires current owner epoch")
        identity = required(
            (
                "docker",
                "inspect",
                "--format",
                "{{.Id}}|{{.Image}}|{{.HostConfig.RestartPolicy.Name}}",
                item[0],
            )
        ).split("|")
        if (
            len(identity) != 3
            or not re.fullmatch(r"[0-9a-f]{64}", identity[0])
            or (len(item[0]) == 64 and identity[0] != item[0])
            or identity[1] != self.observed["services"]["source-api"]["image_id"]
            or identity[2] != "no"
        ):
            raise RuntimeError("Source stop image/restart policy mismatch")
        self.remove(item[0])
        if command(("docker", "inspect", identity[0])).returncode == 0:
            raise RuntimeError("Removed source container still exists")
        if required(
            ("docker", "ps", "-aq", "--no-trunc", "--filter", f"id={identity[0]}")
        ):
            raise RuntimeError("Source container remains after removal")
        self.active.pop(request["id"])
        source_services = ("source-api", "source-web", "source-worker", "source-sender")
        for _ in range(2):
            running = required(
                (
                    "docker",
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.project={os.environ['CP_CI_PROJECT']}",
                    "--format",
                    '{{.Label "com.docker.compose.service"}}|{{.ID}}',
                )
            )
            if any(
                line.split("|", 1)[0] in source_services
                for line in running.splitlines()
            ):
                raise RuntimeError("Live source consumer remains after stop")
        self.sealed_source_targets.add(target)
        self.stopped_source_ids[target] = identity[0]
        return {
            "version": 1,
            "status": "stopped",
            "db_name": db,
            "target_id": target,
            "source_manifest_id": request["source_manifest_id"],
            "owner_epoch": epoch,
            "stop_phase": record[2],
            "launch_sealed": record[2] == "DRAINING",
            "stopped_services": list(source_services),
            "source_container_id": identity[0],
            "source_image_id": identity[1],
        }

    def drain_proof(self, request: dict) -> dict:
        """Host-observed, epoch-bound absence of protected service consumers."""
        db = request.get("db_name")
        target = request.get("target_id")
        if (
            request.get("version") != 1
            or request.get("kind") != "drain_proof"
            or not isinstance(db, str)
            or not IDENT.fullmatch(db)
            or not db.startswith("compat_")
            or target != f"t-{db}"
            or request.get("source_manifest_id") != "source-0040"
            or request.get("candidate_manifest_id") != "candidate-0046"
            or request.get("head") not in (SOURCE_HEAD, *SUCCESSORS)
            or not isinstance(request.get("cluster_id"), str)
            or not request["cluster_id"].isdigit()
            or type(request.get("owner_epoch")) is not int
            or request["owner_epoch"] <= 0
        ):
            raise RuntimeError("Invalid drain-proof target/epoch binding")
        record = compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "carepulse_bootstrap",
            "-d",
            db,
            "-c",
            "SELECT concat_ws('|',t.target_id,t.cluster_id,t.db_name,t.phase,"
            "t.owner_epoch,t.source_manifest_id,t.target_manifest_id,"
            "t.active_generation,CASE WHEN t.source_sealed THEN 'true' ELSE 'false' END,"
            "(SELECT CASE WHEN count(*)=1 THEN min(version_num) END "
            "FROM public.alembic_version)) "
            "FROM compat_control.target_control t WHERE singleton_id=true",
        ).split("|")
        if (
            len(record) != 10
            or record[3] not in ("DRAINING", "MIGRATING")
            or (record[3] == "DRAINING" and request["head"] != SOURCE_HEAD)
            or record
            != [
                target,
                request["cluster_id"],
                db,
                record[3],
                str(request["owner_epoch"]),
                request["source_manifest_id"],
                request["candidate_manifest_id"],
                "none",
                "true",
                request["head"],
            ]
        ):
            raise RuntimeError("Drain proof control/head/epoch mismatch")
        if any(item[1]["target_id"] == target for item in self.active.values()):
            raise RuntimeError("Tracked source ingress is still active")
        service_names = tuple(
            f"{generation}-{service}"
            for generation in ("source", "candidate")
            for service in ("api", "web", "worker", "sender")
        )
        for _ in range(2):
            running = required(
                (
                    "docker",
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.project={os.environ['CP_CI_PROJECT']}",
                    "--format",
                    '{{.Label "com.docker.compose.service"}}|{{.ID}}',
                )
            )
            if any(
                line.split("|", 1)[0] in service_names for line in running.splitlines()
            ):
                raise RuntimeError("Product consumer container is still running")
        previous_id = self.stopped_source_ids.get(target)
        if previous_id is not None:
            if command(("docker", "inspect", previous_id)).returncode == 0:
                raise RuntimeError("Previously stopped source container restarted")
            if required(
                (
                    "docker",
                    "ps",
                    "-aq",
                    "--no-trunc",
                    "--filter",
                    f"id={previous_id}",
                )
            ):
                raise RuntimeError("Previously stopped source container remains")
        self.sealed_source_targets.add(target)
        return {
            **request,
            "status": "stopped",
            "ok": True,
            "stop_phase": record[3],
            "launch_sealed": True,
            "stopped_services": list(service_names),
            "running_consumer_count": 0,
            "prior_source_container_id": previous_id,
        }

    def handle(self, file: Path) -> None:
        token, suffix = file.name.split(".", 1)
        if not TOKEN.fullmatch(token) or suffix not in ("request.json", "stop.json"):
            raise RuntimeError("Invalid CI-host request file")
        request = json.loads(file.read_text(encoding="utf-8"))
        if request.get("id", token) != token or request.get("version") != 1:
            raise RuntimeError("Invalid CI-host request token")
        request.setdefault("id", token)
        if suffix == "request.json":
            if token in self.processed:
                raise RuntimeError("Duplicate CI-host request")
            self.processed.add(token)
            if request["kind"] == "drain_proof":
                response = self.drain_proof(request)
            elif request["kind"] == "intermediate":
                response = self.intermediate(request)
            elif request["kind"] == "booking_source":
                response = self.book_source(request)
            else:
                raise RuntimeError("Unknown CI-host kind")
            emit(self.response_directory / f"{token}.response.json", response)
        else:
            response = self.stop_source(request)
            emit(self.response_directory / f"{token}.stopped.json", response)

    def run(self) -> None:
        while not self.stopped:
            for file in sorted(self.directory.glob("*.json")):
                if (
                    file.name.endswith((".request.json", ".stop.json"))
                    and file.name not in self.processed
                ):
                    # A separate set covers the stop request for the same token.
                    self.processed.add(file.name)
                    try:
                        self.handle(file)
                    except Exception:
                        suffix = (
                            "stopped.json"
                            if file.name.endswith(".stop.json")
                            else "response.json"
                        )
                        emit(
                            self.response_directory
                            / f"{file.name.split('.')[0]}.{suffix}",
                            {"version": 1, "status": "blocked", "ok": False},
                        )
                        self.stopped = True
                        raise
            time.sleep(0.1)

    def close(self) -> None:
        for cid, _ in self.active.values():
            self.remove(cid)
        self.active.clear()


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    supervisor = Supervisor(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    signal.signal(signal.SIGTERM, lambda _s, _f: setattr(supervisor, "stopped", True))
    try:
        supervisor.run()
    except RuntimeError as invalid:
        # Every RuntimeError in this supervisor uses a fixed, sanitized reason.
        print(
            f"FAIL bounded CI-host compatibility handshake: {invalid}", file=sys.stderr
        )
        return 1
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ):
        print("FAIL bounded CI-host compatibility handshake", file=sys.stderr)
        return 1
    finally:
        supervisor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
