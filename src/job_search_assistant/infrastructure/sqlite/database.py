"""SQLite connection and migration support for the local-first application."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Sequence

from job_search_assistant.core.errors import InfrastructureError


_MIGRATION_FILE_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    """A versioned, checksum-protected SQL migration file."""

    version: str
    name: str
    path: Path
    checksum: str
    sql: str


class SQLiteDatabase:
    """Owns SQLite connections, transactions and ordered schema migrations."""

    def __init__(self, database_path: Path | str, migrations_path: Path | str) -> None:
        self._database_path = Path(database_path)
        self._migrations_path = Path(migrations_path)

    @property
    def database_path(self) -> Path:
        """Return the configured database file path."""
        return self._database_path

    def connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection for a single unit of work."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Yield a connection in a transaction and commit/rollback deterministically."""
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> list[str]:
        """Apply each unapplied migration in order and verify prior checksums."""
        migrations = self._load_migrations()
        with self.connect() as connection:
            applied = self._read_applied_migrations(connection)
            for migration in migrations:
                prior_checksum = applied.get(migration.version)
                if prior_checksum is not None:
                    if prior_checksum != migration.checksum:
                        raise InfrastructureError(
                            "An applied database migration was modified.",
                            details={
                                "version": migration.version,
                                "path": str(migration.path),
                                "expected_checksum": prior_checksum,
                                "actual_checksum": migration.checksum,
                            },
                        )
                    continue
                self._apply_migration(connection, migration)
                applied[migration.version] = migration.checksum
        return [migration.version for migration in migrations]

    def _load_migrations(self) -> list[Migration]:
        if not self._migrations_path.exists():
            raise InfrastructureError(
                "The migrations directory does not exist.",
                details={"migrations_path": str(self._migrations_path)},
            )
        if not self._migrations_path.is_dir():
            raise InfrastructureError(
                "The migrations path is not a directory.",
                details={"migrations_path": str(self._migrations_path)},
            )

        migrations: list[Migration] = []
        seen_versions: set[str] = set()
        for path in sorted(self._migrations_path.glob("*.sql")):
            match = _MIGRATION_FILE_PATTERN.match(path.name)
            if match is None:
                raise InfrastructureError(
                    "Migration filenames must use the NNNN_description.sql format.",
                    details={"path": str(path)},
                )
            version = match.group("version")
            if version in seen_versions:
                raise InfrastructureError(
                    "Duplicate migration version found.",
                    details={"version": version, "path": str(path)},
                )
            seen_versions.add(version)
            sql = path.read_text(encoding="utf-8")
            migrations.append(
                Migration(
                    version=version,
                    name=match.group("name"),
                    path=path,
                    checksum=sha256(sql.encode("utf-8")).hexdigest(),
                    sql=sql,
                )
            )
        return migrations

    @staticmethod
    def _read_applied_migrations(connection: sqlite3.Connection) -> dict[str, str]:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if table_exists is None:
            return {}
        rows = connection.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        return {str(row["version"]): str(row["checksum"]) for row in rows}

    @staticmethod
    def _apply_migration(connection: sqlite3.Connection, migration: Migration) -> None:
        """Apply schema changes and the immutable migration record in one SQL script."""
        applied_at = datetime.now(UTC).isoformat()
        version = _sql_literal(migration.version)
        checksum = _sql_literal(migration.checksum)
        timestamp = _sql_literal(applied_at)
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql.rstrip()}\n"
            "INSERT INTO schema_migrations (version, checksum, applied_at) "
            f"VALUES ({version}, {checksum}, {timestamp});\n"
            "COMMIT;"
        )
        try:
            connection.executescript(script)
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise InfrastructureError(
                "Database migration failed.",
                details={"version": migration.version, "path": str(migration.path), "reason": str(exc)},
            ) from exc

    def fetch_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Execute a read-only query in a short-lived connection."""
        with self.connect() as connection:
            return list(connection.execute(sql, parameters).fetchall())


def _sql_literal(value: str) -> str:
    """Return a safely quoted SQL literal for internally generated migration metadata."""
    return "'" + value.replace("'", "''") + "'"
