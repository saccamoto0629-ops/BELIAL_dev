"""TRUE read-only SQLite connections for the execution stores (Issue #78,
PR #100 review).

A plain ``sqlite3.connect(path)`` is NOT read-only: it creates a missing DB
file, can create journal / WAL / SHM side files, and accepts writes. Every
read-only path in the execution package (dry-run probes, report CLIs, store
reads) must therefore connect through :func:`connect_read_only`, which

- opens the file via a ``file:...?mode=ro`` URI (SQLite refuses to create the
  file and refuses every write),
- additionally sets ``PRAGMA query_only = ON`` (defense in depth: any write
  statement fails even if a future change loosened the open mode),
- never creates the file, its parent directory, or ``-wal`` / ``-shm`` side
  files, and works inside a read-only directory,
- raises :class:`ReadOnlyDbMissingError` for a missing path instead of
  creating anything (callers decide whether "missing" means ``None`` / empty
  or an explicit failure).

WAL databases: even a ``mode=ro`` connection materializes the ``-shm`` /
``-wal`` side files (SQLite needs the WAL index to read). Every
execution-owned store uses the default rollback journal, so their reads keep
the fully-locked ``mode=ro`` path; a FOREIGN database whose header declares
WAL (read/write format version 2) is opened with ``immutable=1`` instead --
no side files, no mtime change -- at the documented cost that an immutable
probe sees a point-in-time view and must not race a concurrent writer (the
same trade-off Paper Execution's snapshot-copy dry-run reads make).

A corrupted or foreign file still OPENS here (SQLite validates lazily); the
callers' schema verification is what rejects it -- this module only
guarantees that looking at a file never modifies the filesystem.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_SECONDS = 30


class ReadOnlyDbMissingError(FileNotFoundError):
    """The database file (or its parent directory) does not exist. It is
    NEVER created by the read-only path."""


def _is_wal_header(path: Path) -> bool:
    """True when the SQLite header declares WAL journaling (offsets 18/19 ==
    2). Reading the header bytes touches nothing."""
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return len(header) >= 20 and header[:16] == b"SQLite format 3\x00" and header[18] == 2


def connect_read_only(db_path: Path | str) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only (see the module docstring).

    The caller owns closing the connection (use ``contextlib.closing``).
    """
    path = Path(db_path)
    if not path.exists():
        raise ReadOnlyDbMissingError(
            f"database file {path} does not exist; read-only access never creates it"
        )
    # Path.as_uri() requires an absolute path and percent-encodes every
    # URI-special character (spaces, '?', '#', unicode) -- including Windows
    # drive letters -- so arbitrary filenames cannot smuggle URI parameters.
    uri = path.resolve().as_uri() + "?mode=ro"
    if _is_wal_header(path):
        uri += "&immutable=1"
    connection = sqlite3.connect(
        uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
        connection.execute("PRAGMA query_only = ON")
    except Exception:
        connection.close()
        raise
    connection.row_factory = sqlite3.Row
    return connection
