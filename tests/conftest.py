"""Global pytest safety checks."""

from __future__ import annotations

import os
import re

import pytest
from psycopg.conninfo import conninfo_to_dict


def _is_dedicated_test_database(database: str) -> bool:
    parts = set(filter(None, re.split(r"[-_]", database.lower())))
    return bool(parts & {"test", "pytest"})


def _require_dedicated_test_database(database: str, *, source: str) -> None:
    if _is_dedicated_test_database(database):
        return
    raise pytest.UsageError(
        f"{source} must point to a dedicated test database whose name contains "
        "a 'test' or 'pytest' segment; refusing to run against database "
        f"{database!r}"
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    configured_database = os.getenv("DB_DATABASE")
    if configured_database:
        _require_dedicated_test_database(
            configured_database,
            source="DB_DATABASE",
        )

    dsn = os.getenv("LEDGER_TEST_DSN")
    if not dsn:
        return
    try:
        database = conninfo_to_dict(dsn).get("dbname", "")
    except Exception as error:
        raise pytest.UsageError(f"invalid LEDGER_TEST_DSN: {error}") from error
    _require_dedicated_test_database(database, source="LEDGER_TEST_DSN")
