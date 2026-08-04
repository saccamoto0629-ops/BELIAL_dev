"""Issue #78 / PR #100: connect_read_only is a TRUE read-only connection --
never creates files/directories/journals, survives read-only filesystems and
WAL databases, and refuses every write."""

import os
import sqlite3
import stat

import pytest

from execution.sqlite_ro import ReadOnlyDbMissingError, connect_read_only


def make_db(path, *, wal=False):
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    connection.execute("INSERT INTO t (v) VALUES ('x')")
    connection.commit()
    connection.close()


def snapshot(directory):
    return {
        entry.name: (entry.stat().st_size, entry.stat().st_mtime_ns)
        for entry in directory.iterdir()
    }


def test_missing_path_raises_and_creates_nothing(tmp_path):
    target = tmp_path / "missing.sqlite3"
    with pytest.raises(ReadOnlyDbMissingError):
        connect_read_only(target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_missing_parent_directory_is_never_created(tmp_path):
    target = tmp_path / "nowhere" / "missing.sqlite3"
    with pytest.raises(ReadOnlyDbMissingError):
        connect_read_only(target)
    assert not target.parent.exists()


def test_existing_db_reads_without_touching_bytes_mtime_or_side_files(tmp_path):
    target = tmp_path / "db.sqlite3"
    make_db(target)
    before = snapshot(tmp_path)
    connection = connect_read_only(target)
    assert connection.execute("SELECT v FROM t").fetchone()["v"] == "x"
    connection.close()
    assert snapshot(tmp_path) == before
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["db.sqlite3"]


def test_wal_db_reads_without_creating_wal_or_shm(tmp_path):
    target = tmp_path / "wal.sqlite3"
    make_db(target, wal=True)
    # a cleanly closed WAL DB leaves no -wal/-shm; a read must not create them
    for side in (tmp_path / "wal.sqlite3-wal", tmp_path / "wal.sqlite3-shm"):
        if side.exists():
            side.unlink()
    before = snapshot(tmp_path)
    connection = connect_read_only(target)
    assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    connection.close()
    assert snapshot(tmp_path) == before
    assert not (tmp_path / "wal.sqlite3-wal").exists()
    assert not (tmp_path / "wal.sqlite3-shm").exists()


def test_read_only_file_and_directory_are_readable(tmp_path):
    target = tmp_path / "db.sqlite3"
    make_db(target)
    os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        connection = connect_read_only(target)
        assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
        connection.close()
    finally:
        os.chmod(tmp_path, stat.S_IRWXU)
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)


def test_every_write_attempt_fails(tmp_path):
    target = tmp_path / "db.sqlite3"
    make_db(target)
    connection = connect_read_only(target)
    for sql in (
        "INSERT INTO t (v) VALUES ('y')",
        "UPDATE t SET v = 'z'",
        "DELETE FROM t",
        "CREATE TABLE u (id INTEGER)",
        "PRAGMA user_version = 9",
    ):
        # the authorizer denies at prepare time (DatabaseError "not
        # authorized"); anything it let through would still die on the
        # mode=ro/query_only layer (OperationalError)
        with pytest.raises(sqlite3.Error):
            connection.execute(sql)
    connection.close()
    check = sqlite3.connect(target)
    assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    assert check.execute("PRAGMA user_version").fetchone()[0] == 0
    check.close()


# --- ATTACH / state-change denial (PR #100 re-review Major 2) ------------------


def test_attach_is_denied_and_creates_no_file(tmp_path):
    """mode=ro + query_only alone would let ATTACH create a ZERO-BYTE file;
    the authorizer must reject the statement before SQLite opens the attach
    target -- for missing paths, existing paths, and URI-hostile names."""
    target = tmp_path / "db.sqlite3"
    make_db(target)
    existing = tmp_path / "existing.sqlite3"
    make_db(existing)
    before = snapshot(tmp_path)
    connection = connect_read_only(target)
    attach_targets = [
        tmp_path / "brand-new.sqlite3",
        existing,
        tmp_path / "日本語 スペース.sqlite3",
        tmp_path / "we ird#q?.sqlite3",  # '?'/'#' stay inside the SQL string
        "C:/shadow-attach-test/windows-style.sqlite3",
    ]
    for attach_target in attach_targets:
        with pytest.raises(sqlite3.Error):
            connection.execute(f"ATTACH DATABASE '{attach_target}' AS attached")
    with pytest.raises(sqlite3.Error):
        connection.execute("DETACH DATABASE attached")
    connection.close()
    assert snapshot(tmp_path) == before
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "db.sqlite3", "existing.sqlite3",
    ]


def test_state_changing_statements_leave_the_filesystem_invariant(tmp_path):
    target = tmp_path / "db.sqlite3"
    make_db(target)
    before = snapshot(tmp_path)
    before_bytes = target.read_bytes()
    connection = connect_read_only(target)
    for sql in (
        "VACUUM",
        "PRAGMA journal_mode = WAL",
        "PRAGMA journal_mode = DELETE",
        "PRAGMA user_version = 9",
        "PRAGMA writable_schema = ON",
        "INSERT INTO t (v) VALUES ('y')",
        "UPDATE t SET v = 'z'",
        "DELETE FROM t",
        "CREATE TABLE u (id INTEGER)",
    ):
        with pytest.raises(sqlite3.Error):
            connection.execute(sql)
    # reads (including argument-taking read pragmas) still work afterwards
    assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    assert list(connection.execute("PRAGMA table_info(t)"))
    connection.close()
    assert snapshot(tmp_path) == before
    assert target.read_bytes() == before_bytes
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["db.sqlite3"]


def test_corrupted_file_is_not_silently_successful(tmp_path):
    target = tmp_path / "garbage.sqlite3"
    target.write_bytes(b"this is not a sqlite database at all")
    before = target.read_bytes()
    connection = connect_read_only(target)
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("SELECT * FROM sqlite_master").fetchall()
    connection.close()
    assert target.read_bytes() == before


def test_special_characters_in_the_path_are_handled(tmp_path):
    # Space, '#' and '%' are URI-special but legal filename characters on
    # every supported platform ('?' is NOT valid on Windows, so it cannot be
    # part of this fixture); '%' additionally proves the URI encoding escapes
    # a literal percent instead of misreading it as an escape sequence.
    weird = tmp_path / "we ird#db%.sqlite3"
    make_db(weird)
    connection = connect_read_only(weird)
    assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    connection.close()
