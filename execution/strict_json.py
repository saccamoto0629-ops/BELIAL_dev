"""Hardened JSON input loading for the execution CLIs and providers
(Issue #78, PR #100 review).

``json.loads`` alone silently accepts duplicate object keys (last one wins),
parses ``NaN`` / ``Infinity``, and turns undecodable bytes into raw
``UnicodeDecodeError`` tracebacks. Every operator-supplied JSON input file in
the execution package (shadow cycle fixtures, execution config, market /
account snapshot files, scenario definitions) is therefore loaded through
:func:`load_strict_json_file`, which converts all of the following into ONE
stable, caller-chosen error type:

- oversized files (``MAX_INPUT_FILE_BYTES``),
- invalid UTF-8 / lone surrogates / a UTF-8 BOM (inputs are plain UTF-8; a
  BOM is rejected explicitly, not skipped),
- syntactically invalid JSON,
- duplicate keys in ANY object, nested included,
- ``NaN`` / ``Infinity`` / ``-Infinity`` literals,
- structures beyond ``MAX_JSON_DEPTH`` nesting, strings beyond
  ``MAX_STRING_LENGTH``, arrays beyond ``MAX_ARRAY_ITEMS`` and objects beyond
  ``MAX_OBJECT_KEYS``.

The limits are deliberately generous for the real inputs (a shadow cycle
file is a few KiB) while bounding hostile or accidental blow-ups; they are
documented in ``docs/architecture/shadow_execution.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

MAX_INPUT_FILE_BYTES = 262_144  # 256 KiB
MAX_JSON_DEPTH = 32
MAX_STRING_LENGTH = 4_096
MAX_ARRAY_ITEMS = 1_024
MAX_OBJECT_KEYS = 256

_UTF8_BOM = b"\xef\xbb\xbf"


class StrictJsonError(ValueError):
    """A JSON input file violates the strict input contract."""


def _raise(error_cls, message: str):
    raise error_cls(message)


def parse_strict_json(text: str, *, description: str, error_cls=StrictJsonError):
    """Parse ``text`` under the strict contract (see the module docstring).
    ``description`` names the input in error messages."""

    def no_duplicate_keys(pairs):
        obj: dict = {}
        for key, value in pairs:
            if key in obj:
                _raise(error_cls, f"{description}: duplicate object key {key!r}")
            obj[key] = value
        if len(obj) > MAX_OBJECT_KEYS:
            _raise(error_cls, f"{description}: object exceeds {MAX_OBJECT_KEYS} keys")
        return obj

    def reject_constant(name):
        _raise(error_cls, f"{description}: non-finite number {name!r} is not allowed")

    try:
        data = json.loads(
            text, object_pairs_hook=no_duplicate_keys, parse_constant=reject_constant
        )
    except error_cls:
        raise
    except RecursionError:
        _raise(error_cls, f"{description}: JSON nesting is too deep")
    except json.JSONDecodeError as exc:
        _raise(error_cls, f"{description}: not valid JSON: {exc}")

    _check_limits(data, description, error_cls, depth=1)
    return data


def _check_limits(value, description: str, error_cls, *, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        _raise(
            error_cls,
            f"{description}: JSON nesting exceeds the maximum depth {MAX_JSON_DEPTH}",
        )
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            _raise(
                error_cls,
                f"{description}: a string exceeds {MAX_STRING_LENGTH} characters",
            )
    elif isinstance(value, list):
        if len(value) > MAX_ARRAY_ITEMS:
            _raise(error_cls, f"{description}: an array exceeds {MAX_ARRAY_ITEMS} items")
        for item in value:
            _check_limits(item, description, error_cls, depth=depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if len(key) > MAX_STRING_LENGTH:
                _raise(
                    error_cls,
                    f"{description}: an object key exceeds {MAX_STRING_LENGTH} characters",
                )
            _check_limits(item, description, error_cls, depth=depth + 1)


def load_strict_json_file(path: Path, *, description: str, error_cls=StrictJsonError):
    """Read + decode + parse one JSON input file under the strict contract."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        _raise(error_cls, f"cannot read {description} {path}: {exc}")
    if size > MAX_INPUT_FILE_BYTES:
        _raise(
            error_cls,
            f"{description} {path} is {size} bytes; the maximum accepted size is "
            f"{MAX_INPUT_FILE_BYTES} bytes",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _raise(error_cls, f"cannot read {description} {path}: {exc}")
    if raw.startswith(_UTF8_BOM):
        _raise(
            error_cls,
            f"{description} {path} starts with a UTF-8 BOM; inputs must be plain UTF-8",
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeError) as exc:
        _raise(error_cls, f"{description} {path} is not valid UTF-8: {exc}")
    return parse_strict_json(text, description=f"{description} {path}", error_cls=error_cls)
