"""The real `PipelineRunner`: wires ingest, evidence, court, gate, and
dispatch into one end-to-end tribunal run.

Replaces `job.main._RealPipelineRunner`'s `NotImplementedError` stub. This
module is `job`'s own lane -- it imports the public API of every other
package (`ingest`, `evidence`, `court`, `gate`, `dispatch`) exactly as an
external caller would, never reaching into another package's private
internals.

**Demo-scope note.** This build ships exactly one demo case (per
``docs/data-sources.md``): PAN-661190 / DA2026-0359 / Georges River Council
/ 65A Vista Street, Sans Souci. Rather than hit the live (keyless) NSW
OnlineDA/spatial APIs on every tribunal run, :func:`_load_frozen_ingest`
replays the frozen fixtures already checked into ``tests/fixtures/nsw/``
through a real `httpx.MockTransport` -- driving the exact same
`ingest.onlineda`/`ingest.spatial` parsing code the test suite validates
against those fixtures (`client=` is an injectable parameter on every fetch
function precisely for this), just deterministically and offline instead of
depending on a live government endpoint's uptime during judging. A future
wave generalising past the single demo case should swap
:func:`_load_frozen_ingest` for a live `httpx.AsyncClient` call -- no other
code in this module would need to change.

**Ground derivation.** A candidate ground's category and claim text are read
back from the ``ground_category_assigned``/``document_uploaded`` case
events the console records (see `console.app._propose_ground_for_confirmed_
concern`), not re-derived from the interview transcript here -- the console
is the one place that already has the live `InterviewFlow` object with its
parsed `RaisedConcern`s at the moment a concern is confirmed.

**Durable uploads.** `_build_dossier` reads a case's uploaded documents back
via its injected `DocumentSource` -- in production,
:class:`~setback.evidence.storage.GcsEvidenceStore`, which survives the
container boundary between `setback-console` and a `setback-tribunal` Cloud
Run Job execution (the in-memory `UserUploadedDocumentSource` this module
degrades gracefully around, below, remains the local/offline-test double).
Every `ExhibitedDocument` constructed here carries `case_id` so a
case-scoped store can locate the object; a source that doesn't need it
(`UserUploadedDocumentSource`) simply ignores the field.

**Clerical classification.** Every uploaded PDF is classified via
:func:`setback.clerk.classify_document` (Gemma, the low-cost clerical
tier) before its dossier document is registered, so its title reflects
what kind of exhibited document it actually is (an elevations drawing, a
site plan, a shadow diagram, ...) rather than only its raw filename --
this happens strictly before `evidence.dossier.build_dossier` constructs
the Clause/Evidence reviewer slices, per the classification contract's
intent. `setback.clerk` is a separate work package's module: the import is
deferred to the moment classification actually runs
(`_default_document_classifier`), and only ever runs when a model client
is configured -- classification enriches a document's label, it is never a
hard requirement for a ground to ship on its citation, so a classification
failure (or no model client at all, as in this module's own offline tests)
degrades to the raw filename rather than failing the run.

**Ledger truth (wave 4).** `court.graph.run_court_verbose` now extracts
token usage from ADK's own `Event.usage_metadata` per reviewer/adjudicator
stage and books it against a caller-supplied
:class:`~setback.state.ledger.Ledger` -- this module passes its own
per-case `ledger` through on every call, alongside the
grounding/polish/classification calls it already booked directly, so the
$2 self-abort ceiling is now load-bearing on the full tribunal run,
reviewers and adjudicator included, not just this module's own direct
calls.
"""

from __future__ import annotations

import base64
import io
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from google.adk.models import BaseLlm
from PIL import Image

from setback.court.bench import AdjudicationBench
from setback.court.graph import CourtOutcome, run_court_verbose
from setback.court.roles import (
    ClauseSlice,
    ClauseText,
    EvidencePhoto,
    EvidencePlan,
    EvidenceSlice,
    ReviewStance,
    ZoningControl,
)
from setback.dispatch.composer import CaseInfo, GroundContent, compose_dispatch_package
from setback.evidence.dossier import (
    CaseDossier,
    ProvenanceGrade,
    RenderedPage,
    anchor_id_for,
    build_dossier,
    to_gate_dossier,
)
from setback.evidence.dossier import (
    EvidenceAnchor as DossierEvidenceAnchor,
)
from setback.evidence.grounding import GroundedBox, ground_elements
from setback.evidence.overlays import AnchoredElement, build_overlay_boxes, render_semantic_overlay
from setback.gate.relevance import classify_relevance
from setback.gate.validator import BoundingBox as GateBoundingBox
from setback.gate.validator import (
    CandidateGround,
    Citation,
    GateDecision,
    GateStatus,
    validate_ground,
)
from setback.ingest.onlineda import DevelopmentApplicationRecord, fetch_development_application
from setback.ingest.spatial import DcpDocument, PlanningControls, resolve_site
from setback.ingest.tracker import DocumentSource, ExhibitedDocument
from setback.models.client import ModelCallError, ModelClient
from setback.state.breakers import CircuitBreaker
from setback.state.firestore import (
    CaseEvent,
    CaseStore,
    GroundEvidenceAnchor,
    GroundRecord,
    GroundStatus,
    ResumeState,
)
from setback.state.ledger import BudgetExceededError, Ledger

_FIXTURES_DIR: Final[Path] = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "nsw"

_DEMO_PAN: Final[str] = "PAN-661190"
_DEMO_COUNCIL: Final[str] = "Georges River Council"
_DEMO_ADDRESS: Final[str] = "65A Vista Street Sans Souci NSW 2219"

# The real, labelled elements documented in docs/data-sources.md for this
# fixture's elevations drawing -- the same labels tests/evidence/live_demo.py
# uses for its checked-in demo overlay, reused here so the tribunal's own
# grounding pass looks for real, name-checkable things rather than guessing.
_GROUNDING_LABELS: Final[tuple[str, ...]] = (
    "window W.1",
    "window W.2",
    "window W.3",
    "door D.1",
    "9m height limit datum line",
)

_ADJUDICATOR_BREAKER_NAME: Final[str] = "adjudicator"

_OVERLAY_STORAGE_MAX_WIDTH_PX: Final[int] = 1280
"""Firestore caps a single document (a `CaseEvent`'s whole payload map,
here) at ~1 MiB -- a full-resolution annotated overlay PNG (a rendered PDF
page at `evidence.dossier.DEFAULT_RENDER_DPI`) can run 2-5 MB on its own,
base64-encoded, before it even reaches Firestore, guaranteed to fail as
`INVALID_ARGUMENT: Property payload contains an invalid nested entity.`
(measured live). This width keeps the stored overlay comfortably under
that ceiling while staying large enough to read on the case page."""


def _shrink_png_for_storage(
    png_bytes: bytes, *, max_width_px: int = _OVERLAY_STORAGE_MAX_WIDTH_PX
) -> bytes:
    """Downscale `png_bytes` (if wider than `max_width_px`) so the annotated
    overlay fits safely inside one Firestore document field once
    base64-encoded. A pure post-processing step over `render_overlay`'s
    output -- it never touches the anchors' stored page-point coordinates,
    only the copy of the image persisted for display."""
    image = Image.open(io.BytesIO(png_bytes))
    if image.width <= max_width_px:
        return png_bytes
    height_px = max(1, round(image.height * max_width_px / image.width))
    resized = image.convert("RGB").resize((max_width_px, height_px), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="PNG")
    return buf.getvalue()


@dataclass(frozen=True)
class _GroundedOverlayContext:
    """What one grounding pass located, kept around between
    `_ground_annotated_evidence` and the semantic overlay `run` renders
    once every candidate ground has a gate decision -- see
    `_ground_annotated_evidence`'s docstring for why the render itself is
    deferred."""

    document_id: str
    page: RenderedPage
    boxes: tuple[GroundedBox, ...]


_CLASSIFY_FIRST_PAGE_MAX_CHARS: Final[int] = 4000
"""Cap on how much of a PDF's first page text is sent to `classify_document`
-- classification only needs enough text to recognise the document type,
not the whole page."""


class DocumentClassifier(Protocol):
    """Matches `setback.clerk.classify_document`'s signature -- injectable
    so tests exercise this module's classification wiring against a fake,
    with zero dependency on `setback.clerk` (a separate work package's
    module) actually being importable. Typed `-> Any` rather than
    `setback.clerk.DocumentKind` for the same reason: this module never
    inspects the returned value beyond generic `Enum` attributes
    (`.name`/`str(...)`, see `_plan_document_title`), so it never needs
    that module's type either.
    """

    async def __call__(
        self, filename: str, first_page_text: str, *, client: ModelClient
    ) -> Any: ...


async def _default_document_classifier(
    filename: str, first_page_text: str, *, client: ModelClient
) -> Any:
    """The production `DocumentClassifier`: calls the real
    `setback.clerk.classify_document`. Imported lazily (rather than at
    module scope) purely to keep this module's own import graph from
    growing a hard dependency on `setback.clerk` at the top -- classifying
    a document is one optional enrichment step among many this module
    performs, not a defining dependency of it."""
    from setback.clerk import classify_document

    return await classify_document(filename, first_page_text, client=client)


def _first_page_text(pdf_bytes: bytes, *, max_chars: int = _CLASSIFY_FIRST_PAGE_MAX_CHARS) -> str:
    """The first page's extracted text, truncated to `max_chars` -- enough
    for `classify_document` to recognise the document type without sending
    a whole page of legal/technical text through the clerical tier."""
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        if len(pdf) == 0:
            return ""
        textpage = pdf[0].get_textpage()
        try:
            text = textpage.get_text_bounded()
        finally:
            textpage.close()
    finally:
        pdf.close()
    return str(text)[:max_chars]


def _plan_document_title(filename: str, kind: Any) -> str:
    """A human-readable document title combining the classified kind with
    the original filename (e.g. ``"Elevations (elevations.pdf)"``), or just
    `filename` unchanged when `kind` is `None` (classification was skipped
    or failed) or classified as the catch-all `OTHER` category, which adds
    no information over the filename alone."""
    if kind is None or getattr(kind, "name", None) == "OTHER":
        return filename
    return f"{str(kind).replace('_', ' ').title()} ({filename})"


def _fixture_transport(request: httpx.Request) -> httpx.Response:
    """Serve every ingest request this module makes from the frozen NSW
    fixtures, by URL path, regardless of query string/headers -- a real
    `httpx.MockTransport` (core httpx, not a test-only dependency), so
    production code never imports a test library."""
    path = request.url.path
    fixture_by_suffix = {
        "/OnlineDA": "onlineda_pan-661190.json",
        "/address": "address_65a-vista-street.json",
        "/layerintersect": "layerintersect_propid-6038209.json",
        "/dcp": "dcp_propid-6038209.json",
    }
    for suffix, filename in fixture_by_suffix.items():
        if path.endswith(suffix):
            return httpx.Response(200, content=(_FIXTURES_DIR / filename).read_bytes())
    return httpx.Response(404, json={"error": f"no frozen fixture wired for {request.url}"})


async def _load_frozen_ingest(
    *, client: httpx.AsyncClient | None = None
) -> tuple[DevelopmentApplicationRecord, PlanningControls, list[DcpDocument]]:
    """Load the one demo case's ingest data, from the frozen fixtures by
    default (see module docstring) or from a caller-injected client (tests
    inject their own fixture/respx transport)."""
    owns_client = client is None
    http_client = client or httpx.AsyncClient(transport=httpx.MockTransport(_fixture_transport))
    try:
        da_record = await fetch_development_application(
            _DEMO_PAN, _DEMO_COUNCIL, client=http_client
        )
        controls, dcp_documents = await resolve_site(_DEMO_ADDRESS, client=http_client)
        return da_record, controls, dcp_documents
    finally:
        if owns_client:
            await http_client.aclose()


@dataclass(frozen=True, slots=True)
class _UploadedDocument:
    document_id: str
    filename: str
    is_pdf: bool


def _uploaded_documents(events: Sequence[CaseEvent]) -> list[_UploadedDocument]:
    """Read every `document_uploaded` event back into a classified upload
    record (PDF vs. photo, by content type)."""
    uploads: list[_UploadedDocument] = []
    for event in events:
        if event.event_type != "document_uploaded":
            continue
        document_id = str(event.payload.get("document_id", ""))
        if not document_id:
            continue
        content_type = str(event.payload.get("content_type") or "")
        filename = str(event.payload.get("filename") or document_id)
        uploads.append(
            _UploadedDocument(
                document_id=document_id,
                filename=filename,
                is_pdf="pdf" in content_type.lower() or filename.lower().endswith(".pdf"),
            )
        )
    return uploads


@dataclass(frozen=True, slots=True)
class _GroundIntake:
    ground_id: str
    category: str
    claim: str


def _candidate_grounds(
    events: Sequence[CaseEvent], grounds: Mapping[str, GroundRecord]
) -> list[_GroundIntake]:
    """Read every `ground_category_assigned` event back into the category
    tag the console recorded for that ground, paired with the claim text
    `propose_ground` already stored on the ground record itself."""
    intake: list[_GroundIntake] = []
    seen: set[str] = set()
    for event in events:
        if event.event_type != "ground_category_assigned":
            continue
        ground_id = str(event.payload.get("ground_id", ""))
        if not ground_id or ground_id in seen:
            continue
        record = grounds.get(ground_id)
        if record is None:
            continue
        seen.add(ground_id)
        intake.append(
            _GroundIntake(
                ground_id=ground_id,
                category=str(event.payload.get("category", "")),
                claim=record.claim,
            )
        )
    return intake


def _court_slices(ground: _GroundIntake, dossier: CaseDossier) -> tuple[ClauseSlice, EvidenceSlice]:
    """Build this ground's `court.roles` slices from the dossier's
    whole-case `ClauseSlice`/`EvidenceSlice` (`evidence.dossier`'s own,
    separately-defined shapes -- see that module's docstring for why the
    two packages each own their local slice types)."""
    clause_slice = ClauseSlice(
        ground_id=ground.ground_id,
        ground_text=ground.claim,
        category=ground.category,
        clauses=tuple(
            ClauseText(clause_ref=c.clause_ref, text=c.text) for c in dossier.clause_slice.clauses
        ),
        controls=tuple(
            ZoningControl(name=c.name, value=c.value) for c in dossier.clause_slice.controls
        ),
    )

    photos: list[EvidencePhoto] = []
    plans: list[EvidencePlan] = []
    for document in dossier.documents.values():
        for page in document.pages:
            anchor_id = anchor_id_for(document.document_id, page.page_number, None)
            anchor = dossier.anchors.get(anchor_id)
            grade = anchor.provenance_grade if anchor is not None else document.provenance_grade
            if document.provenance_grade is ProvenanceGrade.DOCUMENTS_ONLY:
                plans.append(
                    EvidencePlan(
                        anchor_id=anchor_id, caption=document.title, source_ref=document.document_id
                    )
                )
            else:
                photos.append(
                    EvidencePhoto(
                        anchor_id=anchor_id,
                        caption=document.title,
                        source_ref=document.document_id,
                        grade=grade,
                    )
                )
    # Fine-grained bbox anchors registered by grounding (see
    # `_ground_annotated_evidence`) are additional, more specific plan
    # entries alongside the page-level one above -- both are valid
    # citations; a reviewer may cite whichever is more specific.
    for anchor in dossier.anchors.values():
        if anchor.bbox is None:
            continue
        plans.append(
            EvidencePlan(
                anchor_id=anchor.anchor_id, caption=anchor.caption, source_ref=anchor.source_doc
            )
        )

    evidence_slice = EvidenceSlice(
        ground_id=ground.ground_id,
        ground_text=ground.claim,
        photos=tuple(photos),
        plans=tuple(plans),
    )
    return clause_slice, evidence_slice


def _known_anchor_ids(dossier: CaseDossier) -> frozenset[str]:
    """Every citation a reviewer may resolve against: evidence anchor ids,
    clause references, and control names (a Clause Reviewer may cite either
    a DCP clause reference or a zoning control by name, e.g.
    ``"height_of_buildings"``)."""
    clause_refs = {c.clause_ref for c in dossier.clause_slice.clauses}
    control_names = {c.name for c in dossier.clause_slice.controls}
    return frozenset(dossier.anchors.keys()) | clause_refs | control_names


def _citation_for_anchor(anchor_id: str, dossier: CaseDossier) -> Citation | None:
    anchor = dossier.anchors.get(anchor_id)
    if anchor is None:
        return None
    bbox = (
        GateBoundingBox(x0=anchor.bbox.x0, y0=anchor.bbox.y0, x1=anchor.bbox.x1, y1=anchor.bbox.y1)
        if anchor.bbox is not None
        else None
    )
    return Citation(document_id=anchor.source_doc, page=anchor.page, bbox=bbox)


def _citation_for_control(control_ref: str, dossier: CaseDossier) -> Citation | None:
    """A clause reviewer may cite a control name (e.g. `height_of_buildings`)
    rather than an evidence anchor -- resolvable only if some rendered
    document in the dossier exists to anchor the citation's document/page
    check (the gate's `_check_citations` requires both a resolvable
    document and, when present, a matching control value)."""
    control = next((c for c in dossier.clause_slice.controls if c.name == control_ref), None)
    if control is None or not dossier.documents:
        return None
    document_id = next(iter(dossier.documents))
    return Citation(
        document_id=document_id, page=1, control_name=control.name, quoted_value=control.value
    )


def _candidate_ground_for(
    ground: _GroundIntake, verdict_anchor_ids: Sequence[str], dossier: CaseDossier
) -> CandidateGround:
    citations: list[Citation] = []
    for anchor_id in verdict_anchor_ids:
        citation = _citation_for_anchor(anchor_id, dossier) or _citation_for_control(
            anchor_id, dossier
        )
        if citation is not None:
            citations.append(citation)
    return CandidateGround(
        ground_id=ground.ground_id, category=ground.category, citations=tuple(citations)
    )


def _ground_status_for(status: GateStatus) -> GroundStatus:
    if status is GateStatus.SHIPPED:
        return GroundStatus.SUPPORTED
    if status is GateStatus.FLAGGED:
        return GroundStatus.FLAGGED
    return GroundStatus.REFUSED


class RealPipelineRunner:
    """The production `job.main.PipelineRunner`: ingest -> evidence dossier
    -> court -> gate -> dispatch, for the one demo case this build supports.
    """

    def __init__(
        self,
        *,
        document_source: DocumentSource,
        polisher: ModelClient | None = None,
        grounding_client: ModelClient | None = None,
        clause_model: str | BaseLlm = "gemini-3.5-flash-lite",
        evidence_model: str | BaseLlm = "gemini-3.5-flash-lite",
        ingest_client: httpx.AsyncClient | None = None,
        document_classifier: DocumentClassifier | None = None,
    ) -> None:
        """Args mirror the pipeline's real dependencies; `clause_model`/
        `evidence_model`/`ingest_client`/`grounding_client`/
        `document_classifier` exist so tests can inject fakes -- production
        (`job.main`) leaves them at their live defaults.
        `document_classifier` defaults to the real
        `setback.clerk.classify_document` (see `_default_document_classifier`)."""
        self._document_source = document_source
        self._polisher = polisher
        self._grounding_client = grounding_client
        self._clause_model = clause_model
        self._evidence_model = evidence_model
        self._ingest_client = ingest_client
        self._document_classifier = document_classifier or _default_document_classifier

    async def _ground_annotated_evidence(
        self, dossier: CaseDossier
    ) -> tuple[CaseDossier, _GroundedOverlayContext | None]:
        """Run one grounding pass over the first rendered plan document (the
        uploaded elevations PDF), registering each located element as a
        fine-grained bbox anchor. Returns the dossier unchanged (with
        `None`) if there is no plan document or no grounding client was
        configured -- grounding is a richer-evidence enhancement, not a
        hard requirement for a ground to ship on its page-level anchor.

        Deliberately does **not** render the annotated overlay image here:
        `evidence.overlays.render_semantic_overlay` colours each box by
        its ground's eventual gate outcome (green/red/neutral -- see that
        module's docstring), which isn't known until every candidate
        ground in this run has a `GateDecision`. `run` renders the actual
        overlay once, after its ground loop, from the context this method
        returns."""
        plan_document = next(
            (
                d
                for d in dossier.documents.values()
                if d.provenance_grade is ProvenanceGrade.DOCUMENTS_ONLY
            ),
            None,
        )
        if plan_document is None or self._grounding_client is None or not plan_document.pages:
            return dossier, None

        page = plan_document.pages[0]
        result = await ground_elements(self._grounding_client, page, _GROUNDING_LABELS)
        if not result.boxes:
            return dossier, None

        for box in result.boxes:
            anchor = DossierEvidenceAnchor(
                anchor_id=anchor_id_for(plan_document.document_id, page.page_number, box.bbox),
                source_doc=plan_document.document_id,
                page=page.page_number,
                bbox=box.bbox,
                provenance_grade=ProvenanceGrade.DOCUMENTS_ONLY,
                caption=box.label,
            )
            dossier = dossier.with_anchor(anchor)

        return dossier, _GroundedOverlayContext(
            document_id=plan_document.document_id, page=page, boxes=tuple(result.boxes)
        )

    def _semantic_overlay_png(
        self,
        ctx: _GroundedOverlayContext,
        *,
        ground_status: Mapping[str, GateStatus],
        anchor_ground: Mapping[str, str],
    ) -> bytes:
        """Render `ctx`'s located boxes as the semantic overlay
        (`evidence.overlays.render_semantic_overlay`), each coloured by
        whether the ground it was cited for (if any, via `anchor_ground`)
        ended up SHIPPED, refused/flagged, or has no ground at all --
        exactly the anchor ids `_ground_annotated_evidence` registered on
        the dossier, recomputed here the same deterministic way
        (`anchor_id_for`) rather than looked up, since this method only
        has `ctx`, not the dossier itself."""
        elements = [
            AnchoredElement(
                anchor_id=anchor_id_for(ctx.document_id, ctx.page.page_number, box.bbox),
                bbox=box.bbox,
                caption=box.label,
                ground_id=anchor_ground.get(
                    anchor_id_for(ctx.document_id, ctx.page.page_number, box.bbox)
                ),
            )
            for box in ctx.boxes
        ]
        overlay_boxes = build_overlay_boxes(elements, ground_status)
        return _shrink_png_for_storage(render_semantic_overlay(ctx.page, overlay_boxes))

    async def _classify_plan_document(self, filename: str, pdf_bytes: bytes) -> Any:
        """Classify an uploaded PDF via `setback.clerk.classify_document`
        before it is registered in the dossier, returning `None` (degrade
        to the raw filename, see `_plan_document_title`) when no model
        client is configured or classification itself fails -- enrichment,
        never a hard requirement for a ground to ship."""
        client = self._grounding_client or self._polisher
        if client is None:
            return None
        try:
            return await self._document_classifier(
                filename, _first_page_text(pdf_bytes), client=client
            )
        except Exception:  # noqa: BLE001 -- classification enriches, never blocks the run
            return None

    async def _build_dossier(self, case_id: str, resume: ResumeState) -> CaseDossier:
        da_record, controls, dcp_documents = await _load_frozen_ingest(client=self._ingest_client)

        plan_documents: list[tuple[str, str, bytes]] = []
        photo_documents: list[tuple[str, str, bytes, ProvenanceGrade]] = []
        for upload in _uploaded_documents(resume.events):
            try:
                content = await self._document_source.download_document(
                    ExhibitedDocument(
                        document_id=upload.document_id,
                        title=upload.filename,
                        source="user-upload",
                        case_id=case_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001 -- degrade the dossier, never crash the run
                # Excluding the document (rather than failing the whole
                # run) is the intended degrade-gracefully behaviour the
                # module docstring's "Durable uploads" note describes --
                # but a *silent* skip here is exactly what let a whole
                # wave's worth of tribunal runs quietly lose every
                # resident-uploaded document to an unrelated wiring bug
                # with nothing in any log to point at why (smoke loop #2).
                # stderr, not an event: this is an operability signal for
                # whoever reads the job's Cloud Run logs, not case-facing
                # state a resident's page should render.
                print(
                    f"evidence download failed for document {upload.document_id!r} "
                    f"({upload.filename!r}); excluding it from the dossier: {exc}",
                    file=sys.stderr,
                )
                continue
            if upload.is_pdf:
                kind = await self._classify_plan_document(upload.filename, content)
                title = _plan_document_title(upload.filename, kind)
                plan_documents.append((upload.document_id, title, content))
            else:
                photo_documents.append(
                    (upload.document_id, upload.filename, content, ProvenanceGrade.RESIDENT_PHOTO)
                )

        return build_dossier(
            da_record=da_record,
            controls=controls,
            dcp_documents=dcp_documents,
            plan_documents=plan_documents,
            photo_documents=photo_documents,
        )

    async def run(self, case_id: str, resume: ResumeState, store: CaseStore) -> None:
        """Run the full court/gate/dispatch pipeline for `case_id` and
        persist every stage's outcome as durable case events, so the
        console's SSE stream and case page render the whole run live."""
        if resume.case is None:
            raise ValueError(f"case {case_id!r} has no resumable state")

        dossier = await self._build_dossier(case_id, resume)
        dossier, grounding_ctx = await self._ground_annotated_evidence(dossier)
        # The semantic overlay itself (colour-coded by each anchor's
        # ground's gate outcome) is rendered and emitted below, after the
        # ground loop -- see `_ground_annotated_evidence`'s docstring.
        anchor_ground: dict[str, str] = {}

        ledger = resume.ledger or Ledger()
        adjudicator_breaker = resume.breakers.get(_ADJUDICATOR_BREAKER_NAME) or CircuitBreaker(
            name=_ADJUDICATOR_BREAKER_NAME
        )
        bench = AdjudicationBench.default(breaker=adjudicator_breaker)
        known_anchor_ids = _known_anchor_ids(dossier)
        gate_dossier = to_gate_dossier(dossier)

        decisions: list[GateDecision] = []
        ground_content: dict[str, GroundContent] = {}

        for ground in _candidate_grounds(resume.events, resume.grounds):
            await store.transition_ground(case_id, ground.ground_id, GroundStatus.UNDER_REVIEW)
            clause_slice, evidence_slice = _court_slices(ground, dossier)

            result = await run_court_verbose(
                clause_slice,
                evidence_slice,
                known_anchor_ids=known_anchor_ids,
                clause_model=self._clause_model,
                evidence_model=self._evidence_model,
                bench=bench,
                ledger=ledger,
            )
            for reviewer, review in (
                ("clause_reviewer", result.clause_review),
                ("evidence_reviewer", result.evidence_review),
            ):
                await store.append_event(
                    case_id,
                    f"review-verdict:{ground.ground_id}:{reviewer}",
                    "review_verdict",
                    payload={
                        "ground_id": ground.ground_id,
                        "reviewer": reviewer,
                        "voided": review is None,
                        **(review.model_dump(mode="json") if review is not None else {}),
                    },
                )
            if (
                result.verdict.source == "adjudicated"
                or result.verdict.outcome is CourtOutcome.UNRESOLVED_FLAGGED
            ):
                await store.append_event(
                    case_id,
                    f"adjudication:{ground.ground_id}",
                    "adjudication_decision",
                    payload=result.verdict.model_dump(mode="json"),
                )

            for anchor_id in result.verdict.cited_anchor_ids:
                anchor_ground[anchor_id] = ground.ground_id
                anchor = dossier.anchors.get(anchor_id)
                if anchor is not None and anchor.bbox is not None:
                    await store.add_evidence_anchor(
                        case_id,
                        ground.ground_id,
                        GroundEvidenceAnchor(
                            source_doc=anchor.source_doc,
                            page=anchor.page,
                            bbox=(anchor.bbox.x0, anchor.bbox.y0, anchor.bbox.x1, anchor.bbox.y1),
                            provenance_grade=anchor.provenance_grade,
                        ),
                    )

            candidate: CandidateGround | None = None
            is_relevant = classify_relevance(ground.category).relevant
            if is_relevant and result.verdict.stance is ReviewStance.REJECT:
                # The court itself -- unanimously or on adjudication -- did
                # not find this (statutorily relevant) ground well-founded
                # on the material given. `gate.validator.CandidateGround`
                # has no stance field: the gate is a citation/relevance
                # filter only, orthogonal to substantive merit, so a
                # rejected-but-relevant ground must never reach it purely
                # on a resolvable citation -- otherwise the tribunal could
                # ship an objection it itself disbelieved. This decision is
                # synthesized here, not by the gate, since it is a
                # court-side outcome the gate has no vocabulary for.
                # An irrelevant ground (e.g. property value) always keeps
                # its proper s4.15 explanation via the `else` branch below,
                # regardless of stance -- irrelevance is categorical and
                # permanent, unlike "the evidence didn't convince us".
                decision = GateDecision(
                    ground_id=ground.ground_id,
                    status=GateStatus.REFUSED_UNSUBSTANTIATED,
                    category=ground.category,
                    explanation=(
                        "The Clause and Evidence Reviewers' assessment of this ground, "
                        + (
                            "resolved by adjudication, "
                            if result.verdict.source == "adjudicated"
                            else ""
                        )
                        + "did not find it well-founded on the material provided, so it has "
                        "not been included in your submission."
                    ),
                    statutory_basis=classify_relevance(ground.category).statutory_basis,
                    citation_issues=("the tribunal's review did not support this ground",),
                )
            else:
                candidate = _candidate_ground_for(ground, result.verdict.cited_anchor_ids, dossier)
                breaker = resume.breakers.get(f"gate:{ground.ground_id}") or CircuitBreaker(
                    name=f"gate:{ground.ground_id}"
                )
                decision = validate_ground(candidate, gate_dossier, breaker)
                await store.save_breaker(case_id, breaker)
            decisions.append(decision)

            await store.append_event(
                case_id,
                f"gate-decision:{ground.ground_id}",
                "gate_decision",
                payload={
                    "ground_id": decision.ground_id,
                    "status": decision.status.value,
                    "category": decision.category,
                    "explanation": decision.explanation,
                    "statutory_basis": decision.statutory_basis,
                    "citation_issues": list(decision.citation_issues),
                },
            )
            await store.transition_ground(
                case_id, ground.ground_id, _ground_status_for(decision.status)
            )

            if decision.status is GateStatus.SHIPPED and candidate is not None:
                first_citation = candidate.citations[0] if candidate.citations else None
                document_title = (
                    dossier.documents[first_citation.document_id].title
                    if first_citation is not None
                    and first_citation.document_id in dossier.documents
                    else "the case dossier"
                )
                annotated_ref = (
                    f"{first_citation.document_id} (grounded overlay)"
                    if grounding_ctx is not None
                    and first_citation is not None
                    and first_citation.document_id == grounding_ctx.document_id
                    else None
                )
                ground_content[ground.ground_id] = GroundContent(
                    statement=f"{ground.claim} {result.verdict.rationale}",
                    document_title=document_title,
                    page=first_citation.page if first_citation is not None else 1,
                    annotated_image_ref=annotated_ref,
                )

        if grounding_ctx is not None:
            ground_status: dict[str, GateStatus] = {d.ground_id: d.status for d in decisions}
            overlay_png = self._semantic_overlay_png(
                grounding_ctx, ground_status=ground_status, anchor_ground=anchor_ground
            )
            await store.append_event(
                case_id,
                f"annotated-overlay:{grounding_ctx.document_id}",
                "annotated_overlay",
                payload={
                    "document_id": grounding_ctx.document_id,
                    "mime_type": "image/png",
                    "image_base64": base64.b64encode(overlay_png).decode("ascii"),
                },
            )

        await store.save_breaker(case_id, adjudicator_breaker)
        await store.save_ledger(case_id, ledger)

        case_info = CaseInfo(
            da_number=dossier.da_record.council_application_number,
            council=dossier.da_record.council,
            property_address=dossier.da_record.address,
            exhibition_start=dossier.da_record.exhibition_start or date.today(),
            exhibition_end=dossier.da_record.exhibition_end or date.today(),
        )
        try:
            package = await compose_dispatch_package(
                decisions, case_info, ground_content, polisher=self._polisher
            )
        except ModelCallError:
            package = await compose_dispatch_package(
                decisions, case_info, ground_content, polisher=None
            )
        except BudgetExceededError:
            package = await compose_dispatch_package(
                decisions, case_info, ground_content, polisher=None
            )

        await store.append_event(
            case_id,
            f"submission-composed:{case_id}",
            "submission_composed",
            payload={
                "submission_markdown": package.submission.markdown,
                "submission_html": package.submission.html,
                "refusals_markdown": package.refusals_explainer.markdown,
                "refusals_html": package.refusals_explainer.html,
            },
        )


__all__ = ["RealPipelineRunner"]
