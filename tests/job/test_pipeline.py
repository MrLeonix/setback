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

import httpx
import pytest
import respx
from PIL import Image

from setback.evidence.dossier import (
    BoundingBox,
    EvidenceAnchor,
    ProvenanceGrade,
    RenderedPage,
    SourceDocument,
    anchor_id_for,
    render_pdf_pages,
)
from setback.evidence.grounding import GroundedBox
from setback.evidence.imagery import STREET_VIEW_IMAGE_URL, STREET_VIEW_METADATA_URL
from setback.evidence.overlays import OVERLAY_COLOR, OverlayRole
from setback.gate.validator import GateStatus
from setback.ingest.onlineda import ONLINEDA_URL
from setback.ingest.spatial import ADDRESS_URL, DCP_URL, LAYERINTERSECT_URL
from setback.ingest.tracker import (
    ETRACK_DOWNLOAD_URL,
    ETRACK_SEARCH_URL,
    ExhibitedDocument,
    UserUploadedDocumentSource,
)
from setback.job.pipeline import (
    _MAX_TRACKER_DOCUMENTS,
    _STREET_VIEW_DOCUMENT_ID,
    RealPipelineRunner,
    _first_page_text,
    _GroundedOverlayContext,
    _load_frozen_ingest,
    _load_ingest_for_application,
    _looks_like_plan_document,
    _plan_document_title,
    _propagate_page_level_anchor_status,
    _rank_tracker_documents,
    _select_plan_document,
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

# --- fixtures for the un-frozen (real-DA) ingest tests ------------------------
# A synthetic "other" DA, distinct from the frozen PAN-661190 demo case, so a
# live-resolved test can prove it actually used the typed number's own data
# rather than silently replaying the frozen fixture.

_OTHER_PAN = "PAN-777001"
_OTHER_COUNCIL_REF = "DA2026/9911"
_OTHER_ADDRESS = "12 EXAMPLE STREET KOGARAH 2217"


def _fake_street_view_secret_accessor() -> str:
    return "fake-test-key-not-real"  # noqa: S105 - not a real credential, test-only


def _other_onlineda_payload() -> dict[str, object]:
    return {
        "TotalCount": 1,
        "Application": [
            {
                "PlanningPortalApplicationNumber": _OTHER_PAN,
                "CouncilApplicationNumber": _OTHER_COUNCIL_REF,
                "Council": {"CouncilName": "Georges River Council"},
                "Location": [{"FullAddress": _OTHER_ADDRESS}],
                "DevelopmentType": [{"DevelopmentType": "Alterations and additions"}],
                "ApplicationStatus": "On Exhibition",
                "AssessmentExhibitionStartDate": "2026-09-01",
                "AssessmentExhibitionEndDate": "2026-09-15",
                "CostOfDevelopment": "150000",
            }
        ],
    }


def _other_address_payload() -> list[dict[str, object]]:
    return [{"GURASID": 1, "address": _OTHER_ADDRESS, "propId": 9911001}]


def _other_layerintersect_payload() -> list[dict[str, object]]:
    return [
        {
            "layerName": "Land Zoning Map",
            "results": [
                {
                    "Zone": "R2",
                    "Land Use": "Low Density Residential",
                    "EPI Name": "Georges River Local Environmental Plan 2021",
                    "legislationUrl": (
                        "https://legislation.nsw.gov.au/view/html/inforce/current/epi-2021-0587"
                    ),
                }
            ],
        }
    ]


def _mock_live_onlineda_and_spatial_for_other_pan() -> None:
    """Mock the full live OnlineDA + ePlanning spatial chain for `_OTHER_PAN`
    -- used by every test proving `job.pipeline` genuinely drives real
    ingest off a case's own typed application number, rather than the
    frozen demo fixture."""
    respx.get(ONLINEDA_URL).mock(return_value=httpx.Response(200, json=_other_onlineda_payload()))
    respx.get(ADDRESS_URL).mock(return_value=httpx.Response(200, json=_other_address_payload()))
    respx.get(LAYERINTERSECT_URL).mock(
        return_value=httpx.Response(200, json=_other_layerintersect_payload())
    )
    respx.get(DCP_URL).mock(return_value=httpx.Response(200, json=[{"dcpResults": []}]))


def _mock_no_street_view_coverage() -> None:
    """Mock Street View metadata as `ZERO_RESULTS` (real "no coverage here"
    behaviour, per `evidence.imagery`'s own docstring) -- for every test
    that configures a real `ingest_client` but isn't itself exercising the
    Street View trigger, so `_build_dossier`'s always-attempted fallback
    check degrades to "no fallback image" instead of an unmocked request."""
    respx.get(STREET_VIEW_METADATA_URL).mock(
        return_value=httpx.Response(200, json={"status": "ZERO_RESULTS"})
    )


def _mock_etrack_lists_no_documents() -> None:
    """Mock eTrack's search step to fail (no WebForms fields) -- the
    simplest way to make `_exhibited_tracker_documents` degrade to an
    empty list for tests that aren't exercising the tracker fetch itself."""
    respx.get(ETRACK_SEARCH_URL, params={"ApplicationNumber": _OTHER_COUNCIL_REF}).mock(
        return_value=httpx.Response(200, text="<html><body>no form here</body></html>")
    )


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


# --- Blocker 1 (CASES.md): real-DA overlay/citation grounds on the wrong ----
# document -- a Resident Notification Letter, not the real Elevations
# drawing -- because (a) `_exhibited_tracker_documents` truncated a real
# eTrack listing at `_MAX_TRACKER_DOCUMENTS` before the letter's later-ranked
# plan documents were ever seen, and (b) `_ground_annotated_evidence` picked
# the first `DOCUMENTS_ONLY` document by dict order, with no preference for
# one actually classified/titled as a plan. Both ends fixed via the shared
# `_looks_like_plan_document` title heuristic.


@pytest.mark.parametrize(
    "title",
    [
        "Elevations",
        "elevations.pdf",
        "Site Plan",
        "Site analysis plan",
        "SECTION A-A",
        "Roof drawing",
        "SITE ANALYSIS",
    ],
)
def test_looks_like_plan_document_matches_plan_shaped_titles(title: str) -> None:
    assert _looks_like_plan_document(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Resident Notification Letter",
        "Cover letter",
        "Fee receipt",
        "Statement of Environmental Effects",
        "BASIX Certificate",
    ],
)
def test_looks_like_plan_document_rejects_administrative_titles(title: str) -> None:
    assert _looks_like_plan_document(title) is False


def _document_only(document_id: str, title: str) -> SourceDocument:
    return SourceDocument(
        document_id=document_id,
        title=title,
        provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
        pages=(),
    )


def test_select_plan_document_prefers_a_plan_titled_document_over_dict_order() -> None:
    """CASES.md's Blocker 1 (b), reproduced directly: the Resident
    Notification Letter is first in dict order (exactly the real eTrack
    listing's own order, most-recently-lodged first), but the real
    Elevations document -- present, just later in the dict -- must be
    preferred."""
    documents = [
        _document_only("etrack-5200464", "Resident Notification Letter"),
        _document_only("etrack-5197134", "Elevations (Elevations.pdf)"),
    ]

    selected = _select_plan_document(documents)

    assert selected is not None
    assert selected.document_id == "etrack-5197134"


def test_select_plan_document_falls_back_to_dict_order_when_nothing_looks_like_a_plan() -> None:
    """No regression for a dossier with no plan-shaped title at all -- the
    prior, still-correct "first DOCUMENTS_ONLY document" behaviour."""
    documents = [
        _document_only("doc-1", "Resident Notification Letter"),
        _document_only("doc-2", "Fee receipt"),
    ]

    selected = _select_plan_document(documents)

    assert selected is not None
    assert selected.document_id == "doc-1"


def test_select_plan_document_ignores_non_documents_only_grade() -> None:
    """A resident photo (grade A) titled "site plan photo" is never picked
    as the plan document -- selection is scoped to `DOCUMENTS_ONLY`
    documents exactly as before this fix."""
    photo = SourceDocument(
        document_id="photo-1",
        title="My site plan photo",
        provenance_grade=ProvenanceGrade.RESIDENT_PHOTO,
        pages=(),
    )

    assert _select_plan_document([photo]) is None


def test_select_plan_document_returns_none_for_no_documents() -> None:
    assert _select_plan_document([]) is None


def _tracker_document(document_id: str, title: str) -> ExhibitedDocument:
    return ExhibitedDocument(document_id=document_id, title=title, source="etrack")


def test_rank_tracker_documents_promotes_plan_titles_ahead_of_the_cap() -> None:
    """The real `DA2026/0359` eTrack listing from CASES.md's Blocker 1
    section, in its own real (most-recently-lodged-first) order: the
    Elevations drawing is rank 4, past `_MAX_TRACKER_DOCUMENTS = 3`'s raw
    cutoff, behind a Resident Notification Letter it should never lose to."""
    listed = [
        _tracker_document("5200464", "Resident Notification letter"),
        _tracker_document("5197136", "Site plan"),
        _tracker_document("5197135", "Site analysis plan"),
        _tracker_document("5197134", "Elevations"),
        _tracker_document("5197132", "Notification plan"),
        _tracker_document("5197131", "Perspectives"),
        _tracker_document("5197130", "BASIX Certificate"),
        _tracker_document("5197129", "Waste management plan"),
        _tracker_document("5197128", "Statement of Environmental Effects"),
        _tracker_document("5197127", "Landscape plan"),
        _tracker_document("5197126", "Arborist report"),
        _tracker_document("5197125", "Fee receipt"),
    ]
    assert len(listed) == 12  # matches CASES.md's "12 total" real listing

    ranked = _rank_tracker_documents(listed)
    top = ranked[:_MAX_TRACKER_DOCUMENTS]

    assert [d.document_id for d in top] == ["5197136", "5197135", "5197134"]
    # The exact regression: the letter no longer occupies a top-3 slot, and
    # the real Elevations document does.
    assert "5197134" in [d.document_id for d in top]
    assert "5200464" not in [d.document_id for d in top]


def test_rank_tracker_documents_keeps_a_non_plan_document_when_room_remains() -> None:
    """Ranking never turns into an all-or-nothing exclusion of ordinary
    paperwork -- once every plan-like document has a slot, a ordinary
    document still fills whatever room is left."""
    listed = [
        _tracker_document("1", "Resident Notification letter"),
        _tracker_document("2", "Elevations"),
        _tracker_document("3", "Fee receipt"),
    ]

    ranked = _rank_tracker_documents(listed)
    top = ranked[:_MAX_TRACKER_DOCUMENTS]

    assert {d.document_id for d in top} == {"1", "2", "3"}


def test_rank_tracker_documents_preserves_relative_order_within_each_group() -> None:
    listed = [
        _tracker_document("1", "Perspectives"),
        _tracker_document("2", "Site plan"),
        _tracker_document("3", "Resident Notification letter"),
        _tracker_document("4", "Elevations"),
    ]

    ranked = _rank_tracker_documents(listed)

    assert [d.document_id for d in ranked] == ["2", "4", "1", "3"]


@respx.mock
async def test_exhibited_tracker_documents_selects_the_real_elevations_document() -> None:
    """End-to-end through `_exhibited_tracker_documents` (not just the pure
    `_rank_tracker_documents` helper above): a real 12-document eTrack
    listing modeled on CASES.md's Blocker 1, with the Elevations drawing
    ranked 4th (past the raw `_MAX_TRACKER_DOCUMENTS = 3` cutoff), must
    still be fetched and registered."""
    _mock_live_onlineda_and_spatial_for_other_pan()
    respx.get(ETRACK_SEARCH_URL, params={"ApplicationNumber": _OTHER_COUNCIL_REF}).mock(
        return_value=httpx.Response(200, text=_OTHER_SEARCH_FORM_HTML)
    )
    respx.post(ETRACK_SEARCH_URL, params={"ApplicationNumber": _OTHER_COUNCIL_REF}).mock(
        return_value=httpx.Response(302, headers={"Location": _OTHER_DETAIL_URL})
    )
    documents_html = (
        "<html><body><table>"
        + "".join(
            f"<tr><td>{title}</td><td>"
            f'<a href="../../Common/Integration/FileDownload.ashx?id={doc_id}&amp;ext=PDF'
            f'&amp;filesize=1000">Download</a></td></tr>'
            for doc_id, title in [
                ("5200464", "Resident Notification letter"),
                ("5197136", "Site plan"),
                ("5197135", "Site analysis plan"),
                ("5197134", "Elevations"),
                ("5197132", "Notification plan"),
                ("5197131", "Perspectives"),
            ]
        )
        + "</table></body></html>"
    )
    respx.get(url__startswith=_OTHER_DETAIL_URL.split("?")[0]).mock(
        return_value=httpx.Response(200, text=documents_html)
    )
    respx.get(ETRACK_DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, content=ELEVATIONS_PDF.read_bytes())
    )
    _mock_no_street_view_coverage()

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case = await store.create_case(application_number=_OTHER_PAN, resident_session="resident-1")
    case_id = case.case_id

    async with httpx.AsyncClient(follow_redirects=True) as ingest_client:
        runner = RealPipelineRunner(
            document_source=document_source, ingest_client=ingest_client, grounding_client=None
        )
        resume = await resume_case(store, case_id)
        dossier, _ingest_outcome = await runner._build_dossier(case_id, resume)  # noqa: SLF001

    titles = {doc.document_id: doc.title for doc in dossier.documents.values()}
    assert "etrack-5197134" in titles
    assert "Elevations" in titles["etrack-5197134"]
    assert "etrack-5200464" not in titles  # the notification letter lost its slot


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
        grounding_client=object(),  # truthiness only, never called  # type: ignore[arg-type]
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

    dossier, _ingest_outcome = await runner._build_dossier(case_id, resume)  # noqa: SLF001 -- white-box unit test

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

    dossier, _ingest_outcome = await runner._build_dossier(case_id, resume)  # noqa: SLF001 -- white-box unit test

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


async def _seed_case_single_ground(
    store: InMemoryCaseStore, *, document_source: UserUploadedDocumentSource
) -> tuple[str, str]:
    """Like `_seed_case` but with exactly one confirmed ground -- for tests
    that need every ground's reviewer citation to land on a single,
    known ground (`FakeLlm` repeats its last queued body for any call
    beyond its queue, so a second, unrelated ground in the case would
    otherwise silently reuse the same fake citation, muddying which
    ground a given anchor ends up attributed to). Returns
    `(case_id, ground_id)`."""
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
    return case_id, overshadowing_id


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
    # P0 privacy fix: a shipped ground's letter body is `ground.claim`
    # alone -- never `CourtVerdict.rationale`, which the CLEAR path
    # (`court/graph.py::_finalize_clear`) always synthesizes as literal
    # "Clause Reviewer: ... | Evidence Reviewer: ..." internal-role
    # labels. A resident-facing composed document leaking those labels
    # was the exact bug; this ground's CLEAR verdict (both reviewers
    # above support it) is the case that used to trip it.
    assert "Clause Reviewer:" not in submission_event.payload["submission_markdown"]
    assert "Evidence Reviewer:" not in submission_event.payload["submission_markdown"]
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
    labelled element wherever it's asked to, regardless of the image
    actually sent -- enough to exercise the real
    `evidence.grounding.ground_elements`/`describe_then_ground`/
    `_map_to_page_points` geometry against `elevations.pdf`'s real
    rendered page, with zero model call.

    Branches on `response_model` (the same way the real two-stage
    `describe_then_ground` distinguishes its own two calls): a stage-1
    describe call gets back one described element (``"window W.1"``, an
    `ELEVATION`), and a stage-2 (or direct `ground_elements`) call gets
    back `self._box` under that same label -- so a caller that runs the
    full `describe_then_ground` pipeline sees the same deterministic
    geometry a caller that calls `ground_elements` directly for a probe
    does.
    """

    def __init__(self, box: list[float]) -> None:
        self._box = box

    async def generate(
        self, tier: object, prompt: str, response_model: object, **kwargs: object
    ) -> Any:
        from setback.evidence.grounding import (
            DescribedElement,
            DrawingDescription,
            DrawingType,
            GroundedElement,
            GroundingResponse,
        )
        from setback.models.client import ModelResult, TokenUsage

        output: DrawingDescription | GroundingResponse
        if response_model is DrawingDescription:
            output = DrawingDescription(
                drawing_type=DrawingType.ELEVATION,
                elements=[DescribedElement(name="window W.1", approx_location="upper-left")],
            )
        else:
            output = GroundingResponse(
                elements=[GroundedElement(label="window W.1", box=self._box)]
            )
        return ModelResult(
            output=output,
            usage=TokenUsage(prompt_tokens=10, output_tokens=5),
            model="gemini-3.5-flash-lite",
        )


class _SitePlanFakeGroundingClient:
    """A `ModelClient`-shaped double that describes a page as a `SITE_PLAN`
    with site-plan-vocabulary elements, then grounds exactly those --
    CASES.md's Blocker 1, the core wave-11 regression: a document that
    looks like a site plan must never be grounded with the old hardcoded
    elevation-only labels (window/door/height datum), regardless of what
    document it replaced them on."""

    async def generate(
        self, tier: object, prompt: str, response_model: object, **kwargs: object
    ) -> Any:
        from setback.evidence.grounding import (
            DescribedElement,
            DrawingDescription,
            DrawingType,
            GroundedElement,
            GroundingResponse,
        )
        from setback.models.client import ModelResult, TokenUsage

        output: DrawingDescription | GroundingResponse
        if response_model is DrawingDescription:
            output = DrawingDescription(
                drawing_type=DrawingType.SITE_PLAN,
                elements=[
                    DescribedElement(
                        name="building footprint",
                        approx_location="centre",
                        relevant_to=["height_bulk"],
                    ),
                    DescribedElement(
                        name="north boundary setback",
                        approx_location="left edge",
                        relevant_to=["overshadowing"],
                    ),
                ],
                orientation_cues="north arrow top-left",
            )
        else:
            output = GroundingResponse(
                elements=[
                    GroundedElement(label="building footprint", box=[100.0, 100.0, 400.0, 400.0]),
                    GroundedElement(label="north boundary setback", box=[0.0, 0.0, 1000.0, 50.0]),
                ]
            )
        return ModelResult(
            output=output,
            usage=TokenUsage(prompt_tokens=10, output_tokens=5),
            model="gemini-3.5-flash-lite",
        )


async def test_ground_annotated_evidence_grounds_site_plan_labels_not_elevation_labels() -> None:
    """The founder's diagnosis, end to end through the real pipeline call
    site: a document whose title looks like a site plan (not an elevation)
    must be grounded in site-plan vocabulary (e.g. "building footprint"),
    never the old hardcoded elevation-only label list -- CASES.md's
    Blocker 1, confirmed live on the real `5e791203...` case's Site Plan
    drawing, the exact defect this wave fixes."""
    document_source = UserUploadedDocumentSource()
    store = InMemoryCaseStore()
    case = await store.create_case(
        application_number=_APPLICATION_NUMBER, resident_session="resident-1"
    )
    case_id = case.case_id

    plan_document_id = "site-plan-doc"
    # The real bytes don't matter for this fake-model unit test -- only the
    # filename (routed through `_looks_like_plan_document`/`_select_plan_
    # document`) and the fake grounding client's own scripted responses do.
    document_source.add_document(_APPLICATION_NUMBER, plan_document_id, ELEVATIONS_PDF.read_bytes())
    await store.append_event(
        case_id,
        f"document-uploaded:{plan_document_id}",
        "document_uploaded",
        payload={
            "document_id": plan_document_id,
            "filename": "site-plan.pdf",
            "content_type": "application/pdf",
            "size_bytes": 12345,
        },
    )

    async def _no_classification(filename: str, first_page_text: str, *, client: object) -> object:
        return None

    grounding_client = _SitePlanFakeGroundingClient()
    runner = RealPipelineRunner(
        document_source=document_source,
        grounding_client=grounding_client,  # type: ignore[arg-type]
        document_classifier=_no_classification,  # type: ignore[arg-type]
    )
    resume = await resume_case(store, case_id)
    dossier, _ingest_outcome = await runner._build_dossier(case_id, resume)  # noqa: SLF001

    _dossier, ctx = await runner._ground_annotated_evidence(dossier)  # noqa: SLF001

    assert ctx is not None
    labels = {box.label for box in ctx.boxes}
    assert labels == {"building footprint", "north boundary setback"}
    assert not any("window" in label.lower() or "door" in label.lower() for label in labels)


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


async def test_run_stores_the_full_resolution_overlay_and_references_it_from_the_event() -> None:
    """Wave-9 click-to-open fix (LEO-FEEDBACK-UIUX.md §5: "overlay image
    clickable -> full resolution"): `render_semantic_overlay` already
    returns full-resolution PNG bytes, but until this fix only the
    `_shrink_png_for_storage`-downscaled copy was ever persisted anywhere,
    so a lightbox had nothing full-resolution to open. `run` must now also
    durably write the pre-shrink bytes via the `EvidenceUploadStore` side
    of `document_source` (the same port `UserUploadedDocumentSource`/
    `GcsEvidenceStore` both satisfy) and reference that document id in the
    `annotated_overlay` event payload, so `console/app.py` has something
    to link the overlay image to."""
    from setback.evidence.grounding import ground_elements

    grounding_client = _FakeGroundingClient(box=[400.0, 400.0, 500.0, 500.0])
    page = render_pdf_pages(ELEVATIONS_PDF.read_bytes())[0]
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

    events = await store.list_events(case_id)
    overlay_events = [e for e in events if e.event_type == "annotated_overlay"]
    assert len(overlay_events) == 1
    full_res_document_id = overlay_events[0].payload.get("full_res_document_id")
    assert full_res_document_id, "the event must reference a stored full-resolution document"

    full_res_bytes = await document_source.download_document(
        ExhibitedDocument(document_id=full_res_document_id, title="x", source="pipeline")
    )
    shrunk_bytes = base64.b64decode(overlay_events[0].payload["image_base64"])
    full_res_image = Image.open(io.BytesIO(full_res_bytes))
    full_res_image.load()
    shrunk_image = Image.open(io.BytesIO(shrunk_bytes))
    shrunk_image.load()
    assert full_res_image.width > shrunk_image.width, (
        "the stored full-resolution document must genuinely be higher resolution than "
        "the shrunk copy embedded directly in the event, not just a second copy of it"
    )


async def test_run_never_recolours_a_directly_cited_shipped_anchor_orange_from_an_unrelated_refused_grounds_page_citation() -> (  # noqa: E501
    None
):
    """End-to-end regression test for SMOKE.md v5's "one honest nuance",
    now closed: the overshadowing ground's reviewers cite the specific,
    grounded bbox anchor directly and SHIP; the property-value ground's
    reviewers only ever cite the plan document's whole-page anchor and are
    REFUSED (irrelevant, regardless of stance). Before the rule-(a) fix,
    the property-value ground's more severe, page-level citation of the
    same page won `_most_severe_ground`'s contest against the
    overshadowing ground's own direct citation, turning the grounded box
    orange even though the ground it was actually cited for shipped. The
    fix must keep the box green."""
    from setback.evidence.grounding import ground_elements

    grounding_client = _FakeGroundingClient(box=[400.0, 400.0, 500.0, 500.0])
    page = render_pdf_pages(ELEVATIONS_PDF.read_bytes())[0]
    probe = await ground_elements(grounding_client, page, ("window W.1",))
    grounded_bbox = probe.boxes[0].bbox
    grounded_anchor_id = anchor_id_for("elevations-doc", 1, grounded_bbox)
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case_id, overshadowing_id, property_value_id = await _seed_case(
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
                    rationale="clause reviewer supports overshadowing, citing the specific box",
                ),
                review_body(
                    ground_id=property_value_id,
                    stance="support",
                    confidence=0.9,
                    cited_anchor_ids=[page_anchor_id],
                    rationale="clause reviewer looked at the whole page for property value",
                ),
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
                    rationale="evidence reviewer supports overshadowing, citing the specific box",
                ),
                review_body(
                    ground_id=property_value_id,
                    stance="support",
                    confidence=0.85,
                    cited_anchor_ids=[page_anchor_id],
                    rationale="evidence reviewer looked at the whole page for property value",
                ),
            ],
        ),
        grounding_client=grounding_client,
    )
    resume = await resume_case(store, case_id)
    await runner.run(case_id, resume, store)

    grounds = {g.ground_id: g for g in await store.list_grounds(case_id)}
    assert grounds[overshadowing_id].status is GroundStatus.SUPPORTED
    assert grounds[property_value_id].status is GroundStatus.REFUSED

    events = await store.list_events(case_id)
    gate_statuses = {
        e.payload["ground_id"]: e.payload["status"]
        for e in events
        if e.event_type == "gate_decision"
    }
    assert gate_statuses[overshadowing_id] == "shipped"
    assert gate_statuses[property_value_id] == "refused-irrelevant"

    overlay_events = [e for e in events if e.event_type == "annotated_overlay"]
    assert len(overlay_events) == 1
    image = Image.open(
        io.BytesIO(base64.b64decode(overlay_events[0].payload["image_base64"]))
    ).convert("RGB")

    # Same real-fixture pixel-geometry derivation as `test_run_colours_a_
    # bbox_anchor_shipped_when_only_the_page_level_anchor_was_cited` above:
    # the grounded box's top-left corner, rescaled by whatever
    # `_shrink_png_for_storage` downscaled the stored overlay by.
    pt_to_px = page.dpi / 72.0
    px_x0 = grounded_bbox.x0 * pt_to_px
    px_y0 = (page.height_pts - grounded_bbox.y1) * pt_to_px
    full_res_width = Image.open(io.BytesIO(page.png_bytes)).width
    scale = image.width / full_res_width
    center_x, center_y = round(px_x0 * scale), round(px_y0 * scale)
    window = [
        image.getpixel((x, y))
        for x in range(max(0, center_x - 30), min(image.width, center_x + 30))
        for y in range(max(0, center_y - 30), min(image.height, center_y + 30))
    ]
    assert OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED] in window, (
        "the directly-cited bbox must keep its own ground's SHIPPED colour, never "
        "overridden orange by the unrelated refused ground's page-level citation "
        "of the same page"
    )
    assert OVERLAY_COLOR[OverlayRole.ANCHOR_OF_REFUSED] not in window


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


# --- page-level anchor status propagation (root cause of neutral overlays --
# in live runs: a reviewer/gate citing a document's whole-page anchor,
# rather than one of the fine-grained bbox anchors `_ground_annotated_
# evidence` registers alongside it, left every drawn bbox with no ground of
# its own -- see `_propagate_page_level_anchor_status`'s docstring) --------


async def _dossier_with_a_bbox_anchor(bbox: BoundingBox) -> tuple[Any, str]:
    """A real dossier (via `_seed_case`/`_build_dossier`, exactly like the
    other tests in this module) with one extra, synthetic fine-grained
    bbox anchor registered on `elevations-doc` page 1 -- standing in for
    what `_ground_annotated_evidence`'s real grounding pass would have
    registered. Returns `(dossier, bbox_anchor_id)`."""
    document_source = UserUploadedDocumentSource()
    store = InMemoryCaseStore()
    case_id, _overshadowing_id, _property_value_id = await _seed_case(
        store, document_source=document_source
    )
    runner = RealPipelineRunner(document_source=document_source, grounding_client=None)
    resume = await resume_case(store, case_id)
    dossier, _ingest_outcome = await runner._build_dossier(case_id, resume)  # noqa: SLF001

    bbox_anchor_id = anchor_id_for("elevations-doc", 1, bbox)
    dossier = dossier.with_anchor(
        EvidenceAnchor(
            anchor_id=bbox_anchor_id,
            source_doc="elevations-doc",
            page=1,
            bbox=bbox,
            provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
            caption="window W.1",
        )
    )
    return dossier, bbox_anchor_id


async def test_propagate_page_level_anchor_status_reaches_an_uncited_bbox_anchor() -> None:
    """The exact root-cause fix: a ground's citation of the whole-page
    anchor (`bbox=None`) -- not any specific bbox anchor -- must still
    colour a bbox anchor drawn on that same page, rather than leaving it
    permanently neutral."""
    bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    dossier, bbox_anchor_id = await _dossier_with_a_bbox_anchor(bbox)
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={page_anchor_id: "ground-1"},
        page_level_ground_ids={page_anchor_id: ["ground-1"]},
        ground_status={"ground-1": GateStatus.SHIPPED},
    )

    assert result[bbox_anchor_id] == "ground-1"


async def test_propagate_page_level_anchor_status_leaves_other_pages_untouched() -> None:
    """A page-level citation on one page must never bleed onto a bbox
    anchor registered on a different page of the same document."""
    bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    document_source = UserUploadedDocumentSource()
    store = InMemoryCaseStore()
    case_id, _overshadowing_id, _property_value_id = await _seed_case(
        store, document_source=document_source
    )
    runner = RealPipelineRunner(document_source=document_source, grounding_client=None)
    resume = await resume_case(store, case_id)
    dossier, _ingest_outcome = await runner._build_dossier(case_id, resume)  # noqa: SLF001
    other_page_bbox_anchor_id = anchor_id_for("elevations-doc", 2, bbox)
    dossier = dossier.with_anchor(
        EvidenceAnchor(
            anchor_id=other_page_bbox_anchor_id,
            source_doc="elevations-doc",
            page=2,
            bbox=bbox,
            provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
            caption="window W.1",
        )
    )
    page_1_anchor_id = anchor_id_for("elevations-doc", 1, None)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={page_1_anchor_id: "ground-1"},
        page_level_ground_ids={page_1_anchor_id: ["ground-1"]},
        ground_status={"ground-1": GateStatus.SHIPPED},
    )

    assert other_page_bbox_anchor_id not in result


async def test_propagate_page_level_anchor_status_most_severe_wins_across_grounds() -> None:
    """When more than one ground cites the same page-level anchor and they
    end up with different gate outcomes, the bbox anchors on that page
    must show the most severe (refused > flagged > shipped) of them --
    never a quiet green just because *some* ground on the page shipped,
    while another ground the gate refused also cited it."""
    bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    dossier, bbox_anchor_id = await _dossier_with_a_bbox_anchor(bbox)
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={page_anchor_id: "ground-shipped"},
        page_level_ground_ids={page_anchor_id: ["ground-shipped", "ground-refused"]},
        ground_status={
            "ground-shipped": GateStatus.SHIPPED,
            "ground-refused": GateStatus.REFUSED_UNSUBSTANTIATED,
        },
    )

    assert result[bbox_anchor_id] == "ground-refused"


async def test_propagate_page_level_anchor_status_most_severe_wins_flagged_over_shipped() -> None:
    bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    dossier, bbox_anchor_id = await _dossier_with_a_bbox_anchor(bbox)
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={page_anchor_id: "ground-shipped"},
        page_level_ground_ids={page_anchor_id: ["ground-shipped", "ground-flagged"]},
        ground_status={
            "ground-shipped": GateStatus.SHIPPED,
            "ground-flagged": GateStatus.FLAGGED,
        },
    )

    assert result[bbox_anchor_id] == "ground-flagged"


async def test_propagate_page_level_anchor_status_a_direct_citation_is_never_overridden() -> None:
    """Rule (a) -- the SMOKE.md v5 fix: a bbox anchor that was itself
    directly cited by a shipped ground must keep that ground's status
    outright, even when the page it lives on is *also* cited by an
    unrelated ground the gate refused. Direct and inherited citations do
    NOT compete on the same most-severe footing (the pre-fix behaviour this
    test previously pinned, since corrected) -- a box a reviewer actually
    pointed at must never be recoloured by a claim on the page it merely
    happens to sit on."""
    bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    dossier, bbox_anchor_id = await _dossier_with_a_bbox_anchor(bbox)
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={
            page_anchor_id: "ground-refused",
            bbox_anchor_id: "ground-shipped",
        },
        page_level_ground_ids={page_anchor_id: ["ground-refused"]},
        ground_status={
            "ground-shipped": GateStatus.SHIPPED,
            "ground-refused": GateStatus.REFUSED_UNSUBSTANTIATED,
        },
    )

    assert result[bbox_anchor_id] == "ground-shipped"


async def test_propagate_page_level_anchor_status_direct_and_inherited_anchors_resolve_independently() -> (  # noqa: E501
    None
):
    """Rules (a) and (b) together, in one call, wave-9 semantics: a bbox
    anchor directly cited by a shipped ground keeps that status untouched,
    while a *second*, uncited bbox anchor on the very same page -- cited by
    nothing of its own -- does NOT inherit the page-level ground's status,
    because that page already has a direct citation of its own (rule (b)'s
    all-or-nothing-per-page fallback). This is the exact "meaningless
    mid-house boxes" regression fix: a page-level citation from a ground
    unrelated to the second element must not paint it by inference just
    because it happened to be the only uncited box on the page."""
    cited_bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    uncited_bbox = BoundingBox(x0=60, y0=60, x1=90, y1=90)
    dossier, cited_anchor_id = await _dossier_with_a_bbox_anchor(cited_bbox)
    uncited_anchor_id = anchor_id_for("elevations-doc", 1, uncited_bbox)
    dossier = dossier.with_anchor(
        EvidenceAnchor(
            anchor_id=uncited_anchor_id,
            source_doc="elevations-doc",
            page=1,
            bbox=uncited_bbox,
            provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
            caption="window W.2",
        )
    )
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={
            page_anchor_id: "ground-refused",
            cited_anchor_id: "ground-shipped",
        },
        page_level_ground_ids={page_anchor_id: ["ground-refused"]},
        ground_status={
            "ground-shipped": GateStatus.SHIPPED,
            "ground-refused": GateStatus.REFUSED_UNSUBSTANTIATED,
        },
    )

    assert result[cited_anchor_id] == "ground-shipped"  # rule (a): untouched
    assert uncited_anchor_id not in result  # rule (b): no longer inherits (wave 9)


async def test_propagate_page_level_anchor_status_does_not_paint_unrelated_elements_on_a_cited_page() -> (  # noqa: E501
    None
):
    """The film-case regression itself, reconstructed: a page has one bbox
    anchor directly cited by a SHIPPED ground (the "9m height limit datum
    line"), plus several other bbox anchors on the same page (windows/door)
    that no ground ever cited directly. A *different*, REFUSED ground's
    page-level citation of that same page must NOT recolour those unrelated
    elements -- they must stay absent from the result (renders neutral
    grey), never repainted orange by a ground that never discussed them."""
    datum_line_bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    window_bboxes = [
        BoundingBox(x0=60, y0=60, x1=90, y1=90),
        BoundingBox(x0=100, y0=100, x1=130, y1=130),
        BoundingBox(x0=140, y0=140, x1=170, y1=170),
    ]
    door_bbox = BoundingBox(x0=180, y0=180, x1=210, y1=210)
    dossier, datum_line_anchor_id = await _dossier_with_a_bbox_anchor(datum_line_bbox)
    unrelated_anchor_ids = []
    for i, bbox in enumerate([*window_bboxes, door_bbox]):
        anchor_id = anchor_id_for("elevations-doc", 1, bbox)
        unrelated_anchor_ids.append(anchor_id)
        dossier = dossier.with_anchor(
            EvidenceAnchor(
                anchor_id=anchor_id,
                source_doc="elevations-doc",
                page=1,
                bbox=bbox,
                provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
                caption=f"element {i}",
            )
        )
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={
            page_anchor_id: "ground-property-value",
            datum_line_anchor_id: "ground-overshadowing",
        },
        page_level_ground_ids={page_anchor_id: ["ground-property-value"]},
        ground_status={
            "ground-overshadowing": GateStatus.SHIPPED,
            "ground-property-value": GateStatus.REFUSED_UNSUBSTANTIATED,
        },
    )

    assert result[datum_line_anchor_id] == "ground-overshadowing"
    for anchor_id in unrelated_anchor_ids:
        assert anchor_id not in result


async def test_propagate_page_level_anchor_status_prefers_the_ground_whose_evidence_included_this_document() -> (  # noqa: E501
    None
):
    """Rule (c): when more than one ground's page-level citation competes
    for the same uncited bbox anchor, a ground whose own `EvidenceSlice`
    actually included this bbox's document is preferred over one that did
    not -- even though the excluded ground's own gate status is more
    severe. Without this preference, severity alone would let a ground
    that was never even shown this document outrank one that was."""
    bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    dossier, bbox_anchor_id = await _dossier_with_a_bbox_anchor(bbox)
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={},
        page_level_ground_ids={page_anchor_id: ["ground-shipped", "ground-refused"]},
        ground_status={
            "ground-shipped": GateStatus.SHIPPED,
            "ground-refused": GateStatus.REFUSED_UNSUBSTANTIATED,
        },
        ground_document_ids={
            "ground-shipped": frozenset({"elevations-doc"}),
            "ground-refused": frozenset({"some-other-document"}),
        },
    )

    assert result[bbox_anchor_id] == "ground-shipped"


async def test_propagate_page_level_anchor_status_falls_back_to_severity_when_no_ground_qualifies() -> (  # noqa: E501
    None
):
    """Rule (c)'s defensive fallback: if `ground_document_ids` is supplied
    but no competing ground's evidence slice actually included this
    document (should not arise given how `page_level_ground_ids` itself is
    built, but handled rather than silently producing no winner), the
    most-severe tie-break still applies across every candidate -- exactly
    as when no preference data is available at all."""
    bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    dossier, bbox_anchor_id = await _dossier_with_a_bbox_anchor(bbox)
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={},
        page_level_ground_ids={page_anchor_id: ["ground-shipped", "ground-refused"]},
        ground_status={
            "ground-shipped": GateStatus.SHIPPED,
            "ground-refused": GateStatus.REFUSED_UNSUBSTANTIATED,
        },
        ground_document_ids={
            "ground-shipped": frozenset({"unrelated-a"}),
            "ground-refused": frozenset({"unrelated-b"}),
        },
    )

    assert result[bbox_anchor_id] == "ground-refused"


async def test_propagate_status_keeps_a_direct_citation_when_no_page_citation_exists() -> None:
    """A bbox anchor with only a direct citation (no page-level citation on
    its page at all) must keep exactly that citation, unchanged."""
    bbox = BoundingBox(x0=10, y0=10, x1=50, y1=50)
    dossier, bbox_anchor_id = await _dossier_with_a_bbox_anchor(bbox)

    result = _propagate_page_level_anchor_status(
        dossier,
        anchor_ground={bbox_anchor_id: "ground-shipped"},
        page_level_ground_ids={},
        ground_status={"ground-shipped": GateStatus.SHIPPED},
    )

    assert result[bbox_anchor_id] == "ground-shipped"


async def test_run_colours_a_bbox_anchor_shipped_when_only_the_page_level_anchor_was_cited() -> (
    None
):
    """End-to-end regression test for the root cause itself: reviewers cite
    only the plan document's whole-page anchor (exactly what a real
    Gemini reviewer looking at one full-page image, rather than a specific
    fine-grained crop, plausibly does) while a grounding pass has also
    registered a fine-grained bbox anchor on that same page. Before this
    fix, the rendered overlay's box stayed neutral grey; the fix must
    colour it green (the shipped ground's colour)."""
    from setback.evidence.grounding import ground_elements

    grounding_client = _FakeGroundingClient(box=[400.0, 400.0, 500.0, 500.0])
    page = render_pdf_pages(ELEVATIONS_PDF.read_bytes())[0]
    probe = await ground_elements(grounding_client, page, ("window W.1",))
    grounded_bbox = probe.boxes[0].bbox
    grounded_anchor_id = anchor_id_for("elevations-doc", 1, grounded_bbox)
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)
    assert grounded_anchor_id != page_anchor_id  # sanity: these are genuinely distinct anchors

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case_id, overshadowing_id = await _seed_case_single_ground(
        store, document_source=document_source
    )

    # Both reviewers cite only the page-level anchor -- never the
    # fine-grained bbox anchor grounding registered alongside it.
    runner = RealPipelineRunner(
        document_source=document_source,
        clause_model=FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[
                review_body(
                    ground_id=overshadowing_id,
                    stance="support",
                    confidence=0.95,
                    cited_anchor_ids=[page_anchor_id],
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
                    cited_anchor_ids=[page_anchor_id],
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
    overlay_events = [e for e in events if e.event_type == "annotated_overlay"]
    assert len(overlay_events) == 1
    image = Image.open(
        io.BytesIO(base64.b64decode(overlay_events[0].payload["image_base64"]))
    ).convert("RGB")
    # The grounded box's top-left corner, in this render's real full-res
    # pixel geometry (same derivation `test_semantic_overlay_png_colours_
    # a_shipped_anchor_green` uses) -- then rescaled by whatever
    # `_shrink_png_for_storage` downscaled the stored overlay by, since
    # this fixture's real full-resolution render (4962px wide) is well
    # over the storage width cap.
    pt_to_px = page.dpi / 72.0
    px_x0 = grounded_bbox.x0 * pt_to_px
    px_y0 = (page.height_pts - grounded_bbox.y1) * pt_to_px
    full_res_width = Image.open(io.BytesIO(page.png_bytes)).width
    scale = image.width / full_res_width
    center_x, center_y = round(px_x0 * scale), round(px_y0 * scale)
    # Scan a window around the estimated corner for the shipped colour.
    # The real `elevations.pdf` render is downscaled ~4x for storage
    # (`_shrink_png_for_storage`), which both shifts the thin (4px-wide,
    # pre-shrink) drawn border by tens of pixels from this estimate and
    # anti-aliases most of it into blended shades -- only a handful of
    # pixels survive as the exact `OVERLAY_COLOR` value, so the window
    # must be generous, not pixel-tight.
    window = [
        image.getpixel((x, y))
        for x in range(max(0, center_x - 30), min(image.width, center_x + 30))
        for y in range(max(0, center_y - 30), min(image.height, center_y + 30))
    ]
    assert OVERLAY_COLOR[OverlayRole.SUPPORTS_SHIPPED] in window, (
        "the grounded bbox must inherit the SHIPPED status from the page-level "
        "citation its reviewers actually made, not stay neutral grey"
    )


# --- un-frozen ingest: `_load_ingest_for_application` (wave 9) --------------


@respx.mock
async def test_load_ingest_for_application_uses_live_data_when_a_client_is_configured() -> None:
    """A typed application number that resolves live must produce the DA
    record it actually resolves to -- not the frozen PAN-661190 demo case
    -- proving real ingest is genuinely wired, not just labelled as such."""
    _mock_live_onlineda_and_spatial_for_other_pan()

    async with httpx.AsyncClient() as client:
        outcome = await _load_ingest_for_application(_OTHER_PAN, client=client)

    assert outcome.used_demo_fixture is False
    assert outcome.demo_fixture_reason is None
    assert outcome.da_record.planning_portal_application_number == _OTHER_PAN
    assert outcome.da_record.council_application_number == _OTHER_COUNCIL_REF
    assert outcome.da_record.address == _OTHER_ADDRESS
    assert outcome.controls.zone_code.value == "R2"


async def test_load_ingest_for_application_uses_the_demo_fixture_with_no_client_configured() -> (
    None
):
    """Every existing offline test leaves `ingest_client=None` -- this must
    keep degrading to the frozen demo fixture exactly as before this wave,
    with no network attempted at all."""
    outcome = await _load_ingest_for_application(_OTHER_PAN, client=None)

    assert outcome.used_demo_fixture is True
    assert outcome.demo_fixture_reason == (
        "no live ingest client configured; showing the demo fixture case"
    )
    assert outcome.da_record.planning_portal_application_number == _APPLICATION_NUMBER


@respx.mock
async def test_load_ingest_for_application_falls_back_on_a_resolution_failure() -> None:
    """A typed number OnlineDA has never heard of (a real, live possibility
    -- a mistyped PAN, a DA from a council this build doesn't speak) must
    fail soft into the labelled demo fixture, never raise and never crash
    the run."""
    respx.get(ONLINEDA_URL).mock(
        return_value=httpx.Response(200, json={"TotalCount": 0, "Application": []})
    )

    async with httpx.AsyncClient() as client:
        outcome = await _load_ingest_for_application("PAN-UNKNOWN-999", client=client)

    assert outcome.used_demo_fixture is True
    assert outcome.demo_fixture_reason is not None
    assert "PAN-UNKNOWN-999" in outcome.demo_fixture_reason
    assert "ApplicationNotFoundError" in outcome.demo_fixture_reason
    assert outcome.da_record.planning_portal_application_number == _APPLICATION_NUMBER


# --- un-frozen ingest: exhibited tracker documents (wave 9) -----------------

_OTHER_DETAIL_URL = (
    "https://etrack.georgesriver.nsw.gov.au/Pages/XC.Track/SearchApplication.aspx"
    "?id=555001&a=DA2026%2f9911"
)

_OTHER_SEARCH_FORM_HTML = """
<html><body><form>
<input type="hidden" id="__VIEWSTATE" value="vs-token" />
<input type="hidden" id="__VIEWSTATEGENERATOR" value="vsg-token" />
<input type="hidden" id="__EVENTVALIDATION" value="ev-token" />
</form></body></html>
"""

_OTHER_DOCUMENTS_HTML = (
    "<html><body><table>"
    "<tr><td>Site Plan</td><td>"
    '<a href="../../Common/Integration/FileDownload.ashx?id=90001&amp;ext=PDF'
    '&amp;filesize=54321">Download</a>'
    "</td></tr>"
    "</table></body></html>"
)


def _mock_etrack_lists_one_document() -> None:
    respx.get(ETRACK_SEARCH_URL, params={"ApplicationNumber": _OTHER_COUNCIL_REF}).mock(
        return_value=httpx.Response(200, text=_OTHER_SEARCH_FORM_HTML)
    )
    respx.post(ETRACK_SEARCH_URL, params={"ApplicationNumber": _OTHER_COUNCIL_REF}).mock(
        return_value=httpx.Response(302, headers={"Location": _OTHER_DETAIL_URL})
    )
    respx.get(url__startswith=_OTHER_DETAIL_URL.split("?")[0]).mock(
        return_value=httpx.Response(200, text=_OTHER_DOCUMENTS_HTML)
    )
    respx.get(ETRACK_DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, content=ELEVATIONS_PDF.read_bytes())
    )


@respx.mock
async def test_build_dossier_includes_a_real_exhibited_document_from_the_tracker() -> None:
    """Once live OnlineDA resolution succeeds, `_build_dossier` must also
    fetch the case's real exhibited documents from the tracker (the eTrack
    "same tracker family" scope this wave allows) -- not just documents a
    resident happened to upload themselves."""
    _mock_live_onlineda_and_spatial_for_other_pan()
    _mock_etrack_lists_one_document()
    _mock_no_street_view_coverage()

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case = await store.create_case(application_number=_OTHER_PAN, resident_session="resident-1")
    case_id = case.case_id

    async with httpx.AsyncClient(follow_redirects=True) as ingest_client:
        runner = RealPipelineRunner(
            document_source=document_source, ingest_client=ingest_client, grounding_client=None
        )
        resume = await resume_case(store, case_id)
        dossier, ingest_outcome = await runner._build_dossier(case_id, resume)  # noqa: SLF001

    assert ingest_outcome.used_demo_fixture is False
    tracker_doc = dossier.documents["etrack-90001"]
    assert tracker_doc.provenance_grade is ProvenanceGrade.DOCUMENTS_ONLY
    assert "Site Plan" in tracker_doc.title


@respx.mock
async def test_build_dossier_never_fetches_tracker_documents_in_demo_fixture_mode() -> None:
    """When live resolution fails and the run degrades to the demo fixture,
    the tracker must never be hit for this case's (unresolved) exhibited
    documents -- there is no council reference to look up."""
    respx.get(ONLINEDA_URL).mock(
        return_value=httpx.Response(200, json={"TotalCount": 0, "Application": []})
    )
    _mock_no_street_view_coverage()
    # Deliberately no eTrack route mocked -- if `_build_dossier` ever
    # attempted a tracker fetch anyway, respx's own assertion (every
    # request must be mocked) would fail this test.

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case = await store.create_case(
        application_number="PAN-UNKNOWN-999", resident_session="resident-1"
    )
    case_id = case.case_id

    async with httpx.AsyncClient(follow_redirects=True) as ingest_client:
        runner = RealPipelineRunner(
            document_source=document_source, ingest_client=ingest_client, grounding_client=None
        )
        resume = await resume_case(store, case_id)
        dossier, ingest_outcome = await runner._build_dossier(case_id, resume)  # noqa: SLF001

    assert ingest_outcome.used_demo_fixture is True
    assert dossier.da_record.planning_portal_application_number == _APPLICATION_NUMBER


# --- un-frozen ingest: `run` emits an honest `ingest_resolved` event, and --
# the composed letterhead always matches whatever was actually ingested ----


@respx.mock
async def test_run_emits_ingest_resolved_with_live_data_and_uses_it_for_the_letterhead() -> None:
    _mock_live_onlineda_and_spatial_for_other_pan()
    _mock_etrack_lists_no_documents()
    _mock_no_street_view_coverage()

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case = await store.create_case(application_number=_OTHER_PAN, resident_session="resident-1")
    case_id = case.case_id

    plan_document_id = "elevations-doc"
    document_source.add_document(_OTHER_PAN, plan_document_id, ELEVATIONS_PDF.read_bytes())
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
    ground_id = "ground-overshadowing"
    await store.propose_ground(case_id, ground_id, claim="Overshadowing concern.")
    await store.append_event(
        case_id,
        f"ground-category:{ground_id}",
        "ground_category_assigned",
        payload={
            "ground_id": ground_id,
            "category": "environmental_and_social_impacts",
            "concern_type": "overshadowing",
            "evidence_document_ids": [plan_document_id],
        },
    )

    page_anchor_id = anchor_id_for(plan_document_id, 1, None)

    async def _run_it() -> None:
        async with httpx.AsyncClient(follow_redirects=True) as ingest_client:
            runner = RealPipelineRunner(
                document_source=document_source,
                ingest_client=ingest_client,
                grounding_client=None,
                clause_model=FakeLlm(
                    model="gemini-3.5-flash-lite",
                    bodies=[
                        review_body(
                            ground_id=ground_id,
                            stance="support",
                            confidence=0.9,
                            cited_anchor_ids=[page_anchor_id],
                        )
                    ],
                ),
                evidence_model=FakeLlm(
                    model="gemini-3.5-flash-lite",
                    bodies=[
                        review_body(
                            ground_id=ground_id,
                            stance="support",
                            confidence=0.9,
                            cited_anchor_ids=[page_anchor_id],
                        )
                    ],
                ),
            )
            resume = await resume_case(store, case_id)
            await runner.run(case_id, resume, store)

    await _run_it()

    events = await store.list_events(case_id)
    ingest_events = [e for e in events if e.event_type == "ingest_resolved"]
    assert len(ingest_events) == 1
    assert ingest_events[0].payload["used_demo_fixture"] is False
    assert ingest_events[0].payload["application_number"] == _OTHER_PAN
    assert ingest_events[0].payload["council_application_number"] == _OTHER_COUNCIL_REF
    assert ingest_events[0].payload["address"] == _OTHER_ADDRESS
    assert "reason" not in ingest_events[0].payload

    submission_event = next(e for e in events if e.event_type == "submission_composed")
    assert _OTHER_COUNCIL_REF in submission_event.payload["submission_markdown"]
    assert "DA2026/0359" not in submission_event.payload["submission_markdown"]


@respx.mock
async def test_run_falls_back_to_the_demo_fixture_and_labels_it_when_live_resolution_fails() -> (
    None
):
    """The letterhead must never lie: when live resolution fails, both the
    event and the composed submission must reflect the demo fixture that
    was *actually* used, not the number the resident typed."""
    respx.get(ONLINEDA_URL).mock(
        return_value=httpx.Response(200, json={"TotalCount": 0, "Application": []})
    )
    _mock_no_street_view_coverage()

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case = await store.create_case(
        application_number="PAN-UNKNOWN-999", resident_session="resident-1"
    )
    case_id = case.case_id

    async with httpx.AsyncClient(follow_redirects=True) as ingest_client:
        runner = RealPipelineRunner(
            document_source=document_source, ingest_client=ingest_client, grounding_client=None
        )
        resume = await resume_case(store, case_id)
        await runner.run(case_id, resume, store)

    events = await store.list_events(case_id)
    ingest_events = [e for e in events if e.event_type == "ingest_resolved"]
    assert len(ingest_events) == 1
    assert ingest_events[0].payload["used_demo_fixture"] is True
    assert "PAN-UNKNOWN-999" in ingest_events[0].payload["reason"]
    assert ingest_events[0].payload["council_application_number"] == "DA2026/0359"

    submission_event = next(e for e in events if e.event_type == "submission_composed")
    assert "DA2026/0359" in submission_event.payload["submission_markdown"]


# --- job-side idempotency guard: the "Fix 4 -- not fixed" re-press crash ---


async def test_run_is_a_safe_noop_when_the_case_tribunal_already_ran_to_completion() -> None:
    """Starting the tribunal a second time on a case that already ran to
    completion must never crash -- a judge will press the button twice."""
    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case_id, overshadowing_id, property_value_id = await _seed_case(
        store, document_source=document_source
    )
    page_anchor_id = anchor_id_for("elevations-doc", 1, None)

    def _fake(ground_id: str) -> FakeLlm:
        return FakeLlm(
            model="gemini-3.5-flash-lite",
            bodies=[
                review_body(
                    ground_id=ground_id,
                    stance="support",
                    confidence=0.9,
                    cited_anchor_ids=[page_anchor_id],
                )
            ],
        )

    runner = RealPipelineRunner(
        document_source=document_source,
        clause_model=_fake(overshadowing_id),
        evidence_model=_fake(overshadowing_id),
        grounding_client=None,
    )
    resume = await resume_case(store, case_id)
    await runner.run(case_id, resume, store)

    events_after_first_run = await store.list_events(case_id)
    first_submission_count = sum(
        1 for e in events_after_first_run if e.event_type == "submission_composed"
    )
    assert first_submission_count == 1

    resume_again = await resume_case(store, case_id)
    await runner.run(case_id, resume_again, store)  # must not raise

    events_after_second_run = await store.list_events(case_id)
    assert (
        sum(1 for e in events_after_second_run if e.event_type == "submission_composed")
        == first_submission_count
    )
    rerun_ignored = [e for e in events_after_second_run if e.event_type == "tribunal_rerun_ignored"]
    assert len(rerun_ignored) == 1

    grounds = {g.ground_id: g for g in await store.list_grounds(case_id)}
    assert grounds[overshadowing_id].status is GroundStatus.SUPPORTED
    assert grounds[property_value_id].status is GroundStatus.REFUSED


async def test_run_skips_an_already_terminal_ground_instead_of_crashing() -> None:
    """Defence in depth alongside the top-level guard above: a ground that
    somehow reached a terminal status without the case's own
    `submission_composed` event yet existing (e.g. a prior execution
    crashed mid-loop) must be skipped, not re-transitioned into a
    `InvalidGroundTransitionError` crash."""
    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case_id, overshadowing_id, property_value_id = await _seed_case(
        store, document_source=document_source
    )
    await store.transition_ground(case_id, overshadowing_id, GroundStatus.UNDER_REVIEW)
    await store.transition_ground(case_id, overshadowing_id, GroundStatus.SUPPORTED)

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
    await runner.run(case_id, resume, store)  # must not raise

    events = await store.list_events(case_id)
    skipped = [e for e in events if e.event_type == "ground_rerun_skipped"]
    assert len(skipped) == 1
    assert skipped[0].payload["ground_id"] == overshadowing_id
    assert skipped[0].payload["status"] == "supported"

    grounds = {g.ground_id: g for g in await store.list_grounds(case_id)}
    assert grounds[overshadowing_id].status is GroundStatus.SUPPORTED  # untouched
    assert grounds[property_value_id].status is GroundStatus.REFUSED  # processed normally


# --- Street View fallback trigger (wave 9) ----------------------------------


@respx.mock
async def test_build_dossier_adds_a_street_view_fallback_when_no_resident_photo_is_uploaded() -> (
    None
):
    """The exact trigger: no resident-photo upload, a resolvable address,
    and a live ingest client configured -- must add a grade-B fallback
    photo document whose title (and therefore its anchor's caption) carries
    the visible attribution."""
    _mock_live_onlineda_and_spatial_for_other_pan()
    _mock_etrack_lists_no_documents()
    respx.get(STREET_VIEW_METADATA_URL).mock(
        return_value=httpx.Response(200, json={"status": "OK", "pano_id": "p1", "date": "2023-01"})
    )
    respx.get(STREET_VIEW_IMAGE_URL).mock(
        return_value=httpx.Response(200, content=_tiny_white_png(50, 50))
    )

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case = await store.create_case(application_number=_OTHER_PAN, resident_session="resident-1")
    case_id = case.case_id
    plan_document_id = "elevations-doc"
    document_source.add_document(_OTHER_PAN, plan_document_id, ELEVATIONS_PDF.read_bytes())
    await store.append_event(
        case_id,
        f"document-uploaded:{plan_document_id}",
        "document_uploaded",
        payload={
            "document_id": plan_document_id,
            "filename": "elevations.pdf",
            "content_type": "application/pdf",
            "size_bytes": 123,
        },
    )

    async with httpx.AsyncClient(follow_redirects=True) as ingest_client:
        runner = RealPipelineRunner(
            document_source=document_source,
            ingest_client=ingest_client,
            grounding_client=None,
            street_view_secret_accessor=_fake_street_view_secret_accessor,
        )
        resume = await resume_case(store, case_id)
        dossier, ingest_outcome = await runner._build_dossier(  # noqa: SLF001
            case_id, resume, store=store
        )

    assert ingest_outcome.used_demo_fixture is False
    street_view_doc = dossier.documents[_STREET_VIEW_DOCUMENT_ID]
    assert street_view_doc.provenance_grade is ProvenanceGrade.STREET_VIEW_SOLAR_FALLBACK
    assert "(c) Google Street View, 2023-01" in street_view_doc.title
    anchor = next(a for a in dossier.anchors.values() if a.source_doc == _STREET_VIEW_DOCUMENT_ID)
    assert "(c) Google Street View, 2023-01" in anchor.caption

    # LEO-FEEDBACK-UIUX.md §4: the fallback must actually render on the
    # resident-facing case page, not just feed the grounding model -- a
    # real gap found live against real (non-fixture) cases (see the
    # populate pass's "Blocker 2"), where the dossier carried the fallback
    # correctly but nothing ever told the console's Evidence section it
    # existed. A `document_uploaded` event (the only event type the
    # Evidence section renders from -- console/app.py's own
    # `_SECTION_FOR_EVENT_TYPE` map) must be recorded, and the image bytes
    # must be durably retrievable through the same
    # `GET /api/cases/{id}/documents/{document_id}` route every other
    # doc-card uses.
    events = await store.list_events(case_id)
    street_view_events = [
        e
        for e in events
        if e.event_type == "document_uploaded"
        and e.payload.get("document_id") == _STREET_VIEW_DOCUMENT_ID
    ]
    assert len(street_view_events) == 1
    payload = street_view_events[0].payload
    assert payload["content_type"] == "image/jpeg"
    assert payload["provenance_grade"] == "B"
    assert "(c) Google Street View, 2023-01" in payload["filename"]
    stored_bytes = await document_source.download_document(
        ExhibitedDocument(
            document_id=_STREET_VIEW_DOCUMENT_ID,
            title="street view",
            source="street-view-fallback",
            case_id=case_id,
        )
    )
    assert stored_bytes == _tiny_white_png(50, 50)


@respx.mock
async def test_build_dossier_does_not_fetch_street_view_when_a_resident_photo_exists() -> None:
    """No Street View route is mocked at all in this test -- if
    `_build_dossier` ever attempted the call anyway despite a resident
    photo already existing, respx's own "every request must be mocked"
    assertion fails this test."""
    _mock_live_onlineda_and_spatial_for_other_pan()
    _mock_etrack_lists_no_documents()

    store = InMemoryCaseStore()
    document_source = UserUploadedDocumentSource()
    case = await store.create_case(application_number=_OTHER_PAN, resident_session="resident-1")
    case_id = case.case_id
    plan_document_id = "elevations-doc"
    document_source.add_document(_OTHER_PAN, plan_document_id, ELEVATIONS_PDF.read_bytes())
    await store.append_event(
        case_id,
        f"document-uploaded:{plan_document_id}",
        "document_uploaded",
        payload={
            "document_id": plan_document_id,
            "filename": "elevations.pdf",
            "content_type": "application/pdf",
            "size_bytes": 123,
        },
    )
    photo_document_id = "resident-photo"
    document_source.add_document(_OTHER_PAN, photo_document_id, _tiny_white_png(50, 50))
    await store.append_event(
        case_id,
        f"document-uploaded:{photo_document_id}",
        "document_uploaded",
        payload={
            "document_id": photo_document_id,
            "filename": "yard.jpg",
            "content_type": "image/jpeg",
            "size_bytes": 10,
        },
    )

    async with httpx.AsyncClient(follow_redirects=True) as ingest_client:
        runner = RealPipelineRunner(
            document_source=document_source,
            ingest_client=ingest_client,
            grounding_client=None,
            street_view_secret_accessor=_fake_street_view_secret_accessor,
        )
        resume = await resume_case(store, case_id)
        dossier, _ingest_outcome = await runner._build_dossier(  # noqa: SLF001
            case_id, resume, store=store
        )

    assert _STREET_VIEW_DOCUMENT_ID not in dossier.documents
    events = await store.list_events(case_id)
    assert not any(
        e.event_type == "document_uploaded"
        and e.payload.get("document_id") == _STREET_VIEW_DOCUMENT_ID
        for e in events
    )


async def test_street_view_fallback_document_returns_none_without_an_ingest_client() -> None:
    """Offline tests that never configure `ingest_client` (the vast
    majority of this module's tests) must never attempt a live Street View
    call -- confirmed directly against the method, not just inferred from
    the absence of a mocked route elsewhere."""
    runner = RealPipelineRunner(document_source=UserUploadedDocumentSource(), grounding_client=None)

    result = await runner._street_view_fallback_document("65A Vista Street Sans Souci NSW 2219")  # noqa: SLF001

    assert result is None
