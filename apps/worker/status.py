"""Private worker heartbeat; no HTTP server, job state or sensitive diagnostics."""

import json
import math
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

STATUS_PATH = Path("/tmp/carepulse-worker-status.json")
HEARTBEAT_INTERVAL_SECONDS = 2
# 10 > heartbeat interval (2) + worst bounded dependency probe (4).
MAX_HEARTBEAT_AGE_SECONDS = 10


def process_identity(pid: int) -> str:
    stat_fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if stat_fields[0] in ("Z", "X"):
        raise ValueError("Worker process is not live")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return f"{boot}:{stat_fields[19]}"


def write_status(path: Path, started: str, ready: bool) -> None:
    status = {
        "pid": os.getpid(),
        "started": started,
        "heartbeat": time.monotonic(),
        "alive": True,
        "ready": ready,
    }
    descriptor, filename = tempfile.mkstemp(dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(status, file)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_status(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > 1024:
            raise ValueError("Invalid worker status file")
        if os.name == "posix" and (
            details.st_uid != os.getuid() or details.st_mode & 0o022
        ):
            raise ValueError("Worker status file is not process-owned")
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            return json.loads(file.read(1025))
    finally:
        os.close(descriptor)


def _matches_process(status: dict, expected_pid: int) -> bool:
    return (
        set(status) == {"pid", "started", "heartbeat", "alive", "ready"}
        and type(status["pid"]) is int
        and status["pid"] == expected_pid
        and status["started"] == process_identity(expected_pid)
        and status["alive"] is True
        and type(status["ready"]) is bool
    )


def _fresh_heartbeat(heartbeat: object) -> bool:
    return (
        type(heartbeat) in (int, float)
        and math.isfinite(heartbeat)
        and 0 <= time.monotonic() - heartbeat <= MAX_HEARTBEAT_AGE_SECONDS
    )


def probe_status(path: Path, requested: str, expected_pid: int = 1) -> bool:
    try:
        status = _read_status(path)
        return (
            requested in ("live", "ready")
            and _matches_process(status, expected_pid)
            and _fresh_heartbeat(status["heartbeat"])
            and (requested == "live" or status["ready"])
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return False


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit(1)
    path = Path(sys.argv[2]) if len(sys.argv) == 3 else STATUS_PATH
    raise SystemExit(0 if probe_status(path, sys.argv[1]) else 1)
