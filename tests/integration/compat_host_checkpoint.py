"""T051 bounded observer↔host handoff for image-backed restart assertions."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from secrets import token_hex

from ops.compat.preflight import PUBLISHED_SUCCESSORS

_ROOT = Path("/tmp/compat-host")
_REPLIES = Path("/tmp/compat-host-replies")
_OBSERVED = Path("/tmp/compat-observed.json")


def checkpoint_restarted_jobs(
    *,
    step: int,
    head: str,
    db_name: str,
    target_id: str,
    cluster_id: str,
    source_manifest_id: str,
    candidate_manifest_id: str,
    owner_epoch: int,
    tenant_id: str,
    job_id: str,
) -> dict:
    """Fail closed unless the host actually exercises both built worker/sender pairs."""
    if (
        os.environ.get("CP_COMPAT_T051_IPC_DIR") != str(_ROOT)
        or not _ROOT.is_dir()
        or os.environ.get("CP_COMPAT_T051_REPLY_DIR") != str(_REPLIES)
        or not _REPLIES.is_dir()
        or _ROOT.is_symlink()
        or not os.access(_ROOT, os.W_OK)
        or not re.fullmatch(r"compat_mid_[0-9a-f]{12}", db_name)
        or target_id != f"t-{db_name}"
        or type(step) is not int
        or not 0 <= step < len(PUBLISHED_SUCCESSORS)
        or head != PUBLISHED_SUCCESSORS[step]
        or type(owner_epoch) is not int
        or owner_epoch < 1
    ):
        raise AssertionError("Missing or invalid T051 host-controlled checkpoint")
    if not _OBSERVED.is_file():
        raise AssertionError("T051 requires actual Docker image inspection from CI")
    observed = json.loads(_OBSERVED.read_text(encoding="utf-8"))
    image_ids = {
        generation: {
            service: observed["services"][f"{generation}-{service}"]["image_id"]
            for service in ("worker", "sender")
        }
        for generation in ("source", "candidate")
    }
    for images in image_ids.values():
        if any(
            not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            for digest in images.values()
        ):
            raise AssertionError("T051 observed Docker image ID is invalid")

    request_id = token_hex(12)
    request = {
        "version": 1,
        "kind": "intermediate",
        "id": request_id,
        "step": step,
        "db_name": db_name,
        "target_id": target_id,
        "cluster_id": cluster_id,
        "source_manifest_id": source_manifest_id,
        "candidate_manifest_id": candidate_manifest_id,
        "head": head,
        "owner_epoch": owner_epoch,
        "tenant_id": tenant_id,
        "job_id": job_id,
    }
    path = _ROOT / f"{request_id}.request.json"
    response_path = _REPLIES / f"{request_id}.response.json"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{request_id}.", dir=_ROOT)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(request, stream, sort_keys=True)
            # Cross-UID host reader; the containing host private root is 0700.
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            descriptor = os.open(
                response_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except FileNotFoundError:
            time.sleep(0.2)
            continue
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise AssertionError("T051 host response is not a bounded regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                response = json.load(stream)
            descriptor = -1
            break
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    else:
        raise AssertionError("T051 image-backed host checkpoint was not executed")

    if not isinstance(response, dict) or any(
        response.get(field) != request[field]
        for field in (
            "version",
            "kind",
            "id",
            "step",
            "db_name",
            "target_id",
            "cluster_id",
            "source_manifest_id",
            "candidate_manifest_id",
            "head",
            "owner_epoch",
        )
    ):
        raise AssertionError("T051 host response target/head/epoch mismatch")
    if response.get("ok") is not True:
        raise AssertionError("T051 host did not verify image-backed execution")
    if set(response.get("workers", {})) != {"source", "candidate"} or set(
        response.get("senders", {})
    ) != {"source", "candidate"}:
        raise AssertionError("T051 host did not invoke both generations")
    for generation in ("source", "candidate"):
        worker = response["workers"][generation]
        sender = response["senders"][generation]
        if (
            worker.get("image_id") != image_ids[generation]["worker"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(worker.get("first_container_id", ""))
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(worker.get("restart_container_id", ""))
            )
            or worker["first_container_id"] == worker["restart_container_id"]
            or not worker.get("first_start")
            or not worker.get("restart_start")
            or worker["first_start"] == worker["restart_start"]
            or worker.get("heartbeat_advanced") is not True
            or worker.get("live") is not True
            or worker.get("ready") is not False
        ):
            raise AssertionError("T051 image-backed worker did not restart fenced")
        if (
            sender.get("image_id") != image_ids[generation]["sender"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(sender.get("container_id", "")))
            or sender.get("invoked") is not True
            or type(sender.get("exit_code")) is not int
            or sender["exit_code"] != 1
            or sender.get("denial") != "Delivery unavailable"
        ):
            raise AssertionError("T051 independent image-backed sender did not deny")
    before = response.get("mail_before")
    after = response.get("mail_after")
    if type(before) is not int or before < 0 or after != before:
        raise AssertionError("T051 Mailpit delivery count changed")
    return response
