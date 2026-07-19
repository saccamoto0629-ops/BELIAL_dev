"""Discord notifier for Paper Execution state transitions (Issue #83).

*** PAPER EXECUTION. VIRTUAL FILLS / VIRTUAL PNL ONLY. NEVER A LIVE ORDER. ***

Paper Signal notifications announce "a hypothesis was generated"; Paper
Execution notifications announce "that hypothesis was virtually filled /
virtually settled as time advanced". This module owns ONLY the latter and is
deliberately decoupled from ``execution_runner``:

    execution_runner --(atomic transition + outbox enqueue in position_store)-->
        paper_execution_notifications (transactional outbox, Position DB)
            --(this module: claim -> format -> HTTP -> settle)--> Discord Webhook

Separated responsibilities:

- **event persistence**: NOT here. Position transitions and their outbox rows
  commit atomically in ``position_store`` (``*_with_notification`` APIs) -- a
  transition can never exist without its outbox row, and a crash at any point
  before delivery leaves the row behind for a later cycle.
- **body / payload generation**: :func:`format_event_message` /
  :func:`build_discord_payload` are pure functions. Missing values render as a
  safe placeholder, never an exception. Every body carries
  ``Event ID: <position_id:event_type>`` so duplicates are identifiable.
- **HTTP delivery**: :func:`deliver_payload` -- timeout always set, exactly one
  attempt per call, errors classified into a sanitized
  :class:`NotificationDeliveryError` (never containing the webhook URL).
- **claim / lease**: :func:`deliver_outbox` claims at most
  ``batch_limit`` rows per cycle via ``position_store.claim_notifications``
  (atomic; single worker per row; expired leases recoverable), delivers, then
  settles each row (``success`` / ``failed`` / ``dead_letter``).
- **dry-run**: :func:`log_dry_run_events` logs the would-be bodies only. No DB
  row, no HTTP call, no file.

DELIVERY CONTRACT -- AT-LEAST-ONCE, NOT EXACTLY-ONCE: the Discord POST and the
``success`` DB update cannot be atomic. A settled (``success``) outbox row is
never re-sent; but if the process dies (or the DB write fails) AFTER a
successful POST and BEFORE the ``success`` record, the row stays claimed until
its lease expires and is then re-delivered -- the SAME Event ID appears twice
on Discord. Consumers identify duplicates by the ``Event ID`` line.

Failure classification (see ``CycleResult``):
- outbox persistence / claim / settle DB failures -> data-plane ``failures``
  (the notification guarantee itself is broken; ``--once`` exits non-zero).
- HTTP delivery failures (timeout / 429 / 5xx / connection error) and the
  success-record failure above -> ``notification_failures`` (positions stay
  consistent; retried automatically).

Configuration: ``PAPER_EXECUTION_DISCORD_WEBHOOK_URL`` in
``paper-execution/.env`` (or the process environment). Deliberately a separate
variable from Paper Signal's ``DISCORD_WEBHOOK_URL``. When unset, outbox rows
keep accumulating (attempt_count untouched, nothing claimed) and are delivered
-- ``batch_limit`` per cycle -- once the URL is configured.
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
DELIVERY_BATCH_LIMIT = 20          # max HTTP deliveries per cycle (backlog drain cap)
DEFAULT_LEASE_SECONDS = 600        # processing-claim lease; expired claims are recoverable
DISCORD_CONTENT_LIMIT = 2000

PAPER_EXECUTION_NOTE = "Paper Execution only. 仮想約定・仮想損益の記録であり、実注文は行っていません。"
MISSING_VALUE = "-"

_logger_name = "paper_execution"


def _get_logger() -> logging.Logger:
    # Shares the runner's logger by name: the runner configures handlers
    # (console-only on dry run); this module never adds its own file handler.
    return logging.getLogger(_logger_name)


def default_worker_id() -> str:
    """Claim owner id for this process (host + pid: unique enough per lease window)."""
    try:
        host = os.uname().nodename
    except AttributeError:  # pragma: no cover - non-POSIX
        host = "local"
    return f"{host}:pid-{os.getpid()}"


@dataclass(frozen=True)
class ExecutionEvent:
    """DTO for dry-run logging: one would-be state transition and its projected record.

    On a NORMAL run events are never carried in memory -- the outbox row is
    committed atomically with the transition in ``position_store`` and delivery
    reads back from the DB. This DTO only feeds :func:`log_dry_run_events`.
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


def _event_id_line(event_type: str, position: dict) -> str:
    """``Event ID: <position_id:event_type>`` -- the at-least-once duplicate marker.

    Matches the outbox ``event_key`` exactly, so a duplicated Discord post
    (possible when the success record fails after a successful POST) is
    identifiable as the same underlying event.
    """
    return f"Event ID: {_display(position.get('position_id'))}:{event_type}"


def format_open_message(position: dict) -> str:
    """pending -> open: the virtual fill notification body."""
    return "\n".join(
        [
            "\U0001f4c8 Paper Execution OPEN — 仮想約定 (virtual fill)",
            "",
            _event_id_line(EVENT_OPEN, position),
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
            _event_id_line(EVENT_CLOSED, position),
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
            _event_id_line(event_type, position),
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


# --- dry-run ------------------------------------------------------------------


def log_dry_run_events(events) -> None:
    """Log the would-be notification bodies. NO side effects: no DB, no HTTP, no file."""
    logger = _get_logger()
    for event in events:
        message = format_event_message(event.event_type, event.position)
        logger.info(
            "[DRY_RUN] would notify Paper Execution %s (%s):\n%s",
            event.event_type, event.position_id or "<no id>", message,
        )


# --- delivery orchestration ---------------------------------------------------


@dataclass
class DeliveryResult:
    """Outcome of one outbox delivery pass (one runner cycle).

    ``outbox_failures`` are DB/bookkeeping failures (claim, settle, reads) --
    the notification guarantee itself is at risk, so the runner counts them as
    data-plane ``failures``. ``delivery_failures`` are HTTP-side failures plus
    the success-record failure (positions stay consistent; retried
    automatically). ``dead_letter_total`` is the CURRENT number of unresolved
    dead-letter rows (a gauge for continuous monitoring, not a per-cycle count).
    """

    sent: int = 0
    delivery_failures: int = 0
    outbox_failures: int = 0
    dead_letter_total: int = 0


def deliver_outbox(
    *,
    db_path: Path | None = None,
    webhook_url_value: str | None = None,
    session=None,
    worker_id: str | None = None,
    batch_limit: int = DELIVERY_BATCH_LIMIT,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    max_attempts: int = MAX_DELIVERY_ATTEMPTS,
    now_utc: str | None = None,
) -> DeliveryResult:
    """Claim and deliver queued outbox rows; settle each attempt. At-least-once.

    Contract (Issue #83):

    - Runs AFTER the cycle's transitions (each already committed atomically with
      its outbox row); nothing here can roll a position back, and no exception
      from here should stop the runner (the runner additionally guards the call).
    - Claims at most ``batch_limit`` rows (atomic claim + lease -- one worker per
      row; expired leases from crashed workers become claimable again).
    - Settled ``success`` rows are never re-claimed. Failures retry on later
      cycles until ``max_attempts``, then move to ``dead_letter`` (kept visible;
      the per-cycle dead-letter gauge is always reported/logged).
    - Missing webhook URL: nothing is claimed, no attempt is consumed; rows keep
      accumulating and drain (``batch_limit`` per cycle) once the URL is set.
    - A successful POST whose ``success`` record then fails is logged loudly and
      counted as a delivery failure: the row will be re-delivered after its
      lease expires (duplicate on Discord, same Event ID -- at-least-once).
    """
    logger = _get_logger()
    result = DeliveryResult()
    db_path = Path(db_path) if db_path is not None else store.position_db_path()
    worker = worker_id or default_worker_id()

    # Pre-delivery dead-letter gauge: monitored EVERY cycle, webhook set or not,
    # so exhausted notifications can never silently disappear from view. When a
    # webhook is configured this is refreshed after delivery (below) so the
    # returned gauge reflects same-cycle dead-letter transitions.
    try:
        counts = store.count_notifications_by_status(db_path=db_path)
    except Exception as exc:
        logger.error("Paper Execution outbox: status read failed: %s", exc)
        result.outbox_failures += 1
        return result
    result.dead_letter_total = counts.get(store.DELIVERY_STATUS_DEAD_LETTER, 0)
    if result.dead_letter_total:
        logger.warning(
            "Paper Execution outbox: %d dead-letter notification(s) awaiting manual recovery "
            "(see docs/operations/operations.md).",
            result.dead_letter_total,
        )

    url = webhook_url_value if webhook_url_value is not None else webhook_url()
    if not url:
        queued = sum(
            counts.get(status, 0)
            for status in (
                store.DELIVERY_STATUS_PENDING,
                store.DELIVERY_STATUS_FAILED,
                store.DELIVERY_STATUS_PROCESSING,
            )
        )
        if queued:
            logger.warning(
                "%s is not set; %d Paper Execution notification(s) stay queued "
                "(nothing claimed, no attempt consumed).",
                WEBHOOK_ENV_VAR, queued,
            )
        return result

    try:
        claimed = store.claim_notifications(
            worker_id=worker, lease_seconds=lease_seconds, batch_limit=batch_limit,
            max_attempts=max_attempts, now_utc=now_utc, db_path=db_path,
        )
    except Exception as exc:
        logger.error("Paper Execution outbox: claim failed: %s", exc)
        result.outbox_failures += 1
        return result

    for row in claimed:
        event_key = row["event_key"]
        try:
            position = store.get_position(row["position_id"], db_path=db_path)
        except Exception as exc:
            # DB read failure: leave the row processing; the lease will expire
            # and a later cycle retries. Outbox-side failure (data plane).
            logger.error("Paper Execution outbox: position read failed for %s: %s", event_key, exc)
            result.outbox_failures += 1
            continue
        if position is None:
            _settle(row, worker, success=False, error="position row not found",
                    max_attempts=max_attempts, db_path=db_path, result=result, logger=logger)
            result.delivery_failures += 1
            continue

        try:
            payload = build_discord_payload(format_event_message(row["event_type"], position))
            deliver_payload(payload, webhook_url=url, session=session)
        except NotificationDeliveryError as exc:
            _settle(row, worker, success=False, error=str(exc),
                    max_attempts=max_attempts, db_path=db_path, result=result, logger=logger)
            logger.error(
                "Paper Execution notification %s failed (attempt %d/%d): %s",
                event_key, row["attempt_count"] + 1, max_attempts, exc,
            )
            result.delivery_failures += 1
            continue
        except Exception as exc:
            # Formatting/bookkeeping bug: never let it kill the loop or the runner.
            logger.error("Paper Execution notification %s failed: %s", event_key, exc)
            _settle(row, worker, success=False, error=f"internal error: {type(exc).__name__}",
                    max_attempts=max_attempts, db_path=db_path, result=result, logger=logger)
            result.delivery_failures += 1
            continue

        # HTTP POST succeeded -> record success. NOT atomic with the POST:
        try:
            store.finish_notification_delivery(
                event_key, worker_id=worker, success=True, max_attempts=max_attempts, db_path=db_path,
            )
        except Exception as exc:
            # AT-LEAST-ONCE window: Discord accepted the message but the success
            # record failed. The row stays claimed until the lease expires, then
            # re-delivers -- the SAME Event ID will appear twice on Discord.
            logger.error(
                "Paper Execution notification %s: Discord POST succeeded but the success record "
                "FAILED (%s). The row will be re-delivered after its lease expires -- a DUPLICATE "
                "Discord message with the same Event ID is possible (at-least-once contract).",
                event_key, exc,
            )
            result.delivery_failures += 1
            continue
        logger.info("Paper Execution notification sent: %s", event_key)
        result.sent += 1

    # Same-cycle dead-letter gauge (Issue #83 review): recount AFTER delivery so
    # rows that reached the attempt cap in THIS cycle (or were reconciled to
    # dead_letter at claim time) are reflected now, not only next cycle.
    try:
        final_counts = store.count_notifications_by_status(db_path=db_path)
    except Exception as exc:
        logger.error("Paper Execution outbox: post-delivery status read failed: %s", exc)
        result.outbox_failures += 1
        return result
    final_dead_letter = final_counts.get(store.DELIVERY_STATUS_DEAD_LETTER, 0)
    if final_dead_letter > result.dead_letter_total:
        logger.warning(
            "Paper Execution outbox: %d dead-letter notification(s) after this cycle "
            "(+%d newly exhausted); manual recovery required (see docs/operations/operations.md).",
            final_dead_letter, final_dead_letter - result.dead_letter_total,
        )
    result.dead_letter_total = final_dead_letter
    return result


def _settle(row, worker, *, success, error, max_attempts, db_path, result, logger) -> None:
    """Settle one claimed row's failed attempt; escalate settle failures to outbox failures."""
    try:
        finished = store.finish_notification_delivery(
            row["event_key"], worker_id=worker, success=success, error=error,
            max_attempts=max_attempts, db_path=db_path,
        )
        if finished["delivery_status"] == store.DELIVERY_STATUS_DEAD_LETTER:
            logger.error(
                "Paper Execution notification %s moved to dead_letter after %d attempt(s); "
                "manual recovery required (see docs/operations/operations.md).",
                row["event_key"], finished["attempt_count"],
            )
    except Exception as exc:
        logger.error("Paper Execution outbox: settling %s failed: %s", row["event_key"], exc)
        result.outbox_failures += 1
