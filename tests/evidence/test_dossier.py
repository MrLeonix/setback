"""Tests for setback.evidence.dossier: PDF/photo rendering, the anchor
manifest, the disjoint ClauseSlice/EvidenceSlice, and the adapter into the
gate's own CaseDossier shape.

Offline throughout: PDFs come from the committed NSW fixtures
(tests/fixtures/nsw/docs/), no network, no model calls.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from setback.evidence.dossier import (
    BoundingBox,
    ClauseSlice,
    EvidenceAnchor,
    EvidenceSlice,
    ProvenanceGrade,
    RenderedPage,
    anchor_id_for,
    build_dossier,
    render_pdf_pages,
    render_photo,
    to_gate_dossier,
)
from setback.gate.validator import CaseDocument as GateCaseDocument
from setback.gate.validator import PlanningControl as GatePlanningControl
from setback.ingest.onlineda import DevelopmentApplicationRecord
from setback.ingest.spatial import DcpDocument, PlanningControls, SourcedValue

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "nsw" / "docs"
ELEVATIONS_PDF = FIXTURES / "elevations.pdf"


def _make_jpeg_bytes(size: tuple[int, int] = (800, 600)) -> bytes:
    from io import BytesIO

    buf = BytesIO()
    Image.new("RGB", size, color=(120, 140, 160)).save(buf, format="JPEG")
    return buf.getvalue()


def _da_record() -> DevelopmentApplicationRecord:
    return DevelopmentApplicationRecord(
        planning_portal_application_number="PAN-661190",
        council_application_number="DA2026/0359",
        council="Georges River Council",
        address="65A Vista Street, Sans Souci NSW 2219",
        lot_dp="Lot 4 DP232626",
        description="Alterations and additions",
        status="Under Assessment",
        exhibition_start=date(2026, 8, 20),
        exhibition_end=date(2026, 9, 3),
        cost_of_development=450_000.0,
    )


def _controls() -> PlanningControls:
    return PlanningControls(
        prop_id=6038209,
        zone_code=SourcedValue(
            value="R2",
            lep_name="Georges River LEP 2021",
            legislation_url="https://legislation.nsw.gov.au/lep",
        ),
        zone_name=SourcedValue(
            value="Low Density Residential",
            lep_name="Georges River LEP 2021",
            legislation_url="https://legislation.nsw.gov.au/lep",
        ),
        height_limit_metres=SourcedValue(
            value=9.0,
            lep_name="Georges River LEP 2021",
            legislation_url="https://legislation.nsw.gov.au/lep#h",
        ),
        floor_space_ratio=SourcedValue(
            value=0.55,
            lep_name="Georges River LEP 2021",
            legislation_url="https://legislation.nsw.gov.au/lep#fsr",
        ),
        lot_size_sqm=SourcedValue(
            value=450.0,
            lep_name="Georges River LEP 2021",
            legislation_url="https://legislation.nsw.gov.au/lep#lot",
        ),
        heritage_flags=(),
    )


def _dcp_documents() -> list[DcpDocument]:
    return [
        DcpDocument(
            plan_name="Hurstville DCP 2015", plan_url="https://example.test/hurstville-dcp.pdf"
        ),
        DcpDocument(plan_name="Kogarah DCP 2013", plan_url="https://example.test/kogarah-dcp.pdf"),
    ]


# --- rendering ----------------------------------------------------------------


def test_render_pdf_pages_returns_one_rendered_page_per_pdf_page() -> None:
    pages = render_pdf_pages(ELEVATIONS_PDF.read_bytes())

    assert len(pages) == 2
    assert [p.page_number for p in pages] == [1, 2]


def test_render_pdf_pages_resized_image_is_the_requested_width() -> None:
    pages = render_pdf_pages(ELEVATIONS_PDF.read_bytes(), resize_width_px=1024)

    from io import BytesIO

    for page in pages:
        img = Image.open(BytesIO(page.resized_png_bytes))
        assert img.width == 1024
        assert page.resized_width_px == 1024


def test_render_pdf_pages_records_true_page_size_in_points() -> None:
    pages = render_pdf_pages(ELEVATIONS_PDF.read_bytes())

    # Elevations.pdf pages are large-format (A0-ish) drawings, not A4.
    assert pages[0].width_pts > 1000
    assert pages[0].height_pts > 700


def test_render_photo_uses_original_pixel_dimensions_as_points() -> None:
    page = render_photo(_make_jpeg_bytes((800, 600)), resize_width_px=400)

    assert page.page_number == 1
    assert page.dpi == 72
    assert page.width_pts == pytest.approx(800.0)
    assert page.height_pts == pytest.approx(600.0)
    assert page.resized_width_px == 400
    assert page.resize_scale == pytest.approx(400 / 800)


# --- anchor manifest ------------------------------------------------------------


def test_anchor_id_is_deterministic_content_hash() -> None:
    bbox = BoundingBox(x0=10.0, y0=20.0, x1=30.0, y1=40.0)

    first = anchor_id_for("elevations", 1, bbox)
    second = anchor_id_for("elevations", 1, bbox)

    assert first == second
    assert len(first) == 16


def test_anchor_id_differs_for_different_bbox_or_page() -> None:
    bbox = BoundingBox(x0=10.0, y0=20.0, x1=30.0, y1=40.0)

    assert anchor_id_for("elevations", 1, bbox) != anchor_id_for("elevations", 2, bbox)
    assert anchor_id_for("elevations", 1, bbox) != anchor_id_for("elevations", 1, None)


def test_build_dossier_registers_a_page_level_anchor_per_rendered_page() -> None:
    dossier = build_dossier(
        da_record=_da_record(),
        controls=_controls(),
        dcp_documents=_dcp_documents(),
        plan_documents=[("elevations", "Elevations", ELEVATIONS_PDF.read_bytes())],
        photo_documents=[
            ("photo-1", "Front yard photo", _make_jpeg_bytes(), ProvenanceGrade.RESIDENT_PHOTO)
        ],
    )

    assert "elevations" in dossier.documents
    assert len(dossier.documents["elevations"].pages) == 2
    assert "photo-1" in dossier.documents
    assert len(dossier.documents["photo-1"].pages) == 1

    # One page-level anchor registered per rendered page (elevations has 2
    # pages, the photo has 1) grade matches the document's provenance.
    elevations_anchors = [a for a in dossier.anchors.values() if a.source_doc == "elevations"]
    photo_anchors = [a for a in dossier.anchors.values() if a.source_doc == "photo-1"]
    assert len(elevations_anchors) == 2
    assert all(a.provenance_grade is ProvenanceGrade.DOCUMENTS_ONLY for a in elevations_anchors)
    assert len(photo_anchors) == 1
    assert photo_anchors[0].provenance_grade is ProvenanceGrade.RESIDENT_PHOTO


def test_with_anchor_registers_a_new_bbox_specific_anchor_without_mutating_original() -> None:
    dossier = build_dossier(
        da_record=_da_record(),
        controls=_controls(),
        dcp_documents=_dcp_documents(),
        plan_documents=[("elevations", "Elevations", ELEVATIONS_PDF.read_bytes())],
        photo_documents=[],
    )
    before = len(dossier.anchors)
    bbox = BoundingBox(x0=1.0, y0=2.0, x1=3.0, y1=4.0)
    anchor = EvidenceAnchor(
        anchor_id=anchor_id_for("elevations", 1, bbox),
        source_doc="elevations",
        page=1,
        bbox=bbox,
        provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
        caption="9m height datum line",
    )

    updated = dossier.with_anchor(anchor)

    assert len(dossier.anchors) == before  # original untouched
    assert len(updated.anchors) == before + 1
    assert updated.anchors[anchor.anchor_id] == anchor


def test_with_anchor_is_idempotent_on_the_same_anchor_id() -> None:
    dossier = build_dossier(
        da_record=_da_record(),
        controls=_controls(),
        dcp_documents=_dcp_documents(),
        plan_documents=[("elevations", "Elevations", ELEVATIONS_PDF.read_bytes())],
        photo_documents=[],
    )
    bbox = BoundingBox(x0=1.0, y0=2.0, x1=3.0, y1=4.0)
    anchor = EvidenceAnchor(
        anchor_id=anchor_id_for("elevations", 1, bbox),
        source_doc="elevations",
        page=1,
        bbox=bbox,
        provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
        caption="",
    )

    once = dossier.with_anchor(anchor)
    twice = once.with_anchor(anchor)

    assert len(once.anchors) == len(twice.anchors)


# --- disjoint slices -----------------------------------------------------------


def test_clause_slice_has_no_field_capable_of_holding_image_bytes() -> None:
    for field in ClauseSlice.model_fields.values():
        assert field.annotation is not bytes
        assert "bytes" not in str(field.annotation)
        assert "base64" not in str(field.annotation).lower()


def test_evidence_slice_has_no_free_form_legislative_text_field() -> None:
    field_names = set(EvidenceSlice.model_fields)
    # Only structured image anchors: no field could carry a clause quotation.
    assert field_names == {"photos", "plans"}


def test_build_dossier_clause_slice_contains_controls_and_categories() -> None:
    dossier = build_dossier(
        da_record=_da_record(),
        controls=_controls(),
        dcp_documents=_dcp_documents(),
        plan_documents=[],
        photo_documents=[],
    )

    control_names = {c.name for c in dossier.clause_slice.controls}
    assert "height_of_buildings" in control_names
    assert "floor_space_ratio" in control_names
    assert dossier.clause_slice.s415_categories  # non-empty, the 5 heads
    dcp_titles = {c.clause_ref for c in dossier.clause_slice.clauses}
    assert "Hurstville DCP 2015" in dcp_titles


def test_build_dossier_evidence_slice_contains_plan_and_photo_images_only() -> None:
    dossier = build_dossier(
        da_record=_da_record(),
        controls=_controls(),
        dcp_documents=_dcp_documents(),
        plan_documents=[("elevations", "Elevations", ELEVATIONS_PDF.read_bytes())],
        photo_documents=[
            ("photo-1", "Front yard", _make_jpeg_bytes(), ProvenanceGrade.RESIDENT_PHOTO)
        ],
    )

    assert len(dossier.evidence_slice.plans) == 2  # 2 elevation pages
    assert len(dossier.evidence_slice.photos) == 1
    for anchor in dossier.evidence_slice.plans + dossier.evidence_slice.photos:
        assert anchor.image_base64  # non-empty, base64-encoded PNG bytes


# --- gate adapter ---------------------------------------------------------------


def test_to_gate_dossier_maps_documents_and_controls() -> None:
    dossier = build_dossier(
        da_record=_da_record(),
        controls=_controls(),
        dcp_documents=_dcp_documents(),
        plan_documents=[("elevations", "Elevations", ELEVATIONS_PDF.read_bytes())],
        photo_documents=[
            ("photo-1", "Front yard", _make_jpeg_bytes(), ProvenanceGrade.RESIDENT_PHOTO)
        ],
    )

    gate_dossier = to_gate_dossier(dossier)

    assert isinstance(gate_dossier.documents["elevations"], GateCaseDocument)
    assert gate_dossier.documents["elevations"].page_count == 2
    assert gate_dossier.documents["photo-1"].page_count == 1

    height_control = gate_dossier.controls["height_of_buildings"]
    assert isinstance(height_control, GatePlanningControl)
    assert height_control.value == "9m"
    assert gate_dossier.controls["floor_space_ratio"].value == "0.55:1"


def test_to_gate_dossier_page_bounds_come_from_the_true_rendered_page_size() -> None:
    dossier = build_dossier(
        da_record=_da_record(),
        controls=_controls(),
        dcp_documents=[],
        plan_documents=[("elevations", "Elevations", ELEVATIONS_PDF.read_bytes())],
        photo_documents=[],
    )

    gate_dossier = to_gate_dossier(dossier)
    bounds = gate_dossier.documents["elevations"].page_bounds
    real_pages = render_pdf_pages(ELEVATIONS_PDF.read_bytes())
    assert bounds.width == pytest.approx(real_pages[0].width_pts)
    assert bounds.height == pytest.approx(real_pages[0].height_pts)


def test_case_dossier_is_frozen() -> None:
    dossier = build_dossier(
        da_record=_da_record(),
        controls=_controls(),
        dcp_documents=[],
        plan_documents=[],
        photo_documents=[],
    )
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        dossier.documents = {}  # type: ignore[misc]


def test_rendered_page_is_a_frozen_dataclass_instance() -> None:
    page = render_photo(_make_jpeg_bytes())
    assert isinstance(page, RenderedPage)
    with pytest.raises(Exception):  # noqa: B017
        page.page_number = 99  # type: ignore[misc]
