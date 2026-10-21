"""Idle foundation worker; domain processing is introduced in later Waves."""

from threading import Event

import carepulse  # noqa: F401 — confirms the shared package is importable


def main() -> None:
    Event().wait()


if __name__ == "__main__":
    main()
