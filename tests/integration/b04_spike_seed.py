"""Explicit-only B-04 spike fixture export; never collected by the normal suite."""

import json
import os
from pathlib import Path

import psycopg
from test_sessions import _seed_owner


def test_export_existing_synthetic_owner(disposable_ci):
    with psycopg.connect(
        host="db",
        dbname="carepulse",
        user="carepulse_bootstrap",
        password=Path("/run/secrets/postgres_password").read_text().strip(),
    ) as connection:
        owner = _seed_owner(connection)
    Path(os.environ["CP_B04_SEED_FILE"]).write_text(
        json.dumps(vars(owner), default=str)
    )
