"""Evidence dossier: assembles the case dossier and anchors every claim to a
source with a provenance grade.

Provenance grades:
    A: resident-supplied photo, directly evidencing the claim.
    B: Street View / Solar API fallback, used when no resident photo exists.
    C: documents-only, derived solely from exhibited plans or public records.

This module owns three things:

1. **Rendering.** :func:`render_pdf_pages` and :func:`render_photo` turn raw
   ingest bytes (an exhibited PDF, a resident's photo) into
   :class:`RenderedPage`\\ s: a full-resolution PNG plus the exact
   1024px-wide resize actually sent to the grounding model
   (:mod:`setback.evidence.grounding`), with the resize factor recorded so a
   model's normalized box can be mapped back to true page coordinates.
2. **The anchor manifest.** Every claim Setback can cite is an
   :class:`EvidenceAnchor` keyed by a deterministic, content-hash
   ``anchor_id`` (same scheme as ARCHITECTURE.md's Firestore `anchor_id`:
   ``sha256(source_doc + page + bbox)[:16]``) so re-registering the same
   anchor on a retry is a no-op, never a duplicate.
3. **The two disjoint slices.** :class:`ClauseSlice` and
   :class:`EvidenceSlice` are the *only* inputs the ADK court graph's
   Clause/Evidence reviewer nodes may be built from (ARCHITECTURE.md §2).
   Disjointness is a type fact, not a prompting convention:
   ``ClauseSlice`` has no field that can hold image bytes, and
   ``EvidenceSlice`` has no free-form field that could carry legislative
   text — only a short resident-supplied ``caption``.

:func:`to_gate_dossier` is the small, tested adapter from this module's rich
:class:`CaseDossier` into :mod:`setback.gate.validator`'s own, deliberately
separate ``CaseDossier`` shape — the gate is a read-only, independently
testable consumer and never imports this module's types directly.

**Integration contract for `to_gate_dossier`'s control names**: the reviewer
prompts that produce s4.15 :class:`~setback.gate.validator.Citation`\\ s
(owned by the court package, not this one) must quote a planning-control
value in *exactly* the string form this adapter produces, because the gate
checks it with plain string equality:

- ``"height_of_buildings"`` -> ``"{n:g}m"`` (e.g. ``9.0`` -> ``"9m"``)
- ``"floor_space_ratio"`` -> ``"{n:g}:1"`` (e.g. ``0.55`` -> ``"0.55:1"``)
- ``"lot_size"`` -> ``"{n:g}m²"``
- ``"zone_code"`` / ``"zone_name"`` -> the raw string value, unformatted

**Scope note on `ClauseSlice.clauses`**: full clause-body text extraction
from LEP/DCP PDFs (OCR/layout parsing) is out of scope for this work
package. Each applicable DCP is represented by name and citation URL only;
a future wave can enrich ``text`` with the actual extracted clause body
without changing this module's public shape.
"""

from __future__ import annotations

import base64
import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image
from pydantic import BaseModel

from setback.gate import validator as gate_validator
from setback.gate.s415 import PLANNING_HEADS
from setback.ingest.onlineda import DevelopmentApplicationRecord
from setback.ingest.spatial import DcpDocument, PlanningControls, SourcedValue

DEFAULT_RENDER_DPI: Final[int] = 300
"""Full-resolution PDF render DPI, per the spike's proven grounding pipeline."""

DEFAULT_RESIZE_WIDTH_PX: Final[int] = 1024
"""Width the grounding model is actually sent, per spike-grounding.md."""

_PHOTO_DPI: Final[int] = 72
"""Photos have no PDF page size; treating them as 72 DPI documents makes 1
pixel == 1 point, so the same points<->pixels math in `grounding.py` applies
uniformly to both rendered PDF pages and raw resident/Street-View photos."""


class ProvenanceGrade(StrEnum):
    """The strength of evidence backing a single claim."""

    RESIDENT_PHOTO = "A"
    STREET_VIEW_SOLAR_FALLBACK = "B"
    DOCUMENTS_ONLY = "C"


# --- rendering ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """One page (a rendered PDF page, or a whole photo treated as page 1),
    at both full resolution and the exact resize sent to the grounding model.
    """

    page_number: int
    """1-indexed, matching :class:`~setback.gate.validator.Citation.page`."""

    width_pts: float
    """The page's true width, in points (1/72 inch)."""

    height_pts: float
    """The page's true height, in points (1/72 inch)."""

    dpi: int
    """The DPI `png_bytes` was rendered at (72 for a photo: 1px == 1pt)."""

    png_bytes: bytes
    """The full-resolution PNG, at `dpi`."""

    resized_png_bytes: bytes
    """The PNG actually sent to the grounding model."""

    resized_width_px: int
    resized_height_px: int

    @property
    def resize_scale(self) -> float:
        """`resized pixels / full-resolution pixels` — multiply a
        full-resolution pixel coordinate by this to get the model's
        normalized-box pixel space, or divide the other way to invert."""
        full_width_px = self.width_pts * self.dpi / 72.0
        return self.resized_width_px / full_width_px


def _resize_proportionally(image: Image.Image, width_px: int) -> Image.Image:
    height_px = max(1, round(image.height * width_px / image.width))
    return image.resize((width_px, height_px), Image.Resampling.LANCZOS)


def _to_png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def render_pdf_pages(
    pdf_bytes: bytes,
    *,
    dpi: int = DEFAULT_RENDER_DPI,
    resize_width_px: int = DEFAULT_RESIZE_WIDTH_PX,
) -> list[RenderedPage]:
    """Render every page of a PDF to a full-resolution PNG plus the resized
    PNG the grounding model is actually sent.

    Args:
        pdf_bytes: The raw PDF file content.
        dpi: The full-resolution render DPI (default matches the spike).
        resize_width_px: The width of the resized image sent to the
            grounding model (default matches the spike: 1024px).

    Returns:
        One :class:`RenderedPage` per page, in page order (page 1 first).
    """
    pages: list[RenderedPage] = []
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            width_pts, height_pts = page.get_size()
            bitmap = page.render(scale=dpi / 72.0)
            try:
                full_image = bitmap.to_pil()
            finally:
                bitmap.close()
            resized_image = _resize_proportionally(full_image, resize_width_px)
            pages.append(
                RenderedPage(
                    page_number=index + 1,
                    width_pts=float(width_pts),
                    height_pts=float(height_pts),
                    dpi=dpi,
                    png_bytes=_to_png_bytes(full_image),
                    resized_png_bytes=_to_png_bytes(resized_image),
                    resized_width_px=resized_image.width,
                    resized_height_px=resized_image.height,
                )
            )
    finally:
        pdf.close()
    return pages


def render_photo(
    image_bytes: bytes, *, resize_width_px: int = DEFAULT_RESIZE_WIDTH_PX
) -> RenderedPage:
    """Wrap a raw photo (a resident's upload, or a Street View fallback
    image) as a single-page :class:`RenderedPage`, so the same grounding and
    anchor pipeline applies uniformly to photos and rendered PDF pages.

    Treats the photo as a 72-DPI document (1 pixel == 1 point): there is no
    PDF page size to convert into, so `width_pts`/`height_pts` are simply
    the photo's own pixel dimensions.
    """
    image = Image.open(io.BytesIO(image_bytes))
    image.load()
    resized_image = _resize_proportionally(image, resize_width_px)
    return RenderedPage(
        page_number=1,
        width_pts=float(image.width),
        height_pts=float(image.height),
        dpi=_PHOTO_DPI,
        png_bytes=_to_png_bytes(image),
        resized_png_bytes=_to_png_bytes(resized_image),
        resized_width_px=resized_image.width,
        resized_height_px=resized_image.height,
    )


# --- the anchor manifest ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """A rectangular region on a page, in points, origin bottom-left.

    A deliberately separate value type from
    :class:`setback.gate.validator.BoundingBox` — this package's own view of
    an anchor's location, mapped across the boundary by :func:`to_gate_dossier`.
    """

    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    """A single claim tied to its source, page, optional location, and
    provenance grade — the atom of the anchor manifest."""

    anchor_id: str
    source_doc: str
    page: int
    bbox: BoundingBox | None
    provenance_grade: ProvenanceGrade
    caption: str = ""


def anchor_id_for(source_doc: str, page: int, bbox: BoundingBox | None) -> str:
    """The deterministic content-hash anchor id for `(source_doc, page,
    bbox)`, matching ARCHITECTURE.md's `sha256(source_doc + page + bbox)[:16]`
    scheme so re-registering the same anchor on a retry is idempotent."""
    bbox_tuple = (bbox.x0, bbox.y0, bbox.x1, bbox.y1) if bbox is not None else None
    payload = f"{source_doc}|{page}|{bbox_tuple}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """One document (or photo) contributing to the case dossier, already
    rendered into pages."""

    document_id: str
    title: str
    provenance_grade: ProvenanceGrade
    pages: tuple[RenderedPage, ...]


# --- the two disjoint slices (ARCHITECTURE.md §2) --------------------------------


class ClauseText(BaseModel):
    """One clause-level reference available to the Clause Reviewer.

    `text` is a citation reference, not full extracted clause body text
    (see the module docstring's scope note) — enough to cite the instrument
    by name and URL, not enough to reproduce its wording.
    """

    document_id: str
    clause_ref: str
    text: str


class ZoningControl(BaseModel):
    """One resolved, citable planning-control value."""

    name: str
    value: str
    lep_name: str
    legislation_url: str


class ClauseSlice(BaseModel):
    """The Clause Reviewer's entire input.

    Structurally incapable of holding an image: every field is text or a
    plain string list — there is no field an image's bytes could ever be
    assigned to, so "the Clause Reviewer never sees photos" is a fact a type
    checker enforces, not a system-prompt request.
    """

    clauses: list[ClauseText]
    controls: list[ZoningControl]
    s415_categories: list[str]


class ImageAnchor(BaseModel):
    """One image (a rendered plan page or a photo) available to the
    Evidence Reviewer, carrying its anchor id for citation.

    `image_base64` is a plain string field, not a `google.genai.types.Part`
    — turning it into an inline/file-data content part is a separate,
    explicit step the court package performs when it actually calls the
    model, keeping this slice itself inert with respect to genai `Content`.
    """

    anchor_id: str
    mime_type: str
    image_base64: str
    caption: str


class EvidenceSlice(BaseModel):
    """The Evidence Reviewer's entire input.

    Structurally incapable of holding legislative text: `caption` is a
    short resident/plan label (e.g. a filename or a one-line description),
    never a field sized or intended to carry a clause quotation, and no
    other field on this model is text at all.
    """

    photos: list[ImageAnchor]
    plans: list[ImageAnchor]


def _image_anchor_for(document_id: str, page: RenderedPage, caption: str) -> ImageAnchor:
    return ImageAnchor(
        anchor_id=anchor_id_for(document_id, page.page_number, None),
        mime_type="image/png",
        image_base64=base64.b64encode(page.resized_png_bytes).decode("ascii"),
        caption=caption,
    )


# --- the case dossier -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseDossier:
    """The fully assembled case dossier: ingest outputs, the rendered
    document store, the anchor manifest, and the two disjoint slices built
    from them.
    """

    da_record: DevelopmentApplicationRecord
    controls: PlanningControls
    dcp_documents: tuple[DcpDocument, ...]
    documents: Mapping[str, SourceDocument]
    anchors: Mapping[str, EvidenceAnchor]
    clause_slice: ClauseSlice
    evidence_slice: EvidenceSlice

    def with_anchor(self, anchor: EvidenceAnchor) -> CaseDossier:
        """Return a new dossier with `anchor` registered in the manifest.

        Idempotent: registering the same `anchor_id` twice (e.g. a retried
        grounding pass) overwrites in place rather than duplicating,
        matching the Firestore `set(..., merge=True)` semantics this
        content-hash id scheme is designed for.
        """
        return replace(self, anchors={**self.anchors, anchor.anchor_id: anchor})


def _control_row(
    name: str, sourced: SourcedValue[float] | SourcedValue[str] | None
) -> ZoningControl | None:
    if sourced is None:
        return None
    return ZoningControl(
        name=name,
        value=str(sourced.value),
        lep_name=sourced.lep_name,
        legislation_url=sourced.legislation_url,
    )


def build_clause_slice(
    controls: PlanningControls,
    dcp_documents: Sequence[DcpDocument],
    *,
    s415_categories: Sequence[str] = tuple(PLANNING_HEADS),
) -> ClauseSlice:
    """Build the Clause Reviewer's disjoint input from resolved planning
    controls and the applicable DCP list. The only function that constructs
    a :class:`ClauseSlice` — see the class docstring for why that matters.
    """
    zoning_controls = [
        row
        for row in (
            _control_row("zone_code", controls.zone_code),
            _control_row("zone_name", controls.zone_name),
            _control_row("height_of_buildings", controls.height_limit_metres),
            _control_row("floor_space_ratio", controls.floor_space_ratio),
            _control_row("lot_size", controls.lot_size_sqm),
            *(
                _control_row(f"heritage_{i}", flag)
                for i, flag in enumerate(controls.heritage_flags)
            ),
        )
        if row is not None
    ]
    # Reformat the two controls with an established textual convention (see
    # the module docstring's integration contract) so a reviewer's quoted
    # citation can match the gate's stored control value verbatim.
    formatted: list[ZoningControl] = []
    for row in zoning_controls:
        if row.name == "height_of_buildings":
            formatted.append(row.model_copy(update={"value": f"{float(row.value):g}m"}))
        elif row.name == "floor_space_ratio":
            formatted.append(row.model_copy(update={"value": f"{float(row.value):g}:1"}))
        elif row.name == "lot_size":
            formatted.append(row.model_copy(update={"value": f"{float(row.value):g}m²"}))
        else:
            formatted.append(row)

    clauses = [
        ClauseText(
            document_id=dcp.plan_name,
            clause_ref=dcp.plan_name,
            text=f"Applicable Development Control Plan: {dcp.plan_name} ({dcp.plan_url})",
        )
        for dcp in dcp_documents
    ]
    return ClauseSlice(clauses=clauses, controls=formatted, s415_categories=list(s415_categories))


def build_evidence_slice(
    documents: Sequence[SourceDocument], photo_document_ids: frozenset[str]
) -> EvidenceSlice:
    """Build the Evidence Reviewer's disjoint input from every rendered
    document's pages, splitting into `photos` vs `plans` by
    `photo_document_ids`. The only function that constructs an
    :class:`EvidenceSlice` — see the class docstring for why that matters.
    """
    photos: list[ImageAnchor] = []
    plans: list[ImageAnchor] = []
    for document in documents:
        bucket = photos if document.document_id in photo_document_ids else plans
        for page in document.pages:
            caption = (
                document.title
                if len(document.pages) == 1
                else f"{document.title} (page {page.page_number})"
            )
            bucket.append(_image_anchor_for(document.document_id, page, caption))
    return EvidenceSlice(photos=photos, plans=plans)


def build_dossier(
    *,
    da_record: DevelopmentApplicationRecord,
    controls: PlanningControls,
    dcp_documents: Sequence[DcpDocument],
    plan_documents: Sequence[tuple[str, str, bytes]],
    photo_documents: Sequence[tuple[str, str, bytes, ProvenanceGrade]],
    s415_categories: Sequence[str] = tuple(PLANNING_HEADS),
) -> CaseDossier:
    """Assemble the full case dossier from ingest outputs.

    Args:
        da_record: The verified OnlineDA record for the case.
        controls: The resolved LEP planning controls for the site.
        dcp_documents: The applicable DCP documents for the site.
        plan_documents: `(document_id, title, pdf_bytes)` for every exhibited
            PDF to render and register (grade
            :attr:`ProvenanceGrade.DOCUMENTS_ONLY`) — e.g. the elevations
            drawing fetched via a :class:`~setback.ingest.tracker.DocumentSource`.
        photo_documents: `(document_id, title, image_bytes, grade)` for
            every photo to render and register — a resident upload (grade
            :attr:`ProvenanceGrade.RESIDENT_PHOTO`) or a Street View
            fallback (grade :attr:`ProvenanceGrade.STREET_VIEW_SOLAR_FALLBACK`,
            see :mod:`setback.evidence.imagery`).
        s415_categories: The category ids the Clause Reviewer may tag a
            ground with; defaults to the five s4.15(1) heads.

    Returns:
        The assembled :class:`CaseDossier`, with one page-level anchor
        already registered per rendered page (bbox-specific anchors are
        added later via :meth:`CaseDossier.with_anchor` once grounding
        locates a specific claim on a page).
    """
    documents: dict[str, SourceDocument] = {}
    anchors: dict[str, EvidenceAnchor] = {}

    for document_id, title, pdf_bytes in plan_documents:
        pages = tuple(render_pdf_pages(pdf_bytes))
        documents[document_id] = SourceDocument(
            document_id=document_id,
            title=title,
            provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
            pages=pages,
        )
        for page in pages:
            anchor = EvidenceAnchor(
                anchor_id=anchor_id_for(document_id, page.page_number, None),
                source_doc=document_id,
                page=page.page_number,
                bbox=None,
                provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
                caption=title,
            )
            anchors[anchor.anchor_id] = anchor

    photo_document_ids: set[str] = set()
    for document_id, title, image_bytes, grade in photo_documents:
        photo_document_ids.add(document_id)
        page = render_photo(image_bytes)
        documents[document_id] = SourceDocument(
            document_id=document_id, title=title, provenance_grade=grade, pages=(page,)
        )
        anchor = EvidenceAnchor(
            anchor_id=anchor_id_for(document_id, page.page_number, None),
            source_doc=document_id,
            page=page.page_number,
            bbox=None,
            provenance_grade=grade,
            caption=title,
        )
        anchors[anchor.anchor_id] = anchor

    clause_slice = build_clause_slice(controls, dcp_documents, s415_categories=s415_categories)
    evidence_slice = build_evidence_slice(list(documents.values()), frozenset(photo_document_ids))

    return CaseDossier(
        da_record=da_record,
        controls=controls,
        dcp_documents=tuple(dcp_documents),
        documents=documents,
        anchors=anchors,
        clause_slice=clause_slice,
        evidence_slice=evidence_slice,
    )


# --- adapter into the gate's own dossier shape -----------------------------------


def to_gate_dossier(dossier: CaseDossier) -> gate_validator.CaseDossier:
    """Map this package's rich :class:`CaseDossier` into the gate's own,
    deliberately separate ``CaseDossier`` shape (see
    :mod:`setback.gate.validator`'s module docstring for why the gate
    defines its own local types rather than importing this module's).

    See the module docstring's "integration contract" note for the exact
    string formatting a citation's `quoted_value` must match for the two
    height/FSR controls produced here.
    """
    gate_documents = {
        document_id: gate_validator.CaseDocument(
            document_id=document_id,
            page_count=len(document.pages),
            page_bounds=gate_validator.PageBounds(
                width=document.pages[0].width_pts if document.pages else 0.0,
                height=document.pages[0].height_pts if document.pages else 0.0,
            ),
        )
        for document_id, document in dossier.documents.items()
    }
    gate_controls = {
        control.name: gate_validator.PlanningControl(name=control.name, value=control.value)
        for control in dossier.clause_slice.controls
    }
    return gate_validator.CaseDossier(documents=gate_documents, controls=gate_controls)
