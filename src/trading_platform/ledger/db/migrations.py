"""PostgreSQL 账本 schema 的有序、事务化迁移。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from trading_platform.ledger.db.models import create_connection_pool
from trading_platform.shared.config import DatabaseConfig


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_FILENAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
_LOCK_NAMESPACE = "trading_platform:ledger_schema_migrations"


class MigrationError(RuntimeError):
    """迁移集合或数据库版本不可信。"""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    checksum: str
    sql: str


@dataclass(frozen=True)
class MigrationResult:
    current_version: int
    applied_versions: tuple[int, ...]


def load_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        content = path.read_bytes()
        migrations.append(
            Migration(
                version=int(match.group("version")),
                filename=path.name,
                checksum=hashlib.sha256(content).hexdigest(),
                sql=content.decode("utf-8"),
            )
        )
    if not migrations:
        raise MigrationError("no ledger migrations found")
    versions = [migration.version for migration in migrations]
    expected = list(range(1, len(migrations) + 1))
    if versions != expected:
        raise MigrationError(
            f"migration versions must be contiguous from 0001: {versions}"
        )
    return tuple(migrations)


async def _set_search_path(conn: object, schema: str) -> None:
    await conn.execute(
        sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
            sql.Identifier(schema)
        )
    )


async def _read_applied(conn: object) -> list[tuple[int, str, str]]:
    relation = await (
        await conn.execute("SELECT to_regclass('ledger_schema_migrations')")
    ).fetchone()
    if relation is None or relation[0] is None:
        return []
    return await (
        await conn.execute(
            "SELECT version, filename, checksum "
            "FROM ledger_schema_migrations ORDER BY version"
        )
    ).fetchall()


def _validate_applied(
    applied: Sequence[tuple[int, str, str]], migrations: Sequence[Migration]
) -> int:
    if len(applied) > len(migrations):
        raise MigrationError("database schema version is newer than this build")
    for index, row in enumerate(applied):
        version, filename, checksum = row
        expected = migrations[index]
        if version != expected.version:
            raise MigrationError(
                f"database migration history has a gap at version {expected.version}"
            )
        if filename != expected.filename or checksum != expected.checksum:
            raise MigrationError(
                f"applied migration {version:04d} does not match this build"
            )
    return applied[-1][0] if applied else 0


async def apply_migrations(
    pool: AsyncConnectionPool,
    *,
    schema: str = "public",
    directory: Path = MIGRATIONS_DIR,
) -> MigrationResult:
    """在一个事务和事务级 advisory lock 中应用全部待执行迁移。"""
    migrations = load_migrations(directory)
    applied_versions: list[int] = []
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_search_path(conn, schema)
            lock_key = f"{_LOCK_NAMESPACE}:{schema}"
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_schema_migrations (
                    version INTEGER PRIMARY KEY CHECK (version > 0),
                    filename TEXT NOT NULL,
                    checksum CHAR(64) NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            current = _validate_applied(await _read_applied(conn), migrations)
            for migration in migrations[current:]:
                await conn.execute(migration.sql)
                await conn.execute(
                    "INSERT INTO ledger_schema_migrations "
                    "(version, filename, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.filename, migration.checksum),
                )
                applied_versions.append(migration.version)
            current = _validate_applied(await _read_applied(conn), migrations)
            if current != migrations[-1].version:
                raise MigrationError("ledger schema did not reach the current version")
    return MigrationResult(current, tuple(applied_versions))


async def verify_current(
    pool: AsyncConnectionPool,
    *,
    schema: str = "public",
    directory: Path = MIGRATIONS_DIR,
) -> int:
    """拒绝未迁移、超前、缺号或已被改写的数据库版本。"""
    migrations = load_migrations(directory)
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_search_path(conn, schema)
            applied = await _read_applied(conn)
            if not applied:
                raise MigrationError("ledger database has not been migrated")
            current = _validate_applied(applied, migrations)
    if current != migrations[-1].version:
        raise MigrationError(
            f"ledger schema is at {current:04d}, expected {migrations[-1].version:04d}"
        )
    return current


async def _run_cli(dsn: str, action: str) -> None:
    pool = await create_connection_pool(dsn, min_size=1, max_size=2)
    try:
        if action == "migrate":
            result = await apply_migrations(pool)
            await verify_current(pool)
            versions = ",".join(f"{value:04d}" for value in result.applied_versions)
            print(
                f"ledger schema current={result.current_version:04d} "
                f"applied={versions or 'none'}"
            )
        else:
            current = await verify_current(pool)
            print(f"ledger schema current={current:04d}")
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ledger PostgreSQL migrations")
    parser.add_argument("action", choices=("migrate", "status"))
    parser.add_argument("--dsn", help="defaults to DB_* environment settings")
    args = parser.parse_args()
    dsn = args.dsn or DatabaseConfig().dsn
    asyncio.run(_run_cli(dsn, args.action))


if __name__ == "__main__":
    main()
