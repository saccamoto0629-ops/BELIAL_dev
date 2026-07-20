#!/usr/bin/env python3
"""Safely remove old PNG files from explicitly configured directories.

Only direct child regular files ending in ``.png`` are considered. Symlinks are
never followed. The command is intentionally fail-closed for dangerous target
paths and reports aggregate counts even when individual deletions fail.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_RETENTION_DAYS = 7.0
ENV_TARGETS = "PNG_CLEANUP_TARGETS"
ENV_RETENTION_DAYS = "PNG_CLEANUP_RETENTION_DAYS"
ENV_LOCK_FILE = "PNG_CLEANUP_LOCK_FILE"
DEFAULT_LOCK_FILE = "/tmp/berial-png-cleanup.lock"


@dataclass
class CleanupResult:
    target_count: int = 0
    candidate_count: int = 0
    candidate_bytes: int = 0
    deleted_count: int = 0
    deleted_bytes: int = 0
    error_count: int = 0
    warning_count: int = 0


def _split_env_targets(value: str) -> list[str]:
    return [item.strip() for item in value.split(os.pathsep) if item.strip()]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def validate_target(raw_target: str, *, repo_root: Path | None = None) -> Path:
    """Resolve and validate one cleanup directory without creating it."""
    if not raw_target or not raw_target.strip():
        raise ValueError("target path is empty")
    target = Path(raw_target).expanduser()
    if not target.is_absolute():
        raise ValueError(f"target must be an absolute path: {raw_target!r}")
    resolved = target.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValueError(f"refusing filesystem root target: {resolved}")
    effective_repo_root = (repo_root or _repo_root()).resolve(strict=False)
    if resolved == effective_repo_root:
        raise ValueError(f"refusing repository root target: {resolved}")
    # Reject broad paths such as /opt, /var, /home, /tmp and /mnt.
    if len(resolved.parts) < 4:
        raise ValueError(f"target is too broad: {resolved}")
    return resolved


def iter_candidates(target: Path, *, cutoff_epoch: float) -> Iterable[tuple[Path, int]]:
    """Yield old direct-child PNG regular files without following symlinks."""
    with os.scandir(target) as entries:
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.lower().endswith(".png"):
                    continue
                stat_result = entry.stat(follow_symlinks=False)
                # Exactly on the boundary is retained; only strictly older files go.
                if stat_result.st_mtime >= cutoff_epoch:
                    continue
                yield Path(entry.path), stat_result.st_size
            except OSError as exc:
                print(f"ERROR stat failed path={entry.path!r}: {exc}", file=sys.stderr)
                yield Path(), -1


def cleanup_targets(
    targets: Sequence[str],
    *,
    retention_days: float,
    dry_run: bool,
    now_epoch: float | None = None,
    repo_root: Path | None = None,
) -> CleanupResult:
    if retention_days < 0:
        raise ValueError("retention days must be zero or greater")
    result = CleanupResult()
    cutoff_epoch = (time.time() if now_epoch is None else now_epoch) - retention_days * 86400

    for raw_target in targets:
        try:
            target = validate_target(raw_target, repo_root=repo_root)
        except ValueError as exc:
            result.error_count += 1
            print(f"ERROR invalid target {raw_target!r}: {exc}", file=sys.stderr)
            continue
        result.target_count += 1
        print(f"TARGET path={target} retention_days={retention_days:g} dry_run={str(dry_run).lower()}")
        if not target.exists():
            result.warning_count += 1
            print(f"WARNING target does not exist: {target}", file=sys.stderr)
            continue
        if not target.is_dir():
            result.error_count += 1
            print(f"ERROR target is not a directory: {target}", file=sys.stderr)
            continue

        try:
            for path, size in iter_candidates(target, cutoff_epoch=cutoff_epoch):
                if size < 0:
                    result.error_count += 1
                    continue
                result.candidate_count += 1
                result.candidate_bytes += size
                print(f"{'WOULD_DELETE' if dry_run else 'DELETE'} bytes={size} path={path}")
                if dry_run:
                    continue
                try:
                    current = path.lstat()
                    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                        raise OSError("candidate changed type before deletion")
                    if current.st_mtime >= cutoff_epoch:
                        print(f"SKIP refreshed file path={path}")
                        continue
                    path.unlink()
                    result.deleted_count += 1
                    result.deleted_bytes += current.st_size
                except OSError as exc:
                    result.error_count += 1
                    print(f"ERROR delete failed path={path}: {exc}", file=sys.stderr)
        except OSError as exc:
            result.error_count += 1
            print(f"ERROR scan failed target={target}: {exc}", file=sys.stderr)
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete old PNG files from explicit directories.")
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help=(
            "Absolute directory to scan. Repeat for multiple directories. "
            f"Alternatively set {ENV_TARGETS} using {os.pathsep!r} separators."
        ),
    )
    parser.add_argument(
        "--retention-days",
        type=float,
        default=None,
        help=f"Delete files older than this many days (default {DEFAULT_RETENTION_DAYS:g}).",
    )
    parser.add_argument("--dry-run", action="store_true", help="List candidates without deleting files.")
    parser.add_argument("--lock-file", default=None, help=f"Advisory lock file (default {DEFAULT_LOCK_FILE}).")
    return parser.parse_args(argv)


def _resolve_targets(args: argparse.Namespace) -> list[str]:
    targets = list(args.target) or _split_env_targets(os.environ.get(ENV_TARGETS, ""))
    if not targets:
        raise ValueError(f"no targets configured; use --target or {ENV_TARGETS}")
    return targets


def _resolve_retention_days(args: argparse.Namespace) -> float:
    if args.retention_days is not None:
        return args.retention_days
    raw = os.environ.get(ENV_RETENTION_DAYS, str(DEFAULT_RETENTION_DAYS))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"invalid {ENV_RETENTION_DAYS}: {raw!r}") from exc


def _print_summary(result: CleanupResult, *, dry_run: bool) -> None:
    print(
        "SUMMARY "
        f"targets={result.target_count} candidates={result.candidate_count} "
        f"candidate_bytes={result.candidate_bytes} deleted={result.deleted_count} "
        f"deleted_bytes={result.deleted_bytes} warnings={result.warning_count} "
        f"errors={result.error_count} dry_run={str(dry_run).lower()}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        targets = _resolve_targets(args)
        retention_days = _resolve_retention_days(args)
    except ValueError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    # A dry-run cannot delete, so it deliberately avoids even creating a lock
    # file or its parent directory. This keeps the mode fully side-effect free.
    if args.dry_run:
        try:
            result = cleanup_targets(targets, retention_days=retention_days, dry_run=True)
        except ValueError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        _print_summary(result, dry_run=True)
        return 1 if result.error_count else 0

    # The lock file's parent directory is never created here: this command must
    # not create directories anywhere (a lock inside a missing target directory
    # would otherwise silently materialise that directory). A missing parent is
    # reported as an error instead.
    lock_path = Path(args.lock_file or os.environ.get(ENV_LOCK_FILE, DEFAULT_LOCK_FILE))
    try:
        lock_handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        print(f"ERROR cannot open lock file {lock_path}: {exc}", file=sys.stderr)
        return 1

    with lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"WARNING cleanup already running; lock={lock_path}", file=sys.stderr)
            return 0
        try:
            result = cleanup_targets(targets, retention_days=retention_days, dry_run=False)
        except ValueError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2

    _print_summary(result, dry_run=False)
    return 1 if result.error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
