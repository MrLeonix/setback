"""The real `PipelineRunner`: wires ingest, evidence, court, gate, and
dispatch into one end-to-end tribunal run.

Replaces `job.main._RealPipelineRunner`'s `NotImplementedError` stub. This
module is `job`'s own lane -- it imports the public API of every other
package (`ingest`, `evidence`, `court`, `gate`, `dispatch`) exactly as an
external caller would, never reaching into another package's private
internals.

**Un-frozen ingest (wave 9).** A case's own typed `application_number`
(``resume.case.application_number``) now drives real fetching through the
existing, proven `ingest.onlineda`/`ingest.spatial` clients:
:func:`_load_ingest_for_application` fetches the live OnlineDA record for
that number (scoped to Georges River Council -- the one tracker family
this build actually speaks, per this wave's explicit scope allowance), then
resolves that record's *own* address through the live spatial chain. This
only happens when a real `ingest_client` is configured (`job.main`'s
production factory now passes one); every existing offline test that
leaves `ingest_client=None` gets exactly the prior behaviour unchanged
(see next paragraph). Any live-resolution failure (an unknown PAN, a
transient NSW API error, a resolvable-but-address-lookup-fails case) is
caught and reported as a case event, never raised -- the run degrades to
demo-fixture mode rather than crashing or silently mislabelling a case.
`RealPipelineRunner._exhibited_tracker_documents` additionally lists and
downloads a real case's exhibited documents from
`ingest.tracker.EtrackDocumentSource` once live ingest succeeds, exactly
the "Same-council/same-tracker-family scope is acceptable" allowance --
per-document download failures are skipped, never fatal to the run.

**Demo-fixture fallback (unchanged mechanism, now a deliberate degrade
path rather than the only path).** This build ships one frozen demo case
(per ``docs/data-sources.md``): PAN-661190 / DA2026-0359 / Georges River
Council / 65A Vista Street, Sans Souci. :func:`_load_frozen_ingest` replays
the frozen fixtures already checked into ``tests/fixtures/nsw/`` through a
real `httpx.MockTransport` -- driving the exact same `ingest.onlineda`/
`ingest.spatial` parsing code the test suite validates against those
fixtures (`client=` is an injectable parameter on every fetch function
precisely for this), just deterministically and offline. Every offline
test in this module that never sets `ingest_client` gets this path,
unconditionally, exactly as before this wave. In production, this is now
also the *documented, event-labelled* fallback when live resolution of a
typed number fails -- never a silent wrong letterhead: `run` always
composes the submission from whatever `CaseDossier.da_record` the ingest
step actually returned, live or fallback, so the letterhead always matches
what was truly ingested.

**Street View fallback trigger (wave 9).** `_build_dossier` fires
`evidence.imagery.fetch_street_view_fallback` exactly when three things
are all true: (a) no uploaded document classified as a resident photo
(`ProvenanceGrade.RESIDENT_PHOTO`) exists among this case's uploads, (b)
`self._ingest_client` is configured (offline tests that never set it never
attempt a live Street View call, matching every other live-only feature in
this module), and (c) the resolved `da_record.address` is non-empty. A
`StreetViewUnavailableError` (a real request failure, not "no coverage
here" -- that is its own, non-exceptional `None` return) degrades to no
fallback image rather than failing the run. A successful fetch registers a
grade-B photo document whose title carries the visible attribution string
verbatim, so `evidence.dossier.build_dossier`'s page-level anchor for it
also carries that attribution as its `caption` -- the fallback image is
never anonymous.

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

**Page-level anchor-status propagation (SMOKE.md v5, wave 9 root-cause
fix).** A reviewer looking at one full-page plan image plausibly cites
that whole page rather than a specific fine-grained crop
`_ground_annotated_evidence` also registered on it;
`_propagate_page_level_anchor_status` spreads such a page-level citation's
ground/status down onto bbox anchors on that same page, so the annotated
overlay colours them by outcome instead of leaving them permanently
neutral grey. Three rules, in order, govern exactly how: (a) a bbox anchor
a reviewer cited *directly* always keeps that citation's own ground and
status -- never put into contention with, or overridden by, a status
merely inherited from the page it sits on, however much more severe that
inherited claim is; (b) page-level inheritance is an all-or-nothing
per-page fallback for a page with ZERO directly-cited bbox anchors of its
own, never a per-anchor gap-filler on a page that already has specific
citations elsewhere -- the wave-9 fix for the live "meaningless mid-house
boxes" regression, where one ground's page-level citation (e.g. property
value) was painting unrelated window/door boxes on the same page orange
even though it never discussed those specific elements; (c) among
competing page-level-only claims on a page with no direct citations, the
ground whose own `EvidenceSlice` actually included this anchor's document
is preferred over one that did not, before falling back to severity
(refused beats flagged beats shipped) as the final tie-break. Before rule
(a), a SHIPPED ground's own directly-cited bbox anchor could render orange
purely because an unrelated REFUSED ground's page-level citation of the
same page was more severe; before rule (b)'s wave-9 tightening, that same
page-level citation could paint every OTHER uncited element on the page
regardless of relevance. See that function's own docstring for the full
detail.
"""

from __future__ import annotations

import base64
import io
import sys
from collections import defaultdict
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
    SourceDocument,
    anchor_id_for,
    build_dossier,
    to_gate_dossier,
)
from setback.evidence.dossier import (
    EvidenceAnchor as DossierEvidenceAnchor,
)
from setback.evidence.grounding import GroundedBox, ground_elements
from setback.evidence.imagery import (
    SecretAccessor,
    StreetViewUnavailableError,
    fetch_street_view_fallback,
)
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
from setback.ingest.onlineda import (
    DevelopmentApplicationRecord,
    OnlineDAError,
    fetch_development_application,
)
from setback.ingest.spatial import DcpDocument, PlanningControls, SpatialApiError, resolve_site
from setback.ingest.tracker import (
    DocumentSource,
    EtrackDocumentSource,
    EvidenceUploadStore,
    ExhibitedDocument,
    TrackerError,
)
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

_MAX_TRACKER_DOCUMENTS: Final[int] = 3
"""Bound on how many of a tracker's listed exhibited documents
`_exhibited_tracker_documents` downloads per run -- a real council register
can list many housekeeping/notice PDFs alongside the substantive plans; this
keeps one tribunal run's live fetch bounded rather than downloading every
listed file regardless of count."""

_STREET_VIEW_DOCUMENT_ID: Final[str] = "street-view-fallback"
"""Fixed document id for the Street View fallback photo -- deterministic
(unlike a resident upload's random id) since at most one is ever registered
per case, and `evidence.dossier.build_dossier` keys its documents dict by
this id."""

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


_PLAN_TITLE_KEYWORDS: Final[tuple[str, ...]] = ("elevation", "plan", "drawing", "section", "site")
"""Title-heuristic keywords for "this exhibited document is probably a
plan/drawing, not an administrative letter" (CASES.md's Blocker 1,
case-insensitive). Deliberately broad -- it is fine for this to also match
"Notification plan" or "Site Plan" alongside "Elevations"; the point is
distinguishing a plan-shaped document from a cover letter/notice, not
picking exactly one document kind. Reused by both
`_rank_tracker_documents` (ranks a tracker's raw listing before the
`_MAX_TRACKER_DOCUMENTS` cap truncates it) and `_select_plan_document`
(picks which dossier document to ground/overlay) so both ends of Blocker 1
agree on what "looks like a plan" means. When `_plan_document_title` has
already classified a document, its kind name (e.g. "Elevations") is baked
into the title this same heuristic reads, so a real clerk classification
is automatically preferred over the raw-filename fallback with no extra
branching -- a title such as "Elevations (elevations.pdf)" matches on
"elevation" whether that word came from the model's classification or the
original filename."""


def _looks_like_plan_document(title: str) -> bool:
    """True if `title` looks like a plan/elevation/drawing rather than an
    administrative letter or notice -- see `_PLAN_TITLE_KEYWORDS`."""
    lowered = title.lower()
    return any(keyword in lowered for keyword in _PLAN_TITLE_KEYWORDS)


def _rank_tracker_documents(listed: Sequence[ExhibitedDocument]) -> list[ExhibitedDocument]:
    """Re-rank a tracker's raw document listing so plan-shaped titles sort
    ahead of everything else, each group keeping its own relative (real
    eTrack: most-recently-lodged-first) order -- CASES.md's Blocker 1 (a):
    a real council register lists documents by lodgement date, not by
    type, so the actual Elevations drawing can sit past
    `_MAX_TRACKER_DOCUMENTS`'s cap while an administrative cover letter
    lodged more recently occupies a slot ahead of it. Ranking *before* the
    cap is applied (rather than raising the cap) lets a real, larger
    register still degrade gracefully: every plan-like document gets a
    slot first, and a non-plan document (a notification letter, a
    statement) still fills any slot that remains once they do -- never an
    all-or-nothing exclusion of ordinary paperwork."""
    plan_docs = [d for d in listed if _looks_like_plan_document(d.title)]
    other_docs = [d for d in listed if not _looks_like_plan_document(d.title)]
    return plan_docs + other_docs


def _select_plan_document(documents: Sequence[SourceDocument]) -> SourceDocument | None:
    """Pick the `DOCUMENTS_ONLY` document to ground/overlay -- CASES.md's
    Blocker 1 (b): the prior behaviour picked the first `DOCUMENTS_ONLY`
    document in dict insertion order, with no preference for one actually
    classified (or, absent classification, title-heuristically
    identified, see `_looks_like_plan_document`) as a plan/elevation. On a
    real DA register a Resident Notification Letter can easily be the
    first such document in tracker-listing order, landing the annotated
    overlay's boxes -- and the objection's own evidence citation -- on an
    administrative cover letter instead of a drawing.

    Prefers the first `DOCUMENTS_ONLY` document whose title looks like a
    plan; falls back to the first `DOCUMENTS_ONLY` document in dict order
    (the prior, still-correct behaviour) when none of them do, so a
    dossier with no plan-classified/plan-titled document at all is
    unaffected by this change. `None` if there is no `DOCUMENTS_ONLY`
    document at all."""
    candidates = [d for d in documents if d.provenance_grade is ProvenanceGrade.DOCUMENTS_ONLY]
    for candidate in candidates:
        if _looks_like_plan_document(candidate.title):
            return candidate
    return candidates[0] if candidates else None


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
class _IngestOutcome:
    """What :func:`_load_ingest_for_application` resolved, plus whether it
    had to fall back to the frozen demo fixture and why -- `run` uses
    `used_demo_fixture`/`demo_fixture_reason` to record an honest
    `ingest_resolved` case event rather than leaving a resident or judge to
    guess which mode produced their submission."""

    da_record: DevelopmentApplicationRecord
    controls: PlanningControls
    dcp_documents: list[DcpDocument]
    used_demo_fixture: bool
    demo_fixture_reason: str | None = None


async def _load_ingest_for_application(
    application_number: str, *, client: httpx.AsyncClient | None
) -> _IngestOutcome:
    """Resolve `application_number`'s real ingest data live when `client`
    is a real (non-`None`) HTTP client, degrading to the frozen PAN-661190
    demo fixture -- clearly labelled in the returned outcome -- on any
    resolution failure. `client is None` (every existing offline test's
    default) is itself treated as "no live capability configured": the
    demo fixture is returned immediately, with no network attempted at
    all, exactly matching this module's pre-wave-9 behaviour.

    Only exceptions from `ingest.onlineda`/`ingest.spatial`'s own narrow
    error hierarchies (`OnlineDAError`, `SpatialApiError`) and raw
    transport failures (`httpx.HTTPError`) are caught here -- anything else
    is a genuine bug and should still surface, not be silently swallowed
    into "demo fixture mode"."""
    if client is not None:
        try:
            da_record = await fetch_development_application(
                application_number, _DEMO_COUNCIL, client=client
            )
            controls, dcp_documents = await resolve_site(da_record.address, client=client)
            return _IngestOutcome(
                da_record=da_record,
                controls=controls,
                dcp_documents=dcp_documents,
                used_demo_fixture=False,
            )
        except (OnlineDAError, SpatialApiError, httpx.HTTPError) as exc:
            reason = (
                f"could not resolve {application_number!r} against {_DEMO_COUNCIL} live "
                f"({type(exc).__name__}: {exc}); showing the demo fixture case instead"
            )
    else:
        reason = "no live ingest client configured; showing the demo fixture case"

    da_record, controls, dcp_documents = await _load_frozen_ingest()
    return _IngestOutcome(
        da_record=da_record,
        controls=controls,
        dcp_documents=dcp_documents,
        used_demo_fixture=True,
        demo_fixture_reason=reason,
    )


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


_TERMINAL_GROUND_STATUSES: Final[frozenset[GroundStatus]] = frozenset(
    {GroundStatus.SUPPORTED, GroundStatus.REFUSED, GroundStatus.FLAGGED}
)
"""A ground at any of these statuses has already completed review and can
never legally transition back to `GroundStatus.UNDER_REVIEW`
(`state.firestore._ALLOWED_GROUND_TRANSITIONS`) -- `run`'s ground loop
checks this before attempting that transition, so re-running the pipeline
against a case with some or all grounds already decided degrades to
skipping them rather than crashing on `InvalidGroundTransitionError`."""

_STATUS_SEVERITY: Final[Mapping[GateStatus, int]] = {
    GateStatus.SHIPPED: 1,
    GateStatus.FLAGGED: 2,
    GateStatus.REFUSED_UNSUBSTANTIATED: 3,
    GateStatus.REFUSED_IRRELEVANT: 3,
}
"""Ordering used only to resolve which ground "wins" when more than one
ground ends up in contention for the same evidence anchor (see
`_propagate_page_level_anchor_status`) -- an outright refusal is the most
severe outcome to surface to the resident, then flagged (needs a human),
then shipped; higher wins. Never used to compare `GateStatus` for any
other purpose -- `_ground_status_for`/`classify_role` remain the source of
truth for what each status *means*, this is purely a tie-break order."""


def _most_severe_ground(
    ground_ids: Sequence[str], ground_status: Mapping[str, GateStatus]
) -> str | None:
    """The single `ground_id` in `ground_ids` whose status is most severe
    per `_STATUS_SEVERITY` (first one seen wins a tie). `None` if
    `ground_ids` is empty or none of them has a decided status yet (should
    not happen when called after the full ground loop, where every ground
    id already has a `GateDecision`, but handled defensively rather than
    assumed)."""
    winner: str | None = None
    winner_severity = -1
    for ground_id in ground_ids:
        status = ground_status.get(ground_id)
        if status is None:
            continue
        severity = _STATUS_SEVERITY.get(status, 0)
        if severity > winner_severity:
            winner = ground_id
            winner_severity = severity
    return winner


def _most_severe_page_level_ground(
    ground_ids: Sequence[str],
    ground_status: Mapping[str, GateStatus],
    ground_document_ids: Mapping[str, frozenset[str]] | None,
    document_id: str,
) -> str | None:
    """Resolve which of `ground_ids` (every ground that cited *this exact*
    page-level anchor -- never a direct bbox citation, see
    `_propagate_page_level_anchor_status`'s rule (b)) wins the inherited
    status for a bbox anchor with no citation of its own.

    Rule (c): a ground whose own `EvidenceSlice` actually included
    `document_id` is preferred over one that did not -- naming the
    ground's own reviewed material as the primary tie-break, rather than
    letting outcome severity alone decide which ground's citation "counts"
    more for a document it may never have been shown. `_most_severe_ground`
    (refused > flagged > shipped) is the tie-break both within that
    preferred group and, if `ground_document_ids` is unavailable (`None`,
    e.g. a caller with no document-membership data at all) or no candidate
    qualifies, across every candidate unchanged -- exactly `_most_severe_
    ground`'s own prior, still-correct behaviour, preserved as the
    fallback rather than replaced by it.
    """
    if ground_document_ids is not None:
        qualifying = [
            gid for gid in ground_ids if document_id in ground_document_ids.get(gid, frozenset())
        ]
        if qualifying:
            return _most_severe_ground(qualifying, ground_status)
    return _most_severe_ground(ground_ids, ground_status)


def _propagate_page_level_anchor_status(
    dossier: CaseDossier,
    anchor_ground: Mapping[str, str],
    page_level_ground_ids: Mapping[str, Sequence[str]],
    ground_status: Mapping[str, GateStatus],
    ground_document_ids: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, str]:
    """Fix for the root cause of neutral (grey) overlay boxes seen in live
    runs: `_court_slices` registers a whole-page anchor
    (``anchor_id_for(doc, page, None)``) alongside every fine-grained bbox
    anchor `_ground_annotated_evidence` locates on that same page, and a
    reviewer is free to cite either -- a real reviewer looking at one
    full-page image plausibly cites the page as a whole rather than a
    specific crop. When it does, the page-level anchor ends up with a
    ground in `anchor_ground`, but every *drawn* bbox anchor on that page
    has none, so `evidence.overlays.classify_role` renders them all as a
    neutral `EVIDENCE_ANCHOR` even though the page they sit on was in fact
    cited and decided.

    Propagates every page-level citation's ground down onto every bbox
    anchor registered on that same ``(source_doc, page)``, under three
    rules (SMOKE.md v5's "one honest nuance" -- a SHIPPED ground's own
    directly-cited bbox anchor was rendering orange, overridden by an
    unrelated REFUSED ground's page-level citation of the same page --
    named the refinement this function now implements):

    * **(a) a direct citation always wins, never overridden.** A bbox
      anchor a reviewer cited *by its own anchor id* keeps that citation's
      ground and status outright, full stop -- it never even enters
      contention with a page-level-only citation from a *different*
      ground, no matter how much more severe that other ground's outcome
      is. Before this rule, a directly-cited-and-SHIPPED box could be
      quietly recoloured refused/orange purely because some other,
      unrelated ground also happened to cite the whole page it sits on --
      exactly backwards from what a resident needs to see (a box they
      were shown evidence about keeps that evidence's own verdict).
    * **(b) page-level inheritance is an all-or-nothing per-page
      fallback, never a per-anchor gap-filler on a page that already has
      specific citations elsewhere (wave 9 fix -- "meaningless mid-house
      boxes").** Inheritance from `page_level_ground_ids` is considered
      only for a bbox anchor on a page that has ZERO directly-cited bbox
      anchors of its own (from ANY ground). Before this rule, one
      ground's page-level citation (e.g. "property value", which never
      discussed the house's windows or doors) could paint every OTHER
      uncited bbox anchor on that same page -- window/door boxes with no
      real relationship to that ground -- purely because nothing else had
      claimed them yet. Now, the moment a page has even one specific,
      directly-cited element, every other element on that page is treated
      as genuinely un-discussed (stays neutral/grey) rather than painted
      by inference from an unrelated ground's whole-page citation. A page
      with no direct citations at all still falls back to whichever
      ground(s) cited it at the page level, exactly as before.
    * **(c) among competing page-level-only claims (on a page with no
      direct citations), prefer the ground whose evidence was actually
      shown this document.** When more than one ground's page-level
      citation reaches the same anchor, `_most_severe_page_level_ground`
      resolves the winner -- preferring a ground whose own
      `EvidenceSlice` actually included this bbox's document over one
      that did not, before falling back to severity (refused beats
      flagged beats shipped) as the tie-break.

    `anchor_ground` is never mutated; a new mapping is returned with an
    entry for every bbox anchor that ends up with an effective ground
    (direct or inherited) -- a bbox anchor with neither stays absent.
    """
    # Root-cause fix (wave 9): page-level inheritance must be an all-or-
    # nothing fallback for a page with NO directly-cited bbox anchors of
    # its own, never a per-anchor gap-filler on a page that already has
    # specific citations elsewhere. Before this guard, a single ground's
    # page-level citation (e.g. "property value") could paint every OTHER
    # uncited bbox anchor on that page (unrelated windows/doors) orange,
    # even though that ground never discussed those specific elements --
    # the exact "meaningless mid-house boxes" regression this fixes.
    pages_with_direct_citation = {
        (anchor.source_doc, anchor.page)
        for anchor in dossier.anchors.values()
        if anchor.bbox is not None and anchor.anchor_id in anchor_ground
    }

    result = dict(anchor_ground)
    for anchor in dossier.anchors.values():
        if anchor.bbox is None:
            continue  # only propagating *onto* fine-grained bbox anchors
        if anchor.anchor_id in anchor_ground:
            # Rule (a): already carried over via the `dict(anchor_ground)`
            # copy above -- explicit `continue` so a direct citation is
            # never put into contention with (and can never be overridden
            # by) an inherited page-level one, regardless of severity.
            continue
        if (anchor.source_doc, anchor.page) in pages_with_direct_citation:
            # This page already has at least one specific, directly-cited
            # bbox anchor (possibly from a different ground) -- a
            # page-level citation from yet another ground is not evidence
            # that THIS particular uncited element was discussed, so it
            # stays neutral/grey rather than being painted by inference.
            continue
        page_anchor_id = anchor_id_for(anchor.source_doc, anchor.page, None)
        inherited = page_level_ground_ids.get(page_anchor_id)
        if not inherited:
            continue  # rule (b): no citation of its own, nothing to inherit either
        winner = _most_severe_page_level_ground(
            inherited, ground_status, ground_document_ids, anchor.source_doc
        )
        if winner is not None:
            result[anchor.anchor_id] = winner
    return result


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
        street_view_secret_accessor: SecretAccessor | None = None,
    ) -> None:
        """Args mirror the pipeline's real dependencies; `clause_model`/
        `evidence_model`/`ingest_client`/`grounding_client`/
        `document_classifier`/`street_view_secret_accessor` exist so tests
        can inject fakes -- production (`job.main`) leaves them at their
        live defaults. `document_classifier` defaults to the real
        `setback.clerk.classify_document` (see `_default_document_classifier`).
        `street_view_secret_accessor` defaults to `None`, which
        `evidence.imagery.fetch_street_view_fallback` itself resolves to a
        live Secret Manager read (`default_secret_accessor()`) -- tests
        inject a fake so the Street View trigger is exercisable with zero
        live Secret Manager/Maps calls."""
        self._document_source = document_source
        self._polisher = polisher
        self._grounding_client = grounding_client
        self._clause_model = clause_model
        self._evidence_model = evidence_model
        self._ingest_client = ingest_client
        self._document_classifier = document_classifier or _default_document_classifier
        self._street_view_secret_accessor = street_view_secret_accessor

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
        plan_document = _select_plan_document(list(dossier.documents.values()))
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

    def _full_res_semantic_overlay_png(
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
        has `ctx`, not the dossier itself.

        Returns `render_semantic_overlay`'s own full-resolution PNG bytes,
        UN-shrunk -- see `_semantic_overlay_png` (which wraps this for the
        Firestore-safe, embedded-in-the-event copy) and `_store_full_res_
        overlay` (which persists exactly this method's output for the
        click-to-open lightbox, LEO-FEEDBACK-UIUX.md §5)."""
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
        return render_semantic_overlay(ctx.page, overlay_boxes)

    def _semantic_overlay_png(
        self,
        ctx: _GroundedOverlayContext,
        *,
        ground_status: Mapping[str, GateStatus],
        anchor_ground: Mapping[str, str],
    ) -> bytes:
        """The Firestore-safe copy of `_full_res_semantic_overlay_png`'s
        output, downscaled via `_shrink_png_for_storage` to fit a
        `CaseEvent`'s ~1 MiB payload limit -- this is the copy embedded
        directly (base64) in the `annotated_overlay` event, never the
        click-to-open target (see `_store_full_res_overlay`)."""
        return _shrink_png_for_storage(
            self._full_res_semantic_overlay_png(
                ctx, ground_status=ground_status, anchor_ground=anchor_ground
            )
        )

    async def _store_full_res_overlay(
        self, case_id: str, ctx: _GroundedOverlayContext, full_res_png: bytes
    ) -> str | None:
        """Durably write `full_res_png` (the un-shrunk overlay
        `_full_res_semantic_overlay_png` produced) so `console/app.py` has
        a real full-resolution image to link the overlay's lightbox to
        (LEO-FEEDBACK-UIUX.md §5: "overlay image clickable -> full
        resolution") -- before this fix, only the shrunk copy embedded in
        the event ever existed anywhere, so "click to open full
        resolution" only re-displayed that same already-downscaled image
        bigger.

        Writes via `self._document_source` when it also satisfies
        `EvidenceUploadStore` (the write side of the same port) --
        `evidence.storage.GcsEvidenceStore` in production, and the
        in-memory `UserUploadedDocumentSource` double every offline test
        already constructs, so this is exercised with zero live network
        calls in the test suite. Returns the document id the overlay was
        stored under (for the `annotated_overlay` event's `full_res_
        document_id` field), or `None` when `document_source` is
        read-only -- degrades to no click-through rather than failing the
        run; should not arise in production, where `job.main` always
        wires a real `GcsEvidenceStore`."""
        if not isinstance(self._document_source, EvidenceUploadStore):
            return None
        document_id = f"overlay-{ctx.document_id}-p{ctx.page.page_number}"
        await self._document_source.add_evidence_document(
            case_id, document_id, full_res_png, content_type="image/png"
        )
        return document_id

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

    async def _exhibited_tracker_documents(
        self, da_record: DevelopmentApplicationRecord, *, client: httpx.AsyncClient
    ) -> list[tuple[str, str, bytes]]:
        """Best-effort fetch of `da_record`'s real exhibited documents from
        `ingest.tracker.EtrackDocumentSource` (Georges River Council's
        tracker -- the one tracker family this build actually speaks, per
        this wave's scope allowance) -- classified the same way an uploaded
        PDF is, so a plan sourced from the tracker looks no different on
        the case page than one a resident uploaded themselves. Only called
        once live OnlineDA resolution has already succeeded (`run` never
        reaches here in demo-fixture mode). A listing or per-document
        download failure is reported to stderr and simply excludes that
        document -- never fatal to the run, matching `_build_dossier`'s own
        upload-download degrade-gracefully convention.

        `client` (this module's shared `self._ingest_client`) must follow
        redirects: `EtrackDocumentSource`'s own search-postback flow relies
        on it (a 302 to the application's detail page) exactly as its own
        default, self-constructed client does -- only relevant when a
        caller (like this one) injects a shared client rather than letting
        `EtrackDocumentSource` build its own."""
        source = EtrackDocumentSource(client=client)
        try:
            listed = await source.list_documents(da_record.council_application_number)
        except TrackerError as exc:
            print(
                f"eTrack document listing failed for "
                f"{da_record.council_application_number!r}: {exc}",
                file=sys.stderr,
            )
            return []

        documents: list[tuple[str, str, bytes]] = []
        for exhibited in _rank_tracker_documents(listed)[:_MAX_TRACKER_DOCUMENTS]:
            try:
                content = await source.download_document(exhibited)
            except TrackerError as exc:
                print(
                    f"eTrack document download failed for {exhibited.document_id!r}: {exc}",
                    file=sys.stderr,
                )
                continue
            kind = await self._classify_plan_document(exhibited.title, content)
            title = _plan_document_title(exhibited.title, kind)
            documents.append((f"etrack-{exhibited.document_id}", title, content))
        return documents

    async def _street_view_fallback_document(
        self, address: str
    ) -> tuple[str, str, bytes, ProvenanceGrade] | None:
        """Fetch the grade-B Street View fallback for `address`, or `None`
        if it is unavailable (no coverage, or a request failure -- both
        degrade the same way here: no fallback image, never a crashed
        run). See the module docstring's "Street View fallback trigger"
        section for the exact three-part trigger condition this method's
        caller (`_build_dossier`) implements."""
        if self._ingest_client is None:
            return None
        try:
            fallback = await fetch_street_view_fallback(
                address,
                client=self._ingest_client,
                secret_accessor=self._street_view_secret_accessor,
            )
        except StreetViewUnavailableError as exc:
            print(f"Street View fallback unavailable for {address!r}: {exc}", file=sys.stderr)
            return None
        if fallback is None:
            return None
        title = f"Street View fallback ({fallback.attribution})"
        return (_STREET_VIEW_DOCUMENT_ID, title, fallback.image_bytes, fallback.provenance_grade)

    async def _record_street_view_fallback_event(
        self,
        case_id: str,
        fallback_document: tuple[str, str, bytes, ProvenanceGrade],
        *,
        store: CaseStore | None,
    ) -> None:
        """Surface a fetched Street View fallback to the resident-facing
        Evidence section, not only to the grounding model.

        **The bug this closes** (LEO-FEEDBACK-UIUX.md §4, "verify the
        Street View fallback... actually fires and renders"): before this
        fix, `_build_dossier` registered the fallback in the in-memory
        `CaseDossier` for grounding purposes only -- it never appended a
        `document_uploaded` event, the *only* event type `console.app`'s
        `_SECTION_FOR_EVENT_TYPE` map routes to the "Evidence" section. The
        fetch itself worked correctly (confirmed live against real Street
        View coverage during the wave-9 populate pass -- see that pass's
        "Blocker 2"), but a resident's case page rendered an empty
        Evidence section regardless, silently, with no error anywhere:
        this was a missing event, not a failed request.

        Durably stores the image bytes via `self._document_source` (when
        it also satisfies `EvidenceUploadStore` -- `GcsEvidenceStore` in
        production, the in-memory double in every offline test) at the
        same `document_id` the dossier already uses, so the existing
        `GET /api/cases/{case_id}/documents/{document_id}` route serves it
        back exactly like any resident-uploaded photo, needing no new
        storage or route. `store` is optional and a no-op when omitted (a
        handful of this module's white-box tests call `_build_dossier`
        directly with no store, and degrading rather than requiring one
        keeps those unaffected) -- `run()` always passes a real one.
        """
        if store is None:
            return
        document_id, title, image_bytes, grade = fallback_document
        if isinstance(self._document_source, EvidenceUploadStore):
            await self._document_source.add_evidence_document(
                case_id, document_id, image_bytes, content_type="image/jpeg"
            )
        await store.append_event(
            case_id,
            f"document-uploaded:{document_id}",
            "document_uploaded",
            payload={
                "document_id": document_id,
                "filename": title,
                "content_type": "image/jpeg",
                "size_bytes": len(image_bytes),
                "provenance_grade": grade.value,
            },
        )

    async def _build_dossier(
        self, case_id: str, resume: ResumeState, *, store: CaseStore | None = None
    ) -> tuple[CaseDossier, _IngestOutcome]:
        application_number = (
            resume.case.application_number if resume.case is not None else _DEMO_PAN
        )
        ingest_outcome = await _load_ingest_for_application(
            application_number, client=self._ingest_client
        )
        da_record = ingest_outcome.da_record
        controls = ingest_outcome.controls
        dcp_documents = ingest_outcome.dcp_documents

        plan_documents: list[tuple[str, str, bytes]] = []
        photo_documents: list[tuple[str, str, bytes, ProvenanceGrade]] = []

        if not ingest_outcome.used_demo_fixture and self._ingest_client is not None:
            plan_documents.extend(
                await self._exhibited_tracker_documents(da_record, client=self._ingest_client)
            )

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

        has_resident_photo = any(
            grade is ProvenanceGrade.RESIDENT_PHOTO
            for _id, _title, _content, grade in photo_documents
        )
        if not has_resident_photo and da_record.address:
            fallback_document = await self._street_view_fallback_document(da_record.address)
            if fallback_document is not None:
                photo_documents.append(fallback_document)
                await self._record_street_view_fallback_event(
                    case_id, fallback_document, store=store
                )

        dossier = build_dossier(
            da_record=da_record,
            controls=controls,
            dcp_documents=dcp_documents,
            plan_documents=plan_documents,
            photo_documents=photo_documents,
        )
        return dossier, ingest_outcome

    async def run(self, case_id: str, resume: ResumeState, store: CaseStore) -> None:
        """Run the full court/gate/dispatch pipeline for `case_id` and
        persist every stage's outcome as durable case events, so the
        console's SSE stream and case page render the whole run live."""
        if resume.case is None:
            raise ValueError(f"case {case_id!r} has no resumable state")

        # Idempotency guard (SMOKE.md's "Fix 4 -- not fixed" re-press
        # crash): a case whose tribunal already ran to completion has a
        # `submission_composed` event on record. Re-running the pipeline
        # against it would try to transition every already-terminal ground
        # back to `under_review`, which `state.firestore`'s lifecycle guard
        # correctly refuses -- `InvalidGroundTransitionError` -- crashing
        # the whole job execution rather than degrading gracefully. A
        # resident only ever clicks "Start tribunal" once, but a judge
        # double-clicking the same button (a case this build must survive)
        # will hit exactly this path. Made a safe, clearly-labelled no-op
        # instead: nothing is re-run, nothing is re-composed, and the
        # existing submission/refusals documents already on the case page
        # are left exactly as they are.
        if any(event.event_type == "submission_composed" for event in resume.events):
            await store.append_event(
                case_id,
                f"tribunal-rerun-ignored:{case_id}",
                "tribunal_rerun_ignored",
                payload={
                    "reason": (
                        "this case's tribunal has already run to completion; "
                        "starting it again made no changes."
                    )
                },
            )
            return

        dossier, ingest_outcome = await self._build_dossier(case_id, resume, store=store)
        await store.append_event(
            case_id,
            f"ingest-resolved:{case_id}",
            "ingest_resolved",
            payload={
                "application_number": resume.case.application_number,
                "council": dossier.da_record.council,
                "council_application_number": dossier.da_record.council_application_number,
                "address": dossier.da_record.address,
                "used_demo_fixture": ingest_outcome.used_demo_fixture,
                **(
                    {"reason": ingest_outcome.demo_fixture_reason}
                    if ingest_outcome.demo_fixture_reason is not None
                    else {}
                ),
            },
        )
        dossier, grounding_ctx = await self._ground_annotated_evidence(dossier)
        # The semantic overlay itself (colour-coded by each anchor's
        # ground's gate outcome) is rendered and emitted below, after the
        # ground loop -- see `_ground_annotated_evidence`'s docstring.
        anchor_ground: dict[str, str] = {}
        # Every ground_id that cited a *page-level* (bbox=None) anchor,
        # keyed by that page-level anchor's own id -- multi-valued (unlike
        # `anchor_ground` above) so `_propagate_page_level_anchor_status`
        # can resolve the most severe outcome when more than one ground
        # cites the same page. See that function's docstring.
        page_level_ground_ids: dict[str, list[str]] = defaultdict(list)
        # Every ground_id's own `EvidenceSlice` document membership (source
        # ids from its `plans`/`photos`), for `_propagate_page_level_anchor_
        # status` rule (c) -- which ground actually had this document in
        # the material it was shown, not just which one happens to cite the
        # right page-level anchor id.
        ground_document_ids: dict[str, frozenset[str]] = {}

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
            existing_record = resume.grounds.get(ground.ground_id)
            if existing_record is not None and existing_record.status in _TERMINAL_GROUND_STATUSES:
                # Defence in depth alongside the top-level `submission_
                # composed` guard above: a ground already at a terminal
                # status (from an earlier, possibly partial run) can never
                # legally transition back to `under_review` -- skip it
                # rather than let `store.transition_ground` raise and
                # crash the whole execution.
                await store.append_event(
                    case_id,
                    f"ground-rerun-skipped:{ground.ground_id}",
                    "ground_rerun_skipped",
                    payload={"ground_id": ground.ground_id, "status": existing_record.status.value},
                )
                continue
            await store.transition_ground(case_id, ground.ground_id, GroundStatus.UNDER_REVIEW)
            clause_slice, evidence_slice = _court_slices(ground, dossier)
            ground_document_ids[ground.ground_id] = frozenset(
                p.source_ref for p in evidence_slice.plans
            ) | frozenset(p.source_ref for p in evidence_slice.photos)

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
                if anchor is not None and anchor.bbox is None:
                    # A page-level citation -- tracked separately (multi-
                    # valued) so `_propagate_page_level_anchor_status` can
                    # spread it onto every bbox anchor on the same page
                    # once every ground's gate decision is known, below.
                    page_level_ground_ids[anchor_id].append(ground.ground_id)
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
            effective_anchor_ground = _propagate_page_level_anchor_status(
                dossier,
                anchor_ground,
                page_level_ground_ids,
                ground_status,
                ground_document_ids=ground_document_ids,
            )
            full_res_overlay_png = self._full_res_semantic_overlay_png(
                grounding_ctx, ground_status=ground_status, anchor_ground=effective_anchor_ground
            )
            overlay_png = _shrink_png_for_storage(full_res_overlay_png)
            full_res_document_id = await self._store_full_res_overlay(
                case_id, grounding_ctx, full_res_overlay_png
            )
            overlay_payload: dict[str, Any] = {
                "document_id": grounding_ctx.document_id,
                "mime_type": "image/png",
                "image_base64": base64.b64encode(overlay_png).decode("ascii"),
            }
            if full_res_document_id is not None:
                overlay_payload["full_res_document_id"] = full_res_document_id
            await store.append_event(
                case_id,
                f"annotated-overlay:{grounding_ctx.document_id}",
                "annotated_overlay",
                payload=overlay_payload,
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
