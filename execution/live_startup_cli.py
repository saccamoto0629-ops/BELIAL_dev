"""Live startup preflight CLI (Issue #82) -- the ONE place ``--confirm-live``
is accepted, and the place the startup safety summary is printed.

*** NO ORDER IS EVER PLACED, CANCELLED OR QUERIED FROM THIS CLI, AND IT
PERFORMS NO NETWORK I/O. *** It evaluates the live activation lock
(``execution/live_lock.py``), prints the safety summary, and composes -- then
immediately drops -- the live ``BrokerConnection`` so that "all locks hold"
is proven by construction rather than asserted. Submissions keep going
through the single sanctioned boundary (``execution/live_submission.py``)
and the Safety Gate; this CLI adds no order path.

``python -m execution.live_startup_cli --execution-mode live --confirm-live \\
    --safety-config <path>``

What must hold for exit 0 (all of it, or nothing runs):

- ``--execution-mode live``;
- ``ENABLE_LIVE_TRADING=true`` in the process environment;
- ``OANDA_LIVE_API_TOKEN`` / ``OANDA_LIVE_ACCOUNT_ID`` set;
- ``OANDA_LIVE_API_URL`` set to the allowlisted live endpoint;
- ``--confirm-live`` passed explicitly;
- demo and live configuration not mixed in either direction;
- the live safety configuration readable and allowing the live target;
- the Kill Switch readable AND off.

Exit codes (shared execution-CLI contract): 0 = every lock held and the live
connection was composed; 1 = the startup was REFUSED (any lock, the kill
switch, an unreadable safety configuration) -- the fail-closed side;
2 = usage error. ``--json`` prints one pure-ASCII JSON document to stdout.

Secrets: the token, the account id and the endpoint URL are never printed.
The summary shows the endpoint KIND (``live`` / ``demo`` / ``unknown`` /
``not-configured``) and the account as a non-reversible digest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from execution.cli_streams import encoding_safe_streams
from execution.kill_switch_store import kill_switch_db_path
from execution.live_lock import (
    LiveSafetyConfigError,
    build_live_connection,
    load_live_safety_policy,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.live_startup_cli",
        description=(
            "Verify the live activation lock and print the startup safety summary "
            "(Issue #82). Never places, cancels or queries an order and never talks to "
            "any broker endpoint."
        ),
    )
    parser.add_argument(
        "--execution-mode",
        required=True,
        help="the configured execution mode; only 'live' can produce a live connection",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="explicit operator confirmation. REQUIRED for live; meaningless (and "
        "refused together with the mode) for every other execution mode",
    )
    parser.add_argument(
        "--safety-config",
        type=Path,
        required=True,
        help="path to the live safety configuration JSON (symbols, quantity ceiling, "
        "position limits). It holds NO credentials",
    )
    parser.add_argument(
        "--kill-switch-db",
        type=Path,
        default=None,
        help="kill switch DB path (default: KILL_SWITCH_DB_PATH or the packaged "
        "execution/data/kill_switch.sqlite3). Read-only: this CLI never creates it",
    )
    parser.add_argument(
        "--json", action="store_true", help="print a machine-readable pure-ASCII JSON outcome"
    )
    return parser


def _run_cli(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)

    # An unreadable safety configuration is NOT a usage error that stops the
    # summary: it is a lock failure, so the operator still sees the summary
    # and the machine-readable refusal code. This load is for the OPERATOR
    # MESSAGE ONLY -- the composer loads the file itself from the same path
    # and never accepts a policy object from here (PR #128 Codex review P1),
    # so nothing this CLI computes can authorize a live connection. The
    # message is value-free by construction (see live_lock's parser).
    config_error = None
    try:
        load_live_safety_policy(args.safety_config)
    except LiveSafetyConfigError as exc:
        config_error = str(exc)

    # The kill-switch store is opened by the composer when it reads the
    # state: a missing / foreign / corrupt DB surfaces there as the
    # fail-closed KILL_SWITCH_STATE_UNVERIFIABLE refusal, and nothing here
    # creates the DB.
    db_path = args.kill_switch_db if args.kill_switch_db is not None else kill_switch_db_path()

    decision, connection = build_live_connection(
        execution_mode=args.execution_mode,
        confirm_live=args.confirm_live,
        safety_config_path=args.safety_config,
        kill_switch_db_path=db_path,
    )
    # The connection is composed only to PROVE every lock held; nothing is
    # sent through it here and it is dropped when this function returns.
    del connection

    if args.json:
        payload = {
            "allowed": decision.allowed,
            "rejection_codes": list(decision.rejection_codes),
            "rejections": [
                {"code": entry.code.value, "message": entry.message}
                for entry in decision.rejections
            ],
            "safety_summary": decision.summary.to_dict(),
            "kill_switch_db": str(db_path),
            "safety_config": str(args.safety_config),
            "safety_config_error": config_error,
        }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        print(decision.summary.render())
        if config_error is not None:
            print(f"  safety configuration error: {config_error}")
        for entry in decision.rejections:
            print(f"  refused [{entry.code.value}]: {entry.message}", file=sys.stderr)
        if decision.allowed:
            print("  live activation lock: ALL LOCKS SATISFIED (no order was submitted)")

    return EXIT_OK if decision.allowed else EXIT_REFUSED


def main(argv: list[str] | None = None) -> int:
    with encoding_safe_streams():
        try:
            return _run_cli(argv)
        except SystemExit as exc:  # argparse usage errors
            return EXIT_USAGE_ERROR if exc.code else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
