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
- installs a ``set_authorizer`` callback that DENIES ``ATTACH`` / ``DETACH``
  and every schema/data-writing action at STATEMENT-PREPARE time (PR #100
  re-review Major 2: ``mode=ro`` + ``query_only`` alone still let
  ``ATTACH DATABASE 'new.sqlite3'`` create a zero-byte file -- the authorizer
  rejects the statement BEFORE SQLite ever opens the attach target). Value-
  setting forms of state-changing pragmas (``user_version``,
  ``journal_mode``, ``writable_schema``, ...) are denied too; read-only
  pragmas (``table_info`` etc.) that the schema verification needs stay
  allowed,
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


# Actions a read-only connection must never perform. ATTACH/DETACH are the
# critical ones (they touch OTHER files); the data/schema writers are listed
# as defense in depth on top of mode=ro + query_only.
_DENIED_ACTIONS: frozenset[int] = frozenset(
    action
    for action in (
        getattr(sqlite3, name, None)
        for name in (
            "SQLITE_ATTACH",
            "SQLITE_DETACH",
            "SQLITE_INSERT",
            "SQLITE_UPDATE",
            "SQLITE_DELETE",
            "SQLITE_CREATE_INDEX",
            "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX",
            "SQLITE_CREATE_TEMP_TABLE",
            "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW",
            "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_VIEW",
            "SQLITE_CREATE_VTABLE",
            "SQLITE_DROP_INDEX",
            "SQLITE_DROP_TABLE",
            "SQLITE_DROP_TEMP_INDEX",
            "SQLITE_DROP_TEMP_TABLE",
            "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW",
            "SQLITE_DROP_TRIGGER",
            "SQLITE_DROP_VIEW",
            "SQLITE_DROP_VTABLE",
            "SQLITE_ALTER_TABLE",
            "SQLITE_REINDEX",
        )
    )
    if action is not None
)

# State-changing pragmas: the VALUE-SETTING form (an argument is present) is
# denied; the read form stays allowed. Read-only pragmas with arguments that
# the schema verification needs (table_info(...), index_info(...), ...) are
# not in this set and remain usable.
_DENIED_PRAGMA_SETS: frozenset[str] = frozenset(
    {
        "user_version",
        "schema_version",
        "application_id",
        "journal_mode",
        "writable_schema",
        "wal_checkpoint",
        "auto_vacuum",
        "incremental_vacuum",
        "page_size",
        "encoding",
        "query_only",
        "locking_mode",
        "synchronous",
        "cache_size",
        "mmap_size",
    }
)


def _read_only_authorizer(action, arg1, arg2, db_name, trigger_or_view):
    if action in _DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in _DENIED_PRAGMA_SETS and arg2 is not None:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _is_wal_header(path: Path) -> bool:
    """True when the SQLite header declares WAL journaling (offsets 18/19 ==
    2). Reading the header bytes touches nothing."""
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return len(header) >= 20 and header[:16] == b"SQLite format 3\x00" and header[18] == 2


def connect_read_only(
    db_path: Path | str, *, allow_immutable_wal: bool = True
) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only (see the module docstring).

    ``allow_immutable_wal`` (default True, the long-standing behaviour) opts
    a WAL-declaring file into the ``immutable=1`` path: no side files, no
    mtime change, at the cost of a point-in-time view that IGNORES anything
    still in ``-wal``. A caller whose correctness depends on never reading a
    stale committed value must pass False (PR #126 Codex review P1: the kill
    switch would otherwise be able to answer "disabled" from a snapshot that
    predates a committed enable). With False the file is opened as a plain
    ``mode=ro`` connection, which reads the WAL content -- and the caller can
    then observe the real journal mode INSIDE its own read transaction
    (``PRAGMA journal_mode``), with no separate header probe to race.

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
    if allow_immutable_wal and _is_wal_header(path):
        uri += "&immutable=1"
    connection = sqlite3.connect(
        uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
        connection.execute("PRAGMA query_only = ON")
        # Installed AFTER the two setup pragmas above (which the authorizer
        # itself would deny): from here on, ATTACH/DETACH, every write action
        # and every state-changing pragma are rejected at prepare time.
        connection.set_authorizer(_read_only_authorizer)
    except Exception:
        connection.close()
        raise
    connection.row_factory = sqlite3.Row
    return connection
