"""SQLite initialization, connections, and transaction boundaries."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

from scholarship_intelligence.settings import DATABASE_PATH


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
BUSY_TIMEOUT_SECONDS = 10.0


@contextmanager
def connect_database(
    database_path: str | Path = DATABASE_PATH,
) -> Iterator[sqlite3.Connection]:
    """Open a connection with foreign keys and explicit transactions.

    The database's parent directory must already exist. Use
    initialize_database() when creating a database for the first time.
    """
    connection = sqlite3.connect(
        str(database_path),
        timeout=BUSY_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA foreign_keys = ON")

        enabled = connection.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0]
        if enabled != 1:
            raise RuntimeError("SQLite foreign-key enforcement is unavailable.")

        yield connection
    finally:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()


@contextmanager
def transaction(
    connection: sqlite3.Connection,
) -> Iterator[sqlite3.Connection]:
    """Commit all operations together, or roll back on failure.

    BEGIN IMMEDIATE reserves the write transaction before operations
    begin. Nested transactions are deliberately rejected.
    """
    if connection.in_transaction:
        raise RuntimeError("Nested transactions are not supported.")

    connection.execute("BEGIN IMMEDIATE")

    try:
        yield connection
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def initialize_database(
    database_path: str | Path = DATABASE_PATH,
    *,
    schema_path: str | Path = SCHEMA_PATH,
) -> Path:
    """Create the database directory and apply the initial schema.

    The schema file owns its BEGIN/COMMIT boundary. Its IF NOT EXISTS
    statements allow repeated initialization; this is not a migration
    mechanism for changing existing tables.
    """
    target = Path(database_path).expanduser().resolve()
    schema_file = Path(schema_path).expanduser().resolve()

    # Read before creating directories, so a missing schema fails early.
    schema = schema_file.read_text(encoding="utf-8")
    if not schema.strip():
        raise ValueError("The schema file is empty.")

    target.parent.mkdir(parents=True, exist_ok=True)

    with connect_database(target) as connection:
        connection.executescript(schema)

        if connection.in_transaction:
            raise RuntimeError(
                "Schema initialization did not finish its transaction."
            )

        violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if violations:
            raise RuntimeError(
                "Database contains foreign-key violations."
            )

    return target