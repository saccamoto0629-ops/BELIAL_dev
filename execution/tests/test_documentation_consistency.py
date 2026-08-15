"""Issue #132: the repository text must not contradict the mode registry.

Why this test exists
--------------------

Registering ``demo`` as a runnable execution mode invalidated statements
scattered across ~20 documents and a few docstrings ("the runner drives
fake / shadow only", "demo is not registered in ``BROKER_TYPE_BY_MODE``").
Those statements are not decoration: an operator reading them concludes the
demo path does not exist, and the PR #136 review found four separate rounds
of leftovers because each sweep was a hand-written grep that missed
paraphrases, whole files, and finally a spelling variant.

What this file pins, precisely
------------------------------

**The exact text Issue #132 deleted, and nothing else.** A file fails if it
contains one of the strings in :data:`REMOVED_BY_THIS_ISSUE` verbatim
(whitespace-normalised). That is a narrow guarantee, and stating it
narrowly is the point of this rewrite.

Earlier revisions tried to be a *claim* detector: regular expressions for
"the runner drives only these modes" or "demo is not registered", written to
generalise over paraphrases. Review rounds r4 through r8 found a defect in
every version, alternating between the two ways such a rule fails:

- **it flagged correct sentences.** ``継続運転する Live Execution runner は
  未実装`` is TRUE (the resident process really is unimplemented).
  ```sandbox` は mode 定義のみ`` is true of a component that does not exist
  yet. ``Issue #131 supports ``fake`` and ``shadow``; Issue #132 adds
  ``demo```` is an accurate history. Each was rejected by CI. A pin that
  forbids writing true things taxes the documentation it protects.
- **it missed stale ones.** ``restricted to`` / ``solely`` / ``alone`` all
  restrict the mode list without the word ``only``; a verbose demo sentence
  outran a proximity anchor. Each near-miss invited another pattern, and
  the next round found the next paraphrase.

Both failures have the same cause: the rules were matching *vocabulary* and
being read as if they matched *claims*. Since a paraphrase detector for
Japanese and English prose needs morphological analysis -- far past what a
regression pin should carry -- the generalisation is abandoned rather than
tuned a sixth time.

So this is a verbatim-reintroduction pin, in the spirit of the AST pins in
``test_submission_boundary.py``: it cannot judge prose, and it no longer
pretends to. What it guarantees is that the specific sentences this Issue
removed cannot come back by revert, copy-paste, or merge resolution -- which
is how all four documented leftovers actually happened.

What it does NOT catch, stated plainly
--------------------------------------

A *rewording* of a stale claim. If someone writes "the runner is restricted
to fake and shadow" in new prose, this file stays green. That belongs to
human review, along with the rest of the prose. The counterweights below
(the registry assert, the live-contract statements, and
:func:`test_the_runner_still_refuses_live`) are what keep the CODE honest;
the prose is reviewed by people.

Scope and limits
----------------

- A future Issue that changes the mode registry must update
  :data:`REMOVED_BY_THIS_ISSUE`; the failure message says so.
- ``docs/development/reports/`` is excluded: release notes are historical
  records of what was true at that release (``demo`` genuinely was
  unregistered in v1.5), and rewriting them would falsify the record.
"""

import pathlib

import pytest

from execution.live_runner import RUNNER_SUPPORTED_MODES
from execution.safety_gate import (
    BROKER_TYPE_BY_MODE,
    ExecutionMode,
    REAL_BROKER_MODES,
    UNIMPLEMENTED_BROKER_MODES,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Text files whose prose describes the current system.
SCANNED_SUFFIXES = {".md", ".py"}

#: Historical records: true when written, deliberately not rewritten.
EXCLUDED_PREFIXES = (
    "docs/development/reports/",
    "docs/archive/",
    # This file necessarily quotes the statements it forbids.
    "execution/tests/test_documentation_consistency.py",
)

#: Shortest entry allowed in the pin. An entry is matched as a substring, so
#: a short one silently becomes the fragment rule this rewrite removed --
#: "mode 定義のみ" would flag any future mode's documentation. The floor is
#: not a magic number: it is the guard that keeps the pin verbatim.
MIN_PIN_LENGTH = 30


def _normalise(text: str) -> str:
    """Collapse whitespace, so re-indenting or re-wrapping cannot smuggle a
    deleted sentence back in."""
    return " ".join(text.split())


def matches(removed: str, line: str) -> bool:
    """True when ``line`` reintroduces ``removed`` verbatim."""
    return _normalise(removed) in _normalise(line)


def _scanned_files():
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        if any(part.startswith(".") or part in {"__pycache__", "venv", "node_modules"}
               for part in pathlib.Path(relative).parts):
            continue
        if relative.startswith(EXCLUDED_PREFIXES):
            continue
        yield relative, path


REMOVED_BY_THIS_ISSUE = (
    "駆動できる mode は **`fake` と `shadow` だけ**",
    "Issue #131 の runner core は `fake` / `shadow` のみを駆動し",
    "runner core は Fake / Shadow のみ実装済み",
    "Issue #131 で **runner core も Fake / Shadow に限って実装済み**",
    '"""Issue #131: the Live Execution runner core (Fake / Shadow only).',
    # the spelling variant the pin originally missed (review r4)
    "existing submission / shadow boundaries. Fake and Shadow only.",
    # the reST enumeration the pin still missed (review r5), copied from
    # the pre-Issue-#132 execution/live_runner.py line 52 verbatim
    "Only :data:`RUNNER_SUPPORTED_MODES` -- ``fake`` and ``shadow`` -- may be",
    "driven by this runner (OANDA Demo integration is a separate Issue, and",
    "**OANDA Demo / Live へは runner から接続できない**",
    "`demo` は mode 定義のみで Execution 層への組み込み未実装",
    # --- the "demo is not registered" family (review r6) ------------------
    # Found by walking every deletion in `git diff main...HEAD` rather than
    # by grepping for phrasings already known to the pin. The first two are
    # the ones the review cited; the rest are their siblings, uncovered for
    # the same reason.
    "- OANDA **Demo** Adapter は Issue #79 で実装済み(Demo 口座での注文ライフサイクル検証用)。"
    "ただし **Live 口座接続・Live Execution runner・Execution 層への組み込みは未実装**: "
    "`execution.safety_gate.BROKER_TYPE_BY_MODE` に `demo` は未登録のままであり、",
    "- **Execution 層への組み込みは未実装**: `execution.safety_gate.BROKER_TYPE_BY_MODE` に "
    "`demo` は登録されておらず、`BrokerConnection(broker=OandaDemoAdapter(...))` は構築できない",
    "  実装は `broker/`。Live 接続・Execution 層への組み込み・Live Execution 本体は未実装のまま。",
    "> - **OANDA Demo Adapter は Issue #79 で実装済み**(下記「OANDA Demo Adapter」節)。ただし "
    "**Live 口座接続・Live Execution runner・Execution 層への組み込み"
    "(`BROKER_TYPE_BY_MODE` への `demo` 登録)は未実装**であり、",
    "> - **Live 接続・実発注・Live Execution runner は引き続き未実装**である。OANDA **Demo** "
    "Adapter は Issue #79 で `broker/` に実装済みだが `BROKER_TYPE_BY_MODE` に `demo` は未登録のため、",
    "`demo` / `live` は**実 Adapter 型が登録されるまで接続オブジェクト自体を構築できない**"
    "(表現不能による fail-closed)。",
    "`demo` / `live` は mode 定義と validation のみ(Adapter 型未登録のため `BrokerConnection` を"
    "構築できず、runner は未実装として明示拒否)。",
    "- `demo` / `live`: **mode 定義と validation のみ。Execution 層への組み込みは未実装**。",
    "- OANDA Adapter・OANDA Demo / Live API・実口座への注文・Live 接続"
    "(`demo` / `live` の `BrokerConnection` は Adapter 型が登録されるまで構築不能)",
    "  (``BROKER_TYPE_BY_MODE`` stays FakeBroker-only -- reconciliation talks to",
    "``live`` remain unconstructible (``BROKER_TYPE_BY_MODE``) and must be refused",
    # the sentence review r6 constructed to show the demo-exemption was too
    # loose: plainly stale, yet the old rule excused it because "Demo"
    # appeared earlier in the clause
    "Demo remains unimplemented, so only ``fake`` and ``shadow`` may be run.",
    # the two lines that broadening "Adapter 型が登録されるまで" finally caught,
    # verbatim as they stood before this round
    "`demo` / `live` は引き続き**定義と validation のみ**で、"
    "Adapter 型が登録されるまで接続を構築できない。",
    "`fake` = FakeBroker のみ、`demo` / `live` は Adapter 型が登録されるまで**構築不能**。",
    # --- entries a paraphrase pin could not hold (review r8) ---------------
    # These were recorded as "not pinnable line by line" while the pin
    # generalised: no regex could separate them from true statements. As
    # VERBATIM strings they pin exactly as well as any other -- the
    # unpinnable record was an artefact of the paraphrase design, not a
    # property of the lines, and it is gone.
    "Live Execution(基盤実装済み・runner 未実装・運用未解禁)",
    "                  (基盤は broker/ + execution/ に実装済み。runner 未実装・運用未解禁)",
    # one item of a NON-SCOPE list: neutral words whose claim came from the
    # heading above them, and which now appear correctly in three documents
    # describing the completed registration. The full deleted line is
    # unambiguous where the phrase alone was not.
    "Live 口座接続・Live 用認証情報、`BROKER_TYPE_BY_MODE` への `demo` 登録(Execution 統合)、"
    "Live Execution runner、",
    # the runner's own fake-composition error (review r7). It spanned two
    # source lines, so no single-line paraphrase rule could state the claim;
    # the first line is exact and distinctive on its own.
    "here, got {connection.execution_mode.value!r}; demo integration and live ",
)


KEPT_ON_PURPOSE = (
    # the corrected mode list must never trip the pin
    "駆動できる mode は **`fake` / `shadow` / `demo`**",
    "runner core は Issue #131 / #132 で `fake` / `shadow` / `demo` まで実装済み",
    "Fake / Shadow / Demo are three different things",
    "- **Fake / Shadow / Demo の責務**",
    # the module header that legitimately names which Issue added what
    "(Issue #131: Fake and Shadow; Issue #132: OANDA Demo).",
    # the corrected reST enumeration, which names three modes
    "``fake``, ``shadow`` and ``demo`` may be run by this runner.",
    # --- the resident-runner caveats ---------------------------------------
    # These are TRUE: the continuously-operating process really is
    # unimplemented, and saying so is a safety statement Issue #132 must
    # keep. Rounds r4-r6 were spent trying to write a rule that flags the
    # stale reading without flagging these; the rule was removed instead
    # (see the module docstring). They stay here so that a future author
    # who re-adds such a rule finds out immediately that it is over-broad.
    "継続運転する常駐 runner は未実装",
    "常駐 runner 未実装・実口座運用は未解禁",
    "定期実行 runner は未実装",
    "継続運転する Live Execution runner は未実装",
    "定期実行の Live Execution runner は未実装",
    "常駐する送信 runner は未実装であり、runner core とは別である",
    "継続運転を担う常駐プロセスとしての runner が未実装",
    # the case review r6 showed a clause-boundary heuristic gets WRONG: the
    # qualifier is separated from its noun phrase by a reading comma
    "継続運転する常駐プロセスとして、Live Execution runner は未実装",
    # ... and the case review r7 showed the last runner-existence rule got
    # wrong, which is why that rule is gone too
    "Demo の 1-cycle API は実装済み。runner 未実装(systemd 常駐プロセスを指す)",
    # --- sentences about OTHER components (PR #136 review r7) --------------
    # A pin is a standing restriction on future writing, not only a record
    # of the past. None of these is anybody's stale statement, and the
    # unanchored fragments failed CI on all of them.
    "`sandbox` は mode 定義のみ",
    "`sandbox` / `paper` は Adapter 型が登録されるまで構築不能",
    "``sandbox`` adapters remain unconstructible until a type is registered",
    "Issue #131 supports ``fake`` and ``shadow``; Issue #132 adds ``demo``.",
    "`demo` は Issue #132 で登録済みだが、`sandbox` は定義と validation のみ",
    # --- correct sentences the proximity anchors rejected (review r8) ------
    # Every one of these was failing CI before the pin became verbatim.
    "`demo` は登録済みだが `sandbox` は実 Adapter 型が登録されるまで構築不能。",
    "``sandbox`` adapters remain unconstructible (``BROKER_TYPE_BY_MODE``) for now",
    "`sandbox` Adapter の Execution 層への組み込みは未実装。",
    # concept.md line 64 was rewritten this round, but NOT because this
    # clause was false -- "the RESIDENT runner is unimplemented" is true.
    # What was stale there was an OMISSION (the sentence listed the
    # foundations without the runner core), and an omission is invisible to
    # a substring pin. Kept here so a future author does not "fix" the pin
    # by making it flag a true statement.
    "常駐 runner が未実装のためリアルタイムフローとしては駆動されず",
)


@pytest.mark.parametrize("removed", REMOVED_BY_THIS_ISSUE, ids=lambda text: text[:60])
def test_no_document_reintroduces_a_deleted_statement(removed):
    """Issue #132 / PR #136 review: these sentences described the system
    before ``demo`` became a runnable mode. If one reappears, either the
    text is stale or the mode registry changed -- in the second case, update
    this list together with the registry."""
    offenders = []
    for relative, path in _scanned_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if matches(removed, line):
                offenders.append(f"{relative}:{lineno}")
    assert offenders == [], (
        f"{removed!r} was deleted by Issue #132 (which registered 'demo' as a runnable "
        f"execution mode) but appears again at: {offenders}. Update the text, or -- if "
        "the mode registry itself changed -- update REMOVED_BY_THIS_ISSUE."
    )


@pytest.mark.parametrize("removed", REMOVED_BY_THIS_ISSUE, ids=lambda text: text[:60])
def test_every_pin_entry_is_specific_enough_to_stay_verbatim(removed):
    """The guard on this file's own design (PR #136 review r8). Entries are
    matched as substrings, so a short generic entry quietly turns the pin
    back into the paraphrase matcher that five review rounds rejected."""
    assert len(_normalise(removed)) >= MIN_PIN_LENGTH, (
        f"{removed!r} is too short to be a verbatim pin; a fragment this generic will "
        "flag correct sentences about other modes and adapters. Pin the whole deleted "
        "sentence instead."
    )


@pytest.mark.parametrize("sentence", KEPT_ON_PURPOSE, ids=lambda text: text[:60])
def test_the_pin_does_not_flag_a_correct_or_still_true_statement(sentence):
    """The costly failure mode, and the one that recurred: an over-broad
    entry that pressures an author into deleting an accurate caveat, or
    blocks a future component from being documented at all."""
    matched = [removed for removed in REMOVED_BY_THIS_ISSUE if matches(removed, sentence)]
    assert matched == [], f"{sentence!r} is correct but matched {matched}"


def test_the_mode_registry_is_what_the_documentation_claims():
    """The other half of the pin: the sentences above are only stale while
    the registry actually says so. If this fails, the docs may be right and
    the list above is what needs changing."""
    assert RUNNER_SUPPORTED_MODES == {
        ExecutionMode.FAKE,
        ExecutionMode.SHADOW,
        ExecutionMode.DEMO,
    }
    assert ExecutionMode.LIVE not in RUNNER_SUPPORTED_MODES
    assert ExecutionMode.DEMO in BROKER_TYPE_BY_MODE
    assert REAL_BROKER_MODES == {ExecutionMode.DEMO, ExecutionMode.LIVE}
    assert UNIMPLEMENTED_BROKER_MODES == frozenset()


# --- the counterweight: live must still read as un-released ----------------
#
# PR #136 review r4: an earlier version asserted only that the WORD 未解禁
# appeared somewhere in concept.md and that a heading string existed in
# operations.md. Both documents contain several unrelated 未解禁, so the
# binding sentences could have been deleted or inverted while the test
# stayed green. The contract statements themselves are asserted instead:
# each entry below is a sentence that would have to go for "live is
# released" to become the documented position.

LIVE_CONTRACT_STATEMENTS = {
    "docs/product/concept.md": (
        # the headline position, and the one the layer table repeats
        "実口座での Live 運用は未解禁",
        # releasing live is a separate operational decision, not a merge
        "**Live 運用開始は別途の運用判断**",
        # demo reaching the broker is explicitly NOT the argument for live
        "**Demo まで接続できることは Live 解禁の根拠ではない**",
    ),
    "docs/operations/operations.md": (
        "#### Live 運用開始の事前条件",
        "Issue #82 の merge だけで Live 運用を開始しない",
        # the locks that must all hold before a live connection exists
        "`ENABLE_LIVE_TRADING=true`",
        "--confirm-live",
        # and the demo section must not sell demo as a safe rehearsal
        "**Demo は「緩い Live」ではない。**",
    ),
}


@pytest.mark.parametrize(
    ("document", "statement"),
    [(doc, text) for doc, texts in LIVE_CONTRACT_STATEMENTS.items() for text in texts],
    ids=lambda value: value if value.startswith("docs/") else value[:40],
)
def test_live_is_still_documented_as_un_released(document, statement):
    """This suite must never be usable to argue that live was released."""
    text = (REPO_ROOT / document).read_text(encoding="utf-8")
    assert statement in text, (
        f"{document} no longer contains {statement!r}. Issue #132 integrated DEMO only; "
        "real-account live operation stays un-released, and the statements that say so "
        "may not be removed as a side effect of documenting demo."
    )


def test_the_runner_still_refuses_live():
    """The code-side counterweight to the documents above."""
    from execution.live_runner import RunnerModeError, resolve_runner_mode

    with pytest.raises(RunnerModeError, match="live activation lock"):
        resolve_runner_mode("live")
