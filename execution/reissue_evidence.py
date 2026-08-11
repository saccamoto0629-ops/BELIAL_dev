"""Cross-DB evidence references for intent reissue (Issue #95 / #115).

The correction basis of a ``gate_correction`` child is a Safety Gate
decision persisted in a SEPARATE DB file (``safety_gate_decisions.sqlite3``,
path swappable via ``SAFETY_GATE_DECISION_DB_PATH``). Its ``decision_id`` is
a DB-local AUTOINCREMENT: a re-created or swapped decision DB re-uses the
same numbers for different records, so a bare id can never prove that a
stored reference still points at the record it was built from. The
reference (:class:`execution_store.GateDecisionReference`) therefore embeds
the FULL decision content next to the id -- the same identity-plus-content
stance the Shadow store takes toward its decision references -- and this
module provides the two boundary helpers:

- :func:`build_gate_decision_reference` -- derive the reference from the
  decision the service layer read (id + typed ``SafetyDecision``), so the
  embedded snapshot is exactly what the decision store returned, never
  caller-assembled field by field.
- :func:`verify_gate_decision_reference` -- resolve the reference against
  the LIVE decision DB and compare EVERY content field. Any failure --
  missing DB, unreadable store, id not found for the referenced request, or
  any field mismatch -- raises
  :class:`execution_store.ExecutionEvidenceReferenceError`: an unverifiable
  reference is no evidence at all (fail-closed). Verification is read-only
  and never creates a missing DB file.

This module deliberately sits ABOVE both stores (it imports them; neither
imports it), keeping ``execution_store`` free of Safety Gate imports while
the reference shape itself stays validatable there.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from execution.execution_store import (
    ExecutionEvidenceReferenceError,
    GateDecisionReference,
    gate_decision_reference_from_json,
)
from execution.safety_gate import SafetyDecision
from execution.safety_store import (
    SafetyStoreError,
    SQLiteSafetyDecisionStore,
    safety_db_path,
)


def build_gate_decision_reference(
    decision_id: int, decision: SafetyDecision
) -> GateDecisionReference:
    """The verifiable reference for one persisted Safety Gate decision.

    ``decision_id`` is the id the decision store assigned (returned by
    ``record`` / ``list_for_request_with_ids``); ``decision`` is the typed
    row content read back from it. The reference constructor re-runs the
    correction-basis invariants (``allowed`` False, rejections non-empty).
    """
    if not isinstance(decision, SafetyDecision):
        raise ValueError(f"decision must be a SafetyDecision, got {decision!r}")
    return GateDecisionReference(
        decision_id=decision_id,
        execution_request_id=decision.execution_request_id,
        allowed=decision.allowed,
        checked_at_utc=decision.checked_at_utc,
        policy_id=decision.policy_id,
        rejections=tuple(
            (rejection.code.value, rejection.message) for rejection in decision.rejections
        ),
        execution_mode=decision.execution_mode,
        connection_target=decision.connection_target,
        account_id=decision.account_id,
        position_count=decision.position_count,
    )


def verify_gate_decision_reference(
    reference: GateDecisionReference | str, *, db_path: Path | None = None
) -> None:
    """Verify that ``reference`` still resolves to the intended decision in
    the LIVE decision DB (fail-closed).

    Accepts the typed reference or its stored JSON text
    (``gate_correction_ref_json``). Raises
    :class:`ExecutionEvidenceReferenceError` when the decision DB is
    missing or unreadable, the referenced request holds no decision with
    that id, or ANY content field differs from the embedded snapshot --
    e.g. after the decision DB was re-created and the local id now numbers
    a different record. Returns None on success; never writes and never
    creates a missing DB file.
    """
    if isinstance(reference, str):
        try:
            reference = gate_decision_reference_from_json(reference)
        except (ValueError, TypeError) as exc:
            raise ExecutionEvidenceReferenceError(
                f"gate decision reference is unreadable and therefore unverifiable: {exc}"
            ) from exc
    elif not isinstance(reference, GateDecisionReference):
        raise ValueError(
            f"reference must be a GateDecisionReference or its stored JSON, got "
            f"{reference!r}"
        )
    path = Path(db_path) if db_path is not None else safety_db_path()
    if not path.exists():
        raise ExecutionEvidenceReferenceError(
            f"gate decision reference (decision_id {reference.decision_id}) cannot be "
            f"verified: no safety decision DB at {path}"
        )
    store = SQLiteSafetyDecisionStore(path)
    try:
        decisions = store.list_for_request_with_ids(reference.execution_request_id)
    except (SafetyStoreError, sqlite3.Error) as exc:
        # A path that EXISTS but cannot be opened as a SQLite file (a
        # directory, a permission-denied file, ...) raises a raw
        # sqlite3.Error straight out of the underlying connection --
        # SQLiteSafetyDecisionStore does not wrap every open failure into
        # SafetyStoreError. Catching sqlite3.Error too keeps the documented
        # contract (every unreadable-store case is
        # ExecutionEvidenceReferenceError, never a leaked driver exception
        # a caller cannot classify) true regardless of which layer the
        # failure surfaces at (PR #121 Codex review r4 P2).
        raise ExecutionEvidenceReferenceError(
            f"gate decision reference (decision_id {reference.decision_id}) cannot be "
            f"verified: the safety decision DB at {path} is unreadable ({exc})"
        ) from exc
    stored = next(
        (
            decision
            for stored_id, decision in decisions
            if stored_id == reference.decision_id
        ),
        None,
    )
    if stored is None:
        raise ExecutionEvidenceReferenceError(
            f"gate decision reference (decision_id {reference.decision_id}) does not "
            f"resolve: request {reference.execution_request_id!r} holds no decision "
            f"with that id in the DB at {path}"
        )
    expected = build_gate_decision_reference(reference.decision_id, stored)
    if expected != reference:
        raise ExecutionEvidenceReferenceError(
            f"gate decision reference (decision_id {reference.decision_id}) does not "
            "match the stored decision content; the referenced DB record is not the "
            "intended one (was the decision DB re-created or swapped?)"
        )
