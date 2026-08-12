"""OANDA Live Adapter — the LIVE-ONLY Broker implementation (Issue #82).

*** A LIVE ADAPTER CANNOT EXIST UNLESS THE ENVIRONMENT ITSELF SAYS SO. ***

Issue #82 protects the live connection with more than one lock, so that no
single mistyped value can reach a production account. This module enforces
locks 2-5; locks 1, 6 and 7 belong to the execution layer:

1. ``execution_mode=live``  -- execution layer (this adapter reports the
   immutable identity ``live`` / ``oanda-live``, and
   ``execution.safety_gate.BROKER_TYPE_BY_MODE`` binds the ``live`` mode to
   THIS class alone, so no other broker can claim it and this one can claim
   nothing else);
2. ``ENABLE_LIVE_TRADING=true``  -- :class:`LiveActivation`;
3. an explicit ``--confirm-live`` startup argument  -- :class:`LiveActivation`;
4. LIVE credentials (account id + token)  -- :class:`OandaLiveConfig`;
5. the LIVE API endpoint, explicitly configured  -- :class:`OandaLiveConfig`;
6. a Kill Switch that is READABLE and OFF  -- ``execution/live_lock.py``;
7. a loadable safety policy that allows the live target  -- same composer.

Locks 2-5 are ISSUANCE preconditions, not merely constructor arguments
(PR #128 Codex review r3): :class:`LiveActivation` and
:class:`OandaLiveConfig` carry a module-private issuance witness that only
their ``from_environment`` / ``from_env`` issuers supply, and those issuers
read the REAL process environment. Writing ``LiveActivation("true", True)``
by hand does not produce a grant, so an adapter instance means the process
environment genuinely carried the flag, the credentials and the endpoint.

What an adapter instance does NOT prove is locks 6 and 7: the Kill Switch
and the safety policy are checked by the composer
(``execution/live_lock.py``), which is the only production site allowed to
build this class (pinned by ``execution/tests/test_submission_boundary.py``).
Every order still crosses the ONE sanctioned submission boundary
(``execution/live_submission.py``) through the Safety Gate -- this module
adds no order path of its own and weakens nothing in the Broker contract.

Demo / Live separation (Issue #82):

- The credentials are DISJOINT environment variables (``OANDA_LIVE_*``),
  never the demo ones, and :meth:`OandaLiveConfig.from_env` refuses an
  environment where a live credential repeats the demo value -- an
  ambiguously shared credential is a configuration error, not a shortcut.
- The endpoint allowlist is exact and one-directional: this adapter accepts
  ONLY the live endpoint (a demo host marker is refused first), exactly as
  the demo adapter accepts only the demo endpoint. Neither can be pointed
  at the other's endpoint by configuration.
- Unlike the demo endpoint, the live endpoint has NO default: it must be
  configured explicitly (``OANDA_LIVE_API_URL``), so "endpoint missing"
  fails closed instead of silently resolving to a live host.

The OANDA v20 translation body is the shared, already-reviewed engine in
``broker/oanda_v20_adapter.py`` (secrets handling, error taxonomy, reissue
handling, "never resend on UNKNOWN"). This module adds no wire behaviour.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from .oanda_v20_adapter import (
    DEFAULT_TIMEOUT_SECONDS,
    OANDA_DEMO_HOST_MARKER,
    OandaV20Adapter,
    require_non_empty_env,
    require_positive_timeout,
    timeout_seconds_from_env,
)

__all__ = [
    "ENABLE_LIVE_TRADING_TRUE",
    "ENV_ENABLE_LIVE_TRADING",
    "ENV_LIVE_ACCOUNT_ID",
    "ENV_LIVE_API_TOKEN",
    "ENV_LIVE_API_URL",
    "ENV_LIVE_HTTP_TIMEOUT_SECONDS",
    "LiveActivation",
    "LiveActivationError",
    "OANDA_LIVE_API_URL",
    "OANDA_LIVE_CONNECTION_TARGET",
    "OandaLiveAdapter",
    "OandaLiveConfig",
]

# The ONLY endpoint this adapter will ever talk to (fail-closed allowlist,
# the mirror image of the demo adapter's).
OANDA_LIVE_API_URL = "https://api-fxtrade.oanda.com"

# The connection-identity target label (Issue #75 contract): a constant,
# NON-SECRET label — never the endpoint URL.
OANDA_LIVE_CONNECTION_TARGET = "oanda-live"

# LIVE-ONLY environment variable names. They deliberately do NOT overlap
# the demo adapter's (``OANDA_API_TOKEN`` / ``OANDA_ACCOUNT_ID`` /
# ``OANDA_API_URL``): one process environment can hold demo credentials,
# live credentials, or both, and a demo run can never pick up a live value
# by accident. Values live in the process environment only — never in the
# repository, never in a log, never in an exception.
ENV_LIVE_API_TOKEN = "OANDA_LIVE_API_TOKEN"
ENV_LIVE_ACCOUNT_ID = "OANDA_LIVE_ACCOUNT_ID"
ENV_LIVE_API_URL = "OANDA_LIVE_API_URL"
ENV_LIVE_HTTP_TIMEOUT_SECONDS = "OANDA_LIVE_HTTP_TIMEOUT_SECONDS"

# The master live switch and the ONE value that enables it.
ENV_ENABLE_LIVE_TRADING = "ENABLE_LIVE_TRADING"
ENABLE_LIVE_TRADING_TRUE = "true"

# The demo adapter's variable names, needed here ONLY to detect an
# ambiguously shared credential. They are compared, never read for use.
_DEMO_ENV_API_TOKEN = "OANDA_API_TOKEN"
_DEMO_ENV_ACCOUNT_ID = "OANDA_ACCOUNT_ID"


class LiveActivationError(ValueError):
    """A live-activation lock is not satisfied (always fail-closed)."""


# ISSUANCE WITNESS (PR #128 Codex review r3 P1). Evidence that a check ran
# must not be constructible from plain field values: a caller could
# otherwise write ``LiveActivation("true", True)`` in an empty process and
# get an object that CLAIMS the environment was checked. This module-private
# sentinel is passed only by the ``from_environment`` / ``from_env``
# issuers, which read the REAL process environment; nothing outside this
# module can name it without deliberately reaching into a private symbol,
# and no combination of public arguments produces one.
_ISSUANCE_WITNESS = object()


def _require_issued(witness, what: str) -> None:
    if witness is not _ISSUANCE_WITNESS:
        raise LiveActivationError(
            f"{what} must be ISSUED by reading the real process environment "
            f"(use the from_environment / from_env constructor); an object built from "
            "supplied field values is not evidence that anything was checked"
        )


@dataclass(frozen=True)
class LiveActivation:
    """Evidence that BOTH out-of-band live locks were satisfied.

    ISSUED, NOT CONSTRUCTED (PR #128 Codex review r3 P1): the first field is
    a module-private issuance witness that only :meth:`from_environment`
    supplies, and that method reads ``os.environ`` itself. A caller cannot
    hand over ``"true"`` and ``True`` as ordinary values to manufacture a
    grant in a process whose environment never had the flag set.

    ``enable_live_trading_value`` is the RAW ``ENABLE_LIVE_TRADING`` value
    as it was found in the process environment (kept raw on purpose: a
    caller cannot launder ``"yes"`` / ``"1"`` / ``"TRUE"`` into a boolean
    somewhere upstream — only the exact lowercase ``"true"`` activates),
    and ``confirm_live`` is the explicit startup argument
    (``--confirm-live``), which must be exactly ``True``.

    The grant covers locks 2 and 3 ONLY. It is NOT a statement that the
    Kill Switch was read or that a safety policy was loaded -- those are
    the composer's checks (``execution/live_lock.py``).
    """

    issuance_witness: object = field(repr=False)
    enable_live_trading_value: str | None = field(repr=False)
    confirm_live: bool = False

    def __post_init__(self) -> None:
        _require_issued(self.issuance_witness, "a LiveActivation grant")
        if self.confirm_live is not True:
            raise LiveActivationError(
                "live trading requires the explicit --confirm-live startup argument; "
                "refusing to build a live connection"
            )
        if self.enable_live_trading_value != ENABLE_LIVE_TRADING_TRUE:
            # Names the VARIABLE and the required literal, never the value
            # that was actually found (fail-closed, and nothing to leak).
            raise LiveActivationError(
                f"live trading requires the environment variable {ENV_ENABLE_LIVE_TRADING} "
                f"to be exactly {ENABLE_LIVE_TRADING_TRUE!r}; refusing to build a live "
                "connection"
            )

    @classmethod
    def from_environment(cls, *, confirm_live: bool) -> "LiveActivation":
        """The ONLY way to obtain the grant: read ``ENABLE_LIVE_TRADING``
        from the REAL process environment and combine it with the explicit
        ``--confirm-live`` startup argument.

        There is deliberately no ``environ`` parameter (PR #128 Codex review
        r3 P1): a caller-supplied mapping would be exactly the "freely
        constructed evidence" this issuance path exists to prevent. Tests
        set the real environment (``monkeypatch.setenv``) like production
        does.
        """
        return cls(
            issuance_witness=_ISSUANCE_WITNESS,
            enable_live_trading_value=os.environ.get(ENV_ENABLE_LIVE_TRADING),
            confirm_live=confirm_live,
        )


@dataclass(frozen=True)
class OandaLiveConfig:
    """Construction-time configuration for :class:`OandaLiveAdapter`.

    ``api_token`` and ``account_id`` are secrets: excluded from ``repr()``
    and never included in exception messages or logs. The endpoint is
    excluded from ``repr()`` as well and validated against the LIVE
    allowlist here, so a demo endpoint (or any other host) can never reach
    the live adapter — the mirror image of the demo config's allowlist.
    """

    issuance_witness: object = field(repr=False)
    api_token: str = field(repr=False)
    account_id: str = field(repr=False)
    endpoint: str = field(repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        # ISSUED, NOT CONSTRUCTED (PR #128 Codex review r3 P1): a
        # hand-built config would also skip the demo/live credential
        # separation check, which only ``from_env`` can perform because only
        # it sees the whole environment.
        _require_issued(self.issuance_witness, "an OandaLiveConfig")
        if not isinstance(self.api_token, str) or not self.api_token.strip():
            raise LiveActivationError(
                f"live api_token must be a non-empty string (set {ENV_LIVE_API_TOKEN})"
            )
        if not isinstance(self.account_id, str) or not self.account_id.strip():
            raise LiveActivationError(
                f"live account_id must be a non-empty string (set {ENV_LIVE_ACCOUNT_ID})"
            )
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise LiveActivationError(
                f"live endpoint must be a non-empty string (set {ENV_LIVE_API_URL})"
            )
        # Rejection messages are FIXED TEXT: neither the configured value
        # nor any endpoint URL is ever echoed into the exception.
        normalized = self.endpoint.strip().rstrip("/")
        if OANDA_DEMO_HOST_MARKER in normalized.lower():
            raise LiveActivationError(
                "OANDA Live endpoint configuration is invalid: a demo endpoint "
                "was configured for a live connection; refusing to start"
            )
        if normalized != OANDA_LIVE_API_URL:
            raise LiveActivationError(
                "OANDA Live endpoint configuration is invalid: only the live "
                "endpoint is allowed; refusing to start"
            )
        object.__setattr__(self, "endpoint", normalized)
        require_positive_timeout(self.timeout_seconds)

    @classmethod
    def from_env(cls) -> "OandaLiveConfig":
        """ISSUE the live config from LIVE-only environment variables.

        ``OANDA_LIVE_API_TOKEN`` / ``OANDA_LIVE_ACCOUNT_ID`` /
        ``OANDA_LIVE_API_URL`` are ALL required — the live endpoint has no
        default, so a missing endpoint fails closed instead of resolving to
        a live host. ``OANDA_LIVE_HTTP_TIMEOUT_SECONDS`` optionally
        overrides the request timeout.

        An environment where a live credential REPEATS the corresponding
        demo credential is refused: sharing one token or one account id
        between demo and live makes "which account did this order hit?"
        unanswerable, which is exactly the failure Issue #82 exists to
        prevent.

        Like :meth:`LiveActivation.from_environment` there is deliberately
        no ``environ`` parameter (PR #128 Codex review r3 P1): the real
        process environment is the only source, so the config an adapter
        accepts always describes the process it is actually running in.
        """
        env = os.environ
        try:
            api_token = require_non_empty_env(env.get(ENV_LIVE_API_TOKEN), ENV_LIVE_API_TOKEN)
            account_id = require_non_empty_env(env.get(ENV_LIVE_ACCOUNT_ID), ENV_LIVE_ACCOUNT_ID)
            endpoint = require_non_empty_env(env.get(ENV_LIVE_API_URL), ENV_LIVE_API_URL)
            timeout_seconds = timeout_seconds_from_env(env, ENV_LIVE_HTTP_TIMEOUT_SECONDS)
        except ValueError as exc:
            raise LiveActivationError(str(exc)) from None
        _refuse_shared_credentials(env, api_token=api_token, account_id=account_id)
        return cls(
            issuance_witness=_ISSUANCE_WITNESS,
            api_token=api_token,
            account_id=account_id,
            endpoint=endpoint,
            timeout_seconds=timeout_seconds,
        )


def _refuse_shared_credentials(
    environ: Mapping[str, str], *, api_token: str, account_id: str
) -> None:
    """Fail closed when a live credential is also configured as the demo
    one. Only variable NAMES appear in the error, never the shared value."""
    for live_name, demo_name, live_value in (
        (ENV_LIVE_API_TOKEN, _DEMO_ENV_API_TOKEN, api_token),
        (ENV_LIVE_ACCOUNT_ID, _DEMO_ENV_ACCOUNT_ID, account_id),
    ):
        demo_value = environ.get(demo_name)
        if isinstance(demo_value, str) and demo_value.strip() == live_value:
            raise LiveActivationError(
                f"{live_name} and {demo_name} are configured with the same value; "
                "live and demo credentials must be separate, refusing to start"
            )


class OandaLiveAdapter(OandaV20Adapter):
    """Live-only OANDA v20 implementation of ``broker_interface.Broker``.

    The class body is the shared engine (``OandaV20Adapter``); what makes
    this adapter live-only — and what makes it unbuildable without the
    locks — are the four declarations below plus the MANDATORY
    :class:`LiveActivation` grant.

    The Broker contract is unchanged: five order operations plus
    ``connection_identity()``. No new order path, no bypass of the Safety
    Gate, the submission journal, the execution store's duplicate
    suppression or the Kill Switch — those all live in the execution layer
    and stay exactly where they were.
    """

    CONFIG_TYPE = OandaLiveConfig
    ENDPOINT_LABEL = "OANDA live"
    IDENTITY_MODE = "live"
    IDENTITY_TARGET = OANDA_LIVE_CONNECTION_TARGET

    def __init__(self, config, activation, *, transport=None, clock=None) -> None:
        # EXACT type, not isinstance (PR #128 Codex review P1): a
        # LiveActivation SUBCLASS can override __post_init__ and hand back a
        # "grant" that proves nothing (confirm_live False, the env flag
        # absent). A grant is only evidence if the validation that makes it
        # evidence actually ran, so only the validated class is accepted.
        # The config type is checked the same way by the engine.
        if type(activation) is not LiveActivation:
            raise LiveActivationError(
                "a live adapter requires an exact LiveActivation grant "
                "(ENABLE_LIVE_TRADING=true plus the explicit --confirm-live argument); "
                "subclasses are refused because they can skip the grant's validation, "
                f"got {type(activation).__name__}"
            )
        super().__init__(config, transport=transport, clock=clock)
        self._activation = activation

    @property
    def activation(self) -> LiveActivation:
        """The grant this adapter was built with (non-secret: it records
        only that the two out-of-band locks held)."""
        return self._activation
