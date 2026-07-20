from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "cleanup_png.py"
SPEC = importlib.util.spec_from_file_location("cleanup_png", MODULE_PATH)
assert SPEC and SPEC.loader
cleanup_png = importlib.util.module_from_spec(SPEC)
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
