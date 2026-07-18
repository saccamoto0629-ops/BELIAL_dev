"""Read and link stored Chart Snapshots for other Berial bots.

Issue #55: Paper Signal notifications reuse the EXISTING ``chart_snapshots``
records (rendered and uploaded to S3 by ``chart_snapshot.py`` /
``chart_alert_context.py``) -- it never draws or uploads anything of its own.
This module is the access layer for that reuse:

  * :func:`find_snapshot_for_candle` -- select the stored snapshot matching a
    Paper Signal's ``symbol`` / ``timeframe`` / ``target_candle_utc``.
    Strictly read-only: it never creates, migrates, or writes the Chart DB.
  * :func:`link_paper_signal_chart_snapshot` -- persist the Paper Signal <->
    Chart Snapshot relation in ``paper_signal_chart_snapshots`` (the
    ``alert_chart_snapshots`` equivalent), idempotently, with the foreign key
    to ``chart_snapshots`` actually enforced.

The link table's schema is owned by the Chart DB's regular schema management:
``chart_collector.init_db()`` calls :func:`ensure_paper_signal_links_schema`
alongside the rest of the chart DDL, so the table exists and is migrated the
same way every other chart table is. The link write path only *repairs* the
schema on an already-initialized Chart DB -- it refuses to conjure a link
table inside a DB that is not a Chart DB at all.

``paper_signal_id`` is a logical string reference only: Paper Signal rows live
in a DIFFERENT SQLite file (``paper-signal/data/paper_signals.sqlite3``), so no
SQLite foreign key can point at them from the Chart DB.

Like ``freshness``, this module stays dependency-light (sqlite3 + pandas via
``freshness``; no yfinance / matplotlib / boto3), so Paper Signal can use it
without pulling in Live's rendering or upload code.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from freshness import parse_timestamp_utc

LINKS_TABLE = "paper_signal_chart_snapshots"

# Same shape and responsibility as alert_chart_snapshots (chart_collector.init_db),
# except paper_signal_id carries no SQLite FK: Paper Signals live in another DB.
# Kept as a template so the schema rebuild can create a staging table with the
# byte-identical canonical DDL.
_LINKS_DDL_TEMPLATE = """
CREATE TABLE IF NOT EXISTS {table} (
    paper_signal_id TEXT NOT NULL,
    chart_snapshot_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (paper_signal_id, chart_snapshot_id),
    FOREIGN KEY (chart_snapshot_id) REFERENCES chart_snapshots(id)
)
"""
PAPER_SIGNAL_LINKS_DDL = _LINKS_DDL_TEMPLATE.format(table=LINKS_TABLE)

_SNAPSHOT_COLUMNS = (
    "id",
    "symbol",
    "timeframe",
    "snapshot_at",
    "s3_bucket",
    "s3_key",
    "captured_at",
    "candle_time",
    "image_s3_key",
)

# A chart_snapshots table cannot be searched without these; the others are
# nullable extras a legacy schema may predate (selected as NULL when absent).
_REQUIRED_SNAPSHOT_COLUMNS = ("id", "symbol", "timeframe", "candle_time", "s3_key")

_LINK_COLUMNS = ("paper_signal_id", "chart_snapshot_id", "created_at")

# A locked Chart DB must degrade the snapshot attachment quickly (text-only
# notification) instead of stalling each symbol on SQLite's 5s default.
LOCK_TIMEOUT_SECONDS = 2.0

_LOGGER = logging.getLogger("chart_snapshot_store")

_NO_CLEANUP = {"orphans_removed": 0, "created_at_backfilled": 0}


class ChartLinkError(RuntimeError):
    """A Paper Signal <-> Chart Snapshot relation could not be persisted.

    Raised when the Chart DB file is missing, when the DB is not an initialized
    Chart DB (no ``chart_snapshots`` table -- the link table alone must never be
    created in such a DB), or when the referenced snapshot row does not exist
    (an orphan relation must never be reported as saved). Callers treat this as
    an image-side failure and degrade to a text-only notification.
    """


class ChartSchemaError(RuntimeError):
    """The ``chart_snapshots`` table exists but lacks a required column.

    Deliberately distinct from "no snapshot matched" (None): a schema this
    module cannot search must surface as an explicit error, never be silently
    conflated with an empty result. Callers may still degrade to a text-only
    notification, but the log then names the real problem.
    """


def _read_only_uri(db_path: Path) -> str:
    """SQLite read-only URI for *db_path*, percent-encoding reserved characters.

    ``Path.as_uri()`` encodes ``#``, ``?``, spaces, and non-ASCII, so a path
    containing URI-reserved characters opens correctly (on both POSIX and
    Windows) instead of silently pointing at a truncated filename.
    """
    return f"{db_path.resolve().as_uri()}?mode=ro"


def _parse_utc_or_none(value) -> pd.Timestamp | None:
    """Parse a stored timestamp to a UTC-aware Timestamp, or None if unusable.

    Chart Snapshot rows carry timestamps in mixed forms (``...Z`` from
    ``to_utc_iso_z``, ``+00:00`` isoformat from ``persist_snapshot_images``), so
    comparisons must go through UTC parsing, never raw string ordering. NULL,
    empty, unparseable, and NaT values all come back as None so a malformed row
    can never win a comparison by accident.
    """
    if value in (None, ""):
        return None
    try:
        parsed = parse_timestamp_utc(value)
    except (ValueError, TypeError):
        return None
    if pd.isna(parsed):
        return None
    return parsed


def find_snapshot_for_candle(
    db_path: Path,
    *,
    symbol: str,
    timeframe: str,
    target_candle_utc: str,
) -> dict | None:
    """Return the stored Chart Snapshot for one Paper Signal target candle, or None.

    Selection rules (Issue #55):
      * ``symbol`` (uppercased, matching the stored form) and ``timeframe``
        must match exactly.
      * Only snapshots with ``candle_time <= target_candle_utc`` qualify --
        a snapshot from a future candle is never selected.
      * Among qualifying rows the newest ``candle_time`` wins.
      * On a ``candle_time`` tie, the newest ``captured_at`` (falling back to
        ``snapshot_at``) wins; remaining ties fall back to the row ``id``
        (compared as text, so mixed-type ids from a legacy schema still order)
        and finally the SQLite ``rowid``, making the choice fully deterministic.
      * Rows with NULL or malformed ``candle_time`` are never selected.

    Strictly read-only: opens the DB in SQLite ``mode=ro`` and returns None
    (rather than creating anything) when the DB file, table, or any qualifying
    row is missing. Optional columns a legacy schema predates (``s3_bucket``,
    ``image_s3_key``, ``captured_at``, ``snapshot_at``) are selected as NULL so
    such a DB stays searchable; a table missing a REQUIRED column raises
    :class:`ChartSchemaError` instead of masquerading as "no snapshot".
    """
    target = _parse_utc_or_none(target_candle_utc)
    db_path = Path(db_path)
    if target is None or not db_path.exists():
        return None

    try:
        with sqlite3.connect(_read_only_uri(db_path), uri=True, timeout=LOCK_TIMEOUT_SECONDS) as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chart_snapshots'"
            ).fetchone()
            if table_exists is None:
                return None
            available = {row[1] for row in connection.execute("PRAGMA table_info(chart_snapshots)")}
            missing = [column for column in _REQUIRED_SNAPSHOT_COLUMNS if column not in available]
            if missing:
                raise ChartSchemaError(
                    f"chart_snapshots is missing required column(s): {', '.join(missing)}"
                )
            select_list = ", ".join(
                column if column in available else f"NULL AS {column}"
                for column in _SNAPSHOT_COLUMNS
            )
            rows = connection.execute(
                f"SELECT rowid, {select_list} FROM chart_snapshots WHERE symbol = ? AND timeframe = ?",
                (symbol.upper(), timeframe),
            ).fetchall()
    except sqlite3.Error:
        return None

    floor_captured = pd.Timestamp.min.tz_localize(UTC)
    best_record: dict | None = None
    best_key = None
    for row in rows:
        rowid = row[0]
        record = dict(zip(_SNAPSHOT_COLUMNS, row[1:]))
        candle_time = _parse_utc_or_none(record["candle_time"])
        if candle_time is None or candle_time > target:
            continue
        captured = (
            _parse_utc_or_none(record["captured_at"])
            or _parse_utc_or_none(record["snapshot_at"])
            or floor_captured
        )
        key = (candle_time, captured, str(record["id"] or ""), rowid)
        if best_key is None or key > best_key:
            best_key, best_record = key, record
    return best_record


def _utc_now_iso_z() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _links_schema_is_canonical(connection: sqlite3.Connection) -> bool:
    """True when the existing link table matches the canonical DDL's constraints.

    Checks the exact column set with TEXT affinity and NOT NULL, the composite
    ``(paper_signal_id, chart_snapshot_id)`` PRIMARY KEY (the duplicate
    suppressor), and the single FK ``chart_snapshot_id -> chart_snapshots(id)``.
    Anything else -- however close -- is non-canonical and must be rebuilt,
    because SQLite cannot add constraints to an existing table via ALTER.
    """
    info = connection.execute(f"PRAGMA table_info({LINKS_TABLE})").fetchall()
    columns = {row[1]: row for row in info}  # (cid, name, type, notnull, dflt, pk)
    if set(columns) != set(_LINK_COLUMNS):
        return False
    expected_pk = {"paper_signal_id": 1, "chart_snapshot_id": 2, "created_at": 0}
    for name in _LINK_COLUMNS:
        _, _, col_type, notnull, _, pk = columns[name]
        if col_type.upper() != "TEXT" or not notnull or pk != expected_pk[name]:
            return False
    fks = connection.execute(f"PRAGMA foreign_key_list({LINKS_TABLE})").fetchall()
    if len(fks) != 1:
        return False
    _, _, ref_table, from_col, to_col, *_ = fks[0]
    return ref_table == "chart_snapshots" and from_col == "chart_snapshot_id" and to_col in ("id", None)


_REBUILD_TABLE = f"{LINKS_TABLE}_rebuild"


def _rebuild_links_table(connection: sqlite3.Connection) -> None:
    """Replace a non-canonical link table with a canonical one, keeping the data.

    Copies every relation whose parent snapshot exists (the JOIN drops orphans a
    constraintless schema may have accumulated), deduplicates through the new
    primary key via INSERT OR IGNORE, drops NULL-keyed rows, backfills a missing
    or empty ``created_at``, then swaps the tables. Runs inside the caller's
    transaction, so any failure rolls back to the untouched original table.
    """
    connection.execute(f"DROP TABLE IF EXISTS {_REBUILD_TABLE}")
    connection.execute(_LINKS_DDL_TEMPLATE.format(table=_REBUILD_TABLE))
    old_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({LINKS_TABLE})")}
    if {"paper_signal_id", "chart_snapshot_id"} <= old_columns:
        created_source = (
            "COALESCE(NULLIF(old.created_at, ''), ?)" if "created_at" in old_columns else "?"
        )
        connection.execute(
            f"""
            INSERT OR IGNORE INTO {_REBUILD_TABLE} (paper_signal_id, chart_snapshot_id, created_at)
            SELECT old.paper_signal_id, old.chart_snapshot_id, {created_source}
            FROM {LINKS_TABLE} AS old
            JOIN chart_snapshots ON chart_snapshots.id = old.chart_snapshot_id
            WHERE old.paper_signal_id IS NOT NULL AND old.chart_snapshot_id IS NOT NULL
            """,
            (_utc_now_iso_z(),),
        )
    connection.execute(f"DROP TABLE {LINKS_TABLE}")
    connection.execute(f"ALTER TABLE {_REBUILD_TABLE} RENAME TO {LINKS_TABLE}")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _begin_if_needed(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")


def _cleanup_canonical_links_data(connection: sqlite3.Connection) -> dict:
    """Repair data inside an already-canonical link table, idempotently.

    The canonical FK is only enforced on connections that switch it on, so a
    historic writer (foreign_keys defaults to OFF) may have left orphaned
    relations, and blank ``created_at`` values may predate the rebuild
    backfill. Orphans are deleted, blank/NULL ``created_at`` is backfilled with
    the current UTC time, and a final ``PRAGMA foreign_key_check`` must come
    back empty -- anything still violating raises :class:`ChartSchemaError` so
    a half-broken table is never reported as normalized. All writes share the
    caller's transaction. Returns the cleanup counts (all zero when the data
    was already consistent, making the fast path read-only).
    """
    orphan_rows = connection.execute(f"PRAGMA foreign_key_check({LINKS_TABLE})").fetchall()
    blank_created = connection.execute(
        f"SELECT COUNT(*) FROM {LINKS_TABLE} WHERE created_at IS NULL OR TRIM(created_at) = ''"
    ).fetchone()[0]
    if not orphan_rows and not blank_created:
        return dict(_NO_CLEANUP)

    _begin_if_needed(connection)
    orphans_removed = 0
    if orphan_rows:
        orphans_removed = connection.execute(
            f"""
            DELETE FROM {LINKS_TABLE}
            WHERE NOT EXISTS (
                SELECT 1 FROM chart_snapshots
                WHERE chart_snapshots.id = {LINKS_TABLE}.chart_snapshot_id
            )
            """
        ).rowcount
    backfilled = 0
    if blank_created:
        backfilled = connection.execute(
            f"UPDATE {LINKS_TABLE} SET created_at = ? WHERE created_at IS NULL OR TRIM(created_at) = ''",
            (_utc_now_iso_z(),),
        ).rowcount
    remaining = connection.execute(f"PRAGMA foreign_key_check({LINKS_TABLE})").fetchall()
    if remaining:
        raise ChartSchemaError(
            f"{len(remaining)} orphaned relation(s) remain in {LINKS_TABLE} after cleanup"
        )
    counts = {"orphans_removed": orphans_removed, "created_at_backfilled": backfilled}
    _LOGGER.info(
        "%s cleanup: removed %d orphaned relation(s), backfilled %d created_at value(s)",
        LINKS_TABLE, orphans_removed, backfilled,
    )
    return counts


def ensure_paper_signal_links_schema(connection: sqlite3.Connection) -> dict:
    """Create -- or rebuild to -- the canonical link table, idempotently.

    Operates on an open Chart DB connection so ``chart_collector.init_db()``
    runs this alongside the rest of the chart schema. A missing table is
    created from the canonical DDL; any other same-name legacy table is rebuilt
    via :func:`_rebuild_links_table`, because the canonical PRIMARY KEY /
    NOT NULL / FOREIGN KEY constraints cannot be added by ALTER. A table
    already matching the canonical schema still gets its DATA verified
    (:func:`_cleanup_canonical_links_data`): orphans left by historic
    FK-disabled writers are deleted and blank ``created_at`` backfilled. A
    stale ``..._rebuild`` staging table (an internal-only name an interrupted
    historic migration may have left) is dropped first -- but only when it
    actually exists, so a healthy DB sees no DROP at all.

    Every mutating step shares one transaction (started here unless the caller
    already holds one, committed by the caller's ``with connection`` block), so
    the original table and data survive any mid-migration failure.

    Returns the canonical-path cleanup counts
    (``{"orphans_removed": n, "created_at_backfilled": m}``; zeros on the
    create/rebuild paths and on already-consistent data).
    """
    if _table_exists(connection, _REBUILD_TABLE):
        _begin_if_needed(connection)
        connection.execute(f"DROP TABLE {_REBUILD_TABLE}")
    if not _table_exists(connection, LINKS_TABLE):
        connection.execute(PAPER_SIGNAL_LINKS_DDL)
        return dict(_NO_CLEANUP)
    if _links_schema_is_canonical(connection):
        return _cleanup_canonical_links_data(connection)
    _begin_if_needed(connection)
    _rebuild_links_table(connection)
    return dict(_NO_CLEANUP)


def link_paper_signal_chart_snapshot(
    db_path: Path,
    *,
    paper_signal_id: str,
    chart_snapshot_id: str,
) -> bool:
    """Persist one Paper Signal <-> Chart Snapshot relation, idempotently.

    Runs with ``PRAGMA foreign_keys = ON`` so the declared FK to
    ``chart_snapshots(id)`` is actually enforced, and verifies the snapshot row
    in the same transaction as the insert. Refuses (raising
    :class:`ChartLinkError`) to write into a missing DB file, a DB without a
    ``chart_snapshots`` table, or against a nonexistent snapshot id -- an
    orphan relation is never reported as saved, and a non-Chart DB never gains
    a stray link table.

    Returns True when a new relation row was inserted, False on a duplicate
    (the canonical composite primary key, guaranteed by
    :func:`ensure_paper_signal_links_schema`, makes INSERT OR IGNORE atomic
    even under concurrent writers).
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise ChartLinkError(f"Chart DB file not found: {db_path}")
    with sqlite3.connect(db_path, timeout=LOCK_TIMEOUT_SECONDS) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        has_snapshots_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chart_snapshots'"
        ).fetchone()
        if has_snapshots_table is None:
            raise ChartLinkError(f"not an initialized Chart DB (no chart_snapshots table): {db_path}")
        ensure_paper_signal_links_schema(connection)
        snapshot_exists = connection.execute(
            "SELECT 1 FROM chart_snapshots WHERE id = ?", (chart_snapshot_id,)
        ).fetchone()
        if snapshot_exists is None:
            raise ChartLinkError(f"chart snapshot not found: {chart_snapshot_id}")
        try:
            cursor = connection.execute(
                f"""
                INSERT OR IGNORE INTO {LINKS_TABLE}
                (paper_signal_id, chart_snapshot_id, created_at)
                VALUES (?, ?, ?)
                """,
                (paper_signal_id, chart_snapshot_id, _utc_now_iso_z()),
            )
        except sqlite3.IntegrityError as exc:
            raise ChartLinkError(f"chart snapshot link rejected: {exc}") from exc
    return cursor.rowcount > 0


def list_paper_signal_links(db_path: Path, paper_signal_id: str) -> list[dict]:
    """Return the stored snapshot relations for one Paper Signal (read-only).

    A missing DB file or link table yields an empty list rather than creating
    anything, so dry runs and reporting can probe safely.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(_read_only_uri(db_path), uri=True, timeout=LOCK_TIMEOUT_SECONDS) as connection:
            rows = connection.execute(
                f"""
                SELECT {', '.join(_LINK_COLUMNS)} FROM {LINKS_TABLE}
                WHERE paper_signal_id = ?
                ORDER BY chart_snapshot_id
                """,
                (paper_signal_id,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(zip(_LINK_COLUMNS, row)) for row in rows]
