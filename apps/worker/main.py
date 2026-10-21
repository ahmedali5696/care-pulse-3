"""Idle foundation worker; domain processing is introduced in later Waves."""

from threading import Event

from carepulse.config import load_config


def main() -> None:
    load_config("worker")
    Event().wait()


if __name__ == "__main__":
    main()
