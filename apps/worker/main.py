"""Idle foundation worker; domain processing is introduced in later Waves."""

import os
import time

from carepulse.config import load_config
from carepulse.health import probe_readiness

from apps.worker.status import (
    HEARTBEAT_INTERVAL_SECONDS,
    STATUS_PATH,
    process_identity,
    write_status,
)


def main() -> None:
    load_config("worker")
    started = process_identity(os.getpid())
    write_status(STATUS_PATH, started, False)
    while True:
        write_status(STATUS_PATH, started, probe_readiness("worker"))
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
