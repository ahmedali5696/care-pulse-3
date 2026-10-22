"""Idle foundation worker; domain processing is introduced in later Waves."""

from threading import Event

from carepulse.config import load_config
from carepulse.db import create_db_engine


def main() -> None:
    config = load_config("worker")
    engine = create_db_engine(config)
    try:
        Event().wait()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
