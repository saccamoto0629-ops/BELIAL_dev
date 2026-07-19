"""Discord notifier for Paper Execution state transitions (Issue #83).

*** PAPER EXECUTION. VIRTUAL FILLS / VIRTUAL PNL ONLY. NEVER A LIVE ORDER. ***

Paper Signal notifications announce "a hypothesis was generated"; Paper
Execution notifications announce "that hypothesis was virtually filled /
virtually settled as time advanced". This module owns ONLY the latter and is
deliberately decoupled from ``execution_runner``:

    execution_runner  ->  ExecutionEvent (DTO)  ->  this module  ->  Discord Webhook

Separated responsibilities:

- **event generation**: the runner emits an :class:`ExecutionEvent` only AFTER
  the position transition committed to the Position DB (DB first, notify
  second; a Discord failure never rolls back a position).
- **body / payload generation**: :func:`format_event_message` /
  :func:`build_discord_payload` are pure functions. Missing values render as a
  safe placeholder, never an exception.
- **HTTP delivery**: :func:`deliver_payload` -- timeout always set, exactly one
  attempt per call, errors classified into a sanitized
  :class:`NotificationDeliveryError` (never containing the webhook URL).
- **history / duplicate suppression**: the ``paper_execution_notifications``
  table in the Position DB (``position_store``): ``event_key`` =
  ``position_id:event_type`` UNIQUE. Delivered events are never re-sent (also
  across runner restarts); failed events are retried on later cycles up to
  ``MAX_DELIVERY_ATTEMPTS`` -- never an infinite retry loop.
- **dry-run**: format + log the would-be body only. No DB row, no HTTP call,
  no file.

Configuration: ``PAPER_EXECUTION_DISCORD_WEBHOOK_URL`` in
``paper-execution/.env`` (or the process environment). Deliberately a separate
variable from Paper Signal's ``DISCORD_WEBHOOK_URL`` so the two notification
responsibilities stay independent. When unset, events are still recorded
(queued) but delivery is skipped with a warning -- never an error, never a
crash of the resident runner.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import position_store as store  # noqa: E402

# Re-exported vocabulary (owned by position_store so the DB CHECK stays in sync).
EVENT_OPEN = store.NOTIFY_EVENT_OPEN
EVENT_CLOSED = store.NOTIFY_EVENT_CLOSED
EVENT_EXPIRED = store.NOTIFY_EVENT_EXPIRED
EVENT_CANCELLED = store.NOTIFY_EVENT_CANCELLED

WEBHOOK_ENV_VAR = "PAPER_EXECUTION_DISCORD_WEBHOOK_URL"
REQUEST_TIMEOUT_SECONDS = 10
MAX_DELIVERY_ATTEMPTS = 5
DISCORD_CONTENT_LIMIT = 2000

PAPER_EXECUTION_NOTE = "Paper Execution only. 仮想約定・仮想損益の記録であり、実注文は行っていません。"
MISSING_VALUE = "-"

_logger_name = "paper_execution"


def _get_logger() -> logging.Logger:
    # Shares the runner's logger by name: the runner configures handlers
    # (console-only on dry run); this module never adds its own file handler.
    return logging.getLogger(_logger_name)


@dataclass(frozen=True)
class ExecutionEvent:
    """DTO handed from the runner to the notifier: one committed state transition.

    ``position`` is the post-transition row (or, on a dry run, the projected
    record) -- used for immediate formatting; retries re-read the row from the
    Position DB by ``position_id``.
    """

    event_type: str
    position: dict = field(default_factory=dict)

    @property
    def position_id(self) -> str:
        return str(self.position.get("position_id") or "")

    @property
    def event_key(self) -> str:
        return store.notification_event_key(self.position_id, self.event_type)


def webhook_url(env_values: dict[str, str] | None = None) -> str:
    """Resolve the webhook URL: process env first, then ``paper-execution/.env``."""
    values = env_values if env_values is not None else store.load_env_file()
    return (os.getenv(WEBHOOK_ENV_VAR) or values.get(WEBHOOK_ENV_VAR) or "").strip()


# --- body generation (pure; None-safe by contract) ---------------------------


def _display(value) -> str:
    """Safe display of any field: None/empty -> a placeholder, never an exception."""
    if value is None or value == "":
        return MISSING_VALUE
    return str(value)


def _number(value, digits: int = 5) -> str:
    """Fixed-point display for prices/PnL; non-numeric values fall back to str."""
    if value is None or value == "":
        return MISSING_VALUE
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def format_open_message(position: dict) -> str:
    """pending -> open: the virtual fill notification body."""
    return "\n".join(
        [
            "\U0001f4c8 Paper Execution OPEN — 仮想約定 (virtual fill)",
            "",
            f"Symbol: {_display(position.get('symbol'))}",
            f"Timeframe: {_display(position.get('timeframe'))}",
            f"Side: {_display(position.get('side'))}",
            f"Signal ID: {_display(position.get('signal_id'))}",
            f"Position ID: {_display(position.get('position_id'))}",
            f"Entry Time (UTC): {_display(position.get('entry_time_utc'))}",
            f"Entry Price: {_number(position.get('entry_price'))}",
            f"Stop Loss: {_number(position.get('stop_loss_price'))}",
            f"Take Profit: {_number(position.get('take_profit_price'))}",
            f"Max Hold Deadline (UTC): {_display(position.get('max_hold_deadline_utc'))}",
            f"Execution Profile: {_display(position.get('execution_profile'))}",
            "",
            PAPER_EXECUTION_NOTE,
        ]
    )


def format_closed_message(position: dict) -> str:
    """open -> closed: the virtual PnL notification body (SL / TP / max hold)."""
    return "\n".join(
        [
            "\U0001f4b0 Paper Execution CLOSED — 仮想損益 (virtual PnL)",
            "",
            f"Symbol: {_display(position.get('symbol'))}",
            f"Timeframe: {_display(position.get('timeframe'))}",
            f"Side: {_display(position.get('side'))}",
            f"Signal ID: {_display(position.get('signal_id'))}",
            f"Position ID: {_display(position.get('position_id'))}",
            f"Terminal Reason: {_display(position.get('terminal_reason'))}",
            f"Entry Time (UTC): {_display(position.get('entry_time_utc'))}",
            f"Close Time (UTC): {_display(position.get('close_time_utc'))}",
            f"Entry Price: {_number(position.get('entry_price'))}",
            f"Close Price: {_number(position.get('close_price'))}",
            f"Gross PnL: {_number(position.get('gross_pnl'))}",
            f"Net PnL: {_number(position.get('net_pnl'))}",
            f"R Multiple: {_number(position.get('r_multiple'), digits=2)}",
            "",
            PAPER_EXECUTION_NOTE,
        ]
    )


def format_terminal_message(event_type: str, position: dict) -> str:
    """pending -> expired / invalid -> cancelled: no fill ever happened."""
    if event_type == EVENT_EXPIRED:
        header = "⌛ Paper Execution EXPIRED — 仮想処理 (no virtual fill)"
    else:
        header = "\U0001f6ab Paper Execution CANCELLED — 仮想処理 (no virtual fill)"
    return "\n".join(
        [
            header,
            "",
            f"Symbol: {_display(position.get('symbol'))}",
            f"Timeframe: {_display(position.get('timeframe'))}",
            f"Side: {_display(position.get('side'))}",
            f"Signal ID: {_display(position.get('signal_id'))}",
            f"Position ID: {_display(position.get('position_id'))}",
            f"State: {_display(position.get('state'))}",
            f"Terminal Reason: {_display(position.get('terminal_reason'))}",
            "",
            PAPER_EXECUTION_NOTE,
        ]
    )


def format_event_message(event_type: str, position: dict) -> str:
    """Dispatch to the per-event body builder (closed vocabulary)."""
    if event_type == EVENT_OPEN:
        return format_open_message(position)
    if event_type == EVENT_CLOSED:
        return format_closed_message(position)
    if event_type in (EVENT_EXPIRED, EVENT_CANCELLED):
        return format_terminal_message(event_type, position)
    raise ValueError(f"unknown notification event_type {event_type!r}")


def build_discord_payload(message: str) -> dict:
    """Webhook payload: plain content, hard-capped at Discord's 2000-char limit."""
    return {"content": message[:DISCORD_CONTENT_LIMIT]}


# --- HTTP delivery ------------------------------------------------------------


class NotificationDeliveryError(RuntimeError):
    """One delivery attempt failed (timeout / HTTP status / connection error).

    Carries only a sanitized reason -- NEVER the webhook URL, the payload, or
    raw exception text that could embed either.
    """


def deliver_payload(
    payload: dict,
    *,
    webhook_url: str,
    session=None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> None:
    """POST one payload to the webhook: single attempt, timeout always set.

    Raises :class:`NotificationDeliveryError` with a sanitized reason on any
    failure (timeout, connection error, 429, 4xx/5xx). Retry policy is the
    caller's responsibility (one attempt per cycle, capped across cycles) --
    this function itself never retries.
    """
    http = session if session is not None else requests
    try:
        response = http.post(webhook_url, json=payload, timeout=timeout)
    except requests.Timeout as exc:
        raise NotificationDeliveryError(f"timeout after {timeout}s") from exc
    except requests.ConnectionError as exc:
        raise NotificationDeliveryError("connection error") from exc
    except requests.RequestException as exc:
        # Only the exception class name: str(exc) can contain the webhook URL.
        raise NotificationDeliveryError(f"request failed: {type(exc).__name__}") from exc
    status = getattr(response, "status_code", None)
    if status == 429:
        raise NotificationDeliveryError("HTTP 429 (rate limited)")
    if status is None or status >= 400:
        raise NotificationDeliveryError(f"HTTP {status}")


# --- orchestration ------------------------------------------------------------


@dataclass
class NotifyResult:
    """Outcome of one notification pass (one runner cycle)."""

    sent: int = 0
    failed: int = 0
    skipped_duplicates: int = 0


def process_events(
    events,
    *,
    db_path: Path | None = None,
    dry_run: bool = False,
    webhook_url_value: str | None = None,
    session=None,
) -> NotifyResult:
    """Record new events, then deliver everything undelivered (incl. retries).

    Contract (Issue #83):

    - Called AFTER the cycle's position transitions committed; nothing here can
      roll them back, and no exception from here should stop the runner (the
      runner additionally guards the call).
    - dry-run: logs each would-be body and returns. No DB row, no HTTP, no file.
    - Duplicate suppression: ``event_key`` UNIQUE insert; already-``success``
      rows are never re-delivered (also across restarts / re-runs).
    - Failed deliveries stay in the table and are retried on later cycles until
      ``MAX_DELIVERY_ATTEMPTS``; each cycle makes at most one attempt per event.
    - Missing webhook URL: events are recorded (queued) but delivery is skipped
      with a warning.
    """
    logger = _get_logger()
    result = NotifyResult()
    events = list(events)

    if dry_run:
        for event in events:
            message = format_event_message(event.event_type, event.position)
            logger.info(
                "[DRY_RUN] would notify Paper Execution %s (%s):\n%s",
                event.event_type, event.position_id or "<no id>", message,
            )
        return result

    db_path = Path(db_path) if db_path is not None else store.position_db_path()

    # Phase 1: record this cycle's events (idempotent; duplicates suppressed).
    for event in events:
        try:
            inserted = store.record_notification_event(
                event.position_id, event.event_type, db_path=db_path
            )
        except Exception as exc:
            logger.error(
                "Paper Execution notification: recording %s for %s failed: %s",
                event.event_type, event.position_id, exc,
            )
            result.failed += 1
            continue
        if not inserted:
            result.skipped_duplicates += 1

    # Phase 2: deliver everything undelivered -- this cycle's rows plus failed
    # rows from earlier cycles (retry), capped at MAX_DELIVERY_ATTEMPTS.
    try:
        rows = store.list_undelivered_notifications(db_path=db_path, max_attempts=MAX_DELIVERY_ATTEMPTS)
    except Exception as exc:
        logger.error("Paper Execution notification: listing undelivered rows failed: %s", exc)
        result.failed += 1
        return result
    if not rows:
        return result

    url = webhook_url_value if webhook_url_value is not None else webhook_url()
    if not url:
        logger.warning(
            "%s is not set; %d Paper Execution notification(s) stay queued (delivery skipped).",
            WEBHOOK_ENV_VAR, len(rows),
        )
        return result

    for row in rows:
        event_key = row["event_key"]
        try:
            position = store.get_position(row["position_id"], db_path=db_path)
            if position is None:
                # Unbuildable message: count the attempt so it cannot retry forever.
                store.mark_notification_delivery(
                    event_key, success=False, error="position row not found", db_path=db_path
                )
                logger.error(
                    "Paper Execution notification %s failed: position row not found.", event_key
                )
                result.failed += 1
                continue
            payload = build_discord_payload(format_event_message(row["event_type"], position))
            deliver_payload(payload, webhook_url=url, session=session)
        except NotificationDeliveryError as exc:
            store.mark_notification_delivery(event_key, success=False, error=str(exc), db_path=db_path)
            logger.error(
                "Paper Execution notification %s failed (attempt %d/%d): %s",
                event_key, row["attempt_count"] + 1, MAX_DELIVERY_ATTEMPTS, exc,
            )
            result.failed += 1
            continue
        except Exception as exc:
            # Formatting / bookkeeping bug: never let it kill the loop or the runner.
            logger.error("Paper Execution notification %s failed: %s", event_key, exc)
            result.failed += 1
            continue
        store.mark_notification_delivery(event_key, success=True, db_path=db_path)
        logger.info("Paper Execution notification sent: %s", event_key)
        result.sent += 1
    return result
