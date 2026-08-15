"""Issue #132: the repository text must not contradict the mode registry.

Why this test exists
--------------------

Registering ``demo`` as a runnable execution mode invalidated statements
scattered across ~20 documents and a few docstrings ("the runner drives
fake / shadow only", "demo is not registered in ``BROKER_TYPE_BY_MODE``").
Those statements are not decoration: an operator reading them concludes the
demo path does not exist, and the PR #136 review found FOUR separate rounds
of leftovers because each sweep was a hand-written grep that missed
paraphrases, whole files, and finally a spelling variant.

So the claims that are now FALSE are pinned here instead of re-swept by
hand. This is the documentation equivalent of the AST pins in
``test_submission_boundary.py``: it does not check that prose is good, only
that a specific set of statements known to be false cannot reappear.

What this pin deliberately does NOT do (PR #136 review r6)
----------------------------------------------------------

Review rounds 4, 5 and 6 were all spent on ONE rule: ``runner は未実装``
("the runner is unimplemented"). That sentence is genuinely ambiguous in
this repository, because BOTH readings are in use and both are written the
same way:

- the runner CORE exists since Issue #131 -- saying it does not is stale;
- the RESIDENT / continuously-operating runner really is unimplemented, and
  that caveat is a safety statement this Issue must keep.

Distinguishing them needs to know which noun phrase the qualifier attaches
to. Three successive attempts failed in both directions -- a fixed-width
lookbehind flagged ``継続運転する Live Execution runner は未実装`` (true), and
a clause-boundary heuristic then exempted
``常駐対応は後続 Issue とするが、runner は未実装`` (stale) because a
DIFFERENT assertion on the same line carried the qualifier. Widening the
boundary list re-broke the first case: ``、`` splits
``継続運転する常駐プロセスとして、Live Execution runner は未実装`` in the
wrong place. Getting this right needs morphological analysis, which is far
past what a regression pin should carry.

**That rule and the whole exemption mechanism are therefore gone.** Every
rule below matches an unambiguous, machine-checkable statement -- one that
names a code identifier (``BROKER_TYPE_BY_MODE``, ``demo``) or spells out a
restrictive mode list -- and no rule has an exemption. The consequence is
stated plainly rather than hidden: **free-form prose about whether "the
runner" exists is not policed by this file.** It is reviewed by humans,
like the rest of the prose. What is pinned is the part where a machine can
be certain.

Two corollaries, both learned the same way (review r7)
------------------------------------------------------

A pin is scanned against EVERY ``.md`` and ``.py`` in the repository, so a
rule is not only a claim about the past -- it is a standing restriction on
what anyone may write in the future. Two kinds of rule fail that test:

- **Unanchored fragments.** ``mode 定義のみ`` / ``remain unconstructible`` /
  ``Adapter 型が登録されるまで`` describe demo here, but they are ordinary
  ways to describe ANY mode or adapter. As written they would fail CI on
  "`sandbox` は mode 定義のみ" -- a sentence about a component that does
  not exist yet and whose author has done nothing wrong. Every fragment is
  now anchored to ``demo`` or ``BROKER_TYPE_BY_MODE``.
- **Bare enumerations.** the bare "``fake`` and ``shadow``" pair was pinned on
  its own, on the theory that correct text always names three modes. It
  does not: "Issue #131 supports ``fake`` and ``shadow``; Issue #132 adds
  ``demo``" is true, and was flagged. What is stale is LIMITING the runner
  to the pair, so the rule requires the limiting word.

The general shape: pin the claim, never the vocabulary the claim happens to
use.

The one line that legitimately reads like a stale claim is listed verbatim
in :data:`ALLOWED_LINES` and checked to still exist, so the allowlist
cannot rot or be padded with speculative entries.

Scope and limits
----------------

- It pins a fixed set of statements that are false as of Issue #132, not a
  general "docs are accurate" property -- no test can check that.
- A future Issue that changes the mode registry again must update this
  list; the failure message says so.
- ``docs/development/reports/`` is excluded: release notes are historical
  records of what was true at that release (``demo`` genuinely was
  unregistered in v1.5), and rewriting them would falsify the record.
"""

import pathlib
import re
from dataclasses import dataclass

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
    # This file necessarily quotes the false statements it forbids.
    "execution/tests/test_documentation_consistency.py",
)


@dataclass(frozen=True)
class StaleClaim:
    """One statement that Issue #132 made false.

    A claim carries no exemptions. If a rule needs "except when ..." to
    avoid flagging a true sentence, the rule is matching prose rather than
    a fact, and belongs outside this pin (see the module docstring). The
    only escape is :data:`ALLOWED_LINES`, which names a whole line.
    """

    pattern: str

    def __post_init__(self) -> None:
        re.compile(self.pattern)


STALE_CLAIMS = (
    # --- the runner's mode list -------------------------------------------
    # Each rule carries the LIMITING word ("only" / のみ / だけ / 限定), so a
    # correct enumeration that includes demo cannot trip it. Both the slash
    # and the "and" spelling occur in this repository (PR #136 review r4).
    StaleClaim(r"Fake / Shadow (のみ|限定|に限って)"),
    StaleClaim(r"Fake (/|and) Shadow only"),
    StaleClaim(r"`fake` / `shadow` (のみ|だけ|限定)"),
    StaleClaim(r"`fake` と `shadow` だけ"),
    # The reST enumeration form (PR #136 review r5), restricted to the
    # RESTRICTIVE claim. Review r6 removed an "unless the clause mentions
    # demo" exemption from this rule; review r7 then showed the bare pair
    # is not stale by itself -- "Issue #131 supports ``fake`` and
    # ``shadow``; Issue #132 adds ``demo``" is a true sentence about the
    # history. What is stale is limiting the runner TO those two, so the
    # rule now requires the limiting word on either side of the pair.
    StaleClaim(
        r"(?i)\bonly\b[^。\n]{0,60}?``fake`` and ``shadow``"
        r"|``fake`` and ``shadow``[^。\n]{0,30}?\bonly\b"
    ),
    # --- demo's Execution-layer status ------------------------------------
    # Every pattern here NAMES what it is about -- ``demo`` or
    # ``BROKER_TYPE_BY_MODE`` -- so it is unambiguous: either demo is in the
    # registry or it is not, and since Issue #132 it is.
    #
    # Review r7: earlier drafts used the bare fragments ("mode 定義のみ",
    # "remain unconstructible", "Adapter 型が登録されるまで"). Those are
    # scanned against every .md and .py in the repository, so they would
    # fail CI on a CORRECT sentence about some other mode or adapter --
    # "`sandbox` は mode 定義のみ" is nobody's stale claim. A pin that
    # forbids describing future components taxes the documentation it is
    # supposed to protect, so each fragment is anchored to the demo claim.
    StaleClaim(r"Execution 層への組み込み[^。]{0,60}未実装"),
    StaleClaim(r"`demo` (は|が)未登録"),
    StaleClaim(r"`demo` は登録されておらず"),
    # Registration is DONE (live in Issue #82, demo in Issue #132); what
    # gates construction now is the activation lock, not the registry.
    # Broadening this from "実 Adapter 型..." caught two lines that had
    # survived all six review rounds (safety_gate.md, execution/README.md).
    StaleClaim(r"`demo`[^。\n]{0,40}(実 )?Adapter 型が登録されるまで"),
    StaleClaim(r"`demo`[^。\n]{0,25}(mode )?定義(と validation )?のみ"),
    StaleClaim(r"``BROKER_TYPE_BY_MODE`` stays FakeBroker-only"),
    StaleClaim(r"remain unconstructible \(``BROKER_TYPE_BY_MODE``\)"),
    StaleClaim(r"`BROKER_TYPE_BY_MODE` への `demo` 登録は未実装"),
    # Both spellings of the "that is a different Issue" note. Review r7
    # found the runner's OWN fake-composition error still saying it in
    # lowercase, with "and live operation" wedged into the middle, so the
    # rule matches the claim rather than one sentence shape.
    StaleClaim(r"(?i)demo integration[^。\n]{0,40}separate Issue"),
    StaleClaim(r"OANDA Demo の runner 統合は未実装"),
    StaleClaim(r"実 Broker(OANDA Demo / Live)への runner 統合は未実装"),
    StaleClaim(r"OANDA Demo / Live へは runner から接続できない"),
)


#: Whole lines that contain a stale-looking phrase and are nonetheless
#: CORRECT. Matched verbatim against the stripped line, never as a
#: substring rule, so an author cannot widen an exemption by accident:
#: editing such a line breaks the allowlist on purpose and forces a review.
#: :func:`test_every_allowed_line_still_exists` keeps the list honest.
ALLOWED_LINES = frozenset({
    # Version-scoped history: demo really was unregistered in v1.5, and the
    # same line says Issue #132 implemented it. Removing the caveat would
    # make the roadmap's own history wrong.
    "- `demo` mode の Execution 層統合(v1.6 の Issue #132 で実装。"
    "v1.5 時点では `BROKER_TYPE_BY_MODE` へ `demo` は未登録)",
})


def matches(claim: StaleClaim, line: str) -> bool:
    """True when ``line`` states ``claim`` and is not an allowed line."""
    if line.strip() in ALLOWED_LINES:
        return False
    return re.search(claim.pattern, line) is not None


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


@pytest.mark.parametrize("claim", STALE_CLAIMS, ids=lambda claim: claim.pattern)
def test_no_document_repeats_a_statement_demo_integration_made_false(claim):
    """Issue #132 / PR #136 review: these statements described the system
    before ``demo`` became a runnable mode. If one reappears, either the
    text is stale or the mode registry changed -- in the second case, update
    this list together with the registry."""
    offenders = []
    for relative, path in _scanned_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if matches(claim, line):
                offenders.append(f"{relative}:{lineno}")
    assert offenders == [], (
        f"{claim.pattern!r} is no longer true (Issue #132 registered 'demo' as a runnable "
        f"execution mode) but still appears at: {offenders}. Update the text, or -- if "
        "the mode registry itself changed -- update STALE_CLAIMS."
    )


def test_every_allowed_line_still_exists():
    """An allowlist that outlives the line it excuses is a silent hole: the
    next author may write a genuinely stale sentence that happens to match
    a stale entry. Each entry must still be present somewhere in scope."""
    present = set()
    for _relative, path in _scanned_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped in ALLOWED_LINES:
                present.add(stripped)
    assert present == set(ALLOWED_LINES), (
        f"ALLOWED_LINES entries no longer in the repository: {set(ALLOWED_LINES) - present}. "
        "Remove the entry, or restore the line it was written for."
    )


def test_every_allowed_line_actually_needs_the_allowance():
    """The mirror check: an entry that no rule would flag is dead weight
    and hides the fact that the rules moved on."""
    for allowed in ALLOWED_LINES:
        flagged = [claim.pattern for claim in STALE_CLAIMS
                   if re.search(claim.pattern, allowed)]
        assert flagged, (
            f"{allowed!r} is allowlisted but no rule flags it; drop the entry."
        )


def test_the_mode_registry_is_what_the_documentation_claims():
    """The other half of the pin: the statements above are only false while
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


# --- the pin's own tests ---------------------------------------------------
#
# A pattern with a typo, or one that only knows a single spelling, matches
# nothing and passes silently -- which is exactly how a stale docstring
# survived in execution/live_runner.py through three review rounds. So the
# rules are exercised against the ACTUAL sentences this PR removed, and
# against sentences that must never be flagged.
#
# Entries are copied VERBATIM from `git diff main...HEAD` deletions. A
# paraphrase would let the fixture pass while the real sentence stayed
# uncovered, which is how review rounds r4, r5 and r6 each found another
# one.

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
)


#: Deletions this Issue made that a LINE-BASED pin cannot cover, recorded so
#: the gap is known rather than assumed absent. Both were found by walking
#: every deletion in ``git diff main...HEAD`` (PR #136 review r6).
#:
#: - "``BROKER_TYPE_BY_MODE`` への ``demo`` 登録(Execution 統合)" appeared as
#:   one item in a NON-SCOPE list. The words are neutral; only the heading
#:   above them made the line a claim. The same words now appear -- correctly
#:   -- in three documents describing the completed registration, so no rule
#:   can separate them without reading the document structure.
#: Making the pin document-structure-aware would rebuild the
#: natural-language machinery this file removed, so these stay for human
#: review, which is where the rest of the prose already lives.
#:
#: (The neighbouring "``demo`` / ``live`` remain unconstructible" sentence
#: also spanned two source lines, but its PREDICATE line carries the claim
#: on its own and is pinned; a claim only escapes when no single line
#: states it.)
NOT_PINNABLE_LINE_BY_LINE = (
    (
        "`BROKER_TYPE_BY_MODE` への `demo` 登録(Execution 統合)",
        "structural: one item in a NON-SCOPE list. The words are neutral -- the "
        "claim came from the heading above them -- and the same words now appear "
        "correctly in three documents describing the completed registration.",
    ),
    (
        "Live Execution(基盤実装済み・runner 未実装・運用未解禁)",
        "ambiguous: asserts 'runner 未実装', which is false of the runner core and "
        "true of the resident process. Rule 実装済み[。・]runner 未実装 covered this "
        "until review r7 showed it also flags the true caveat "
        "'Demo の 1-cycle API は実装済み。runner 未実装(systemd 常駐プロセスを指す)'.",
    ),
    (
        "(基盤は broker/ + execution/ に実装済み。runner 未実装・運用未解禁)",
        "ambiguous: same claim as the entry above, same reason.",
    ),
    (
        "here, got {connection.execution_mode.value!r}; demo integration and live ",
        "split: the runner's own fake-composition error said 'demo integration and "
        "live operation are separate Issues' across TWO source lines, so no single "
        "line states the claim (review r7 found it; the message is fixed). The "
        "single-line spelling IS pinned; anchoring on 'demo integration and live' "
        "alone would flag correct sentences pairing the two subjects.",
    ),
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
    # ... and the case review r7 showed the LAST runner-existence rule got
    # wrong, which is why that rule is gone too
    "Demo の 1-cycle API は実装済み。runner 未実装(systemd 常駐プロセスを指す)",
    # --- sentences about OTHER components (PR #136 review r7) --------------
    # A pin is a standing restriction on future writing, not only a claim
    # about the past. None of these is anybody's stale statement, and an
    # unanchored fragment would have failed CI on all of them.
    "`sandbox` は mode 定義のみ",
    "`sandbox` / `paper` は Adapter 型が登録されるまで構築不能",
    "``sandbox`` adapters remain unconstructible until a type is registered",
    "Issue #131 supports ``fake`` and ``shadow``; Issue #132 adds ``demo``.",
    "`demo` は Issue #132 で登録済みだが、`sandbox` は定義と validation のみ",
    # concept.md line 64 was rewritten this round, but NOT because this
    # clause was false -- "the RESIDENT runner is unimplemented" is true.
    # What was stale there was an OMISSION (the sentence listed the
    # foundations without the runner core), and an omission is invisible to
    # a substring pin. Kept here so a future author does not "fix" the pin
    # by making it flag a true statement.
    "常駐 runner が未実装のためリアルタイムフローとしては駆動されず",
)


@pytest.mark.parametrize("sentence", REMOVED_BY_THIS_ISSUE)
def test_the_pin_catches_the_statements_this_issue_removed(sentence):
    """If no rule matches a sentence this PR actually deleted, the pin would
    not have prevented the review round that produced it."""
    assert any(matches(claim, sentence) for claim in STALE_CLAIMS), (
        f"no rule matches {sentence!r}; the pin would not catch its reintroduction"
    )


@pytest.mark.parametrize("sentence", KEPT_ON_PURPOSE)
def test_the_pin_does_not_flag_a_correct_or_still_true_statement(sentence):
    """The costly failure mode: an over-broad rule that pressures an author
    into deleting an accurate caveat -- especially "the CONTINUOUS runner is
    unimplemented", which must survive however it is phrased."""
    matched = [claim.pattern for claim in STALE_CLAIMS if matches(claim, sentence)]
    assert matched == [], f"{sentence!r} is correct but matched {matched}"


@pytest.mark.parametrize(("text", "reason"), NOT_PINNABLE_LINE_BY_LINE,
                         ids=lambda value: value[:40])
def test_the_unpinnable_record_stays_honest(text, reason):
    """These are recorded as OUT of the pin's reach. If a rule starts
    catching one, the record is stale and should shrink -- an entry that
    quietly became coverable makes the list read as an excuse rather than a
    limit."""
    caught = [claim.pattern for claim in STALE_CLAIMS if matches(claim, text)]
    assert caught == [], (
        f"{text!r} is listed as not pinnable but {caught} now catches it. "
        f"Move it into REMOVED_BY_THIS_ISSUE. Recorded reason was: {reason}"
    )


def test_no_rule_carries_a_natural_language_exemption():
    """PR #136 review r6: exemptions are what made rounds r4-r6 necessary.
    A rule that needs "except when the sentence also says X" is judging
    prose, not a fact, and belongs outside this pin. :data:`ALLOWED_LINES`
    is the only escape, and it names whole lines rather than phrases."""
    assert [field for field in StaleClaim.__dataclass_fields__] == ["pattern"], (
        "StaleClaim grew a field beyond 'pattern'. If it is an exemption, see the "
        "module docstring: the rule likely belongs outside this pin instead."
    )
