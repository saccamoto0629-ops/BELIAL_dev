"""Issue #132: the repository text must not contradict the mode registry.

Why this test exists
--------------------

Registering ``demo`` as a runnable execution mode invalidated statements
scattered across ~20 documents and a few docstrings ("the runner drives
fake / shadow only", "demo is not integrated", "no runner exists"). Those
statements are not decoration: an operator reading them concludes the demo
path does not exist, and the PR #136 review found FOUR separate rounds of
leftovers because each sweep was a hand-written grep that missed
paraphrases, whole files, and finally a spelling variant.

So the claims that are now FALSE are pinned here instead of re-swept by
hand. This is the documentation equivalent of the AST pins in
``test_submission_boundary.py``: it does not check that prose is good, only
that a specific set of statements known to be false cannot reappear.

Two failure modes, both real
----------------------------

The fourth review round hit BOTH of them in one pass, which is why the
rules below are structured rather than a flat regex list:

- **Too narrow** -- the first draft pinned only the slash spelling
  ``Fake / Shadow only`` while ``execution/live_runner.py`` said ``Fake and
  Shadow only``. The suite was green with a stale docstring in the runner's
  own module, and the self-verification fixture used the same slash
  spelling, so it could not notice. Every rule now carries the spellings
  that actually occur, and :data:`REMOVED_BY_THIS_ISSUE` holds the REAL
  sentences that were removed, spelling variants included.
- **Too broad** -- the runner-existence rule used a fixed-width lookbehind,
  so it exempted ``継続運転する常駐 runner は未実装`` but still flagged
  ``継続運転する Live Execution runner は未実装``: the same true statement
  with words in between. An over-broad rule pushes an author to delete an
  accurate caveat about continuous operation, which is exactly the safety
  statement this Issue must keep. Exemptions are therefore evaluated over
  the whole CLAUSE containing the match (:func:`_clause_before`), not a
  fixed number of characters.

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

    ``exempt_if_clause_contains`` is checked against the text preceding the
    match WITHIN THE SAME CLAUSE. It exists for claims whose wording is
    false on its own but true under a qualifier -- "the runner is
    unimplemented" is false, "the RESIDENT runner is unimplemented" is
    true -- and the qualifier may sit several words away from the match.
    """

    pattern: str
    exempt_if_clause_contains: str | None = None

    def __post_init__(self) -> None:
        re.compile(self.pattern)
        if self.exempt_if_clause_contains is not None:
            re.compile(self.exempt_if_clause_contains)


#: Qualifiers that turn "the runner is unimplemented" into a TRUE statement
#: about the continuous-operation process, which really does not exist yet.
_CONTINUOUS_OPERATION = r"常駐|継続運転|定期実行|resident|continuous"

STALE_CLAIMS = (
    # --- the runner's mode list ------------------------------------------
    # "Fake / Shadow / Demo" must never trip these, so each carries its
    # limiting word. Both the slash and the "and" spelling occur in this
    # repository (PR #136 review r4).
    StaleClaim(r"Fake / Shadow (のみ|限定|に限って)"),
    StaleClaim(r"Fake (/|and) Shadow only"),
    StaleClaim(r"`fake` / `shadow` (のみ|だけ|限定)"),
    StaleClaim(r"`fake` と `shadow` だけ"),
    # The reST enumeration form (PR #136 review r5). The old
    # ``live_runner`` module docstring wrote the mode list as
    # ``fake`` and ``shadow``, which none of the rules above matched --
    # so re-introducing that exact sentence passed. The rule is exempted
    # when the same clause also names demo, which is how the CORRECTED
    # sentence (``fake``, ``shadow`` and ``demo``) and the "which Issue
    # added what" note in ``runner_composition`` are written.
    StaleClaim(
        r"``fake`` and ``shadow``",
        exempt_if_clause_contains=r"demo|Demo",
    ),
    # --- demo's Execution-layer status ------------------------------------
    StaleClaim(r"`demo` は mode 定義のみ"),
    StaleClaim(r"`BROKER_TYPE_BY_MODE` への `demo` 登録は未実装"),
    # "v1.5 時点では ... 未登録" is a TRUE version-scoped statement.
    StaleClaim(
        r"`BROKER_TYPE_BY_MODE` へ `demo` は未登録",
        exempt_if_clause_contains=r"v1\.5 時点では",
    ),
    StaleClaim(r"(OANDA )?Demo integration is a separate Issue"),
    StaleClaim(r"OANDA Demo の runner 統合は未実装"),
    StaleClaim(r"実 Broker(OANDA Demo / Live)への runner 統合は未実装"),
    StaleClaim(r"OANDA Demo / Live へは runner から接続できない"),
    # --- the runner core's existence --------------------------------------
    # NOT the resident / continuous process, which is genuinely still
    # unimplemented -- and whose qualifier may be words away from "runner".
    StaleClaim(
        r"runner (は|が)?未実装",
        exempt_if_clause_contains=_CONTINUOUS_OPERATION,
    ),
)

#: Clause separators. Japanese prose in this repository packs several
#: independent statements into one line (markdown table cells especially),
#: so an exemption earned by one clause must not cover the next.
#:
#: PR #136 review r5: ``。`` and ``|`` alone left a loophole -- in
#: "常駐対応は後続 Issue とするが、runner は未実装" the qualifier belongs to a
#: DIFFERENT assertion in the same sentence, yet it exempted the stale one.
#: Writing 常駐 anywhere earlier was a free pass. Two more boundaries close
#: it without cutting genuine qualification:
#:
#: - ``、`` -- the reading comma, which separates assertions;
#: - a contrastive ``が`` followed by whitespace. Bare ``が`` is also the
#:   subject marker ("継続運転が必要な runner"), where it must NOT split, and
#:   there it is followed by a word rather than a space. In this repository
#:   the space appears exactly when the clause switches to a Latin-script
#:   term, which is the contrastive case we need to cut.
#:
#: Residual: a contrastive with neither comma nor space still slips. The
#: adversarial fixtures below record what IS covered rather than claiming
#: the split is complete.
_CLAUSE_BOUNDARY = re.compile(r"[。|、]|が(?=\s)")


def _clause_before(line: str, start: int) -> str:
    """The text from the last clause boundary up to ``start``."""
    boundaries = [match.end() for match in _CLAUSE_BOUNDARY.finditer(line, 0, start)]
    return line[(boundaries[-1] if boundaries else 0):start]


def matches(claim: StaleClaim, line: str) -> bool:
    """True when ``line`` makes ``claim`` without an exempting qualifier."""
    for match in re.finditer(claim.pattern, line):
        if claim.exempt_if_clause_contains is None:
            return True
        if not re.search(claim.exempt_if_clause_contains, _clause_before(line, match.start())):
            return True
    return False


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
    "Live Execution(基盤実装済み・runner 未実装・運用未解禁)",
    "                  (基盤は broker/ + execution/ に実装済み。runner 未実装・運用未解禁)",
    "`demo` は mode 定義のみで Execution 層への組み込み未実装",
)

KEPT_ON_PURPOSE = (
    # the corrected mode list must never trip the pin
    "駆動できる mode は **`fake` / `shadow` / `demo`**",
    "runner core は Issue #131 / #132 で `fake` / `shadow` / `demo` まで実装済み",
    "Fake / Shadow / Demo are three different things",
    "- **Fake / Shadow / Demo の責務**",
    # the module header that legitimately names which Issue added what
    "(Issue #131: Fake and Shadow; Issue #132: OANDA Demo).",
    # ... and neither may the TRUE caveats about continuous operation,
    # which are the safety statements this Issue must keep. The qualifier
    # is ADJACENT here ...
    "継続運転する常駐 runner は未実装",
    "常駐 runner 未実装・実口座運用は未解禁",
    "定期実行 runner は未実装",
    # ... and SEPARATED here, which the fixed-width lookbehind used to miss
    # (PR #136 review r4).
    "継続運転する Live Execution runner は未実装",
    "定期実行の Live Execution runner は未実装",
    "常駐する送信 runner は未実装であり、runner core とは別である",
    "継続運転を担う常駐プロセスとしての runner が未実装",
    # version-scoped history stays true
    "v1.5 時点では `BROKER_TYPE_BY_MODE` へ `demo` は未登録",
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
    unimplemented", which must survive, qualifier adjacent or not."""
    matched = [claim.pattern for claim in STALE_CLAIMS if matches(claim, sentence)]
    assert matched == [], f"{sentence!r} is correct but matched {matched}"


#: Sentences where a continuous-operation qualifier appears, but belongs to
#: a DIFFERENT assertion than the stale claim. Without these the exemption
#: becomes "write 常駐 anywhere in the line and the stale claim passes"
#: (PR #136 review r5).
EXEMPTION_MUST_NOT_BE_EARNED_BY = (
    # separate sentence
    "継続運転する常駐プロセスは未実装。runner は未実装であり demo も動かない",
    # separate assertion, reading comma
    "常駐対応は後続 Issue とするが、runner は未実装",
    # separate assertion, contrastive が before a Latin-script term
    "常駐対応は後続 Issue とするが runner は未実装",
    # markdown table cell: the qualifier is in the previous column
    "| 常駐プロセスは未実装 | runner は未実装 |",
)


@pytest.mark.parametrize("line", EXEMPTION_MUST_NOT_BE_EARNED_BY)
def test_an_exemption_does_not_leak_across_assertions(line):
    """A qualifier earned by one assertion must not excuse a stale claim in
    another one on the same line -- markdown table rows and Japanese
    sentences both pack several statements together."""
    claim = next(c for c in STALE_CLAIMS if c.exempt_if_clause_contains == _CONTINUOUS_OPERATION)
    assert matches(claim, line), "the stale claim in the other assertion must still be caught"
