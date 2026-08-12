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
(PR #128 Codex review r3 / r4): :class:`LiveActivation` and
:class:`OandaLiveConfig` carry an issuance seal that only their
``from_environment`` / ``from_env`` issuers compute, and those issuers read
the REAL process environment. The seal is an HMAC over the object's own field
values under a per-process key, so evidence cannot be written by hand from
plain values, lifted from another issued object, edited through
``dataclasses.replace`` or resurrected through pickle
(see :func:`_issuance_seal`).

WHAT THE SEAL IS NOT (PR #128 Codex review r5, correcting what earlier
rounds of this docstring claimed): it is not a security boundary against
arbitrary code running INSIDE this process. ``_issuance_seal`` and its key
are module attributes, and Python has no way to hide them -- any caller that
can import and call the signer can equally read ``os.environ`` and POST to
OANDA directly, so it already holds the credentials and needs none of this.
The seal defends against the failure Issue #82 is actually about: a
mis-wired or mistaken composition producing something that LOOKS like
checked evidence. The boundaries that are enforceable, and enforced, are

- ``execution/tests/test_submission_boundary.py``, which pins production to
  exactly ONE construction site (``execution/live_lock.py``), and
- ``execution/live_submission.py``, which refuses to build a LIVE submission
  boundary unless the gate reads the DURABLE kill switch -- so a connection,
  however it was obtained, still cannot submit while the switch is on or
  unreadable.

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

import hashlib
import hmac
import os
import secrets
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


# ISSUANCE SEAL (PR #128 Codex review r3 / r4 P1).
#
# Evidence that a check ran must not be constructible from plain field
# values, and it must not be TRANSFERABLE either. A shared sentinel was not
# enough (r4): a caller who could issue one object could read the sentinel
# off it and attach it to a different, hand-built object -- e.g. take a
# ``LiveActivation`` obtained with only ``ENABLE_LIVE_TRADING=true`` and use
# its witness to stamp an ``OandaLiveConfig`` full of invented credentials
# that ``from_env`` never saw. ``dataclasses.replace`` carried it along the
# same way, letting issued credentials be swapped while skipping the
# demo/live separation check.
#
# So the seal is BOUND to what it attests: an HMAC over the issuing kind
# plus that object's own field values, keyed by a per-process secret. It is
# therefore
#
# - non-transferable: the kind is part of the input, so an activation's seal
#   never verifies as a config's, and vice versa;
# - field-bound: change any attested value (directly or through
#   ``dataclasses.replace``) and the seal no longer matches;
# - process-bound: the key is generated at import, so a seal pickled out of
#   one process is refused in another (an in-process pickle round trip that
#   preserves every value is simply the same evidence, and is accepted);
# - re-checked at CONSUMPTION: :class:`OandaLiveAdapter` verifies both seals
#   again, which is what catches an object reconstructed by a path that
#   skips ``__post_init__`` entirely (pickle).
_ISSUANCE_KEY = secrets.token_bytes(32)

_ACTIVATION_KIND = "belial.live-activation.v1"
_CONFIG_KIND = "belial.oanda-live-config.v1"


def _issuance_seal(kind: str, *attested: str) -> bytes:
    """Seal ``attested`` under ``kind``. The values are joined with a NUL
    separator so no two different field tuples can produce one message."""
    message = "\0".join((kind,) + attested).encode("utf-8")
    return hmac.new(_ISSUANCE_KEY, message, hashlib.sha256).digest()


def _verify_issuance_seal(seal, kind: str, attested: tuple, what: str) -> None:
    """Recompute the seal from the object's CURRENT values and compare in
    constant time. Any mismatch means the object was not issued, was issued
    as something else, or has been altered since."""
    expected = _issuance_seal(kind, *attested)
    if not isinstance(seal, bytes) or not hmac.compare_digest(seal, expected):
        raise LiveActivationError(
            f"{what} must be ISSUED by reading the real process environment "
            "(use the from_environment / from_env constructor). Its issuance seal is "
            "missing, belongs to a different kind of evidence, or no longer matches the "
            "values it attests to"
        )


@dataclass(frozen=True)
class LiveActivation:
    """Evidence that BOTH out-of-band live locks were satisfied.

    ISSUED, NOT CONSTRUCTED (PR #128 Codex review r3 / r4 P1): the first
    field is an issuance seal that only :meth:`from_environment` can compute,
    and that method reads ``os.environ`` itself. A caller cannot hand over
    ``"true"`` and ``True`` as ordinary values to manufacture a grant in a
    process whose environment never had the flag set, and cannot lift the
    seal off another issued object either — it is bound to the values below.

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

    issuance_seal: bytes | None = field(repr=False)
    enable_live_trading_value: str | None = field(repr=False)
    confirm_live: bool = False

    def _attested(self) -> tuple:
        return (repr(self.enable_live_trading_value), repr(self.confirm_live))

    def verify_issued(self) -> None:
        """Re-check the seal against the CURRENT field values (called again
        by the adapter at consumption, which is what catches an object
        rebuilt by a path that skips ``__post_init__``)."""
        _verify_issuance_seal(
            self.issuance_seal, _ACTIVATION_KIND, self._attested(), "a LiveActivation grant"
        )

    def __post_init__(self) -> None:
        self.verify_issued()
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
        found = os.environ.get(ENV_ENABLE_LIVE_TRADING)
        return cls(
            issuance_seal=_issuance_seal(
                _ACTIVATION_KIND, repr(found), repr(confirm_live)
            ),
            enable_live_trading_value=found,
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

    issuance_seal: bytes | None = field(repr=False)
    api_token: str = field(repr=False)
    account_id: str = field(repr=False)
    endpoint: str = field(repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def _attested(self) -> tuple:
        # The endpoint is normalized in __post_init__ AFTER verification, so
        # the seal is computed over the value as ISSUED (from_env passes the
        # already-normalized endpoint, so the two agree).
        return (
            repr(self.api_token),
            repr(self.account_id),
            repr(self.endpoint),
            repr(self.timeout_seconds),
        )

    def verify_issued(self) -> None:
        """Re-check the seal against the CURRENT field values (see
        :meth:`LiveActivation.verify_issued`)."""
        _verify_issuance_seal(
            self.issuance_seal, _CONFIG_KIND, self._attested(), "an OandaLiveConfig"
        )

    def __post_init__(self) -> None:
        # ISSUED, NOT CONSTRUCTED (PR #128 Codex review r3 / r4 P1): a
        # hand-built config would also skip the demo/live credential
        # separation check, which only ``from_env`` can perform because only
        # it sees the whole environment. The seal is bound to THESE values,
        # so a seal lifted off another issued object does not verify here.
        self.verify_issued()
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
        # Seal the values EXACTLY as the config will hold them: the endpoint
        # is normalized here so the constructor's own normalization is a
        # no-op and the seal keeps matching.
        normalized_endpoint = endpoint.strip().rstrip("/")
        return cls(
            issuance_seal=_issuance_seal(
                _CONFIG_KIND,
                repr(api_token),
                repr(account_id),
                repr(normalized_endpoint),
                repr(timeout_seconds),
            ),
            api_token=api_token,
            account_id=account_id,
            endpoint=normalized_endpoint,
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
        # Re-verify at CONSUMPTION (PR #128 Codex review r4 P1): the
        # constructors check their own seal, but an object rebuilt by a path
        # that skips __post_init__ (pickle) never runs it. Checking here means
        # the adapter refuses anything whose seal does not match the values it
        # is about to send with.
        activation.verify_issued()
        super().__init__(config, transport=transport, clock=clock)
        # The engine has now confirmed the exact config type.
        config.verify_issued()
        self._activation = activation

    @property
    def activation(self) -> LiveActivation:
        """The grant this adapter was built with (non-secret: it records
        only that the two out-of-band locks held)."""
        return self._activation
