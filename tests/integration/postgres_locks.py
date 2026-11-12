"""Observe lock dependencies between known test transactions."""

from time import monotonic, sleep

from sqlalchemy import text


class PostgresLockNotObserved(AssertionError):
    def __init__(self, waiter, holder, blockers):
        self.waiter = waiter
        self.holder = holder
        self.blockers = blockers
        super().__init__(
            f"PostgreSQL waiter PID {waiter} never reported expected blocker PID "
            f"{holder}; last observed blocker PIDs: {blockers}"
        )


def wait_for_postgres_lock(observer, waiter, holder, *, timeout=15):
    deadline = monotonic() + timeout
    blockers = []
    while monotonic() < deadline:
        # Includes queued blockers and works across roles without viewing query text.
        blockers = observer.scalar(
            text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}
        )
        if holder in blockers:
            return
        sleep(0.02)
    raise PostgresLockNotObserved(waiter, holder, blockers)
