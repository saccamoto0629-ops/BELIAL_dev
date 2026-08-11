"""Composition root for the intent-reissue boundary (Issue #116, PR #122
Codex review r3).

*** WIRING ONLY. Nothing here decides, registers or submits anything: it
constructs the two production services from ONE policy object and hands
them back. ***

Why this module exists
----------------------

The clearance freshness window is enforced at TWO boundaries -- the
registration (``ReissueService``, which also passes the cutoff into the
registration transaction) and the submission
(``LiveOrderSubmissionService``, which passes it into
``mark_submitting``). A submission window WIDER than the registration one
silently authorizes evidence the registration policy already treats as
stale, and that mismatch is invisible precisely because nobody writes it
down.

Making the argument mandatory on both constructors (review r2) removed the
"forgot to pass it" failure, but two SEPARATELY CONFIGURED policies could
still be handed to the two services. :func:`build_reissue_boundary` closes
that: it takes one ``ReissuePolicy`` and is the ONLY sanctioned way to
build the pair, so both services provably enforce the same window.
``execution/tests/test_submission_boundary.py`` pins that no other
production module constructs either service directly -- the same
structural-pin stance the repository already takes toward
``Broker.submit_order``.

Residual (explicit boundary)
----------------------------

Object identity is a WITHIN-PROCESS guarantee. Registration (an operator
action) and submission (the future Live Execution runner) may eventually
run in different processes, where no object can be shared: there, the two
processes must load the policy from ONE configuration source, and this
module cannot enforce that. No such runner exists yet (Live Execution is
out of scope), so today the composition root plus its pin is the complete
enforcement. The store-side checks remain the fail-closed backstop in
every case: the registration transaction re-verifies the clearance is
current and valid, and ``mark_submitting`` re-verifies status, account
scope and freshness at the submission instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from execution.live_submission import LiveOrderSubmissionService
from execution.reissue_service import ReissuePolicy, ReissueService
from execution.safety_gate import BrokerConnection, SafetyGate, SafetyPolicy


@dataclass(frozen=True)
class ReissueBoundary:
    """The two production services built from ONE :class:`ReissuePolicy`.

    ``policy is submission.reissue_policy is registration.reissue_policy``
    holds by construction."""

    submission: LiveOrderSubmissionService
    registration: ReissueService
    policy: ReissuePolicy

    def __post_init__(self) -> None:
        # Defence in depth: the invariant this module exists for is checked
        # on the way out, not merely intended on the way in.
        if (
            self.submission.reissue_policy is not self.policy
            or self.registration.reissue_policy is not self.policy
        ):  # pragma: no cover - unreachable through build_reissue_boundary
            raise ValueError(
                "the submission and registration boundaries must enforce the SAME "
                "ReissuePolicy instance; refusing to hand back a pair whose "
                "freshness windows can diverge"
            )


def build_reissue_boundary(
    *,
    connection: BrokerConnection,
    gate: SafetyGate,
    current_policy: SafetyPolicy,
    reissue_policy: ReissuePolicy,
    decision_db_path: str | Path,
    order_db_path: str | Path,
    journal_db_path: str | Path,
    clock: Callable[[], datetime],
) -> ReissueBoundary:
    """Build the submission and registration services from ONE reissue
    policy (see the module docstring).

    ``current_policy`` is the Safety Gate policy currently applied -- the
    registration side compares its CONTENT against the policy that produced
    a correction's basis decision, so it must be the same policy the gate
    evaluates with. Everything else is the usual sealed-path wiring:
    filesystem paths only, no store objects.
    """
    if not isinstance(reissue_policy, ReissuePolicy):
        raise ValueError(
            f"reissue_policy must be a ReissuePolicy, got {reissue_policy!r}"
        )
    if not isinstance(gate, SafetyGate):
        raise ValueError(f"gate must be a SafetyGate, got {gate!r}")
    if not isinstance(current_policy, SafetyPolicy):
        raise ValueError(
            f"current_policy must be a SafetyPolicy, got {current_policy!r}"
        )
    if current_policy is not gate.policy:
        # The correction eligibility rule compares the basis decision's
        # policy snapshot against "the currently applied policy". If that
        # were a different object than the one the gate evaluates with, the
        # comparison would judge a policy nothing is actually enforcing.
        raise ValueError(
            "current_policy must be the SAME SafetyPolicy instance the gate "
            "evaluates with; otherwise the correction eligibility check compares "
            "against a policy that is not the one being enforced"
        )
    submission = LiveOrderSubmissionService(
        connection=connection,
        gate=gate,
        decision_db_path=decision_db_path,
        order_db_path=order_db_path,
        journal_db_path=journal_db_path,
        clock=clock,
        reissue_policy=reissue_policy,
    )
    registration = ReissueService(
        execution_db_path=order_db_path,
        journal_db_path=journal_db_path,
        decision_db_path=decision_db_path,
        current_policy=current_policy,
        reissue_policy=reissue_policy,
        clock=clock,
    )
    return ReissueBoundary(
        submission=submission, registration=registration, policy=reissue_policy
    )
