"""The demo activation lock (Issue #132) -- THE only composer of a DEMO
``BrokerConnection``, and the startup safety summary an operator reads
before a demo runner is allowed to send anything.

*** NOTHING HERE PLACES, CANCELS OR QUERIES AN ORDER, AND CONSTRUCTION
PERFORMS NO NETWORK I/O. This module builds a connection object and refuses
to build one; every order still crosses the single sanctioned submission
boundary (``execution/live_submission.py``) through the Safety Gate. ***

Demo is not a rehearsal mode with relaxed rules
-----------------------------------------------

A demo submission is a REAL order on a REAL (practice) account over the
public internet. So this lock is the live lock's shape, not a lighter
version of it: the same fail-closed collection style, the same value-free
messages, the same sealed composition (paths in, objects never), the same
verified-OFF Kill Switch requirement. What differs is WHAT is demanded --
demo credentials, the demo endpoint, and the operator-pinned demo account --
and, deliberately, that no ``ENABLE_LIVE_TRADING`` / ``--confirm-live`` can
appear anywhere near it.

A demo connection requires ALL of the following; one missing item refuses
the whole startup:

1. ``execution_mode=demo`` -- the configured mode;
2. DEMO credentials -- ``OANDA_API_TOKEN`` / ``OANDA_ACCOUNT_ID``,
   variables the live path never reads;
3. the DEMO endpoint -- ``OANDA_API_URL``, allowlisted to exactly the
   practice URL (it defaults to that URL, so "unset" is the endpoint, but
   any other value is refused rather than corrected);
4. demo and live configuration NOT mixed in either direction -- including
   a demo process that carries ``ENABLE_LIVE_TRADING`` or ANY live
   credential variable at all;
5. the ACCOUNT identity -- the safety configuration's
   ``expected_account_reference`` must equal the digest of the configured
   ``OANDA_ACCOUNT_ID``, so a deployment cannot silently start trading a
   different demo account than the one it was reviewed for;
6. the safety policy readable AND allowing the ``oanda-demo`` connection
   target;
7. the Kill Switch readable AND off.

Bootstrapping the account pin: the digest is NOT a secret (it is the shared
non-reversible ``broker.broker_models.safe_account_reference``), and the
summary prints it even when the startup is refused. So an operator runs
``python -m execution.demo_startup_cli`` once, reads
``account (masked)``, and pins that value in the configuration.

Live is NOT unlocked by any of this
-----------------------------------

The two paths share no credential variable, no endpoint, no composer and no
safety document. This module can only produce an ``OandaDemoAdapter``, whose
construction-time allowlist refuses every non-practice endpoint (Issue #79),
and it never reads ``--confirm-live``. Real-account live operation remains
un-released (docs/product/concept.md, docs/operations/operations.md).

Secrets: the token, the account id and the endpoint URL never appear in a
decision, a summary, a rendered line, a ``repr`` or an exception message --
the same contract as the live lock, with the same shared value-free
validation body (``execution/safety_config.py``).
"""

from __future__ import annotations

import dataclasses
import enum
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from broker.broker_models import safe_account_reference
from broker.oanda_demo_adapter import (
    ENV_ACCOUNT_ID as DEMO_ENV_ACCOUNT_ID,
    ENV_API_TOKEN as DEMO_ENV_API_TOKEN,
    ENV_API_URL as DEMO_ENV_API_URL,
    OANDA_DEMO_API_URL,
    OANDA_DEMO_CONNECTION_TARGET,
    OandaDemoAdapter,
    OandaDemoConfig,
)
from broker.oanda_live_adapter import (
    ENV_ENABLE_LIVE_TRADING,
    ENV_LIVE_ACCOUNT_ID,
    ENV_LIVE_API_TOKEN,
    ENV_LIVE_API_URL,
)
from execution.safety_config import (
    SafetyConfigError,
    classify_endpoint,
    collect_credential_separation_rejections,
    configured as _configured,
    displayable_symbols as _displayable_symbols,
    load_safety_document,
    parse_safety_policy,
    read_kill_switch,
)
from execution.safety_gate import (
    BrokerConnection,
    ExecutionMode,
    SafetyPolicy,
    resolve_execution_mode,
)

#: Human-readable name of this document in every validation message.
_DEMO_CONFIG_DESCRIPTION = "demo safety configuration"

#: The demo-specific document fields.
_DEMO_TARGETS_KEY = "allowed_demo_targets"
_EXPECTED_ACCOUNT_KEY = "expected_account_reference"

#: The shape ``safe_account_reference`` produces. Pinning the SHAPE means a
#: raw account id pasted into the field is refused (unprinted) instead of
#: being compared -- and never echoed back into a summary.
_ACCOUNT_REFERENCE_PATTERN = re.compile(r"account-[0-9a-f]{12}")

#: Live-only environment variables. A demo process must not carry ANY of
#: them: sharing a value is already refused by the separation checks, but a
#: process holding both configurations at once is exactly the "one typo away
#: from a production order" deployment Issue #82 refuses to normalize.
_LIVE_ONLY_ENV_VARS = (
    ENV_ENABLE_LIVE_TRADING,
    ENV_LIVE_API_TOKEN,
    ENV_LIVE_ACCOUNT_ID,
    ENV_LIVE_API_URL,
)


class DemoSafetyConfigError(SafetyConfigError):
    """The demo safety configuration is missing, incomplete or invalid.

    Always fail-closed: a configuration that cannot be read produces NO
    policy at all, and a demo startup without a policy is refused
    (``SAFETY_POLICY_UNAVAILABLE``) rather than run on defaults.
    """


class DemoLockCode(enum.Enum):
    """Machine-readable reasons a demo startup was refused. These values are
    the PRIMARY record; rendered text is secondary."""

    EXECUTION_MODE_NOT_DEMO = "execution_mode_not_demo"
    DEMO_CREDENTIALS_MISSING = "demo_credentials_missing"
    DEMO_ENDPOINT_INVALID = "demo_endpoint_invalid"
    DEMO_LIVE_CONFIGURATION_MIXED = "demo_live_configuration_mixed"
    DEMO_ACCOUNT_MISMATCH = "demo_account_mismatch"
    KILL_SWITCH_ENABLED = "kill_switch_enabled"
    KILL_SWITCH_STATE_UNVERIFIABLE = "kill_switch_state_unverifiable"
    SAFETY_POLICY_UNAVAILABLE = "safety_policy_unavailable"
    DEMO_TARGET_NOT_ALLOWED = "demo_target_not_allowed"
    DEMO_CONNECTION_REFUSED = "demo_connection_refused"


@dataclass(frozen=True)
class DemoLockRejection:
    """One refusal. ``message`` is operator-facing and holds NO secret --
    variable names and fixed text only, never a configured value."""

    code: DemoLockCode
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, DemoLockCode):
            raise ValueError(f"code must be a DemoLockCode, got {self.code!r}")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError(f"message must be a non-empty string, got {self.message!r}")


@dataclass(frozen=True)
class DemoSafetySummary:
    """The demo startup safety summary: everything a human must be able to
    check BEFORE a demo runner sends anything, and nothing that could
    disclose a credential.

    Deliberately absent, and never to be added: the API token, the full
    account id, any endpoint / URL, any Authorization header. The endpoint
    appears as a KIND label and the account as a non-reversible digest --
    which is also what an operator copies into
    ``expected_account_reference``.
    """

    execution_mode: str
    endpoint_kind: str
    account_reference: str | None
    expected_account_reference: str | None
    allowed_symbols: tuple[str, ...]
    max_order_quantity: str | None
    max_positions_per_symbol: int | None
    max_positions_account: int | None
    kill_switch: str
    demo_confirmed: bool
    locks: tuple[tuple[str, bool], ...]
    rejection_codes: tuple[str, ...]

    def to_dict(self) -> dict:
        """JSON-able form (pure ASCII values; the CLI prints this)."""
        return {
            "execution_mode": self.execution_mode,
            "endpoint_kind": self.endpoint_kind,
            "account_reference": self.account_reference,
            "expected_account_reference": self.expected_account_reference,
            "allowed_symbols": list(self.allowed_symbols),
            "max_order_quantity": self.max_order_quantity,
            "max_positions_per_symbol": self.max_positions_per_symbol,
            "max_positions_account": self.max_positions_account,
            "kill_switch": self.kill_switch,
            "demo_confirmed": self.demo_confirmed,
            "locks": {name: satisfied for name, satisfied in self.locks},
            "rejection_codes": list(self.rejection_codes),
        }

    def render(self) -> str:
        """Human-readable block for the startup log / console."""
        lines = [
            "=== BELIAL demo safety summary (Issue #132) ===",
            f"  execution mode          : {self.execution_mode}",
            f"  endpoint kind           : {self.endpoint_kind}",
            f"  account (masked)        : {self.account_reference or 'not-configured'}",
            f"  account pinned in config: {self.expected_account_reference or 'not-configured'}",
            f"  allowed symbols         : {', '.join(self.allowed_symbols) or 'unavailable'}",
            f"  max order quantity      : {self.max_order_quantity or 'unavailable'}",
            f"  max positions / symbol  : "
            f"{'unavailable' if self.max_positions_per_symbol is None else self.max_positions_per_symbol}",
            f"  max positions / account : "
            f"{'unavailable' if self.max_positions_account is None else self.max_positions_account}",
            f"  kill switch             : {self.kill_switch}",
            "  demo locks:",
        ]
        for name, satisfied in self.locks:
            lines.append(f"    [{'x' if satisfied else ' '}] {name}")
        lines.append("  real-account LIVE trading is NOT released by a demo startup")
        if self.rejection_codes:
            lines.append(f"  refused: {', '.join(self.rejection_codes)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class DemoActivationDecision:
    """The outcome of one demo-activation evaluation.

    ``allowed`` is True only when EVERY lock held; ``rejections`` lists all
    of them otherwise (never just the first), and ``summary`` is safe to
    print in either case.
    """

    allowed: bool
    rejections: tuple[DemoLockRejection, ...]
    summary: DemoSafetySummary

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError(f"allowed must be a bool, got {self.allowed!r}")
        if not isinstance(self.rejections, tuple):
            raise ValueError("rejections must be a tuple")
        for entry in self.rejections:
            if not isinstance(entry, DemoLockRejection):
                raise ValueError(f"rejections entries must be DemoLockRejection, got {entry!r}")
        if self.allowed == bool(self.rejections):
            raise ValueError(
                "an allowed activation must have no rejections; a refused one must have "
                "at least one"
            )
        if not isinstance(self.summary, DemoSafetySummary):
            raise ValueError(f"summary must be a DemoSafetySummary, got {self.summary!r}")

    @property
    def rejection_codes(self) -> tuple[str, ...]:
        return tuple(entry.code.value for entry in self.rejections)


@dataclass(frozen=True)
class DemoRuntimeGrant:
    """What a FULLY satisfied demo activation hands to the runner
    composition root -- and the only way to obtain a demo runner.

    It carries the three things that must not be allowed to diverge from
    what this lock actually verified:

    - ``connection``: the composed demo ``BrokerConnection``;
    - ``policy``: the SafetyPolicy loaded from the verified document (so the
      gate cannot end up enforcing a different, more permissive policy than
      the one whose demo target was checked);
    - ``kill_switch_db_path``: the store the boundary's DURABLE kill-switch
      reader is built over (so a demo runner cannot be composed with an
      in-memory provider that answers "off").

    ``execution/tests/test_submission_boundary.py`` pins that no production
    module other than this one constructs a grant. As with the live
    activation grant (Issue #82 / PR #128 review r5), that pin -- not
    object-level trickery -- is the enforceable boundary: any in-process code
    able to forge a grant could equally read the credentials out of the
    environment and POST directly.
    """

    connection: BrokerConnection
    policy: SafetyPolicy
    kill_switch_db_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.connection, BrokerConnection):
            raise ValueError(
                f"connection must be a BrokerConnection, got {self.connection!r}"
            )
        if self.connection.execution_mode is not ExecutionMode.DEMO:
            raise ValueError(
                f"a demo runtime grant requires a 'demo' connection, got "
                f"{self.connection.execution_mode.value!r}"
            )
        if not isinstance(self.policy, SafetyPolicy):
            raise ValueError(f"policy must be a SafetyPolicy, got {self.policy!r}")
        if self.connection.connection_target not in self.policy.allowed_connection_targets.get(
            ExecutionMode.DEMO, frozenset()
        ):
            # Defence in depth: the evaluation already checked this, and the
            # gate checks it again per submission. A grant that carries a
            # policy which does NOT allow its own connection target would be
            # a wiring mistake that only surfaces as a rejection at the first
            # order; refuse it where it is built.
            raise ValueError(
                "the granted policy does not allow the granted demo connection target"
            )
        if not isinstance(self.kill_switch_db_path, Path):
            raise ValueError(
                "kill_switch_db_path must be a Path (the durable kill-switch store is "
                f"addressed by path, never by object), got {self.kill_switch_db_path!r}"
            )


# --- demo safety configuration --------------------------------------------------
#
# The same CLOSED-WORLD document as the live one (shared validation in
# ``execution/safety_config.py``): every field is required, unknown keys are
# an error, no permissive defaults. It holds NO credentials -- the token, the
# account id and the endpoint come from the process environment only. The two
# demo-specific fields are ``allowed_demo_targets`` and the account pin
# ``expected_account_reference`` (a NON-reversible digest, not an account id).


def parse_demo_safety_policy(raw) -> SafetyPolicy:
    """Parse one demo safety configuration document into a
    :class:`SafetyPolicy` whose ``demo`` connection targets are the
    configured ones (the mode/target check the Safety Gate already performs
    per submission)."""
    policy = parse_safety_policy(
        raw,
        mode=ExecutionMode.DEMO,
        targets_key=_DEMO_TARGETS_KEY,
        description=_DEMO_CONFIG_DESCRIPTION,
        error_cls=DemoSafetyConfigError,
        extra_keys=frozenset({_EXPECTED_ACCOUNT_KEY}),
    )
    # Validated here (not in the shared parser) because it is demo-specific.
    # The VALUE is never echoed: an operator who pastes the raw account id
    # instead of its digest must not see it in a message the CLI prints.
    parse_expected_account_reference(raw)
    return policy


def parse_expected_account_reference(raw) -> str:
    """The account pin from one demo safety document, shape-checked.

    Required and closed-world like every other field: there is no "unpinned"
    spelling, because "which demo account is this deployment allowed to
    trade?" has to have an answer before an order is sent.
    """
    if not isinstance(raw, dict):
        raise DemoSafetyConfigError(
            f"{_DEMO_CONFIG_DESCRIPTION} must be an object, got {type(raw).__name__}"
        )
    if _EXPECTED_ACCOUNT_KEY not in raw:
        raise DemoSafetyConfigError(
            f"{_DEMO_CONFIG_DESCRIPTION} field {_EXPECTED_ACCOUNT_KEY!r} is required "
            "(there are no defaults); it is the non-reversible digest of the demo "
            "account id, printed as 'account (masked)' by "
            "python -m execution.demo_startup_cli"
        )
    value = raw[_EXPECTED_ACCOUNT_KEY]
    if not isinstance(value, str) or not _ACCOUNT_REFERENCE_PATTERN.fullmatch(value.strip()):
        raise DemoSafetyConfigError(
            f"{_EXPECTED_ACCOUNT_KEY} must be an account digest of the form "
            "'account-<12 lower-case hex digits>' (a raw account id is refused and is "
            "not echoed); read it from python -m execution.demo_startup_cli"
        )
    return value.strip()


def load_demo_safety_policy(path: str | Path) -> SafetyPolicy:
    """Load the demo safety configuration from a JSON file (strict loader:
    duplicate keys, invalid UTF-8, oversized inputs and non-finite numbers
    are explicit errors). Any failure raises :class:`DemoSafetyConfigError`
    -- the caller then passes ``policy=None`` and the startup is refused."""
    return parse_demo_safety_policy(_load_demo_document(path))


def load_demo_account_pin(path: str | Path) -> str:
    """The account pin alone, for the evaluation (which needs it even when
    the rest of the document is fine)."""
    return parse_expected_account_reference(_load_demo_document(path))


def _load_demo_document(path: str | Path) -> dict:
    return load_safety_document(
        path, description=_DEMO_CONFIG_DESCRIPTION, error_cls=DemoSafetyConfigError
    )


# --- evaluation -----------------------------------------------------------------


def evaluate_demo_activation(
    *,
    execution_mode,
    environ: Mapping[str, str] | None = None,
    policy: SafetyPolicy | None,
    expected_account_reference: str | None,
    state_provider,
) -> DemoActivationDecision:
    """Evaluate EVERY demo lock and return one fail-closed decision.

    Never raises for a configuration problem -- a problem is a rejection.
    ``policy=None`` / ``expected_account_reference=None`` mean the safety
    configuration could not be loaded and are themselves refusals
    (``SAFETY_POLICY_UNAVAILABLE``), so a caller whose load failed passes
    ``None`` instead of inventing defaults.

    The evaluation is deliberately usable for a NON-demo mode too: the mode
    lock simply fails, so the same command can inspect a configuration
    without ever being able to return a connection.
    """
    env = os.environ if environ is None else environ
    rejections: list[DemoLockRejection] = []

    def reject(code: DemoLockCode, message: str) -> None:
        rejections.append(DemoLockRejection(code=code, message=message))

    # --- lock 1: execution mode ------------------------------------------
    mode: ExecutionMode | None
    try:
        mode = resolve_execution_mode(execution_mode)
    except ValueError:
        mode = None
        # ``resolve_execution_mode`` quotes the value it was given; that text
        # is not forwarded (the value-free contract): the mode arrives from
        # the command line, where an operator can mistype a credential just
        # as easily as anywhere else.
        reject(
            DemoLockCode.EXECUTION_MODE_NOT_DEMO,
            "the configured execution mode is not a known mode (the configured value is "
            f"not echoed); a demo connection requires exactly {ExecutionMode.DEMO.value!r}",
        )
    if mode is not None and mode is not ExecutionMode.DEMO:
        reject(
            DemoLockCode.EXECUTION_MODE_NOT_DEMO,
            f"execution mode is {mode.value!r}; a demo connection requires "
            f"{ExecutionMode.DEMO.value!r}",
        )
    mode_is_demo = mode is ExecutionMode.DEMO

    # --- lock 2: demo credentials ----------------------------------------
    missing_credentials = [
        name
        for name in (DEMO_ENV_API_TOKEN, DEMO_ENV_ACCOUNT_ID)
        if _configured(env, name) is None
    ]
    credentials_ok = not missing_credentials
    if missing_credentials:
        reject(
            DemoLockCode.DEMO_CREDENTIALS_MISSING,
            "demo credentials are missing: " + ", ".join(sorted(missing_credentials)),
        )

    # --- lock 3: the demo endpoint ---------------------------------------
    # ``OANDA_API_URL`` DEFAULTS to the practice endpoint (Issue #79), so an
    # unset variable satisfies the lock; every other value is refused rather
    # than normalized.
    configured_endpoint = _configured(env, DEMO_ENV_API_URL)
    demo_endpoint_kind = (
        classify_endpoint(OANDA_DEMO_API_URL)
        if configured_endpoint is None
        else classify_endpoint(configured_endpoint)
    )
    endpoint_ok = (
        configured_endpoint is None or configured_endpoint.rstrip("/") == OANDA_DEMO_API_URL
    )
    if not endpoint_ok:
        reject(
            DemoLockCode.DEMO_ENDPOINT_INVALID,
            f"{DEMO_ENV_API_URL} is not the allowlisted demo (practice) endpoint; the "
            "value is refused without being echoed",
        )

    # --- lock 4: demo / live separation (both directions) -----------------
    collect_credential_separation_rejections(
        mode, env, reject, mixed_code=DemoLockCode.DEMO_LIVE_CONFIGURATION_MIXED
    )
    present_live_vars = [name for name in _LIVE_ONLY_ENV_VARS if _configured(env, name) is not None]
    separation_ok = not present_live_vars
    if present_live_vars:
        reject(
            DemoLockCode.DEMO_LIVE_CONFIGURATION_MIXED,
            "a demo process must not carry live configuration at all; unset: "
            + ", ".join(sorted(present_live_vars)),
        )

    # --- lock 5: the account identity ------------------------------------
    # NOT a network call: the account this connection WILL address is the
    # configured one, so the check is local and happens long before any
    # broker contact. The gate re-checks the mode/target identity per
    # submission; this is the account half, which the gate cannot know.
    account_reference = safe_account_reference(_configured(env, DEMO_ENV_ACCOUNT_ID))
    account_ok = (
        expected_account_reference is not None
        and account_reference is not None
        and account_reference == expected_account_reference
    )
    if not account_ok and expected_account_reference is not None:
        reject(
            DemoLockCode.DEMO_ACCOUNT_MISMATCH,
            f"the configured {DEMO_ENV_ACCOUNT_ID} does not match the account pinned in "
            f"the safety configuration ({_EXPECTED_ACCOUNT_KEY}); both are compared as "
            "non-reversible digests and neither account id is echoed",
        )

    # --- safety policy ----------------------------------------------------
    if policy is None or expected_account_reference is None:
        reject(
            DemoLockCode.SAFETY_POLICY_UNAVAILABLE,
            "the demo safety configuration could not be loaded; a demo connection is "
            "never composed without verified safety limits and a pinned account",
        )
        policy = None
    elif not isinstance(policy, SafetyPolicy):
        reject(
            DemoLockCode.SAFETY_POLICY_UNAVAILABLE,
            f"policy must be a SafetyPolicy, got {type(policy).__name__}",
        )
        policy = None
    elif OANDA_DEMO_CONNECTION_TARGET not in policy.allowed_connection_targets.get(
        ExecutionMode.DEMO, frozenset()
    ):
        reject(
            DemoLockCode.DEMO_TARGET_NOT_ALLOWED,
            f"connection target {OANDA_DEMO_CONNECTION_TARGET!r} is not listed in the "
            "safety configuration's allowed_demo_targets (the configured values are not "
            "echoed)",
        )

    # --- kill switch (fail closed: unreadable is as blocking as ON) -------
    kill_switch_label = read_kill_switch(
        state_provider,
        reject,
        enabled_code=DemoLockCode.KILL_SWITCH_ENABLED,
        unverifiable_code=DemoLockCode.KILL_SWITCH_STATE_UNVERIFIABLE,
        subject="demo connection",
    ).label

    summary = _summary(
        mode=mode,
        account_reference=account_reference,
        expected_account_reference=expected_account_reference,
        policy=policy,
        endpoint_kind=demo_endpoint_kind,
        kill_switch_label=kill_switch_label,
        locks=(
            ("execution_mode=demo", mode_is_demo),
            ("demo credentials configured", credentials_ok),
            ("demo endpoint allowlisted", bool(endpoint_ok)),
            ("no live configuration present", separation_ok),
            ("demo account matches the pinned digest", account_ok),
        ),
        rejections=rejections,
    )
    return DemoActivationDecision(
        allowed=not rejections, rejections=tuple(rejections), summary=summary
    )


def _summary(
    *,
    mode: ExecutionMode | None,
    account_reference: str | None,
    expected_account_reference: str | None,
    policy: SafetyPolicy | None,
    endpoint_kind: str,
    kill_switch_label: str,
    locks: tuple[tuple[str, bool], ...],
    rejections: list[DemoLockRejection],
) -> DemoSafetySummary:
    # The summary is meant to be pasted into a ticket, so it never echoes a
    # configured value -- not even a mistyped mode (the detailed rejection
    # message, which stays on stderr, names it). The two account fields are
    # digests, which is exactly why the operator can copy one into the
    # configuration.
    mode_label = mode.value if mode is not None else "invalid"
    return DemoSafetySummary(
        execution_mode=mode_label,
        endpoint_kind=endpoint_kind,
        account_reference=account_reference,
        expected_account_reference=expected_account_reference,
        # Never a raw symbol: the strict loader already refuses anything that
        # is not instrument-shaped, and this second pass keeps a policy built
        # by some other path from printing an operator's paste.
        allowed_symbols=(
            _displayable_symbols(sorted(policy.allowed_symbols)) if policy is not None else ()
        ),
        max_order_quantity=str(policy.max_order_quantity) if policy is not None else None,
        max_positions_per_symbol=(
            policy.max_positions_per_symbol if policy is not None else None
        ),
        max_positions_account=policy.max_positions_account if policy is not None else None,
        kill_switch=kill_switch_label,
        demo_confirmed=all(satisfied for _, satisfied in locks),
        locks=locks,
        rejection_codes=tuple(entry.code.value for entry in rejections),
    )


# --- composition ----------------------------------------------------------------


def build_demo_connection(
    *,
    execution_mode,
    safety_config_path: str | Path,
    kill_switch_db_path: str | Path | None = None,
) -> tuple[DemoActivationDecision, DemoRuntimeGrant | None]:
    """THE sanctioned composer of a demo ``BrokerConnection``.

    NO TRANSPORT INJECTION (the rule PR #128 Codex review r2 P1 established
    for live). The endpoint allowlist only means something if the composed
    connection actually talks to the allowlisted endpoint: a caller-supplied
    transport would receive the demo ``Authorization`` header and could send
    it anywhere. The public composer therefore always uses the adapter's own
    single-attempt ``requests`` transport, and the injection seam lives on
    the module-private :func:`_compose_demo_connection` used by tests only.

    SEALED PROOFS. Every input this function judges is obtained HERE, from
    the real source, and cannot be injected:

    - the safety policy and the account pin are LOADED from
      ``safety_config_path``; only a filesystem path can be passed, never a
      ``SafetyPolicy`` object, so a caller cannot hand over a permissive
      policy in place of one that failed to load;
    - the kill-switch state comes from a ``PersistentKillSwitchStateProvider``
      built HERE over ``kill_switch_db_path`` (default: the packaged /
      ``KILL_SWITCH_DB_PATH`` location), so a provider that answers ``False``
      cannot be substituted for a store that is ON or unreadable;
    - the environment is ``os.environ`` itself, so a fabricated mapping
      cannot stand in for the process that is actually running.

    Returns ``(decision, grant)``. The grant is ``None`` unless every lock
    held. No order is placed here, and construction performs no network I/O.
    """
    return _compose_demo_connection(
        execution_mode=execution_mode,
        safety_config_path=safety_config_path,
        kill_switch_db_path=kill_switch_db_path,
        transport=None,
        clock=None,
    )


def _compose_demo_connection(
    *,
    execution_mode,
    safety_config_path: str | Path,
    kill_switch_db_path: str | Path | None,
    transport: Callable | None,
    clock: Callable[[], datetime] | None,
) -> tuple[DemoActivationDecision, DemoRuntimeGrant | None]:
    """Internal composition seam -- TESTS ONLY for ``transport`` / ``clock``.

    Production goes through :func:`build_demo_connection`, which passes
    ``None`` for both. The seam exists so the offline test suite can prove
    "a demo connection was composed and an order crossed the boundary"
    without a network and without a real credential; it is deliberately
    private and is NOT part of the sanctioned startup path. Every LOCK is
    evaluated here exactly as it is for production -- the seam changes how
    the endpoint is reached, never whether the startup is allowed.
    """
    # Imported here so the module stays importable without touching the
    # kill-switch store, and so the store is opened only when a demo startup
    # is actually attempted.
    from execution.kill_switch_store import (
        PersistentKillSwitchStateProvider,
        kill_switch_db_path as default_kill_switch_db_path,
    )

    policy: SafetyPolicy | None
    account_pin: str | None
    try:
        document = _load_demo_document(safety_config_path)
        policy = parse_demo_safety_policy(document)
        account_pin = parse_expected_account_reference(document)
    except DemoSafetyConfigError:
        # Fail closed: no policy at all, which the evaluation below turns
        # into SAFETY_POLICY_UNAVAILABLE.
        policy = None
        account_pin = None
    resolved_kill_switch_db = Path(
        kill_switch_db_path if kill_switch_db_path is not None else default_kill_switch_db_path()
    )
    decision = evaluate_demo_activation(
        execution_mode=execution_mode,
        environ=os.environ,
        policy=policy,
        expected_account_reference=account_pin,
        state_provider=PersistentKillSwitchStateProvider(resolved_kill_switch_db),
    )
    if not decision.allowed:
        return decision, None
    try:
        # ISSUED from the REAL process environment: ``from_env`` accepts no
        # mapping, so the adapter can only exist for the process it is
        # actually running in, and its own construction-time allowlist
        # refuses every non-practice endpoint (Issue #79).
        config = OandaDemoConfig.from_env()
        adapter = OandaDemoAdapter(config, transport=transport, clock=clock)
        grant = DemoRuntimeGrant(
            connection=BrokerConnection(broker=adapter),
            # ``policy`` is not None here: the evaluation refuses otherwise.
            policy=policy,
            kill_switch_db_path=resolved_kill_switch_db,
        )
    except Exception as exc:
        # Defense in depth: the evaluation above already passed, so this is
        # a disagreement between the checks and the construction-time
        # allowlists. Fail closed and report it as a refusal, never as a
        # partially built demo connection.
        refusal = DemoLockRejection(
            code=DemoLockCode.DEMO_CONNECTION_REFUSED,
            message=(
                f"the demo connection could not be composed ({type(exc).__name__}); "
                "refusing to continue"
            ),
        )
        summary = dataclasses.replace(
            decision.summary,
            demo_confirmed=False,
            rejection_codes=(refusal.code.value,),
        )
        return (
            DemoActivationDecision(allowed=False, rejections=(refusal,), summary=summary),
            None,
        )
    return decision, grant


__all__ = [
    "DemoActivationDecision",
    "DemoLockCode",
    "DemoLockRejection",
    "DemoRuntimeGrant",
    "DemoSafetyConfigError",
    "DemoSafetySummary",
    "build_demo_connection",
    "evaluate_demo_activation",
    "load_demo_account_pin",
    "load_demo_safety_policy",
    "parse_demo_safety_policy",
    "parse_expected_account_reference",
]
