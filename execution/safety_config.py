"""Shared broker-activation configuration primitives (Issue #132).

*** NOTHING HERE COMPOSES A CONNECTION, PLACES AN ORDER OR TOUCHES A
NETWORK. *** This module holds the value-free validation the ACTIVATION
LOCKS are built from -- ``execution/live_lock.py`` (Issue #82) and
``execution/demo_lock.py`` (Issue #132) -- so that both locks judge a
configuration with ONE implementation instead of two that can drift.

Why it exists
-------------

Issue #132 needed the demo path to apply exactly the checks the live path
already applies: the same closed-world safety document, the same endpoint
classification, the same demo/live credential separation and the same
fail-closed kill-switch reading. Copying ~250 lines of security-critical
validation into a second module would have meant two places to fix every
future finding, so the shared body was moved here VERBATIM and the two
locks now differ only in what they are locks FOR (their codes, their
summaries, and which connection they may compose).

Value-free messages (PR #128 Codex review r1/r2, kept unchanged)
----------------------------------------------------------------

The startup CLIs copy every message below into stdout and into their JSON
output, so no validation message echoes a configured VALUE, a configured
KEY, or any document content the strict JSON loader would otherwise quote.
An operator can paste a credential into any field, name a field with one,
or duplicate such a key -- none of that reaches a log. Messages carry the
schema field name (fixed vocabulary), the expected shape, and the file path
only. ``allowed_symbols`` is the one free-form value a summary prints, so it
is CONSTRAINED to an instrument shape rather than trusted.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping

from broker.broker_models import OrderSide
from broker.oanda_demo_adapter import (
    ENV_ACCOUNT_ID as DEMO_ENV_ACCOUNT_ID,
    ENV_API_TOKEN as DEMO_ENV_API_TOKEN,
    ENV_API_URL as DEMO_ENV_API_URL,
    OANDA_DEMO_API_URL,
)
from broker.oanda_live_adapter import (
    ENV_ENABLE_LIVE_TRADING,
    ENV_LIVE_ACCOUNT_ID,
    ENV_LIVE_API_TOKEN,
    ENV_LIVE_API_URL,
    OANDA_LIVE_API_URL,
)
from broker.oanda_v20_adapter import OANDA_DEMO_HOST_MARKER, OANDA_LIVE_HOST_MARKER
from execution.safety_gate import ExecutionMode, SafetyPolicy
from execution.strict_json import load_strict_json_file

# Endpoint KIND labels (non-secret): what an endpoint value IS, never the
# value itself. ``unknown`` covers every host that is neither allowlisted
# endpoint -- including a typo'd one, which must never render as "live".
ENDPOINT_KIND_LIVE = "live"
ENDPOINT_KIND_DEMO = "demo"
ENDPOINT_KIND_UNKNOWN = "unknown"
ENDPOINT_KIND_NOT_CONFIGURED = "not-configured"

# Kill switch labels for a startup summary (three states, never two: "cannot
# be read" is its own answer and is as blocking as "on").
KILL_SWITCH_ENABLED = "enabled"
KILL_SWITCH_DISABLED = "disabled"
KILL_SWITCH_UNVERIFIABLE = "unverifiable"


class SafetyConfigError(ValueError):
    """A broker safety configuration is missing, incomplete or invalid.

    Always fail-closed: a configuration that cannot be read produces NO
    policy at all, and a startup without a policy is refused rather than run
    on defaults. Each lock subclasses this so its own ``except`` clauses stay
    mode-specific.
    """


# --- the closed-world safety document ------------------------------------------
#
# Every field is required, unknown keys are an error, and there are NO
# permissive defaults -- an operator must state the order quantity ceiling,
# the tradable symbols and the position limits that will apply. The document
# holds NO credentials: tokens, account ids and endpoints come from the
# process environment only and must never be written into a repository file.

#: The fields every activation document shares. The per-mode target list
#: (``allowed_live_targets`` / ``allowed_demo_targets``) and any mode-specific
#: extra are added by the caller.
COMMON_POLICY_KEYS: frozenset[str] = frozenset(
    {
        "policy_id",
        "allowed_symbols",
        "allowed_sides",
        "max_order_quantity",
        "max_positions_per_symbol",
        "max_positions_account",
        "max_reference_age_seconds",
        "max_spread_by_symbol",
        "require_stop_loss",
    }
)

# SYMBOLS ARE THE ONE EXCEPTION to "never trust an operator value", so they
# are constrained instead of trusted (PR #128 Codex review r5 P1).
# ``allowed_symbols`` is the only free-form operator value a startup summary
# PRINTS, and "non-empty string" was weak enough that a token pasted into it
# (with the matching ``max_spread_by_symbol`` key) made a perfectly valid
# policy whose summary then echoed the secret. Symbols must look like an
# instrument -- upper case alphanumerics either side of a single underscore,
# which covers the OANDA vocabulary (``USD_JPY``, ``XAU_USD``,
# ``SPX500_USD``) while a v20 token (lowercase hex + hyphen), an account id
# (``101-004-...-001``) and a URL (``:`` and ``/``) all fail the shape and
# are refused, unprinted.
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{2,8}_[A-Z0-9]{2,8}")

# Defence in depth for a policy built somewhere other than the strict loader:
# the DISPLAY boundary refuses to print a symbol it cannot recognise.
UNPRINTABLE_SYMBOL = "<unprintable>"


def displayable_symbols(symbols) -> tuple[str, ...]:
    return tuple(
        symbol if SYMBOL_PATTERN.fullmatch(symbol) else UNPRINTABLE_SYMBOL
        for symbol in symbols
    )


def _require_symbol_shape(symbols, key: str, error_cls: type) -> list[str]:
    for symbol in symbols:
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise error_cls(
                f"{key} must each be an instrument symbol of the form BASE_QUOTE "
                "(upper-case letters or digits, 2-8 characters each side of one "
                "underscore); the configured value is not echoed"
            )
    return list(symbols)


def _require_key(mapping: dict, key: str, *, description: str, error_cls: type):
    if key not in mapping:
        raise error_cls(
            f"{description} field {key!r} is required (there are no defaults)"
        )
    return mapping[key]


def _positive_decimal(value, name: str, error_cls: type) -> Decimal:
    if not isinstance(value, str):
        raise error_cls(
            f"{name} must be a decimal string (the configured value is not echoed)"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise error_cls(
            f"{name} is not a valid decimal string (the configured value is not echoed)"
        ) from None
    if not parsed.is_finite() or parsed < 0:
        raise error_cls(
            f"{name} must be a non-negative finite decimal (the configured value is not echoed)"
        )
    return parsed


def _non_empty_str_list(mapping: dict, key: str, *, description: str, error_cls: type) -> list[str]:
    value = _require_key(mapping, key, description=description, error_cls=error_cls)
    if not isinstance(value, list) or not value:
        raise error_cls(f"{key} must be a non-empty list of strings")
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise error_cls(
                f"{key} entries must be non-empty strings (the configured values are not echoed)"
            )
    return list(value)


def _config_int(mapping: dict, key: str, *, minimum: int, description: str, error_cls: type) -> int:
    value = _require_key(mapping, key, description=description, error_cls=error_cls)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise error_cls(
            f"{key} must be an integer >= {minimum} (the configured value is not echoed)"
        )
    return value


def parse_safety_policy(
    raw,
    *,
    mode: ExecutionMode,
    targets_key: str,
    description: str,
    error_cls: type,
    extra_keys: frozenset[str] = frozenset(),
) -> SafetyPolicy:
    """Parse ONE activation safety document into a :class:`SafetyPolicy`
    whose ``mode`` connection targets are the configured ones (the
    mode/target check the Safety Gate already performs per submission).

    ``extra_keys`` are mode-specific fields the CALLER validates itself; they
    are accepted by the closed-world key check and otherwise ignored here.
    """
    allowed_keys = COMMON_POLICY_KEYS | {targets_key} | extra_keys
    if not isinstance(raw, dict):
        # A TYPE name is not operator data.
        raise error_cls(f"{description} must be an object, got {type(raw).__name__}")
    unknown = set(raw) - allowed_keys
    if unknown:
        # The unknown key NAMES are not echoed either (PR #128 Codex review
        # r2 P1): an operator can paste a token / account id / URL as a KEY
        # just as easily as as a value, and this message reaches stdout and
        # the JSON output. The count plus the allowed vocabulary is enough
        # to fix the document.
        raise error_cls(
            f"{description} has {len(unknown)} unknown field(s) "
            "(their names are not echoed); allowed fields: "
            f"{sorted(allowed_keys)}"
        )
    sides = []
    for entry in _non_empty_str_list(
        raw, "allowed_sides", description=description, error_cls=error_cls
    ):
        try:
            sides.append(OrderSide(entry))
        except ValueError:
            raise error_cls(
                "allowed_sides entries must each be one of "
                f"{sorted(item.value for item in OrderSide)} "
                "(the configured value is not echoed)"
            ) from None
    spreads_raw = _require_key(
        raw, "max_spread_by_symbol", description=description, error_cls=error_cls
    )
    if not isinstance(spreads_raw, dict) or not spreads_raw:
        raise error_cls("max_spread_by_symbol must be a non-empty object")
    # The per-symbol key is NOT interpolated into the error text: it is
    # operator-supplied data like any value (PR #128 Codex review P1), and it
    # must satisfy the instrument shape for the same reason ``allowed_symbols``
    # does -- the two key sets have to match, so a credential pasted into one
    # would otherwise have to be pasted into the other (r5 P1).
    _require_symbol_shape(spreads_raw.keys(), "max_spread_by_symbol entries", error_cls)
    spreads = {
        symbol: _positive_decimal(spread, "max_spread_by_symbol entries", error_cls)
        for symbol, spread in spreads_raw.items()
    }
    policy_id = _require_key(raw, "policy_id", description=description, error_cls=error_cls)
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise error_cls(
            "policy_id must be a non-empty string (the configured value is not echoed)"
        )
    require_stop_loss = _require_key(
        raw, "require_stop_loss", description=description, error_cls=error_cls
    )
    if not isinstance(require_stop_loss, bool):
        raise error_cls(
            "require_stop_loss must be a boolean (the configured value is not echoed)"
        )
    try:
        return SafetyPolicy(
            policy_id=policy_id,
            allowed_symbols=frozenset(
                _require_symbol_shape(
                    _non_empty_str_list(
                        raw, "allowed_symbols", description=description, error_cls=error_cls
                    ),
                    "allowed_symbols entries",
                    error_cls,
                )
            ),
            allowed_sides=frozenset(sides),
            max_order_quantity=_positive_decimal(
                _require_key(
                    raw, "max_order_quantity", description=description, error_cls=error_cls
                ),
                "max_order_quantity",
                error_cls,
            ),
            max_positions_per_symbol=_config_int(
                raw,
                "max_positions_per_symbol",
                minimum=0,
                description=description,
                error_cls=error_cls,
            ),
            max_positions_account=_config_int(
                raw,
                "max_positions_account",
                minimum=0,
                description=description,
                error_cls=error_cls,
            ),
            max_reference_age=timedelta(
                seconds=_config_int(
                    raw,
                    "max_reference_age_seconds",
                    minimum=1,
                    description=description,
                    error_cls=error_cls,
                )
            ),
            max_spread_by_symbol=spreads,
            allowed_connection_targets={
                mode: frozenset(
                    _non_empty_str_list(
                        raw, targets_key, description=description, error_cls=error_cls
                    )
                )
            },
            require_stop_loss=require_stop_loss,
        )
    except error_cls:
        raise
    except ValueError as exc:
        # SafetyPolicy's own cross-field messages quote symbols, targets and
        # limits, so they are NOT forwarded (PR #128 Codex review P1): the
        # operator gets the constraint list, and the original exception stays
        # available as ``__cause__`` for an interactive debugger only -- the
        # CLI prints ``str(error)``, never the chain.
        raise error_cls(
            f"{description} violates the safety-policy invariants (values are "
            "not echoed): max_spread_by_symbol keys must exactly cover allowed_symbols, "
            "symbols and targets must carry no surrounding whitespace, "
            "max_order_quantity and max_reference_age_seconds must be > 0, and one "
            "connection target may belong to only one execution mode"
        ) from exc


def load_safety_document(path: str | Path, *, description: str, error_cls: type) -> dict:
    """Read one activation safety document with the strict JSON loader
    (duplicate keys, invalid UTF-8, oversized inputs and non-finite numbers
    are explicit errors).

    The strict loader's own messages quote DOCUMENT CONTENT (the duplicate
    key's name, the offending field name of a ``NaN``), and this text reaches
    stdout and the JSON output through the CLIs. That is the same leak the
    value-free validation contract closes above, so the loader's message is
    NOT forwarded: the failure classes are listed instead. The PATH is kept
    -- it is the operator's own argument, printed by the CLI anyway, and it
    is what makes the error actionable.
    """
    path = Path(path)
    try:
        return load_strict_json_file(path, description=description, error_cls=error_cls)
    except error_cls as exc:
        raise error_cls(
            f"{description} {path} was rejected before parsing (the document "
            "content is not echoed): it is unreadable, oversized, not plain UTF-8, not "
            "valid JSON, has duplicate object keys, contains NaN / Infinity, or exceeds "
            "the nesting / string / array / object limits"
        ) from exc


# --- endpoint classification ----------------------------------------------------


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


def configured(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def collect_credential_separation_rejections(
    mode: ExecutionMode | None,
    environ: Mapping[str, str],
    reject: Callable[[enum.Enum, str], None],
    *,
    mixed_code: enum.Enum,
) -> None:
    """Refuse every way demo and live configuration can be mixed.

    Both directions matter, because either one makes "which account did
    this order hit?" unanswerable:

    - a LIVE run whose live endpoint slot holds a demo endpoint (or whose
      endpoint is unknown) -- refused as an invalid live endpoint by the
      caller's endpoint check, and refused again here when the demo slot is
      what carries the live host;
    - a DEMO run whose demo endpoint slot holds a LIVE endpoint;
    - the same token or the same account id configured for both;
    - a DEMO process that carries ``ENABLE_LIVE_TRADING`` at all.

    ``mixed_code`` is the caller's own machine-readable code for "demo and
    live configuration are mixed", so each lock keeps its own vocabulary.
    """
    if endpoint_host_family(environ.get(DEMO_ENV_API_URL)) == ENDPOINT_KIND_LIVE:
        reject(
            mixed_code,
            f"{DEMO_ENV_API_URL} (the DEMO endpoint variable) is configured with a live "
            "endpoint; demo and live endpoints must never be interchanged",
        )
    if endpoint_host_family(environ.get(ENV_LIVE_API_URL)) == ENDPOINT_KIND_DEMO:
        reject(
            mixed_code,
            f"{ENV_LIVE_API_URL} (the LIVE endpoint variable) is configured with a demo "
            "endpoint; demo and live endpoints must never be interchanged",
        )
    for live_name, demo_name in (
        (ENV_LIVE_API_TOKEN, DEMO_ENV_API_TOKEN),
        (ENV_LIVE_ACCOUNT_ID, DEMO_ENV_ACCOUNT_ID),
    ):
        live_value = configured(environ, live_name)
        demo_value = configured(environ, demo_name)
        if live_value is not None and live_value == demo_value:
            reject(
                mixed_code,
                f"{live_name} and {demo_name} hold the same value; live and demo "
                "credentials must be separate",
            )
    if mode is ExecutionMode.DEMO and configured(environ, ENV_ENABLE_LIVE_TRADING) is not None:
        reject(
            mixed_code,
            f"execution mode 'demo' must not be started with {ENV_ENABLE_LIVE_TRADING} set; "
            "unset it in the demo process environment",
        )


# --- kill switch ----------------------------------------------------------------


@dataclass(frozen=True)
class KillSwitchReading:
    """What a startup learned about the Kill Switch. ``label`` is the
    non-secret summary word; ``blocked`` is True unless the switch was read
    AND is off (fail closed: unreadable is as blocking as ON)."""

    label: str
    blocked: bool


def read_kill_switch(
    state_provider,
    reject: Callable[[enum.Enum, str], None],
    *,
    enabled_code: enum.Enum,
    unverifiable_code: enum.Enum,
    subject: str,
) -> KillSwitchReading:
    """Read the Kill Switch fail-closed and reject on anything but a
    verified OFF.

    ``subject`` names what is refused ("live connection" / "demo
    connection") so the operator message stays specific without echoing any
    configuration. This is the SAME rule the Safety Gate applies per
    submission, applied once more before a connection exists -- neither
    replaces the other: a switch flipped after startup is caught by the
    gate, a switch already on is caught here.
    """
    try:
        state = state_provider.is_kill_switch_enabled()
    except Exception as exc:
        reject(
            unverifiable_code,
            f"kill switch state could not be read ({type(exc).__name__}); an unverifiable "
            "kill switch never reads as OFF",
        )
        return KillSwitchReading(label=KILL_SWITCH_UNVERIFIABLE, blocked=True)
    if not isinstance(state, bool):
        reject(
            unverifiable_code,
            "kill switch provider returned a non-boolean state; refusing to treat it "
            "as OFF",
        )
        return KillSwitchReading(label=KILL_SWITCH_UNVERIFIABLE, blocked=True)
    if state:
        reject(
            enabled_code,
            f"the kill switch is enabled; no {subject} is composed and no order "
            "is submitted",
        )
        return KillSwitchReading(label=KILL_SWITCH_ENABLED, blocked=True)
    return KillSwitchReading(label=KILL_SWITCH_DISABLED, blocked=False)
