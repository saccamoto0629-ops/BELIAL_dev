#!/usr/bin/env python3
"""Safely remove old PNG files from explicitly configured directories.

Only direct child regular files ending in ``.png`` are considered. Symlinks are
never followed: target paths containing a symlink component are rejected, the
directory is opened with ``O_DIRECTORY | O_NOFOLLOW`` and all scanning and
deletion go through that pinned directory FD. The command is intentionally
fail-closed for dangerous target paths, never creates directories, and reports
aggregate counts even when individual deletions fail.
"""

from __future__ import annotations

import argparse
import fcntl
import math
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


def _display(value: object) -> str:
    """repr-quote a path for logging: newlines and control characters in file
    names must not be able to fabricate extra journal lines."""
    return repr(str(value))


def _split_env_targets(value: str) -> list[str]:
    return [item.strip() for item in value.split(os.pathsep) if item.strip()]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _validate_retention_days(value: float) -> float:
    # NaN passes a plain "< 0" check and then poisons the cutoff (NaN compares
    # False against every mtime, marking ALL files for deletion), so finiteness
    # must be checked explicitly.
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"retention days must be a finite number >= 0: {value!r}")
    return value


def validate_target(raw_target: str, *, repo_root: Path | None = None) -> Path:
    """Validate one cleanup directory without creating or resolving through it.

    The path is normalised lexically only. ``..`` components and any symlink
    among the existing path components are rejected, so the returned path can
    never denote a directory reached through a symlink.
    """
    if not raw_target or not raw_target.strip():
        raise ValueError("target path is empty")
    target = Path(raw_target).expanduser()
    if not target.is_absolute():
        raise ValueError(f"target must be an absolute path: {raw_target!r}")
    if ".." in target.parts:
        raise ValueError(f"refusing '..' in target path: {raw_target!r}")
    normalized = Path(os.path.normpath(str(target)))
    current = Path(normalized.anchor)
    for part in normalized.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"refusing symlink in target path: {current}")
    if normalized == Path(normalized.anchor):
        raise ValueError(f"refusing filesystem root target: {normalized}")
    effective_repo_root = (repo_root or _repo_root()).resolve(strict=False)
    if normalized == effective_repo_root:
        raise ValueError(f"refusing repository root target: {normalized}")
    # Reject broad paths such as /opt, /var, /home, /tmp and /mnt.
    if len(normalized.parts) < 4:
        raise ValueError(f"target is too broad: {normalized}")
    return normalized


def _open_target_dir(target: Path) -> int:
    """Open a validated target directory by walking it component by component.

    Every component is opened with ``O_DIRECTORY | O_NOFOLLOW`` relative to
    the previously opened directory FD, so a component swapped to a symlink
    AFTER validate_target() ran still cannot be followed (opening the full
    path string in one call would only protect the final component). Raises
    OSError: ENOENT for a missing target, ELOOP for a symlink component,
    ENOTDIR for a non-directory component.
    """
    fd = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in target.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except OSError:
        os.close(fd)
        raise
    return fd


def iter_candidates(
    target: Path, dir_fd: int, *, cutoff_epoch: float
) -> Iterable[tuple[str, os.stat_result | None]]:
    """Yield (name, stat) of old direct-child PNG regular files via dir_fd.

    Symlinks are never followed. A failed stat yields (name, None) so the
    caller can count the error and continue. ``target`` is only used for
    log messages; every filesystem operation goes through ``dir_fd``.
    """
    with os.scandir(dir_fd) as entries:
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
                yield entry.name, stat_result
            except OSError as exc:
                print(f"ERROR stat failed path={_display(target / entry.name)}: {exc}", file=sys.stderr)
                yield entry.name, None


def cleanup_targets(
    targets: Sequence[str],
    *,
    retention_days: float,
    dry_run: bool,
    now_epoch: float | None = None,
    repo_root: Path | None = None,
) -> CleanupResult:
    _validate_retention_days(retention_days)
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
        print(f"TARGET path={_display(target)} retention_days={retention_days:g} dry_run={str(dry_run).lower()}")

        # Pin the directory via a component-wise O_NOFOLLOW walk (no
        # exists()/is_dir() path re-resolution beforehand -- a missing target
        # is detected as ENOENT from the walk itself). All scanning/deletion
        # below go through the returned FD, so neither a symlink swapped into
        # any path component after validation nor a concurrent rename of the
        # path can redirect the run.
        try:
            dir_fd = _open_target_dir(target)
        except FileNotFoundError:
            result.warning_count += 1
            print(f"WARNING target does not exist: {_display(target)}", file=sys.stderr)
            continue
        except OSError as exc:
            result.error_count += 1
            print(f"ERROR cannot open target {_display(target)}: {exc}", file=sys.stderr)
            continue
        try:
            for name, enum_stat in iter_candidates(target, dir_fd, cutoff_epoch=cutoff_epoch):
                if enum_stat is None:
                    result.error_count += 1
                    continue
                path = target / name
                result.candidate_count += 1
                result.candidate_bytes += enum_stat.st_size
                print(f"{'WOULD_DELETE' if dry_run else 'DELETE'} bytes={enum_stat.st_size} path={_display(path)}")
                if dry_run:
                    continue
                try:
                    current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (enum_stat.st_dev, enum_stat.st_ino):
                        # The name now denotes a different file than the one
                        # enumerated (swapped after listing): leave it alone.
                        print(f"SKIP replaced file path={_display(path)}")
                        continue
                    if not stat.S_ISREG(current.st_mode):
                        raise OSError("candidate changed type before deletion")
                    if current.st_mtime >= cutoff_epoch:
                        print(f"SKIP refreshed file path={_display(path)}")
                        continue
                    # A freed inode number can be reused immediately by a new
                    # file, so dev/ino alone cannot prove identity: any change
                    # in size or timestamps since enumeration also disqualifies
                    # the candidate (fail-safe: it is retried on the next run).
                    if (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
                        enum_stat.st_size,
                        enum_stat.st_mtime_ns,
                        enum_stat.st_ctime_ns,
                    ):
                        print(f"SKIP changed file path={_display(path)}")
                        continue
                    os.unlink(name, dir_fd=dir_fd)
                    result.deleted_count += 1
                    result.deleted_bytes += current.st_size
                except OSError as exc:
                    result.error_count += 1
                    print(f"ERROR delete failed path={_display(path)}: {exc}", file=sys.stderr)
        except OSError as exc:
            result.error_count += 1
            print(f"ERROR scan failed target={_display(target)}: {exc}", file=sys.stderr)
        finally:
            os.close(dir_fd)
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
        return _validate_retention_days(args.retention_days)
    raw = os.environ.get(ENV_RETENTION_DAYS, str(DEFAULT_RETENTION_DAYS))
    try:
        return _validate_retention_days(float(raw))
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
        # Validated (finite, >= 0) BEFORE any lock file is created, so an
        # invalid configuration exits 2 without leaving side effects.
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

    # O_NOFOLLOW: a symlinked lock path (e.g. squatted in a shared /tmp) must
    # fail instead of being followed; mode 0600 keeps the lock private. The
    # parent directory is never created here: this command must not create
    # directories anywhere, so a missing parent is a visible error instead.
    lock_path = Path(args.lock_file or os.environ.get(ENV_LOCK_FILE, DEFAULT_LOCK_FILE))
    try:
        lock_fd = os.open(
            str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
    except OSError as exc:
        print(f"ERROR cannot open lock file {_display(lock_path)}: {exc}", file=sys.stderr)
        return 1

    with os.fdopen(lock_fd, "a+", encoding="utf-8") as lock_handle:
        # A pre-existing lock entry is only trusted if it is a plain,
        # un-hardlinked regular file owned by this user: O_CREAT applies the
        # 0600 mode to NEW files only, so a squatted world-writable file (whose
        # owner could unlink/recreate it mid-run and break the exclusion) must
        # be rejected, and a too-open mode on our own older lock is tightened.
        lock_stat = os.fstat(lock_handle.fileno())
        if not stat.S_ISREG(lock_stat.st_mode):
            print(f"ERROR lock file is not a regular file: {_display(lock_path)}", file=sys.stderr)
            return 1
        if lock_stat.st_uid != os.geteuid():
            print(
                f"ERROR lock file owned by another user (uid {lock_stat.st_uid}): {_display(lock_path)}",
                file=sys.stderr,
            )
            return 1
        if lock_stat.st_nlink != 1:
            print(
                f"ERROR lock file has {lock_stat.st_nlink} links, expected 1: {_display(lock_path)}",
                file=sys.stderr,
            )
            return 1
        if lock_stat.st_mode & 0o077:
            os.fchmod(lock_handle.fileno(), 0o600)
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"WARNING cleanup already running; lock={_display(lock_path)}", file=sys.stderr)
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
