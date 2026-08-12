"""Operator CLI for the persistent Execution Kill Switch (Issue #81).

*** NO ORDER IS EVER PLACED, CANCELLED OR QUERIED FROM THIS CLI. It only
reads and appends the kill switch state store
(``execution/kill_switch_store.py``); enforcement stays entirely inside the
Safety Gate / ``LiveOrderSubmissionService`` boundary. ***

``python -m execution.kill_switch_cli`` is the sanctioned operational path
for the emergency stop:

- ``status``  -- read-only state + change history. NEVER creates the DB
  (an uninitialized store is reported as UNAVAILABLE = fail-closed, exit 1;
  it is never reported as "disabled").
- ``enable``  -- engage the switch (halt all new submissions). Requires
  ``--operator`` and ``--reason``; on a missing store the schema and the
  enable transition are created in ONE transaction (PR #126 Codex review
  P1: a failure mid-command leaves the file unverifiable = fail-closed,
  never a committed empty store that would read as disabled), so the
  emergency action stays one command. Idempotent in effect: enabling an
  already-enabled switch appends another audit row and keeps it enabled.
- ``disable`` -- resume new submissions. DELIBERATELY harder than enable
  (Issue #81: releasing must be an explicit contract): on top of
  ``--operator`` / ``--reason`` it requires the literal
  ``--confirm-resume`` flag, otherwise the CLI refuses with a usage error
  (exit 2) and changes nothing.

Exit codes (shared execution-CLI contract): 0 = the command succeeded;
1 = the state store failed (missing / foreign schema / corrupt /
out-of-band edits -- the fail-closed side: while this persists, the gate
rejects every new submission with ``state_query_failed``); 2 = usage error
(missing operator/reason, missing ``--confirm-resume``). ``--json`` prints
one pure-ASCII JSON document to stdout.

Timestamps are recorded in UTC (the repository's clock contract); history
ordering is by ``transition_id``, timestamps are auxiliary.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from execution.cli_streams import encoding_safe_streams
from execution.kill_switch_store import (
    KillSwitchStoreError,
    KillSwitchTransition,
    SQLiteKillSwitchStore,
    kill_switch_db_path,
)

EXIT_OK = 0
EXIT_STORE_ERROR = 1
EXIT_USAGE_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.kill_switch_cli",
        description=(
            "Inspect and operate the persistent Execution Kill Switch (Issue #81). "
            "While enabled -- or while the state store is unreadable -- the Safety "
            "Gate rejects every NEW submission; reconciliation and read-only "
            "queries are unaffected. This CLI never talks to any broker."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="kill switch DB path (default: KILL_SWITCH_DB_PATH or the packaged "
        "execution/data/kill_switch.sqlite3)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print a machine-readable pure-ASCII JSON outcome"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "status",
        help="show the current state and change history (read-only; never creates "
        "the DB; an unreadable store reports UNAVAILABLE and exits 1)",
    )

    enable = subparsers.add_parser(
        "enable",
        help="engage the kill switch: every new submission is rejected until an "
        "explicit disable (initializes a missing store first)",
    )
    disable = subparsers.add_parser(
        "disable",
        help="resume new submissions; requires the explicit --confirm-resume flag "
        "on top of --operator/--reason",
    )
    for sub in (enable, disable):
        sub.add_argument(
            "--operator",
            required=True,
            help="who performs the change (recorded in the audit history)",
        )
        sub.add_argument(
            "--reason",
            required=True,
            help="why the change is performed (recorded in the audit history)",
        )
    disable.add_argument(
        "--confirm-resume",
        action="store_true",
        help="explicit confirmation that NEW submissions may resume; without this "
        "flag the disable is refused (usage error) and nothing changes",
    )
    return parser


def _transition_payload(transition: KillSwitchTransition) -> dict:
    return {
        "transition_id": transition.transition_id,
        "enabled": transition.enabled,
        "changed_at_utc": transition.changed_at_utc.isoformat(),
        "operator": transition.operator,
        "reason": transition.reason,
    }


def _print_state(payload: dict) -> None:
    label = "ENABLED (all new submissions are rejected)" if payload["kill_switch_enabled"] \
        else "DISABLED (new submissions are allowed)"
    print(f"kill switch: {label}")
    print(f"db: {payload['db_path']}")
    last = payload["last_transition"]
    if last is None:
        print("last transition: none (initialized, never switched)")
    else:
        print(
            "last transition: #{0} enabled={1} at={2} operator={3} reason={4}".format(
                last["transition_id"],
                last["enabled"],
                last["changed_at_utc"],
                last["operator"],
                last["reason"],
            )
        )
    print(f"transitions recorded: {payload['transition_count']}")
    for entry in payload["history"]:
        print(
            "  #{0} enabled={1} at={2} operator={3} reason={4}".format(
                entry["transition_id"],
                entry["enabled"],
                entry["changed_at_utc"],
                entry["operator"],
                entry["reason"],
            )
        )


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        _print_state(payload)


def _payload(command: str, db_path, state, history) -> dict:
    """Build the report from an ALREADY-OBTAINED state + history pair.

    Both always come from ONE verified transaction (PR #126 Codex review
    P2): a concurrent change can never make this report say "DISABLED"
    while its own history already lists a newer enable. For enable /
    disable that transaction is the one that COMMITTED the change (review
    r6), so a durable transition can never be reported as a fail-closed
    store.
    """
    return {
        "command": command,
        "db_path": str(db_path),
        "kill_switch_enabled": state.enabled,
        "last_transition": None
        if state.transition is None
        else _transition_payload(state.transition),
        "transition_count": len(history),
        "history": [_transition_payload(entry) for entry in history],
    }


def _run_cli(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)
    db_path = args.db if args.db is not None else kill_switch_db_path()
    store = SQLiteKillSwitchStore(db_path)

    if args.command in ("enable", "disable"):
        operator = args.operator.strip()
        reason = args.reason.strip()
        if not operator or not reason:
            print("--operator and --reason must be non-empty", file=sys.stderr)
            return EXIT_USAGE_ERROR
        if args.command == "disable" and not args.confirm_resume:
            print(
                "disable refused: releasing the kill switch resumes NEW submissions "
                "and requires the explicit --confirm-resume flag; nothing was changed",
                file=sys.stderr,
            )
            return EXIT_USAGE_ERROR

    try:
        if args.command == "status":
            # Read-only on purpose: status must never create or migrate the
            # DB, and a store that cannot be read is the fail-closed state,
            # reported as such -- never as "disabled".
            state, history = store.snapshot()
            payload = _payload("status", store.path, state, history)
        else:
            enabled = args.command == "enable"
            # Schema creation (missing store), the transition AND the report
            # are ONE transaction (PR #126 Codex review P1 / P2 r6): a
            # failure mid-command can never leave a committed empty store --
            # which would read as DISABLED -- behind, and a transition that
            # DID commit is never reported as a fail-closed store, because
            # describing it needs no second read that could fail on its own.
            outcome = store.initialize_and_record(
                enabled=enabled,
                operator=operator,
                reason=reason,
                changed_at_utc=datetime.now(timezone.utc),
            )
            if not outcome.state_is_verifiable:
                # The transition IS durable -- say so plainly. Reporting only
                # "UNAVAILABLE" here would tell an operator their disable did
                # not take effect while submissions were already free to
                # resume. Exit 1 still stands: the state cannot be verified,
                # so the gate keeps rejecting.
                recorded = outcome.transition
                print(
                    f"kill switch transition #{recorded.transition_id} WAS RECORDED "
                    f"(enabled={recorded.enabled}, operator={recorded.operator}, "
                    f"at={recorded.changed_at_utc.isoformat()}), but the store's "
                    f"history does not validate, so the current state cannot be "
                    f"confirmed: {outcome.integrity_error}. The Safety Gate rejects "
                    "every new submission while this persists.",
                    file=sys.stderr,
                )
                return EXIT_STORE_ERROR
            payload = _payload(args.command, store.path, outcome.state, outcome.history)
            payload["recorded_transition"] = _transition_payload(outcome.transition)
    except KillSwitchStoreError as exc:
        print(
            "kill switch state UNAVAILABLE (fail-closed: the Safety Gate rejects "
            f"every new submission while this persists): {exc}",
            file=sys.stderr,
        )
        return EXIT_STORE_ERROR

    _emit(payload, args.json)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    with encoding_safe_streams():
        return _run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
