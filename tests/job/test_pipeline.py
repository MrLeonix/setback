"""Tests for setback.job.pipeline.RealPipelineRunner: the real end-to-end
tribunal wiring (ingest -> evidence -> court -> gate -> dispatch).

Offline throughout: `_load_frozen_ingest` reads the real checked-in NSW
fixtures (no network), and the court graph is driven by `FakeLlm` doubles
(no ADC, no live model call) -- exactly like `tests/court/test_graph.py`.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from setback.evidence.dossier import BoundingBox, RenderedPage, anchor_id_for, render_pdf_pages
from setback.evidence.grounding import GroundedBox
from setback.evidence.overlays import OVERLAY_COLOR, OverlayRole
from setback.gate.validator import GateStatus
from setback.ingest.tracker import ExhibitedDocument, UserUploadedDocumentSource
from setback.job.pipeline import (
    RealPipelineRunner,
    _first_page_text,
    _GroundedOverlayContext,
    _load_frozen_ingest,
    _plan_document_title,
    _shrink_png_for_storage,
)
from setback.state.firestore import GroundStatus, InMemoryCaseStore, resume_case
from tests.court._fakes import FakeLlm, review_body


def _tiny_white_png(width: int, height: int) -> bytes:
    """A minimal blank white PNG, for overlay-rendering tests that need a
    real (but tiny, shrink-exempt) `RenderedPage.png_bytes` to draw onto."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "nsw" / "docs"
ELEVATIONS_PDF = FIXTURES / "elevations.pdf"
SEE_PDF = FIXTURES / "statement-of-environmental-effects.pdf"

_APPLICATION_NUMBER = "PAN-661190"


_FIRESTORE_DOCUMENT_LIMIT_BYTES = 1_048_487


def test_shrink_png_for_storage_fits_under_the_firestore_document_limit() -> None:
    """A full-resolution rendered PDF page (this fixture's page 1 at
    `DEFAULT_RENDER_DPI`) is ~2 MB as a PNG -- comfortably enough to blow
    past Firestore's ~1 MiB single-document limit once base64-encoded, and
    was measured live doing exactly that
    (`INVALID_ARGUMENT: Property payload contains an invalid nested
    entity.`) before this shrink step existed."""
    page = render_pdf_pages(ELEVATIONS_PDF.read_bytes())[0]
    assert len(page.png_bytes) > _FIRESTORE_DOCUMENT_LIMIT_BYTES  # the regression this guards

    shrunk = _shrink_png_for_storage(page.png_bytes)
    base64_len = (len(shrunk) + 2) // 3 * 4  # base64 expansion, no need to actually encode
    assert base64_len < _FIRESTORE_DOCUMENT_LIMIT_BYTES
    # still a valid, readable PNG
    Image.open(io.BytesIO(shrunk)).load()


def test_shrink_png_for_storage_leaves_a_small_image_untouched() -> None:
    page = render_pdf_pages(ELEVATIONS_PDF.read_bytes())[0]
    assert _shrink_png_for_storage(page.resized_png_bytes) == page.resized_png_bytes


# --- document classification (setback.clerk.classify_document contract) -----


class _FakeDocumentKind:
    """A minimal stand-in for `setback.clerk.DocumentKind.ELEVATIONS` --
    `_plan_document_title` only ever reads `.name`/`str(...)` off whatever
    `classify_document` returns, so a fake needs to support only that."""

    def __init__(self, value: str) -> None:
        self.name = value
        self._value = value

    def __str__(self) -> str:
        return self._value


def test_first_page_text_extracts_real_text_from_a_text_bearing_pdf() -> None:
    text = _first_page_text(SEE_PDF.read_bytes())
    assert "Statement of Environmental Effects" in text


def test_first_page_text_truncates_to_max_chars() -> None:
    text = _first_page_text(SEE_PDF.read_bytes(), max_chars=5)
    assert len(text) <= 5


def test_first_page_text_returns_empty_string_for_a_drawing_with_no_text_layer() -> None:
    """`elevations.pdf` is a rendered architectural drawing with no
    extractable text layer -- classification must degrade gracefully
    (empty string in, `classify_document` still gets called) rather than
    erroring."""
    assert _first_page_text(ELEVATIONS_PDF.read_bytes()) == ""


def test_plan_document_title_combines_kind_and_filename() -> None:
    title = _plan_document_title("elevations.pdf", _FakeDocumentKind("ELEVATIONS"))
    assert title == "Elevations (elevations.pdf)"


def test_plan_document_title_falls_back_to_filename_when_kind_is_none() -> None:
    assert _plan_document_title("elevations.pdf", None) == "elevations.pdf"


def test_plan_document_title_falls_back_to_filename_for_other_kind() -> None:
    """`OTHER` adds no information over the filename alone."""
    assert _plan_document_title("mystery.pdf", _FakeDocumentKind("OTHER")) == "mystery.pdf"


async def test_classify_plan_document_returns_none_without_a_model_client() -> None:
    runner = RealPipelineRunner(document_source=UserUploadedDocumentSource(), grounding_client=None)
    kind = await runner._classify_plan_document(  # noqa: SLF001 -- white-box unit test
        "elevations.pdf", ELEVATIONS_PDF.read_bytes()
    )
    assert kind is None


async def test_classify_plan_document_calls_the_injected_classifier() -> None:
    calls: list[tuple[str, str]] = []

    async def fake_classifier(filename: str, first_page_text: str, *, client: object) -> object:
        calls.append((filename, first_page_text))
        return _FakeDocumentKind("ELEVATIONS")

    runner = RealPipelineRunner(
        document_source=UserUploadedDocumentSource(),
        grounding_client=object(),  # type: ignore[arg-type] -- truthiness only, never called
        document_classifier=fake_classifier,  # type: ignore[arg-type]
    )
    kind = await runner._classify_plan_document(  # noqa: SLF001 -- white-box unit test
        "see.pdf", SEE_PDF.read_bytes()
    )
    assert str(kind) == "ELEVATIONS"
    assert len(calls) == 1
    filename, first_page_text = calls[0]
    assert filename == "see.pdf"
    assert "Statement of Environmental Effects" in first_page_text


async def test_classify_plan_document_degrades_to_none_on_classifier_failure() -> None:
    async def failing_classifier(filename: str, first_page_text: str, *, client: object) -> object:
        raise RuntimeError("clerk model call failed")

    runner = RealPipelineRunner(
        document_source=UserUploadedDocumentSource(),
        grounding_client=object(),  # type: ignore[arg-type]
        document_classifier=failing_classifier,  # type: ignore[arg-type]
    )
    kind = await runner._classify_plan_document(  # noqa: SLF001 -- white-box unit test
        "elevations.pdf", ELEVATIONS_PDF.read_bytes()
    )
    assert kind is None


async def test_build_dossier_uses_the_classified_title_for_the_plan_document() -> None:
    async def fake_classifier(filename: str, first_page_text: str, *, client: object) -> object:
        return _FakeDocumentKind("ELEVATIONS")

    document_source = UserUploadedDocumentSource()
    store = InMemoryCaseStore()
    case_id, _overshadowing_id, _property_value_id = await _seed_case(
        store, document_source=document_source
    )
    runner = RealPipelineRunner(
        document_source=document_source,
        grounding_client=object(),  # type: ignore[arg-type]
        document_classifier=fake_classifier,  # type: ignore[arg-type]
    )
    resume = await resume_case(store, case_id)

    dossier = await runner._build_dossier(case_id, resume)  # noqa: SLF001 -- white-box unit test

    assert dossier.documents["elevations-doc"].title == "Elevations (elevations.pdf)"


async def test_build_dossier_threads_case_id_into_downloaded_documents() -> None:
    """`RealPipelineRunner` must ask its `document_source` for each upload
    with `case_id` set, so a case-scoped store (`GcsEvidenceStore` in
    production) can locate the object."""

    class _RecordingDocumentSource:
        def __init__(self) -> None:
            self.requested: list[ExhibitedDocument] = []

        async def list_documents(self, da_number: str) -> list[ExhibitedDocument]:
            return []

        async def download_document(self, document: ExhibitedDocument) -> bytes:
            self.requested.append(document)
            return ELEVATIONS_PDF.read_bytes()

    document_source = _RecordingDocumentSource()
    store = InMemoryCaseStore()
    case = await store.create_case(application_number=_APPLICATION_NUMBER, resident_session="r-1")
    case_id = case.case_id
    await store.append_event(
        case_id,
        "document-uploaded:doc-1",
        "document_uploaded",
        payload={
            "document_id": "doc-1",
            "filename": "elevations.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1,
        },
    )
    runner = RealPipelineRunner(document_source=document_source, grounding_client=None)  # type: ignore[arg-type]
    resume = await resume_case(store, case_id)

    await runner._build_dossier(case_id, resume)  # noqa: SLF001 -- white-box unit test


async def test_build_dossier_reports_a_download_failure_instead_of_silently_dropping_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed `document_source.download_document` call degrades the
    dossier gracefully (the document is simply excluded, exactly as
    before) but must no longer vanish without a trace -- smoke loop #2
    found `job.main`'s pipeline factory silently handing the job an
    always-empty document source for an entire wave with nothing in any
    log to point at why every evidence-dependent ground kept failing
    review. A bare `except: continue` here would hide the *next* such
    wiring regression exactly the same way."""

    class _FailingDocumentSource:
        async def list_documents(self, da_number: str) -> list[ExhibitedDocument]:
            return []

        async def download_document(self, document: ExhibitedDocument) -> bytes:
            raise RuntimeError("simulated: object not found in bucket")

    store = InMemoryCaseStore()
    case = await store.create_case(application_number=_APPLICATION_NUMBER, resident_session="r-1")
    case_id = case.case_id
    await store.append_event(
        case_id,
        "document-uploaded:doc-1",
        "document_uploaded",
        payload={
            "document_id": "doc-1",
            "filename": "elevations.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1,
        },
    )
    runner = RealPipelineRunner(document_source=_FailingDocumentSource(), grounding_client=None)  # type: ignore[arg-type]
    resume = await resume_case(store, case_id)

    dossier = await runner._build_dossier(case_id, resume)  # noqa: SLF001 -- white-box unit test

    assert "doc-1" not in dossier.documents
    err = capsys.readouterr().err
    assert "doc-1" in err
    assert "elevations.pdf" in err


async def test_load_frozen_ingest_matches_the_checked_in_demo_fixtures() -> None:
    da_record, controls, dcp_documents = await _load_frozen_ingest()

    assert da_record.planning_portal_application_number == _APPLICATION_NUMBER
    assert da_record.council == "Georges River Council"
    assert controls.height_limit_metres is not None
    assert float(controls.height_limit_metres.value) == 9.0
    assert len(dcp_documents) == 5


async def _seed_case(
    store: InMemoryCaseStore,
    *,
    document_source: UserUploadedDocumentSource,
) -> tuple[str, str, str]:
    """Seed a case with two confirmed concerns (one planning-relevant, one
    not) and one uploaded plan document, mirroring what the console's
    interview + upload endpoints record. Returns
    `(case_id, overshadowing_ground_id, property_value_ground_id)`."""
    case = await store.create_case(
        application_number=_APPLICATION_NUMBER, resident_session="resident-1"
    )
    case_id = case.case_id

    plan_document_id = "elevations-doc"
    document_source.add_document(_APPLICATION_NUMBER, plan_document_id, ELEVATIONS_PDF.read_bytes())
    await store.append_event(
        case_id,
        f"document-uploaded:{plan_document_id}",
        "document_uploaded",
        payload={
            "document_id": plan_document_id,
            "filename": "elevations.pdf",
            "content_type": "application/pdf",
            "size_bytes": 12345,
        },
    )

    overshadowing_id = "ground-overshadowing"
    await store.propose_ground(
        case_id, overshadowing_id, claim="The new build will overshadow our north-facing yard."
    )
    await store.append_event(
        case_id,
        f"ground-category:{overshadowing_id}",
        "ground_category_assigned",
        payload={
            "ground_id": overshadowing_id,
            "category": "environmental_and_social_impacts",
            "concern_type": "overshadowing",
            "evidence_document_ids": [plan_document_id],
        },
    )

    property_value_id = "ground-property-value"
    await store.propose_ground(case_id, property_value_id, claim="This will devalue our property.")
    await store.append_event(
        case_id,
        f"ground-category:{property_value_id}",
        "ground_category_assigned",
        payload={
            "ground_id": property_value_id,
            "category": "property_value",
            "concern_type": "property_value",
            "evidence_document_ids": [],
        },
    )

    return case_id, overshadowing_id, property_value_id


async def test_run_ships_a_resolvable_ground_and_refuses_the_irrelevant_one() -> None:
    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case_id, overshadowing_id, property_value_id = await _seed_case(
        store, document_source=document_source
    )

    # The plan document's page-level anchor id -- the only citation the
    # fake reviewers below actually have available to cite.
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    def _clause_fake(ground_id: str) -> FakeLlm:
        return FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[
                review_body(
                    ground_id=ground_id,
                    stance="support",
                    confidence=0.95,
                    cited_anchor_ids=[page_anchor_id],
                    rationale="clause reviewer supports",
                )
            ],
        )

    def _evidence_fake(ground_id: str) -> FakeLlm:
        return FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[
                review_body(
                    ground_id=ground_id,
                    stance="support",
                    confidence=0.9,
                    cited_anchor_ids=[page_anchor_id],
                    rationale="evidence reviewer supports",
                )
            ],
        )

    # `run()` calls the same clause/evidence model for every candidate
    # ground in one pass; `FakeLlm` repeats its last queued body for any
    # call beyond its queue, so one pair of fakes (keyed to the
    # overshadowing ground's id) safely serves both grounds here -- the
    # property-value ground is refused for irrelevance regardless of what
    # its reviewers say, since the gate checks relevance before citations.
    runner = RealPipelineRunner(
        document_source=document_source,
        clause_model=_clause_fake(overshadowing_id),
        evidence_model=_evidence_fake(overshadowing_id),
        grounding_client=None,
    )
    resume = await resume_case(store, case_id)
    await runner.run(case_id, resume, store)

    grounds = {g.ground_id: g for g in await store.list_grounds(case_id)}
    assert grounds[overshadowing_id].status is GroundStatus.SUPPORTED
    assert grounds[property_value_id].status is GroundStatus.REFUSED

    events = await store.list_events(case_id)
    event_types = [e.event_type for e in events]
    assert "review_verdict" in event_types
    assert "gate_decision" in event_types
    assert "submission_composed" in event_types

    gate_decisions = {
        e.payload["ground_id"]: e.payload for e in events if e.event_type == "gate_decision"
    }
    assert gate_decisions[overshadowing_id]["status"] == "shipped"
    assert gate_decisions[property_value_id]["status"] == "refused-irrelevant"

    submission_event = next(e for e in events if e.event_type == "submission_composed")
    assert "overshadow" in submission_event.payload["submission_markdown"].lower()
    assert "property value" not in submission_event.payload["submission_markdown"].lower()
    # Docs-truth-fix wave: `dispatch/composer.py` no longer headers a refusal
    # with the raw internal `ground_id` hash -- it renders a human-readable
    # category label instead (see that module's `_refusal_heading`). This
    # assertion previously checked for `property_value_id` itself, which was
    # exactly the bug being fixed here.
    assert property_value_id not in submission_event.payload["refusals_markdown"]
    assert "### Property value" in submission_event.payload["refusals_markdown"]
    assert "not a matter listed in s4.15(1)" in submission_event.payload["refusals_markdown"]


async def test_a_ground_the_court_rejects_never_ships_even_with_a_resolving_citation() -> None:
    """The gate is a citation/relevance filter only -- it has no concept of
    whether the court found a ground substantively well-founded. A ground
    the reviewers/adjudicator rejected must never reach dispatch just
    because its citation happens to resolve (measured live: a resident's
    unrelated test photo made the Evidence Reviewer correctly reject an
    overshadowing ground that still cited a real, resolvable anchor)."""
    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case_id, overshadowing_id, _property_value_id = await _seed_case(
        store, document_source=document_source
    )
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    runner = RealPipelineRunner(
        document_source=document_source,
        clause_model=FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[
                review_body(
                    ground_id=overshadowing_id,
                    stance="reject",
                    confidence=0.9,
                    cited_anchor_ids=[page_anchor_id],
                    rationale="the evidence does not support the claim",
                )
            ],
        ),
        evidence_model=FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[
                review_body(
                    ground_id=overshadowing_id,
                    stance="reject",
                    confidence=0.9,
                    cited_anchor_ids=[page_anchor_id],
                    rationale="the photo does not show overshadowing",
                )
            ],
        ),
        grounding_client=None,
    )
    resume = await resume_case(store, case_id)
    await runner.run(case_id, resume, store)

    grounds = {g.ground_id: g for g in await store.list_grounds(case_id)}
    assert grounds[overshadowing_id].status is GroundStatus.REFUSED

    events = await store.list_events(case_id)
    gate_decisions = {
        e.payload["ground_id"]: e.payload for e in events if e.event_type == "gate_decision"
    }
    assert gate_decisions[overshadowing_id]["status"] == "refused-unsubstantiated"
    assert "did not find it well-founded" in gate_decisions[overshadowing_id]["explanation"]

    submission_event = next(e for e in events if e.event_type == "submission_composed")
    assert "overshadow" not in submission_event.payload["submission_markdown"].lower()


async def test_an_irrelevant_ground_keeps_its_s415_explanation_even_if_the_court_rejects_it() -> (
    None
):
    """Irrelevance is categorical and permanent (property value is never a
    s4.15(1) matter, no matter the evidence) -- a non-planning ground must
    still surface the specific statutory explanation, not the generic
    "the tribunal didn't find it well-founded" message reserved for a
    relevant-but-unconvincing ground (measured live: both reviewers happen
    to reject property-value grounds outright, which must not swallow the
    more informative irrelevance explanation)."""
    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    _case_id, _overshadowing_id, property_value_id = await _seed_case(
        store, document_source=document_source
    )
    case_id = _case_id

    runner = RealPipelineRunner(
        document_source=document_source,
        clause_model=FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[review_body(ground_id=property_value_id, stance="reject", confidence=1.0)],
        ),
        evidence_model=FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[review_body(ground_id=property_value_id, stance="reject", confidence=1.0)],
        ),
        grounding_client=None,
    )
    resume = await resume_case(store, case_id)
    await runner.run(case_id, resume, store)

    events = await store.list_events(case_id)
    gate_decisions = {
        e.payload["ground_id"]: e.payload for e in events if e.event_type == "gate_decision"
    }
    assert gate_decisions[property_value_id]["status"] == "refused-irrelevant"
    assert "not a matter listed in s4.15(1)" in gate_decisions[property_value_id]["explanation"]


# --- semantic overlay wiring (evidence.overlays, wired in at wave 4's ------
# integration checkpoint -- render_semantic_overlay replaces the old flat
# single-colour render_overlay, and moves from before the ground loop to
# after it, since a box's colour depends on its ground's gate decision) ----


class _FakeGroundingClient:
    """A `ModelClient`-shaped double (duck-typed -- `ModelClient` is a
    concrete class, not a Protocol) that deterministically "locates" one
    labelled element wherever `ground_elements` asks it to, regardless of
    the image actually sent -- enough to exercise the real
    `evidence.grounding.ground_elements`/`_map_to_page_points` geometry
    against `elevations.pdf`'s real rendered page, with zero model call."""

    def __init__(self, box: list[float]) -> None:
        self._box = box

    async def generate(
        self, tier: object, prompt: str, response_model: object, **kwargs: object
    ) -> Any:
        from setback.evidence.grounding import GroundedElement, GroundingResponse
        from setback.models.client import ModelResult, TokenUsage

        return ModelResult(
            output=GroundingResponse(elements=[GroundedElement(label="window W.1", box=self._box)]),
            usage=TokenUsage(prompt_tokens=10, output_tokens=5),
            model="gemini-3.5-flash-lite",
        )


async def test_run_renders_semantic_overlay_colouring_a_shipped_grounds_anchor_green() -> None:
    """The annotated-overlay event `run` emits for a grounded box cited by a
    ground that SHIPPED must come from the semantic (colour-by-outcome)
    renderer, wired in at this checkpoint to replace the old flat
    single-colour overlay, and must be recorded strictly after the ground
    loop's own gate decisions (a box's colour depends on them). Exact
    per-role colour correctness is pinned precisely, at pixel level, by
    `test_semantic_overlay_png_colours_a_shipped_anchor_green` below --
    this test is the end-to-end wiring proof: real geometry, real gate
    decisions, exactly one overlay event, correctly ordered."""
    from setback.evidence.grounding import ground_elements

    grounding_client = _FakeGroundingClient(box=[400.0, 400.0, 500.0, 500.0])
    page = render_pdf_pages(ELEVATIONS_PDF.read_bytes())[0]
    # Run the real grounding call once, up front, purely to learn the real
    # page-point bbox `_map_to_page_points` produces for this fake box --
    # the pipeline run below performs the same call again internally.
    probe = await ground_elements(grounding_client, page, ("window W.1",))
    grounded_bbox = probe.boxes[0].bbox
    grounded_anchor_id = anchor_id_for("elevations-doc", 1, grounded_bbox)

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case_id, overshadowing_id, _property_value_id = await _seed_case(
        store, document_source=document_source
    )

    runner = RealPipelineRunner(
        document_source=document_source,
        clause_model=FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[
                review_body(
                    ground_id=overshadowing_id,
                    stance="support",
                    confidence=0.95,
                    cited_anchor_ids=[grounded_anchor_id],
                    rationale="clause reviewer supports",
                )
            ],
        ),
        evidence_model=FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[
                review_body(
                    ground_id=overshadowing_id,
                    stance="support",
                    confidence=0.9,
                    cited_anchor_ids=[grounded_anchor_id],
                    rationale="evidence reviewer supports",
                )
            ],
        ),
        grounding_client=grounding_client,
    )
    resume = await resume_case(store, case_id)
    await runner.run(case_id, resume, store)

    grounds = {g.ground_id: g for g in await store.list_grounds(case_id)}
    assert grounds[overshadowing_id].status is GroundStatus.SUPPORTED

    events = await store.list_events(case_id)
    gate_decisions = {
        e.payload["ground_id"]: e.payload for e in events if e.event_type == "gate_decision"
    }
    assert gate_decisions[overshadowing_id]["status"] == "shipped"
    overlay_events = [e for e in events if e.event_type == "annotated_overlay"]
    assert len(overlay_events) == 1
    gate_events = [e for e in events if e.event_type == "gate_decision"]
    assert gate_events
    assert overlay_events[0].sequence > gate_events[-1].sequence, (
        "the semantic overlay's colour depends on the gate's decisions, so it "
        "must be recorded strictly after all of them, not before"
    )
    assert overlay_events[0].payload["document_id"] == "elevations-doc"
    # A real, decodable PNG -- shrunk to fit Firestore's document limit
    # (`_shrink_png_for_storage`), which this real `elevations.pdf` render
    # (4962px wide at 300 DPI) is comfortably over.
    image = Image.open(io.BytesIO(base64.b64decode(overlay_events[0].payload["image_base64"])))
    image.load()
    assert image.width <= 1280


def test_semantic_overlay_png_colours_a_shipped_anchor_green() -> None:
    """`RealPipelineRunner._semantic_overlay_png` (the method the wiring
    test above exercises end-to-end) colours a box green exactly when its
    anchor was cited by a SHIPPED ground, pinned here at exact pixel level
    against a small synthetic page -- small enough that `_shrink_png_for_
    storage` is a no-op, so there is no downscale/anti-aliasing to blur the
    colour comparison the way there would be against the real, much larger
    `elevations.pdf` render."""
    tiny_png = _tiny_white_png(100, 100)
    page = RenderedPage(
        page_number=1,
        width_pts=100.0,
        height_pts=100.0,
        dpi=72,
        png_bytes=tiny_png,
        resized_png_bytes=tiny_png,
        resized_width_px=100,
        resized_height_px=100,
    )
    # Page points, origin bottom-left -> top-down pixel box (20, 50)-(50, 80).
    shipped_box = GroundedBox(label="window W.1", bbox=BoundingBox(x0=20, y0=20, x1=50, y1=50))
    shipped_anchor_id = anchor_id_for("doc-1", 1, shipped_box.bbox)
    ctx = _GroundedOverlayContext(document_id="doc-1", page=page, boxes=(shipped_box,))

    runner = RealPipelineRunner(document_source=UserUploadedDocumentSource())
    png_bytes = runner._semantic_overlay_png(
        ctx,
        ground_status={"ground-1": GateStatus.SHIPPED},
        anchor_ground={shipped_anchor_id: "ground-1"},
    )

    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    # Sample just inside the box's top-left corner, comfortably within the
    # 4px-wide drawn border.
    assert image.getpixel((21, 51)) == OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED]


def test_semantic_overlay_png_colours_a_refused_grounds_anchor_red() -> None:
    """The same box, cited by a ground the gate REFUSED instead, is
    coloured red -- `classify_role`'s other branch, exercised through the
    same real method under test."""
    tiny_png = _tiny_white_png(100, 100)
    page = RenderedPage(
        page_number=1,
        width_pts=100.0,
        height_pts=100.0,
        dpi=72,
        png_bytes=tiny_png,
        resized_png_bytes=tiny_png,
        resized_width_px=100,
        resized_height_px=100,
    )
    refused_box = GroundedBox(label="window W.1", bbox=BoundingBox(x0=20, y0=20, x1=50, y1=50))
    refused_anchor_id = anchor_id_for("doc-1", 1, refused_box.bbox)
    ctx = _GroundedOverlayContext(document_id="doc-1", page=page, boxes=(refused_box,))

    runner = RealPipelineRunner(document_source=UserUploadedDocumentSource())
    png_bytes = runner._semantic_overlay_png(
        ctx,
        ground_status={"ground-1": GateStatus.REFUSED_UNSUBSTANTIATED},
        anchor_ground={refused_anchor_id: "ground-1"},
    )

    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    assert image.getpixel((21, 51)) == OVERLAY_COLOR[OverlayRole.ANCHOR_OF_REFUSED]


def test_semantic_overlay_png_colours_an_undecided_anchor_neutral_blue() -> None:
    """An anchor with no ground (or whose ground has no gate decision at
    all yet) stays the neutral blue -- the "just an evidence anchor, no
    outcome to report yet" default."""
    tiny_png = _tiny_white_png(100, 100)
    page = RenderedPage(
        page_number=1,
        width_pts=100.0,
        height_pts=100.0,
        dpi=72,
        png_bytes=tiny_png,
        resized_png_bytes=tiny_png,
        resized_width_px=100,
        resized_height_px=100,
    )
    box = GroundedBox(label="window W.1", bbox=BoundingBox(x0=20, y0=20, x1=50, y1=50))
    ctx = _GroundedOverlayContext(document_id="doc-1", page=page, boxes=(box,))

    runner = RealPipelineRunner(document_source=UserUploadedDocumentSource())
    png_bytes = runner._semantic_overlay_png(ctx, ground_status={}, anchor_ground={})

    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    assert image.getpixel((21, 51)) == OVERLAY_COLOR[OverlayRole.EVIDENCE_ANCHOR]
