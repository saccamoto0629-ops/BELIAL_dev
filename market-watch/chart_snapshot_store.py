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

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from freshness import parse_timestamp_utc

LINKS_TABLE = "paper_signal_chart_snapshots"

# Same shape and responsibility as alert_chart_snapshots (chart_collector.init_db),
# except paper_signal_id carries no SQLite FK: Paper Signals live in another DB.
PAPER_SIGNAL_LINKS_DDL = f"""
CREATE TABLE IF NOT EXISTS {LINKS_TABLE} (
    paper_signal_id TEXT NOT NULL,
    chart_snapshot_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (paper_signal_id, chart_snapshot_id),
    FOREIGN KEY (chart_snapshot_id) REFERENCES chart_snapshots(id)
)
"""

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

_LINK_COLUMNS = ("paper_signal_id", "chart_snapshot_id", "created_at")


class ChartLinkError(RuntimeError):
    """A Paper Signal <-> Chart Snapshot relation could not be persisted.

    Raised when the Chart DB file is missing, when the DB is not an initialized
    Chart DB (no ``chart_snapshots`` table -- the link table alone must never be
    created in such a DB), or when the referenced snapshot row does not exist
    (an orphan relation must never be reported as saved). Callers treat this as
    an image-side failure and degrade to a text-only notification.
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
    row is missing.
    """
    target = _parse_utc_or_none(target_candle_utc)
    db_path = Path(db_path)
    if target is None or not db_path.exists():
        return None

    query = (
        f"SELECT rowid, {', '.join(_SNAPSHOT_COLUMNS)} FROM chart_snapshots "
        "WHERE symbol = ? AND timeframe = ?"
    )
    try:
        with sqlite3.connect(_read_only_uri(db_path), uri=True) as connection:
            rows = connection.execute(query, (symbol.upper(), timeframe)).fetchall()
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


def ensure_paper_signal_links_schema(connection: sqlite3.Connection) -> None:
    """Create or repair the ``paper_signal_chart_snapshots`` table, idempotently.

    Operates on an open Chart DB connection so ``chart_collector.init_db()``
    runs this alongside the rest of the chart schema. ``CREATE TABLE IF NOT
    EXISTS`` never alters an existing table, so a same-name table left by a
    partial legacy schema additionally gets any missing column ALTERed in
    (mirroring ``chart_collector._ensure_column``) -- otherwise every relation
    insert would fail forever with an OperationalError.
    """
    connection.execute(PAPER_SIGNAL_LINKS_DDL)
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({LINKS_TABLE})")}
    for name in _LINK_COLUMNS:
        if name not in existing:
            connection.execute(f"ALTER TABLE {LINKS_TABLE} ADD COLUMN {name} TEXT")


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
    (checked explicitly, so idempotency holds even on a repaired legacy table
    that lost its primary key).
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise ChartLinkError(f"Chart DB file not found: {db_path}")
    with sqlite3.connect(db_path) as connection:
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
        already_linked = connection.execute(
            f"SELECT 1 FROM {LINKS_TABLE} WHERE paper_signal_id = ? AND chart_snapshot_id = ?",
            (paper_signal_id, chart_snapshot_id),
        ).fetchone()
        if already_linked is not None:
            return False
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
        with sqlite3.connect(_read_only_uri(db_path), uri=True) as connection:
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
