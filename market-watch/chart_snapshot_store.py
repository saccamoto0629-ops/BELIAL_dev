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
    ``alert_chart_snapshots`` equivalent), idempotently.

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

# Same shape and responsibility as alert_chart_snapshots (chart_collector.init_db),
# except paper_signal_id carries no SQLite FK: Paper Signals live in another DB.
PAPER_SIGNAL_LINKS_DDL = """
CREATE TABLE IF NOT EXISTS paper_signal_chart_snapshots (
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
        ``snapshot_at``) wins; a final tie falls back to the row ``id`` so the
        choice is fully deterministic.
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
        f"SELECT {', '.join(_SNAPSHOT_COLUMNS)} FROM chart_snapshots "
        "WHERE symbol = ? AND timeframe = ?"
    )
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            rows = connection.execute(query, (symbol.upper(), timeframe)).fetchall()
    except sqlite3.Error:
        return None

    floor_captured = pd.Timestamp.min.tz_localize(UTC)
    best_record: dict | None = None
    best_key = None
    for row in rows:
        record = dict(zip(_SNAPSHOT_COLUMNS, row))
        candle_time = _parse_utc_or_none(record["candle_time"])
        if candle_time is None or candle_time > target:
            continue
        captured = (
            _parse_utc_or_none(record["captured_at"])
            or _parse_utc_or_none(record["snapshot_at"])
            or floor_captured
        )
        key = (candle_time, captured, record["id"] or "")
        if best_key is None or key > best_key:
            best_key, best_record = key, record
    return best_record


def _utc_now_iso_z() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def init_paper_signal_links(db_path: Path) -> None:
    """Idempotently create the ``paper_signal_chart_snapshots`` link table.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so running
    this against a Chart DB of any age (or repeatedly) is safe. It is only ever
    called on the write path (:func:`link_paper_signal_chart_snapshot`), so a
    read-only consumer -- e.g. a Paper Signal ``--dry-run`` -- never triggers
    this migration.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(PAPER_SIGNAL_LINKS_DDL)


def link_paper_signal_chart_snapshot(
    db_path: Path,
    *,
    paper_signal_id: str,
    chart_snapshot_id: str,
) -> bool:
    """Persist one Paper Signal <-> Chart Snapshot relation, idempotently.

    ``INSERT OR IGNORE`` on the (paper_signal_id, chart_snapshot_id) primary key
    makes a repeat link a no-op, mirroring ``link_alert_chart_snapshot``.
    Returns True when a new relation row was inserted, False on a duplicate.
    """
    init_paper_signal_links(db_path)
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO paper_signal_chart_snapshots
            (paper_signal_id, chart_snapshot_id, created_at)
            VALUES (?, ?, ?)
            """,
            (paper_signal_id, chart_snapshot_id, _utc_now_iso_z()),
        )
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
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {', '.join(_LINK_COLUMNS)} FROM paper_signal_chart_snapshots
                WHERE paper_signal_id = ?
                ORDER BY chart_snapshot_id
                """,
                (paper_signal_id,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(zip(_LINK_COLUMNS, row)) for row in rows]
