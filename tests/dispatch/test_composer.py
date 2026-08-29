"""Tests for setback.dispatch.composer: submission + refusals explainer.

Fully offline, per the work package's live budget of 0. Composition itself
is exercised with `polisher=None` throughout — including the golden-file
tests, which assert byte-for-byte deterministic output against a fixture
case built from the same demo DA (`PAN-661190`, Georges River Council)
used elsewhere in the suite. A small set of tests exercise the optional
polish path against a fake `ModelClient` shaped exactly like
`tests/models/test_client.py`'s fakes (no live network, no real ADC).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from setback.dispatch.composer import (
    CaseInfo,
    GroundContent,
    PolishedProse,
    compose_dispatch_package,
)
from setback.gate.s415 import ACT_CITATION, NON_PLANNING_GROUNDS, PLANNING_HEADS
from setback.gate.validator import GateDecision, GateStatus
from setback.models.client import ModelClient, RetryPolicy

GOLDEN_DIR = Path(__file__).parent / "golden"


def _golden(name: str) -> str:
    return (GOLDEN_DIR / name).read_text()


# --- fixture case -------------------------------------------------------------


def _case() -> CaseInfo:
    return CaseInfo(
        da_number="PAN-661190",
        council="Georges River Council",
        property_address="65A Vista Street, Sans Souci NSW 2219",
        exhibition_start=date(2026, 8, 20),
        exhibition_end=date(2026, 9, 3),
    )


def _height_ground() -> tuple[GateDecision, GroundContent]:
    ruling = PLANNING_HEADS["epi_dcp_provisions"]
    decision = GateDecision(
        ground_id="ground-height",
        status=GateStatus.SHIPPED,
        category=ruling.category,
        explanation=ruling.explanation,
        statutory_basis=ruling.statutory_basis,
        citation_issues=(),
    )
    content = GroundContent(
        statement=(
            "The proposed roof ridge sits at 10.2m, exceeding the 9m maximum height of "
            "buildings control that applies to this site under the Georges River LEP 2021."
        ),
        document_title="Statement of Environmental Effects",
        page=14,
    )
    return decision, content


def _overshadowing_ground() -> tuple[GateDecision, GroundContent]:
    ruling = PLANNING_HEADS["environmental_and_social_impacts"]
    decision = GateDecision(
        ground_id="ground-overshadowing",
        status=GateStatus.SHIPPED,
        category=ruling.category,
        explanation=ruling.explanation,
        statutory_basis=ruling.statutory_basis,
        citation_issues=(),
    )
    content = GroundContent(
        statement=(
            "The shadow diagrams show the rear yard of 63 Vista Street in total shadow "
            "from 2pm to 4pm at the winter solstice, well beyond the amenity loss the DCP "
            "considers acceptable."
        ),
        document_title="Shadow Diagrams",
        page=3,
        annotated_image_ref="overshadowing-3pm-annotated.png",
    )
    return decision, content


def _property_value_ground() -> GateDecision:
    ruling = NON_PLANNING_GROUNDS["property_value"]
    return GateDecision(
        ground_id="ground-property-value",
        status=GateStatus.REFUSED_IRRELEVANT,
        category=ruling.category,
        explanation=ruling.explanation,
        statutory_basis=ruling.statutory_basis,
        citation_issues=(),
    )


def _view_loss_ground() -> GateDecision:
    ruling = NON_PLANNING_GROUNDS["private_view_loss"]
    return GateDecision(
        ground_id="ground-view-loss",
        status=GateStatus.REFUSED_IRRELEVANT,
        category=ruling.category,
        explanation=ruling.explanation,
        statutory_basis=ruling.statutory_basis,
        citation_issues=(),
    )


def _unsubstantiated_fsr_ground() -> GateDecision:
    return GateDecision(
        ground_id="ground-fsr",
        status=GateStatus.REFUSED_UNSUBSTANTIATED,
        category="epi_dcp_provisions",
        explanation=(
            "This ground is planning-relevant, but one or more of its citations could not "
            "be substantiated against the case dossier."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1)(a)",
        citation_issues=("cited document 'fsr-calc' does not exist in the case dossier",),
    )


def _flagged_ground() -> GateDecision:
    return GateDecision(
        ground_id="ground-heritage",
        status=GateStatus.FLAGGED,
        category="site_suitability",
        explanation=(
            "This ground is planning-relevant, but repeated attempts to substantiate its "
            "citations have failed and it has been flagged for human review."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1)(c)",
        citation_issues=("quoted value '12m' for control 'height_of_buildings' does not match",),
    )


def _all_decisions_and_content() -> tuple[list[GateDecision], dict[str, GroundContent]]:
    height_decision, height_content = _height_ground()
    shadow_decision, shadow_content = _overshadowing_ground()
    decisions = [
        height_decision,
        shadow_decision,
        _property_value_ground(),
        _view_loss_ground(),
        _unsubstantiated_fsr_ground(),
        _flagged_ground(),
    ]
    content = {
        height_decision.ground_id: height_content,
        shadow_decision.ground_id: shadow_content,
    }
    return decisions, content


# --- golden-file tests ---------------------------------------------------------


async def test_submission_markdown_matches_golden() -> None:
    decisions, content = _all_decisions_and_content()

    package = await compose_dispatch_package(decisions, _case(), content)

    assert package.submission.markdown == _golden("submission.md")


async def test_submission_html_matches_golden() -> None:
    decisions, content = _all_decisions_and_content()

    package = await compose_dispatch_package(decisions, _case(), content)

    assert package.submission.html == _golden("submission.html")


async def test_refusals_explainer_markdown_matches_golden() -> None:
    decisions, content = _all_decisions_and_content()

    package = await compose_dispatch_package(decisions, _case(), content)

    assert package.refusals_explainer.markdown == _golden("refusals.md")


async def test_refusals_explainer_html_matches_golden() -> None:
    decisions, content = _all_decisions_and_content()

    package = await compose_dispatch_package(decisions, _case(), content)

    assert package.refusals_explainer.html == _golden("refusals.html")


async def test_property_value_refusal_renders_with_no_encouraging_note() -> None:
    """The property-value refusal explicitly requested by the work package:
    it must appear in the explainer with its statutory basis and plain
    explanation, and — since no citation could ever rescue it — no
    "what would make this viable" note."""
    decisions = [_property_value_ground()]

    package = await compose_dispatch_package(decisions, _case(), {})

    md = package.refusals_explainer.markdown
    assert "### Property value" in md
    assert NON_PLANNING_GROUNDS["property_value"].statutory_basis in md
    assert NON_PLANNING_GROUNDS["property_value"].explanation in md
    assert "What would make this viable" not in md

    html = package.refusals_explainer.html
    assert "<h3>Property value</h3>" in html
    assert "What would make this viable" not in html


async def test_refusal_heading_never_leaks_the_raw_internal_ground_id() -> None:
    """Regression test for the docs-truth-fix wave: `ground_id` (a content
    hash like `sha256(...)[:16]`, e.g. `ground-b72d23845dda7b8e` in
    production) must never appear anywhere in the resident-facing refusals
    explainer -- it is an internal identifier, not something a resident
    should ever see. The heading must use a human-readable label derived
    from the ground's `category` instead, since that's the one piece of
    human-legible data always present on a `GateDecision` regardless of
    whether the caller has supplied a `GroundContent` entry for this
    (unshipped) ground."""
    decisions = [_property_value_ground(), _view_loss_ground(), _flagged_ground()]

    package = await compose_dispatch_package(decisions, _case(), {})

    for decision in decisions:
        assert decision.ground_id not in package.refusals_explainer.markdown
        assert decision.ground_id not in package.refusals_explainer.html

    assert "### Property value" in package.refusals_explainer.markdown
    assert "### Private view loss" in package.refusals_explainer.markdown
    assert "### Site suitability" in package.refusals_explainer.markdown


async def test_refusal_heading_prefers_supplied_claim_text_over_category_label() -> None:
    """When a caller *does* supply a `GroundContent` entry for an unshipped
    ground (today only shipped grounds get one from `job/pipeline.py`, but
    the composer's own contract supports any ground so a future caller can
    extend that), the heading uses a short form of the resident's actual
    claim text rather than the generic category label -- the richer, more
    specific heading wins when it's available."""
    decision = _view_loss_ground()
    claim_content = GroundContent(
        statement=(
            "The new second storey will completely block the harbour view we've had "
            "from our back deck for twenty years, which is the whole reason we bought "
            "this house in the first place."
        ),
        document_title="Objection narrative",
        page=1,
    )

    package = await compose_dispatch_package(
        [decision], _case(), {decision.ground_id: claim_content}
    )

    md = package.refusals_explainer.markdown
    assert decision.ground_id not in md
    assert "### Private view loss" not in md
    assert "The new second storey will completely block the harbour view" in md


async def test_unanchored_view_loss_gets_an_encouraging_note() -> None:
    decisions = [_view_loss_ground()]

    package = await compose_dispatch_package(decisions, _case(), {})

    assert "What would make this viable" in package.refusals_explainer.markdown
    assert "specific planning" in package.refusals_explainer.markdown


async def test_unsubstantiated_ground_encouraging_note_names_the_fix() -> None:
    decisions = [_unsubstantiated_fsr_ground()]

    package = await compose_dispatch_package(decisions, _case(), {})

    md = package.refusals_explainer.markdown
    assert "fsr-calc" in md
    assert "What would make this viable" in md


async def test_flagged_ground_appears_under_review_not_refused() -> None:
    decisions = [_flagged_ground()]

    package = await compose_dispatch_package(decisions, _case(), {})

    md = package.refusals_explainer.markdown
    assert "Still under review" in md
    assert "Refused grounds" not in md
    assert "### Site suitability" in md
    assert "ground-heritage" not in md


async def test_no_shipped_grounds_yields_empty_grounds_section() -> None:
    decisions = [_property_value_ground()]

    package = await compose_dispatch_package(decisions, _case(), {})

    assert "## Grounds of objection" in package.submission.markdown
    assert "### 1." not in package.submission.markdown


async def test_missing_ground_content_for_shipped_ground_raises() -> None:
    height_decision, _content = _height_ground()

    with pytest.raises(ValueError, match="ground-height"):
        await compose_dispatch_package([height_decision], _case(), {})


async def test_annotated_image_reference_appears_only_when_present() -> None:
    height_decision, height_content = _height_ground()
    shadow_decision, shadow_content = _overshadowing_ground()
    decisions = [height_decision, shadow_decision]
    content = {
        height_decision.ground_id: height_content,
        shadow_decision.ground_id: shadow_content,
    }

    package = await compose_dispatch_package(decisions, _case(), content)

    assert "Annotated evidence: overshadowing-3pm-annotated.png" in package.submission.markdown
    # The height ground has no image reference and must not get an empty one.
    height_section = package.submission.markdown.split("### 1.")[1].split("### 2.")[0]
    assert "Annotated evidence" not in height_section


async def test_submission_html_escapes_ground_statement() -> None:
    ruling = PLANNING_HEADS["epi_dcp_provisions"]
    decision = GateDecision(
        ground_id="ground-escape",
        status=GateStatus.SHIPPED,
        category=ruling.category,
        explanation=ruling.explanation,
        statutory_basis=ruling.statutory_basis,
        citation_issues=(),
    )
    content = GroundContent(
        statement="This <script>alert('x')</script> & that",
        document_title="Doc",
        page=1,
    )

    package = await compose_dispatch_package([decision], _case(), {decision.ground_id: content})

    assert "<script>" not in package.submission.html
    assert "&lt;script&gt;" in package.submission.html


# --- optional model polish -----------------------------------------------------


class _FakeUsage:
    def __init__(self) -> None:
        self.prompt_token_count = 10
        self.candidates_token_count = 5
        self.thoughts_token_count = 0


class _FakeResponse:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed
        self.usage_metadata = _FakeUsage()


class _FakeAsyncModels:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeAio:
    def __init__(self, models: _FakeAsyncModels) -> None:
        self.models = models


class _FakeGenaiClient:
    def __init__(self, models: _FakeAsyncModels) -> None:
        self.aio = _FakeAio(models)


_NO_RETRY = RetryPolicy(max_attempts=1)
"""Composer tests never exercise retry backoff: one attempt, fail fast."""


def _polisher(items: list[Any]) -> tuple[ModelClient, _FakeAsyncModels]:
    fake_models = _FakeAsyncModels(items)
    client = ModelClient(genai_client=_FakeGenaiClient(fake_models), retry_policy=_NO_RETRY)
    return client, fake_models


async def test_polish_replaces_markdown_when_headings_preserved() -> None:
    decisions = [_property_value_ground()]
    unpolished = await compose_dispatch_package(decisions, _case(), {})
    polished_text = unpolished.refusals_explainer.markdown.replace(
        "Not every ground", "Not each and every ground"
    )
    # One polish call for the submission, then one for the explainer.
    client, fake_models = _polisher(
        [
            _FakeResponse(PolishedProse(polished_markdown=unpolished.submission.markdown)),
            _FakeResponse(PolishedProse(polished_markdown=polished_text)),
        ]
    )

    package = await compose_dispatch_package(decisions, _case(), {}, polisher=client)

    assert package.refusals_explainer.markdown == polished_text
    assert len(fake_models.calls) == 2


async def test_polish_discarded_when_headings_change() -> None:
    decisions = [_property_value_ground()]
    unpolished = await compose_dispatch_package(decisions, _case(), {})
    bad_polish = "not markdown at all, no headings here"
    client, _fake_models = _polisher(
        [
            _FakeResponse(PolishedProse(polished_markdown=bad_polish)),
            _FakeResponse(PolishedProse(polished_markdown=bad_polish)),
        ]
    )

    package = await compose_dispatch_package(decisions, _case(), {}, polisher=client)

    assert package.refusals_explainer.markdown == unpolished.refusals_explainer.markdown
    assert package.submission.markdown == unpolished.submission.markdown


async def test_polish_discarded_on_model_call_error() -> None:
    from google.genai import errors

    decisions = [_property_value_ground()]
    unpolished = await compose_dispatch_package(decisions, _case(), {})
    error = errors.ServerError(503, {"error": {"message": "unavailable"}})
    client, _fake_models = _polisher([error, error])

    package = await compose_dispatch_package(decisions, _case(), {}, polisher=client)

    assert package.refusals_explainer.markdown == unpolished.refusals_explainer.markdown
    assert package.submission.markdown == unpolished.submission.markdown


async def test_polish_called_independently_for_each_document() -> None:
    height_decision, height_content = _height_ground()
    decisions = [height_decision, _property_value_ground()]
    content = {height_decision.ground_id: height_content}
    submission_heading = "# Objection to Development Application"
    explainer_heading = "# What I left out, and why"
    client, fake_models = _polisher(
        [
            _FakeResponse(PolishedProse(polished_markdown=submission_heading)),
            _FakeResponse(PolishedProse(polished_markdown=explainer_heading)),
        ]
    )

    await compose_dispatch_package(decisions, _case(), content, polisher=client)

    assert len(fake_models.calls) == 2
