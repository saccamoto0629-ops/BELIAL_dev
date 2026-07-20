from __future__ import annotations

import fcntl
import importlib.util
import os
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "cleanup_png.py"
SPEC = importlib.util.spec_from_file_location("cleanup_png", MODULE_PATH)
assert SPEC and SPEC.loader
cleanup_png = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup_png
SPEC.loader.exec_module(cleanup_png)


def _write(path: Path, content: bytes, mtime: float) -> None:
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


def test_deletes_only_png_older_than_retention(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    now = 1_000_000.0
    old_png = target / "old.png"
    new_png = target / "new.png"
    old_text = target / "old.txt"
    _write(old_png, b"old-png", now - 8 * 86400)
    _write(new_png, b"new-png", now - 6 * 86400)
    _write(old_text, b"keep", now - 30 * 86400)

    result = cleanup_png.cleanup_targets(
        [str(target)], retention_days=7, dry_run=False, now_epoch=now, repo_root=tmp_path / "repo"
    )

    assert result.deleted_count == 1
    assert result.deleted_bytes == len(b"old-png")
    assert not old_png.exists()
    assert new_png.exists()
    assert old_text.exists()


def test_boundary_timestamp_is_retained(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    now = 2_000_000.0
    boundary = target / "boundary.png"
    _write(boundary, b"boundary", now - 7 * 86400)

    result = cleanup_png.cleanup_targets(
        [str(target)], retention_days=7, dry_run=False, now_epoch=now, repo_root=tmp_path / "repo"
    )

    assert result.candidate_count == 0
    assert boundary.exists()


def test_dry_run_lists_but_does_not_delete(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    old_png = target / "古い 画像\nname.png"
    _write(old_png, b"png", 1.0)

    result = cleanup_png.cleanup_targets(
        [str(target)], retention_days=1, dry_run=True, now_epoch=200_000.0, repo_root=tmp_path / "repo"
    )

    assert result.candidate_count == 1
    assert result.deleted_count == 0
    assert old_png.exists()


def test_symlink_is_never_followed(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "chart_images"
    outside = tmp_path / "outside"
    target.mkdir(parents=True)
    outside.mkdir()
    victim = outside / "victim.png"
    _write(victim, b"outside", 1.0)
    link = target / "linked.png"
    try:
        link.symlink_to(victim)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = cleanup_png.cleanup_targets(
        [str(target)], retention_days=1, dry_run=False, now_epoch=200_000.0, repo_root=tmp_path / "repo"
    )

    assert result.candidate_count == 0
    assert victim.exists()
    assert link.is_symlink()


def test_multiple_targets_and_missing_directory(tmp_path: Path) -> None:
    first = tmp_path / "one" / "chart_images"
    second = tmp_path / "two" / "chart_images"
    missing = tmp_path / "three" / "chart_images"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _write(first / "a.png", b"a", 1.0)
    _write(second / "b.png", b"bb", 1.0)

    result = cleanup_png.cleanup_targets(
        [str(first), str(second), str(missing)],
        retention_days=1,
        dry_run=False,
        now_epoch=200_000.0,
        repo_root=tmp_path / "repo",
    )

    assert result.target_count == 3
    assert result.deleted_count == 2
    assert result.deleted_bytes == 3
    assert result.warning_count == 1
    assert result.error_count == 0


@pytest.mark.parametrize("target", ["", "/", "/tmp", "/opt", "relative/path"])
def test_dangerous_targets_are_rejected(target: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        cleanup_png.validate_target(target, repo_root=tmp_path / "repo")


def test_repository_root_is_rejected(tmp_path: Path) -> None:
    repo_root = tmp_path / "deep" / "repo"
    repo_root.mkdir(parents=True)
    with pytest.raises(ValueError):
        cleanup_png.validate_target(str(repo_root), repo_root=repo_root)


def test_empty_directory_yields_no_candidates(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)

    result = cleanup_png.cleanup_targets(
        [str(target)], retention_days=1, dry_run=False, now_epoch=200_000.0, repo_root=tmp_path / "repo"
    )

    assert result.candidate_count == 0
    assert result.deleted_count == 0
    assert result.error_count == 0


def test_partial_delete_failure_continues_and_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing unlink must not stop the run: the rest is still deleted and
    the failure stays visible in error_count (Issue #67)."""
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    stuck = target / "stuck.png"
    other = target / "other.png"
    _write(stuck, b"stuck", 1.0)
    _write(other, b"other", 1.0)

    real_unlink = Path.unlink

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "stuck.png":
            raise PermissionError(13, "unlink not permitted", str(self))
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    result = cleanup_png.cleanup_targets(
        [str(target)], retention_days=1, dry_run=False, now_epoch=200_000.0, repo_root=tmp_path / "repo"
    )

    assert result.error_count == 1
    assert result.deleted_count == 1
    assert stuck.exists()
    assert not other.exists()


def _main_args(target: Path, lock: Path, *extra: str) -> list[str]:
    return ["--target", str(target), "--retention-days", "1", "--lock-file", str(lock), *extra]


def test_main_dry_run_creates_no_lock_file_or_directory(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    old_png = target / "old.png"
    _write(old_png, b"png", 1.0)
    lock = tmp_path / "locks" / "cleanup.lock"

    exit_code = cleanup_png.main(_main_args(target, lock, "--dry-run"))

    assert exit_code == 0
    assert old_png.exists()
    assert not lock.exists()
    assert not lock.parent.exists()


def test_main_deletes_and_exits_zero(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    old_png = target / "old.png"
    _write(old_png, b"png", 1.0)
    lock = target / ".cleanup.lock"

    exit_code = cleanup_png.main(_main_args(target, lock))

    assert exit_code == 0
    assert not old_png.exists()
    assert lock.exists()


def test_main_exits_nonzero_on_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    stuck = target / "stuck.png"
    other = target / "other.png"
    _write(stuck, b"stuck", 1.0)
    _write(other, b"other", 1.0)

    real_unlink = Path.unlink

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "stuck.png":
            raise PermissionError(13, "unlink not permitted", str(self))
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    exit_code = cleanup_png.main(_main_args(target, tmp_path / "deep" / "cleanup.lock"))

    assert exit_code == 1
    assert stuck.exists()
    assert not other.exists()


def test_main_with_lock_already_held_deletes_nothing_and_exits_zero(tmp_path: Path) -> None:
    """Concurrent runs must not race on the same files: the loser of the flock
    exits 0 without deleting anything (Issue #67 多重起動)."""
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    old_png = target / "old.png"
    _write(old_png, b"png", 1.0)
    lock = tmp_path / "deep" / "cleanup.lock"

    with lock.open("a+", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        exit_code = cleanup_png.main(_main_args(target, lock))

    assert exit_code == 0
    assert old_png.exists()


def test_main_does_not_create_missing_lock_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    old_png = target / "old.png"
    _write(old_png, b"png", 1.0)
    lock = tmp_path / "missing" / "cleanup.lock"

    exit_code = cleanup_png.main(_main_args(target, lock))

    assert exit_code == 1
    assert old_png.exists()
    assert not lock.parent.exists()


def test_main_without_targets_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cleanup_png.ENV_TARGETS, raising=False)
    assert cleanup_png.main([]) == 2
