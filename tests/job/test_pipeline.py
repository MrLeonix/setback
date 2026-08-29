"""Tests for setback.job.pipeline.RealPipelineRunner: the real end-to-end
tribunal wiring (ingest -> evidence -> court -> gate -> dispatch).

Offline throughout: `_load_frozen_ingest` reads the real checked-in NSW
fixtures (no network), and the court graph is driven by `FakeLlm` doubles
(no ADC, no live model call) -- exactly like `tests/court/test_graph.py`.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from setback.evidence.dossier import anchor_id_for, render_pdf_pages
from setback.ingest.tracker import UserUploadedDocumentSource
from setback.job.pipeline import RealPipelineRunner, _load_frozen_ingest, _shrink_png_for_storage
from setback.state.firestore import GroundStatus, InMemoryCaseStore, resume_case
from tests.court._fakes import FakeLlm, review_body

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "nsw" / "docs"
ELEVATIONS_PDF = FIXTURES / "elevations.pdf"

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
            model="fake-clause",
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
            model="fake-evidence",
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
    assert property_value_id in submission_event.payload["refusals_markdown"]
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
            model="fake-clause",
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
            model="fake-evidence",
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
            model="fake-clause",
            bodies=[review_body(ground_id=property_value_id, stance="reject", confidence=1.0)],
        ),
        evidence_model=FakeLlm(
            model="fake-evidence",
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
