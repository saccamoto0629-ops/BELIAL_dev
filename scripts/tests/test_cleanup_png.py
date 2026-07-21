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


def test_dry_run_lists_but_does_not_delete(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    # Paths are repr-escaped in the log: a newline in a file name must not be
    # able to fabricate extra lines (e.g. a fake SUMMARY) in the journal.
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert all(line.startswith(("TARGET ", "WOULD_DELETE ")) for line in lines)
    assert any("\\n" in line for line in lines if line.startswith("WOULD_DELETE "))


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


@pytest.mark.parametrize(
    "target",
    ["", "/", "/tmp", "/opt", "relative/path", "/opt/berial/market-watch/data/../data/chart_images"],
)
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


def _patch_flaky_unlink(monkeypatch: pytest.MonkeyPatch, stuck_name: str) -> None:
    """Make os.unlink fail for one file name (deletion goes through
    os.unlink(name, dir_fd=...), so PermissionError is simulated there)."""
    real_unlink = os.unlink

    def flaky_unlink(path: object, *args: object, **kwargs: object) -> None:
        if os.path.basename(str(path)) == stuck_name:
            raise PermissionError(13, "unlink not permitted", str(path))
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", flaky_unlink)


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

    _patch_flaky_unlink(monkeypatch, "stuck.png")
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

    _patch_flaky_unlink(monkeypatch, "stuck.png")
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


@pytest.mark.parametrize("days", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_invalid_retention_days_is_rejected_before_scanning(tmp_path: Path, days: float) -> None:
    """NaN would poison the cutoff (every mtime compares False against NaN,
    deleting ALL files), so non-finite and negative values must raise."""
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    png = target / "keep.png"
    _write(png, b"keep", 1.0)

    with pytest.raises(ValueError):
        cleanup_png.cleanup_targets(
            [str(target)], retention_days=days, dry_run=False, now_epoch=200_000.0, repo_root=tmp_path / "repo"
        )
    assert png.exists()


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-1"])
def test_main_rejects_invalid_retention_cli_before_locking(tmp_path: Path, raw: str) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    png = target / "keep.png"
    _write(png, b"keep", 1.0)
    lock = tmp_path / "cleanup.lock"

    # "=" form: argparse would otherwise parse "-inf" as an option name.
    exit_code = cleanup_png.main([f"--target={target}", f"--retention-days={raw}", f"--lock-file={lock}"])

    assert exit_code == 2
    assert png.exists()
    assert not lock.exists()


@pytest.mark.parametrize("raw", ["nan", "inf", "-1", "not-a-number"])
def test_main_rejects_invalid_retention_env_before_locking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    png = target / "keep.png"
    _write(png, b"keep", 1.0)
    lock = tmp_path / "cleanup.lock"
    monkeypatch.setenv(cleanup_png.ENV_RETENTION_DAYS, raw)

    exit_code = cleanup_png.main(["--target", str(target), "--lock-file", str(lock)])

    assert exit_code == 2
    assert png.exists()
    assert not lock.exists()


def test_symlinked_target_directory_is_rejected(tmp_path: Path) -> None:
    """A target that IS a symlink must never be scanned through (Issue #67):
    validate_target rejects it and no outside file is deleted."""
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.png"
    _write(victim, b"outside", 1.0)
    config = tmp_path / "config" / "deep"
    config.mkdir(parents=True)
    link = config / "chart_images"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError):
        cleanup_png.validate_target(str(link), repo_root=tmp_path / "repo")

    result = cleanup_png.cleanup_targets(
        [str(link)], retention_days=1, dry_run=False, now_epoch=200_000.0, repo_root=tmp_path / "repo"
    )

    assert result.error_count == 1
    assert result.deleted_count == 0
    assert victim.exists()


def test_symlinked_parent_component_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real" / "deep" / "chart_images"
    real.mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(tmp_path / "real")
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError):
        cleanup_png.validate_target(str(alias / "deep" / "chart_images"), repo_root=tmp_path / "repo")


def test_file_replaced_after_enumeration_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the name points at a DIFFERENT inode than the enumerated one by
    deletion time, the replacement must survive -- even with an old mtime,
    where the mtime re-check alone would not save it."""
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    original = target / "swap.png"
    _write(original, b"original", 1.0)

    real_iter = cleanup_png.iter_candidates

    def swapping_iter(target_path: Path, dir_fd: int, *, cutoff_epoch: float):
        for name, enum_stat in real_iter(target_path, dir_fd, cutoff_epoch=cutoff_epoch):
            replaced = target_path / name
            replaced.unlink()
            _write(replaced, b"replacement", 1.0)
            yield name, enum_stat

    monkeypatch.setattr(cleanup_png, "iter_candidates", swapping_iter)
    result = cleanup_png.cleanup_targets(
        [str(target)], retention_days=1, dry_run=False, now_epoch=200_000.0, repo_root=tmp_path / "repo"
    )

    assert result.candidate_count == 1
    assert result.deleted_count == 0
    assert result.error_count == 0
    assert original.read_bytes() == b"replacement"


def test_main_missing_target_warns_and_exits_zero(tmp_path: Path) -> None:
    """The lock does not depend on the target existing (it lives in /tmp by
    default), so a missing target stays a WARNING with exit 0 (Issue #67)."""
    missing = tmp_path / "deep" / "chart_images"
    lock = tmp_path / "cleanup.lock"

    exit_code = cleanup_png.main(_main_args(missing, lock))

    assert exit_code == 0
    assert not missing.exists()


def test_main_symlinked_lock_file_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "chart_images"
    target.mkdir(parents=True)
    png = target / "old.png"
    _write(png, b"png", 1.0)
    real_lock = tmp_path / "real.lock"
    real_lock.touch()
    lock = tmp_path / "cleanup.lock"
    try:
        lock.symlink_to(real_lock)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    exit_code = cleanup_png.main(_main_args(target, lock))

    assert exit_code == 1
    assert png.exists()
