"""Demo startup preflight CLI (Issue #132) -- the place an operator verifies
the demo activation locks and reads the account digest to pin.

*** NO ORDER IS EVER PLACED, CANCELLED OR QUERIED FROM THIS CLI, AND IT
PERFORMS NO NETWORK I/O. *** It evaluates the demo activation lock
(``execution/demo_lock.py``), prints the safety summary, and composes -- then
immediately drops -- the demo ``BrokerConnection`` so that "all locks hold"
is proven by construction rather than asserted. Submissions keep going
through the single sanctioned boundary (``execution/live_submission.py``)
and the Safety Gate; this CLI adds no order path.

``python -m execution.demo_startup_cli --execution-mode demo \\
    --safety-config <path>``

What must hold for exit 0 (all of it, or nothing runs):

- ``--execution-mode demo``;
- ``OANDA_API_TOKEN`` / ``OANDA_ACCOUNT_ID`` set;
- ``OANDA_API_URL`` unset or exactly the allowlisted practice endpoint;
- NO live configuration in the process environment at all
  (``ENABLE_LIVE_TRADING`` / ``OANDA_LIVE_*``);
- the demo safety configuration readable, allowing the ``oanda-demo``
  target, and pinning THIS account in ``expected_account_reference``;
- the Kill Switch readable AND off.

Pinning the account: run this command once. The summary prints
``account (masked)`` -- the non-reversible digest of the configured
``OANDA_ACCOUNT_ID`` -- even when the startup is refused. Copy that value
into the safety configuration's ``expected_account_reference``.

Exit codes (shared execution-CLI contract): 0 = every lock held and the demo
connection was composed; 1 = the startup was REFUSED (any lock, the kill
switch, an unreadable safety configuration) -- the fail-closed side;
2 = usage error. ``--json`` prints one pure-ASCII JSON document to stdout.

Secrets: the token, the account id and the endpoint URL are never printed.
The summary shows the endpoint KIND (``live`` / ``demo`` / ``unknown`` /
``not-configured``) and the account as a non-reversible digest.

This CLI does NOT start a runner and does NOT unlock live operation. It is a
preflight check; continuous demo operation (systemd, periodic
reconciliation) is deliberately out of Issue #132's scope.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from execution.cli_streams import encoding_safe_streams
from execution.demo_lock import (
    DemoSafetyConfigError,
    build_demo_connection,
    load_demo_safety_policy,
)
from execution.kill_switch_store import kill_switch_db_path

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.demo_startup_cli",
        description=(
            "Verify the demo activation lock and print the startup safety summary "
            "(Issue #132). Never places, cancels or queries an order and never talks to "
            "any broker endpoint."
        ),
    )
    parser.add_argument(
        "--execution-mode",
        required=True,
        help="the configured execution mode; only 'demo' can produce a demo connection",
    )
    parser.add_argument(
        "--safety-config",
        type=Path,
        required=True,
        help="path to the demo safety configuration JSON (symbols, quantity ceiling, "
        "position limits, the pinned account digest). It holds NO credentials",
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
    # and never accepts a policy object from here, so nothing this CLI
    # computes can authorize a demo connection. The message is value-free by
    # construction (see the shared parser in execution/safety_config.py).
    config_error = None
    try:
        load_demo_safety_policy(args.safety_config)
    except DemoSafetyConfigError as exc:
        config_error = str(exc)

    # The kill-switch store is opened by the composer when it reads the
    # state: a missing / foreign / corrupt DB surfaces there as the
    # fail-closed KILL_SWITCH_STATE_UNVERIFIABLE refusal, and nothing here
    # creates the DB.
    db_path = args.kill_switch_db if args.kill_switch_db is not None else kill_switch_db_path()

    decision, grant = build_demo_connection(
        execution_mode=args.execution_mode,
        safety_config_path=args.safety_config,
        kill_switch_db_path=db_path,
    )
    # The connection is composed only to PROVE every lock held; nothing is
    # sent through it here and it is dropped when this function returns.
    del grant

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
            print("  demo activation lock: ALL LOCKS SATISFIED (no order was submitted)")

    return EXIT_OK if decision.allowed else EXIT_REFUSED


def main(argv: list[str] | None = None) -> int:
    with encoding_safe_streams():
        try:
            return _run_cli(argv)
        except SystemExit as exc:  # argparse usage errors
            return EXIT_USAGE_ERROR if exc.code else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
