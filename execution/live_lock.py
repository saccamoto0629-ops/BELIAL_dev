"""The live activation lock (Issue #82) -- THE only composer of a LIVE
``BrokerConnection``, and the startup safety summary an operator reads
before anything is sent.

*** NOTHING HERE PLACES, CANCELS OR QUERIES AN ORDER. This module builds a
connection object and refuses to build one; every order still crosses the
single sanctioned submission boundary (``execution/live_submission.py``)
through the Safety Gate. ***

Why more than one lock (Issue #82): a deployment where one setting flips
demo into live is one typo away from a production order. So a live
connection requires ALL of the following, and a single missing item
refuses the whole startup:

1. ``execution_mode=live`` -- the configured mode;
2. ``ENABLE_LIVE_TRADING=true`` -- the master switch, exact lowercase;
3. LIVE credentials -- ``OANDA_LIVE_API_TOKEN`` / ``OANDA_LIVE_ACCOUNT_ID``,
   variables the demo path never reads;
4. the LIVE endpoint -- ``OANDA_LIVE_API_URL``, no default, allowlisted to
   exactly one URL;
5. ``--confirm-live`` -- the explicit startup argument (an operator
   action, not a file someone edited months ago).

On top of those five, this module refuses to compose a connection when:

- demo and live configuration are MIXED in either direction (a live run
  pointed at a demo endpoint, a demo run pointed at a live endpoint, or one
  credential value shared by both) -- see
  :func:`_collect_credential_separation_rejections`;
- the Kill Switch is ON, or its state cannot be verified at all (a store
  that is missing, foreign, corrupt or raising reads as UNVERIFIABLE and
  refuses -- never as "off"; the same fail-closed rule the Safety Gate
  applies per submission, applied once more before the connection exists);
- the safety policy is unavailable, or does not allow the live connection
  target.

Fail-closed style follows the Safety Gate (Issue #75): ALL violations are
collected rather than the first one, every violation carries a
machine-readable :class:`LiveLockCode`, and an unexpected exception
anywhere in the evaluation becomes a rejection instead of an allow.

Secrets: the token, the account id and the endpoint URL never appear in a
decision, a summary, a rendered line, a ``repr`` or an exception message.
The summary reports the endpoint as a KIND label (``live`` / ``demo`` /
``unknown`` / ``not-configured``), and the account as the shared
non-reversible digest ``broker.broker_models.safe_account_reference``.

The same rule covers the safety CONFIGURATION, because the CLI copies its
error text to stdout and to the JSON output (PR #128 Codex review r1/r2):
no validation message echoes a configured VALUE, a configured KEY, or any
document content the strict JSON loader would otherwise quote. An operator
can paste a credential into any field, name a field with one, or duplicate
such a key -- none of that reaches a log. Messages carry the schema field
name (fixed vocabulary), the expected shape, and the file path only.
"""

from __future__ import annotations

import dataclasses
import enum
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping

from broker.broker_models import OrderSide, safe_account_reference
from broker.oanda_demo_adapter import (
    ENV_ACCOUNT_ID as DEMO_ENV_ACCOUNT_ID,
    ENV_API_TOKEN as DEMO_ENV_API_TOKEN,
    ENV_API_URL as DEMO_ENV_API_URL,
    OANDA_DEMO_API_URL,
)
from broker.oanda_live_adapter import (
    ENABLE_LIVE_TRADING_TRUE,
    ENV_ENABLE_LIVE_TRADING,
    ENV_LIVE_ACCOUNT_ID,
    ENV_LIVE_API_TOKEN,
    ENV_LIVE_API_URL,
    LiveActivation,
    OANDA_LIVE_API_URL,
    OANDA_LIVE_CONNECTION_TARGET,
    OandaLiveAdapter,
    OandaLiveConfig,
)
from broker.oanda_v20_adapter import OANDA_DEMO_HOST_MARKER, OANDA_LIVE_HOST_MARKER
from execution.safety_gate import (
    BrokerConnection,
    ExecutionMode,
    SafetyPolicy,
    resolve_execution_mode,
)
from execution.strict_json import load_strict_json_file

# Endpoint KIND labels (non-secret): what an endpoint value IS, never the
# value itself. ``unknown`` covers every host that is neither allowlisted
# endpoint -- including a typo'd one, which must never render as "live".
ENDPOINT_KIND_LIVE = "live"
ENDPOINT_KIND_DEMO = "demo"
ENDPOINT_KIND_UNKNOWN = "unknown"
ENDPOINT_KIND_NOT_CONFIGURED = "not-configured"

# Kill switch labels for the summary (three states, never two: "cannot be
# read" is its own answer and is as blocking as "on").
KILL_SWITCH_ENABLED = "enabled"
KILL_SWITCH_DISABLED = "disabled"
KILL_SWITCH_UNVERIFIABLE = "unverifiable"


class LiveSafetyConfigError(ValueError):
    """The live safety configuration is missing, incomplete or invalid.

    Always fail-closed: a configuration that cannot be read produces NO
    policy at all, and a live startup without a policy is refused
    (``SAFETY_POLICY_UNAVAILABLE``) rather than run on defaults.
    """


class LiveLockCode(enum.Enum):
    """Machine-readable reasons a live startup was refused. These values are
    the PRIMARY record; rendered text is secondary."""

    EXECUTION_MODE_NOT_LIVE = "execution_mode_not_live"
    LIVE_TRADING_DISABLED = "live_trading_disabled"
    LIVE_CONFIRMATION_MISSING = "live_confirmation_missing"
    LIVE_CREDENTIALS_MISSING = "live_credentials_missing"
    LIVE_ENDPOINT_INVALID = "live_endpoint_invalid"
    DEMO_LIVE_CONFIGURATION_MIXED = "demo_live_configuration_mixed"
    KILL_SWITCH_ENABLED = "kill_switch_enabled"
    KILL_SWITCH_STATE_UNVERIFIABLE = "kill_switch_state_unverifiable"
    SAFETY_POLICY_UNAVAILABLE = "safety_policy_unavailable"
    LIVE_TARGET_NOT_ALLOWED = "live_target_not_allowed"
    LIVE_CONNECTION_REFUSED = "live_connection_refused"


@dataclass(frozen=True)
class LiveLockRejection:
    """One refusal. ``message`` is operator-facing and holds NO secret --
    variable names and fixed text only, never a configured value."""

    code: LiveLockCode
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, LiveLockCode):
            raise ValueError(f"code must be a LiveLockCode, got {self.code!r}")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError(f"message must be a non-empty string, got {self.message!r}")


@dataclass(frozen=True)
class LiveSafetySummary:
    """The startup safety summary (Issue #82): everything a human must be
    able to check BEFORE a live connection is used, and nothing that could
    disclose a credential.

    Deliberately absent, and never to be added: the API token, the full
    account id, any endpoint / URL, any Authorization header. The endpoint
    appears as a KIND label and the account as a non-reversible digest.
    """

    execution_mode: str
    endpoint_kind: str
    account_reference: str | None
    allowed_symbols: tuple[str, ...]
    max_order_quantity: str | None
    max_positions_per_symbol: int | None
    max_positions_account: int | None
    kill_switch: str
    live_confirmed: bool
    locks: tuple[tuple[str, bool], ...]
    rejection_codes: tuple[str, ...]

    def to_dict(self) -> dict:
        """JSON-able form (pure ASCII values; the CLI prints this)."""
        return {
            "execution_mode": self.execution_mode,
            "endpoint_kind": self.endpoint_kind,
            "account_reference": self.account_reference,
            "allowed_symbols": list(self.allowed_symbols),
            "max_order_quantity": self.max_order_quantity,
            "max_positions_per_symbol": self.max_positions_per_symbol,
            "max_positions_account": self.max_positions_account,
            "kill_switch": self.kill_switch,
            "live_confirmed": self.live_confirmed,
            "locks": {name: satisfied for name, satisfied in self.locks},
            "rejection_codes": list(self.rejection_codes),
        }

    def render(self) -> str:
        """Human-readable block for the startup log / console."""
        lines = [
            "=== BELIAL live safety summary (Issue #82) ===",
            f"  execution mode          : {self.execution_mode}",
            f"  endpoint kind           : {self.endpoint_kind}",
            f"  account (masked)        : {self.account_reference or 'not-configured'}",
            f"  allowed symbols         : {', '.join(self.allowed_symbols) or 'unavailable'}",
            f"  max order quantity      : {self.max_order_quantity or 'unavailable'}",
            f"  max positions / symbol  : "
            f"{'unavailable' if self.max_positions_per_symbol is None else self.max_positions_per_symbol}",
            f"  max positions / account : "
            f"{'unavailable' if self.max_positions_account is None else self.max_positions_account}",
            f"  kill switch             : {self.kill_switch}",
            f"  live confirmation       : {'satisfied' if self.live_confirmed else 'NOT satisfied'}",
            "  live locks:",
        ]
        for name, satisfied in self.locks:
            lines.append(f"    [{'x' if satisfied else ' '}] {name}")
        if self.rejection_codes:
            lines.append(f"  refused: {', '.join(self.rejection_codes)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class LiveActivationDecision:
    """The outcome of one live-activation evaluation.

    ``allowed`` is True only when EVERY lock held; ``rejections`` lists all
    of them otherwise (never just the first), and ``summary`` is safe to
    print in either case.
    """

    allowed: bool
    rejections: tuple[LiveLockRejection, ...]
    summary: LiveSafetySummary

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError(f"allowed must be a bool, got {self.allowed!r}")
        if not isinstance(self.rejections, tuple):
            raise ValueError("rejections must be a tuple")
        for entry in self.rejections:
            if not isinstance(entry, LiveLockRejection):
                raise ValueError(f"rejections entries must be LiveLockRejection, got {entry!r}")
        if self.allowed == bool(self.rejections):
            raise ValueError(
                "an allowed activation must have no rejections; a refused one must have "
                "at least one"
            )
        if not isinstance(self.summary, LiveSafetySummary):
            raise ValueError(f"summary must be a LiveSafetySummary, got {self.summary!r}")

    @property
    def rejection_codes(self) -> tuple[str, ...]:
        return tuple(entry.code.value for entry in self.rejections)


# --- live safety configuration -------------------------------------------------
#
# A CLOSED-WORLD document, exactly like the production shadow configuration
# (Issue #78 / PR #100): every field is required, unknown keys are an error,
# and there are NO permissive defaults -- an operator must state the order
# quantity ceiling, the tradable symbols and the position limits that will
# apply to real money. It holds NO credentials: tokens, account ids and
# endpoints come from the process environment only and must never be written
# into a repository file.

_LIVE_POLICY_KEYS = frozenset(
    {
        "policy_id",
        "allowed_symbols",
        "allowed_sides",
        "max_order_quantity",
        "max_positions_per_symbol",
        "max_positions_account",
        "max_reference_age_seconds",
        "max_spread_by_symbol",
        "allowed_live_targets",
        "require_stop_loss",
    }
)


# Every message below names the FIELD and the EXPECTED SHAPE and NEVER the
# value that was found (PR #128 Codex review P1). The CLI copies this text
# into its stdout / JSON output, so an operator who accidentally pastes a
# token, a full account id or an endpoint URL into any field must not see it
# echoed back into a log. The field NAMES are the closed-world schema keys,
# which are fixed vocabulary, not operator data.


def _require_key(mapping: dict, key: str):
    if key not in mapping:
        raise LiveSafetyConfigError(
            f"live safety configuration field {key!r} is required (there are no defaults)"
        )
    return mapping[key]


def _positive_decimal(value, name: str) -> Decimal:
    if not isinstance(value, str):
        raise LiveSafetyConfigError(
            f"{name} must be a decimal string (the configured value is not echoed)"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise LiveSafetyConfigError(
            f"{name} is not a valid decimal string (the configured value is not echoed)"
        ) from None
    if not parsed.is_finite() or parsed < 0:
        raise LiveSafetyConfigError(
            f"{name} must be a non-negative finite decimal (the configured value is not echoed)"
        )
    return parsed


def _non_empty_str_list(mapping: dict, key: str) -> list[str]:
    value = _require_key(mapping, key)
    if not isinstance(value, list) or not value:
        raise LiveSafetyConfigError(f"{key} must be a non-empty list of strings")
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise LiveSafetyConfigError(
                f"{key} entries must be non-empty strings (the configured values are not echoed)"
            )
    return list(value)


def _config_int(mapping: dict, key: str, *, minimum: int) -> int:
    value = _require_key(mapping, key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise LiveSafetyConfigError(
            f"{key} must be an integer >= {minimum} (the configured value is not echoed)"
        )
    return value


def parse_live_safety_policy(raw) -> SafetyPolicy:
    """Parse one live safety configuration document into a
    :class:`SafetyPolicy` whose ``live`` connection targets are the
    configured ones (the mode/target check the Safety Gate already
    performs per submission)."""
    if not isinstance(raw, dict):
        # A TYPE name is not operator data.
        raise LiveSafetyConfigError(
            f"live safety configuration must be an object, got {type(raw).__name__}"
        )
    unknown = set(raw) - _LIVE_POLICY_KEYS
    if unknown:
        # The unknown key NAMES are not echoed either (PR #128 Codex review
        # r2 P1): an operator can paste a token / account id / URL as a KEY
        # just as easily as as a value, and this message reaches stdout and
        # the JSON output. The count plus the allowed vocabulary is enough
        # to fix the document.
        raise LiveSafetyConfigError(
            f"live safety configuration has {len(unknown)} unknown field(s) "
            "(their names are not echoed); allowed fields: "
            f"{sorted(_LIVE_POLICY_KEYS)}"
        )
    sides = []
    for entry in _non_empty_str_list(raw, "allowed_sides"):
        try:
            sides.append(OrderSide(entry))
        except ValueError:
            raise LiveSafetyConfigError(
                "allowed_sides entries must each be one of "
                f"{sorted(item.value for item in OrderSide)} "
                "(the configured value is not echoed)"
            ) from None
    spreads_raw = _require_key(raw, "max_spread_by_symbol")
    if not isinstance(spreads_raw, dict) or not spreads_raw:
        raise LiveSafetyConfigError("max_spread_by_symbol must be a non-empty object")
    # The per-symbol key is NOT interpolated into the error text: it is
    # operator-supplied data like any value (PR #128 Codex review P1).
    spreads = {
        symbol: _positive_decimal(spread, "max_spread_by_symbol entries")
        for symbol, spread in spreads_raw.items()
    }
    policy_id = _require_key(raw, "policy_id")
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise LiveSafetyConfigError(
            "policy_id must be a non-empty string (the configured value is not echoed)"
        )
    require_stop_loss = _require_key(raw, "require_stop_loss")
    if not isinstance(require_stop_loss, bool):
        raise LiveSafetyConfigError(
            "require_stop_loss must be a boolean (the configured value is not echoed)"
        )
    try:
        return SafetyPolicy(
            policy_id=policy_id,
            allowed_symbols=frozenset(_non_empty_str_list(raw, "allowed_symbols")),
            allowed_sides=frozenset(sides),
            max_order_quantity=_positive_decimal(
                _require_key(raw, "max_order_quantity"), "max_order_quantity"
            ),
            max_positions_per_symbol=_config_int(raw, "max_positions_per_symbol", minimum=0),
            max_positions_account=_config_int(raw, "max_positions_account", minimum=0),
            max_reference_age=timedelta(
                seconds=_config_int(raw, "max_reference_age_seconds", minimum=1)
            ),
            max_spread_by_symbol=spreads,
            allowed_connection_targets={
                ExecutionMode.LIVE: frozenset(_non_empty_str_list(raw, "allowed_live_targets"))
            },
            require_stop_loss=require_stop_loss,
        )
    except LiveSafetyConfigError:
        raise
    except ValueError as exc:
        # SafetyPolicy's own cross-field messages quote symbols, targets and
        # limits, so they are NOT forwarded (PR #128 Codex review P1): the
        # operator gets the constraint list, and the original exception stays
        # available as ``__cause__`` for an interactive debugger only -- the
        # CLI prints ``str(error)``, never the chain.
        raise LiveSafetyConfigError(
            "live safety configuration violates the safety-policy invariants (values are "
            "not echoed): max_spread_by_symbol keys must exactly cover allowed_symbols, "
            "symbols and targets must carry no surrounding whitespace, "
            "max_order_quantity and max_reference_age_seconds must be > 0, and one "
            "connection target may belong to only one execution mode"
        ) from exc


def load_live_safety_policy(path: str | Path) -> SafetyPolicy:
    """Load the live safety configuration from a JSON file (strict loader:
    duplicate keys, invalid UTF-8, oversized inputs and non-finite numbers
    are explicit errors). Any failure raises :class:`LiveSafetyConfigError`
    -- the caller then passes ``policy=None`` and the startup is refused.

    The strict loader's own messages quote DOCUMENT CONTENT (the duplicate
    key's name, the offending field name of a ``NaN``), and this text
    reaches stdout and the JSON output through the CLI. That is the same
    leak the value-free validation contract closes above, so the loader's
    message is NOT forwarded: the failure classes are listed instead. The
    PATH is kept -- it is the operator's own argument, printed by the CLI
    anyway, and it is what makes the error actionable.
    """
    path = Path(path)
    try:
        raw = load_strict_json_file(
            path,
            description="live safety configuration",
            error_cls=LiveSafetyConfigError,
        )
    except LiveSafetyConfigError as exc:
        raise LiveSafetyConfigError(
            f"live safety configuration {path} was rejected before parsing (the document "
            "content is not echoed): it is unreadable, oversized, not plain UTF-8, not "
            "valid JSON, has duplicate object keys, contains NaN / Infinity, or exceeds "
            "the nesting / string / array / object limits"
        ) from exc
    return parse_live_safety_policy(raw)


def classify_endpoint(value) -> str:
    """Classify a configured endpoint into a NON-SECRET kind label.

    The value itself is never returned or echoed, and ONLY the two exactly
    allowlisted endpoints earn the ``live`` / ``demo`` labels (PR #128 Codex
    review r2 P2). A lookalike such as ``https://api-fxtrade.oanda.com.evil
    .example`` is ``unknown``: it is refused by the exact allowlist, so
    telling the operator "endpoint kind: live" in a SAFETY summary would
    describe a connection that will never be made -- misleading exactly
    where the summary has to be trustworthy.

    Suspicion is not lost by this: :func:`endpoint_host_family` keeps the
    marker-based "looks like" detection that the demo/live MIXING checks
    need, and every non-exact endpoint still fails the endpoint lock.
    """
    if not isinstance(value, str) or not value.strip():
        return ENDPOINT_KIND_NOT_CONFIGURED
    normalized = value.strip().rstrip("/")
    if normalized == OANDA_LIVE_API_URL:
        return ENDPOINT_KIND_LIVE
    if normalized == OANDA_DEMO_API_URL:
        return ENDPOINT_KIND_DEMO
    return ENDPOINT_KIND_UNKNOWN


def endpoint_host_family(value) -> str | None:
    """"Looks like" detection for the demo/live MIXING checks only.

    Returns ``live`` / ``demo`` when the host merely CONTAINS the
    corresponding OANDA marker, and ``None`` otherwise. Deliberately looser
    than :func:`classify_endpoint`: putting a live-looking host in the DEMO
    endpoint variable is a configuration mistake worth refusing even when
    the URL would be rejected by the allowlist anyway. It is NEVER used as a
    summary label, because "looks live" is not "is live".
    """
    if not isinstance(value, str) or not value.strip():
        return None
    lowered = value.strip().rstrip("/").lower()
    if OANDA_LIVE_HOST_MARKER in lowered:
        return ENDPOINT_KIND_LIVE
    if OANDA_DEMO_HOST_MARKER in lowered:
        return ENDPOINT_KIND_DEMO
    return None


def _configured(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _collect_credential_separation_rejections(
    mode: ExecutionMode | None,
    environ: Mapping[str, str],
    reject: Callable[[LiveLockCode, str], None],
) -> None:
    """Refuse every way demo and live configuration can be mixed.

    Both directions matter, because either one makes "which account did
    this order hit?" unanswerable:

    - a LIVE run whose live endpoint slot holds a demo endpoint (or whose
      endpoint is unknown) -- refused as ``LIVE_ENDPOINT_INVALID`` by the
      caller's endpoint check, and refused again here when the demo slot is
      what carries the live host;
    - a DEMO run whose demo endpoint slot holds a LIVE endpoint;
    - the same token or the same account id configured for both.
    """
    if endpoint_host_family(environ.get(DEMO_ENV_API_URL)) == ENDPOINT_KIND_LIVE:
        reject(
            LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED,
            f"{DEMO_ENV_API_URL} (the DEMO endpoint variable) is configured with a live "
            "endpoint; demo and live endpoints must never be interchanged",
        )
    if endpoint_host_family(environ.get(ENV_LIVE_API_URL)) == ENDPOINT_KIND_DEMO:
        reject(
            LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED,
            f"{ENV_LIVE_API_URL} (the LIVE endpoint variable) is configured with a demo "
            "endpoint; demo and live endpoints must never be interchanged",
        )
    for live_name, demo_name in (
        (ENV_LIVE_API_TOKEN, DEMO_ENV_API_TOKEN),
        (ENV_LIVE_ACCOUNT_ID, DEMO_ENV_ACCOUNT_ID),
    ):
        live_value = _configured(environ, live_name)
        demo_value = _configured(environ, demo_name)
        if live_value is not None and live_value == demo_value:
            reject(
                LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED,
                f"{live_name} and {demo_name} hold the same value; live and demo "
                "credentials must be separate",
            )
    if mode is ExecutionMode.DEMO and _configured(environ, ENV_ENABLE_LIVE_TRADING) is not None:
        reject(
            LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED,
            f"execution mode 'demo' must not be started with {ENV_ENABLE_LIVE_TRADING} set; "
            "unset it in the demo process environment",
        )


def evaluate_live_activation(
    *,
    execution_mode,
    confirm_live: bool,
    environ: Mapping[str, str] | None = None,
    policy: SafetyPolicy | None,
    state_provider,
) -> LiveActivationDecision:
    """Evaluate EVERY live lock and return one fail-closed decision.

    Never raises for a configuration problem -- a problem is a rejection.
    ``policy=None`` means the safety configuration could not be loaded and
    is itself a refusal (``SAFETY_POLICY_UNAVAILABLE``), so a caller whose
    policy load failed passes ``None`` instead of inventing defaults.

    The evaluation is deliberately usable for a NON-live mode too: the mode
    lock simply fails, and the demo/live mixing checks still run, so the
    same command can verify a demo configuration without ever being able to
    return a live connection.
    """
    env = os.environ if environ is None else environ
    rejections: list[LiveLockRejection] = []

    def reject(code: LiveLockCode, message: str) -> None:
        rejections.append(LiveLockRejection(code=code, message=message))

    # --- lock 1: execution mode ------------------------------------------
    mode: ExecutionMode | None
    try:
        mode = resolve_execution_mode(execution_mode)
    except ValueError:
        mode = None
        # ``resolve_execution_mode`` quotes the value it was given; that text
        # is not forwarded (PR #128 Codex review r1/r2 contract): the mode
        # arrives from the command line, where an operator can mistype a
        # credential just as easily as anywhere else.
        reject(
            LiveLockCode.EXECUTION_MODE_NOT_LIVE,
            "the configured execution mode is not a known mode (the configured value is "
            f"not echoed); a live connection requires exactly {ExecutionMode.LIVE.value!r}",
        )
    if mode is not None and mode is not ExecutionMode.LIVE:
        reject(
            LiveLockCode.EXECUTION_MODE_NOT_LIVE,
            f"execution mode is {mode.value!r}; a live connection requires "
            f"{ExecutionMode.LIVE.value!r}",
        )
    mode_is_live = mode is ExecutionMode.LIVE

    # --- lock 2: the master switch ---------------------------------------
    live_flag_raw = env.get(ENV_ENABLE_LIVE_TRADING)
    live_flag_ok = live_flag_raw == ENABLE_LIVE_TRADING_TRUE
    if not live_flag_ok:
        reject(
            LiveLockCode.LIVE_TRADING_DISABLED,
            f"{ENV_ENABLE_LIVE_TRADING} must be exactly {ENABLE_LIVE_TRADING_TRUE!r} "
            "(no other spelling enables live trading)",
        )

    # --- lock 3: the explicit startup confirmation ------------------------
    confirmed = confirm_live is True
    if not confirmed:
        reject(
            LiveLockCode.LIVE_CONFIRMATION_MISSING,
            "the explicit --confirm-live startup argument is required for a live "
            "connection",
        )

    # --- lock 4: live credentials ----------------------------------------
    missing_credentials = [
        name
        for name in (ENV_LIVE_API_TOKEN, ENV_LIVE_ACCOUNT_ID)
        if _configured(env, name) is None
    ]
    credentials_ok = not missing_credentials
    if missing_credentials:
        reject(
            LiveLockCode.LIVE_CREDENTIALS_MISSING,
            "live credentials are missing: " + ", ".join(sorted(missing_credentials)),
        )

    # --- lock 5: the live endpoint ---------------------------------------
    live_endpoint_kind = classify_endpoint(env.get(ENV_LIVE_API_URL))
    endpoint_ok = (
        _configured(env, ENV_LIVE_API_URL) or ""
    ).rstrip("/") == OANDA_LIVE_API_URL
    if not endpoint_ok:
        if live_endpoint_kind == ENDPOINT_KIND_NOT_CONFIGURED:
            reject(
                LiveLockCode.LIVE_ENDPOINT_INVALID,
                f"{ENV_LIVE_API_URL} is not configured; the live endpoint has no default",
            )
        elif endpoint_host_family(env.get(ENV_LIVE_API_URL)) == ENDPOINT_KIND_DEMO:
            reject(
                LiveLockCode.LIVE_ENDPOINT_INVALID,
                f"{ENV_LIVE_API_URL} points at a demo endpoint; a live connection "
                "accepts only the live endpoint",
            )
        else:
            reject(
                LiveLockCode.LIVE_ENDPOINT_INVALID,
                f"{ENV_LIVE_API_URL} is not the allowlisted live endpoint; the value is "
                "refused without being echoed",
            )

    # --- demo / live separation (both directions) -------------------------
    _collect_credential_separation_rejections(mode, env, reject)

    # --- safety policy ----------------------------------------------------
    if policy is None:
        reject(
            LiveLockCode.SAFETY_POLICY_UNAVAILABLE,
            "the safety policy could not be loaded; a live connection is never composed "
            "without verified safety limits",
        )
    elif not isinstance(policy, SafetyPolicy):
        reject(
            LiveLockCode.SAFETY_POLICY_UNAVAILABLE,
            f"policy must be a SafetyPolicy, got {type(policy).__name__}",
        )
        policy = None
    elif OANDA_LIVE_CONNECTION_TARGET not in policy.allowed_connection_targets.get(
        ExecutionMode.LIVE, frozenset()
    ):
        reject(
            LiveLockCode.LIVE_TARGET_NOT_ALLOWED,
            f"connection target {OANDA_LIVE_CONNECTION_TARGET!r} is not listed in the "
            "safety configuration's allowed_live_targets (the configured values are not "
            "echoed)",
        )

    # --- kill switch (fail closed: unreadable is as blocking as ON) -------
    kill_switch_label = KILL_SWITCH_UNVERIFIABLE
    try:
        kill_switch = state_provider.is_kill_switch_enabled()
    except Exception as exc:
        reject(
            LiveLockCode.KILL_SWITCH_STATE_UNVERIFIABLE,
            f"kill switch state could not be read ({type(exc).__name__}); an unverifiable "
            "kill switch never reads as OFF",
        )
    else:
        if not isinstance(kill_switch, bool):
            reject(
                LiveLockCode.KILL_SWITCH_STATE_UNVERIFIABLE,
                "kill switch provider returned a non-boolean state; refusing to treat it "
                "as OFF",
            )
        elif kill_switch:
            kill_switch_label = KILL_SWITCH_ENABLED
            reject(
                LiveLockCode.KILL_SWITCH_ENABLED,
                "the kill switch is enabled; no live connection is composed and no order "
                "is submitted",
            )
        else:
            kill_switch_label = KILL_SWITCH_DISABLED

    summary = _summary(
        mode=mode,
        environ=env,
        policy=policy,
        endpoint_kind=live_endpoint_kind,
        kill_switch_label=kill_switch_label,
        locks=(
            ("execution_mode=live", mode_is_live),
            (f"{ENV_ENABLE_LIVE_TRADING}={ENABLE_LIVE_TRADING_TRUE}", live_flag_ok),
            ("live credentials configured", credentials_ok),
            ("live endpoint allowlisted", bool(endpoint_ok)),
            ("--confirm-live", confirmed),
        ),
        rejections=rejections,
    )
    return LiveActivationDecision(
        allowed=not rejections, rejections=tuple(rejections), summary=summary
    )


def _summary(
    *,
    mode: ExecutionMode | None,
    environ: Mapping[str, str],
    policy: SafetyPolicy | None,
    endpoint_kind: str,
    kill_switch_label: str,
    locks: tuple[tuple[str, bool], ...],
    rejections: list[LiveLockRejection],
) -> LiveSafetySummary:
    account_id = _configured(environ, ENV_LIVE_ACCOUNT_ID)
    # The summary is meant to be pasted into a ticket, so it never echoes a
    # configured value -- not even a mistyped mode (the detailed rejection
    # message, which stays on stderr, names it).
    mode_label = mode.value if mode is not None else "invalid"
    return LiveSafetySummary(
        execution_mode=mode_label,
        endpoint_kind=endpoint_kind,
        # Never the id itself: the shared non-reversible digest (Issue #79 /
        # PR #101) is what every display boundary in this repository uses.
        account_reference=safe_account_reference(account_id),
        allowed_symbols=tuple(sorted(policy.allowed_symbols)) if policy is not None else (),
        max_order_quantity=str(policy.max_order_quantity) if policy is not None else None,
        max_positions_per_symbol=(
            policy.max_positions_per_symbol if policy is not None else None
        ),
        max_positions_account=policy.max_positions_account if policy is not None else None,
        kill_switch=kill_switch_label,
        live_confirmed=all(satisfied for _, satisfied in locks),
        locks=locks,
        rejection_codes=tuple(entry.code.value for entry in rejections),
    )


def build_live_connection(
    *,
    execution_mode,
    confirm_live: bool,
    safety_config_path: str | Path,
    kill_switch_db_path: str | Path | None = None,
) -> tuple[LiveActivationDecision, BrokerConnection | None]:
    """THE sanctioned composer of a live ``BrokerConnection``.

    NO TRANSPORT INJECTION (PR #128 Codex review r2 P1). The endpoint
    allowlist only means something if the composed connection actually
    talks to the allowlisted endpoint: a caller-supplied transport would
    receive the live ``Authorization`` header and could send it anywhere,
    which is precisely what pinning the URL exists to prevent. The public
    composer therefore always uses the adapter's own single-attempt
    ``requests`` transport, and the injection seam lives on the
    module-private :func:`_compose_live_connection` used by tests only.

    SEALED PROOFS (PR #128 Codex review P1). Every input this function
    judges is obtained HERE, from the real source, and cannot be injected:

    - the safety policy is LOADED from ``safety_config_path``; only a
      filesystem path can be passed, never a ``SafetyPolicy`` object, so a
      caller cannot hand over a permissive policy in place of one that
      failed to load (a load failure becomes ``SAFETY_POLICY_UNAVAILABLE``);
    - the kill-switch state comes from a ``PersistentKillSwitchStateProvider``
      built HERE over ``kill_switch_db_path`` (default: the packaged /
      ``KILL_SWITCH_DB_PATH`` location), so a provider that answers ``False``
      cannot be substituted for a store that is ON or unreadable;
    - the environment is ``os.environ`` itself, so a fabricated mapping
      cannot stand in for a process that never had ``ENABLE_LIVE_TRADING``
      set.

    This is the same sealing rule ``LiveOrderSubmissionService`` applies to
    its audit store and submission journal (Issue #75 / #93): a path can be
    configured, an object cannot be injected.
    :func:`evaluate_live_activation` remains injectable on purpose -- it
    returns a DECISION and never a connection, so injected inputs there can
    authorize nothing.

    Returns ``(decision, connection)``. The connection is ``None`` unless
    every lock held; when it is not ``None``, the adapter behind it was
    itself built under the full grant (``LiveActivation`` +
    ``OandaLiveConfig``), so the locks are enforced twice: once as a
    decision here, once as an unrepresentable object in the broker layer.

    No order is placed here. Construction performs no network I/O.
    """
    return _compose_live_connection(
        execution_mode=execution_mode,
        confirm_live=confirm_live,
        safety_config_path=safety_config_path,
        kill_switch_db_path=kill_switch_db_path,
        transport=None,
        clock=None,
    )


def _compose_live_connection(
    *,
    execution_mode,
    confirm_live: bool,
    safety_config_path: str | Path,
    kill_switch_db_path: str | Path | None,
    transport: Callable | None,
    clock: Callable[[], datetime] | None,
) -> tuple[LiveActivationDecision, BrokerConnection | None]:
    """Internal composition seam -- TESTS ONLY for ``transport`` / ``clock``.

    Production goes through :func:`build_live_connection`, which passes
    ``None`` for both. The seam exists so the offline test suite can prove
    "a live connection was composed" without a network and without a real
    credential; it is deliberately private and is NOT part of the sanctioned
    startup path. Every LOCK is evaluated here exactly as it is for
    production -- the seam changes how the endpoint is reached, never
    whether the startup is allowed.
    """
    # Imported here so the module stays importable without touching the
    # kill-switch store, and so the store is opened only when a live startup
    # is actually attempted.
    from execution.kill_switch_store import PersistentKillSwitchStateProvider

    policy: SafetyPolicy | None
    try:
        policy = load_live_safety_policy(safety_config_path)
    except LiveSafetyConfigError:
        # Fail closed: no policy at all, which the evaluation below turns
        # into SAFETY_POLICY_UNAVAILABLE.
        policy = None
    decision = evaluate_live_activation(
        execution_mode=execution_mode,
        confirm_live=confirm_live,
        environ=os.environ,
        policy=policy,
        state_provider=PersistentKillSwitchStateProvider(kill_switch_db_path),
    )
    if not decision.allowed:
        return decision, None
    env = os.environ
    try:
        config = OandaLiveConfig.from_env(env)
        activation = LiveActivation.from_environment(env, confirm_live=confirm_live)
        adapter = OandaLiveAdapter(config, activation, transport=transport, clock=clock)
        connection = BrokerConnection(broker=adapter)
    except Exception as exc:
        # Defense in depth: the evaluation above already passed, so this is
        # a disagreement between the checks and the construction-time
        # allowlists. Fail closed and report it as a refusal, never as a
        # partially built live connection.
        refusal = LiveLockRejection(
            code=LiveLockCode.LIVE_CONNECTION_REFUSED,
            message=(
                f"the live connection could not be composed ({type(exc).__name__}); "
                "refusing to continue"
            ),
        )
        summary = dataclasses.replace(
            decision.summary,
            live_confirmed=False,
            rejection_codes=(refusal.code.value,),
        )
        return (
            LiveActivationDecision(allowed=False, rejections=(refusal,), summary=summary),
            None,
        )
    return decision, connection
