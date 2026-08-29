"""The Gemma Clerk: low-cost clerical extraction over ``config.CLERK``.

Two model-backed functions, both routed through :class:`~setback.models.
client.ModelClient` to the Gemma MaaS tier (``gemma-4-26b-a4b-it-maas``,
cheap, no GPU, OpenAI-compatible endpoint -- see that module's docstring):

* :func:`classify_document` -- given a document's filename and its
  rendered first page's text, classify what kind of DA document it is
  (elevations, a site plan, a shadow diagram, ...). Used to tag an
  uploaded/exhibited document without a human ever naming its type.
* :func:`normalise_concerns` -- given a resident's free-text concern,
  extract one or more :class:`NormalisedConcern`\\ s: a structured
  category, an optional target ("the rear yard", "my bedroom window"),
  qualifiers ("only in winter afternoons"), and -- the part that matters
  for privacy -- ``redacted_text``, a copy of the resident's words with
  personal names, phone numbers, and email addresses stripped out. Every
  downstream prompt (the tribunal's court/gate/dispatch stages) is meant
  to consume ``redacted_text``, never the resident's raw statement.

Both functions carry a **deterministic fallback** -- a keyword classifier
for documents (:data:`DocumentKind.OTHER` on no match) and the pre-existing
keyword-based :func:`classify_concern` plus a regex :func:`redact_
personal_information` for concerns -- so a Gemma outage or a malformed
reply (:class:`~setback.models.client.ModelCallError`) never blocks the
pipeline; it just degrades to the cheaper, less nuanced classification.

``ConcernType`` (the interview's own concern triage) is defined here rather
than in :mod:`setback.interview.flow` specifically so this module has no
import-time dependency on the interview package -- :mod:`setback.interview.
flow` imports it (and :func:`classify_concern`) back from here and
re-exports both names for backward compatibility with every existing
caller.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel

from setback.config import CLERK
from setback.models.client import ModelCallError

if TYPE_CHECKING:
    from setback.models.client import ModelClient

# --- document classification -------------------------------------------------


class DocumentKind(StrEnum):
    """The kinds of DA document Setback's evidence pipeline recognises."""

    ELEVATIONS = "elevations"
    SITE_PLAN = "site_plan"
    SEE = "see"
    SHADOW_DIAGRAM = "shadow_diagram"
    SURVEY = "survey"
    BASIX = "basix"
    WASTE = "waste"
    OTHER = "other"


# Order matters: checked top to bottom, first match wins. Shadow diagrams
# are checked before elevations since a shadow study's first page often
# also mentions "elevation" (e.g. "north elevation shadow diagram").
_DOCUMENT_KEYWORDS: tuple[tuple[DocumentKind, tuple[str, ...]], ...] = (
    (
        DocumentKind.SHADOW_DIAGRAM,
        ("shadow diagram", "shadow diagrams", "shadow study", "shadow analysis", "sun study"),
    ),
    (DocumentKind.ELEVATIONS, ("elevation", "elevations")),
    (DocumentKind.SITE_PLAN, ("site plan", "site analysis plan")),
    (
        DocumentKind.SEE,
        ("statement of environmental effects", "statement of environmental effect"),
    ),
    (DocumentKind.SURVEY, ("survey", "detail and level survey", "surveyor")),
    (DocumentKind.BASIX, ("basix",)),
    (DocumentKind.WASTE, ("waste management plan", "waste management", "wmp")),
)


def _classify_document_by_keywords(filename: str, first_page_text: str) -> DocumentKind:
    """Deterministic fallback: keyword match over filename + first-page text,
    `DocumentKind.OTHER` on no match -- never raises, never blocks."""
    haystack = f"{filename} {first_page_text}".lower()
    for kind, keywords in _DOCUMENT_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return kind
    return DocumentKind.OTHER


class _DocumentClassification(BaseModel):
    """Structured-output schema for the one Gemma call `classify_document` makes."""

    kind: DocumentKind


async def classify_document(
    filename: str, first_page_text: str, *, client: ModelClient
) -> DocumentKind:
    """Classify an uploaded/exhibited DA document from its filename and its
    rendered first page's extracted text, via one ``CLERK``-tier call.

    Falls back to :func:`_classify_document_by_keywords` (never raises) on
    :class:`~setback.models.client.ModelCallError`, so a Gemma outage
    degrades classification quality rather than blocking ingestion.
    """
    prompt = (
        "You are a council planning clerk classifying one document exhibited as part "
        "of a NSW development application. Read its filename and the text of its "
        "first page, and classify it into exactly one document kind: elevations "
        "(building elevation drawings), site_plan (a site plan or site analysis "
        "plan), see (a Statement of Environmental Effects), shadow_diagram (a shadow "
        "diagram/study), survey (a detail and level survey), basix (a BASIX "
        "certificate), waste (a waste management plan), or other if none of those "
        "fit.\n\n"
        f"Filename: {filename!r}\n\n"
        f"First page text:\n{first_page_text}"
    )
    try:
        result = await client.generate(CLERK, prompt, _DocumentClassification)
    except ModelCallError:
        return _classify_document_by_keywords(filename, first_page_text)
    return result.output.kind


# --- concern classification: deterministic keyword triage --------------------
#
# This is the interview's own light triage -- a fixed, keyword-matched
# classification used purely to pick which targeted clarifying question to
# ask next, and the deterministic fallback `normalise_concerns` degrades to
# when the model call fails. It is deliberately *not* the s4.15(1) category
# a ground is later tagged with (that is the reviewers'/gate's job, over
# richer evidence than one opening sentence); several of these concern
# types (e.g. `PROPERTY_VALUE`, `VIEW_LOSS`) map to categories the gate
# refuses outright, and that is fine -- the interview's job is to draw the
# resident out, not to pre-judge relevance.


class ConcernType(StrEnum):
    """The presenting concern types the interview recognises."""

    HEIGHT_BULK = "height_bulk"
    PRIVACY_OVERLOOKING = "privacy_overlooking"
    OVERSHADOWING = "overshadowing"
    TREES_LANDSCAPE = "trees_landscape"
    TRAFFIC_PARKING = "traffic_parking"
    HERITAGE_CHARACTER = "heritage_character"
    VIEW_LOSS = "view_loss"
    PROPERTY_VALUE = "property_value"
    NOISE = "noise"
    OTHER = "other"


_CONCERN_KEYWORDS: tuple[tuple[ConcernType, tuple[str, ...]], ...] = (
    (
        ConcernType.OVERSHADOWING,
        ("shadow", "overshadow", "shade", "sunlight", "sun ", "block the sky", "block the sun"),
    ),
    (
        ConcernType.PRIVACY_OVERLOOKING,
        ("privacy", "overlook", "see into", "see straight into", "bedroom window"),
    ),
    (
        ConcernType.VIEW_LOSS,
        ("view", "harbour", "outlook", "skyline"),
    ),
    (
        ConcernType.TREES_LANDSCAPE,
        ("tree", "fig", "canopy", "landscap", "vegetation"),
    ),
    (
        ConcernType.TRAFFIC_PARKING,
        ("traffic", "parking", "park ", "cars", "congestion"),
    ),
    (
        ConcernType.HERITAGE_CHARACTER,
        ("heritage", "streetscape", "character of the street", "conservation"),
    ),
    (
        ConcernType.NOISE,
        ("noise", "noisy", "loud", "jackhammer", "construction hours"),
    ),
    (
        ConcernType.PROPERTY_VALUE,
        ("property value", "worth less", "resale", "devalue"),
    ),
    (
        ConcernType.HEIGHT_BULK,
        ("tall", "height", "storey", "storeys", "bulk", "massing", "towers over"),
    ),
)


def _keyword_present(keyword: str, lowered_text: str) -> bool:
    """True if `keyword` occurs in `lowered_text` with a real word start
    immediately before it (not embedded mid-word, e.g. "tree" inside
    "street") -- but no trailing boundary required, so a stem like
    "landscap" still matches "landscaping"."""
    pattern = r"(?<![a-z])" + re.escape(keyword)
    return re.search(pattern, lowered_text) is not None


def classify_concern(text: str) -> ConcernType:
    """Classify a resident's free-text concern into a :class:`ConcernType`
    via simple, deterministic keyword matching -- no model call, and no
    silent fallback to "probably fine": an unrecognised concern is
    classified `OTHER`, which still gets a real (generic) clarifying
    question, never dropped."""
    lowered = text.lower()
    for concern_type, keywords in _CONCERN_KEYWORDS:
        if any(_keyword_present(keyword, lowered) for keyword in keywords):
            return concern_type
    return ConcernType.OTHER


# --- personal-information redaction (deterministic, always available) -------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_PHONE_RE = re.compile(
    r"(?:\+?61[\s-]?4|04)\d{2}[\s-]?\d{3}[\s-]?\d{3}"  # AU mobile
    r"|(?:\+?61[\s-]?[2378]|\(0[2378]\)|0[2378])[\s-]?\d{4}[\s-]?\d{4}"  # AU landline
)

_NAME_INTRO_RE = re.compile(
    r"\b(my name is|i am|i'm|this is|call me)\s+"
    r"([A-Z][a-zA-Z']+(?:\s+[A-Z][a-zA-Z']+){0,2})",
    re.IGNORECASE,
)


def redact_personal_information(text: str) -> str:
    """Deterministic redaction of email addresses, AU phone numbers, and
    self-introduced personal names (``"my name is Jane Smith"`` ->
    ``"my name is [NAME]"``) from resident-supplied free text.

    This is intentionally conservative -- it only catches names the
    resident explicitly introduces themselves with, not every capitalised
    word (which would also catch place/street names) -- and always
    available with no model call, so it is safe to run on every turn, not
    just as `normalise_concerns`'s failure fallback.
    """
    redacted = _EMAIL_RE.sub("[EMAIL]", text)
    redacted = _PHONE_RE.sub("[PHONE]", redacted)
    redacted = _NAME_INTRO_RE.sub(lambda m: f"{m.group(1)} [NAME]", redacted)
    return redacted


class NormalisedConcern(BaseModel):
    """One resident concern, structured and made safe to pass downstream.

    ``redacted_text`` is the field every tribunal-facing prompt (court
    reviewers, the gate, dispatch composition) should consume in place of
    the resident's raw statement -- see the module docstring.
    """

    category: ConcernType
    target: str | None
    qualifiers: list[str]
    redacted_text: str


class _ConcernsExtraction(BaseModel):
    """Structured-output schema for the one Gemma call `normalise_concerns` makes."""

    concerns: list[NormalisedConcern]


async def normalise_concerns(text: str, *, client: ModelClient) -> list[NormalisedConcern]:
    """Extract one or more :class:`NormalisedConcern`\\ s from a resident's
    free-text concern, via one ``CLERK``-tier call.

    Falls back, on :class:`~setback.models.client.ModelCallError`, to a
    single concern built from the deterministic :func:`classify_concern`
    keyword triage and :func:`redact_personal_information` -- so the
    interview never blocks on the clerk, only loses the model's finer
    category/target/qualifier extraction.
    """
    prompt = (
        "You are a council planning clerk normalising a resident's free-text objection "
        "to a neighbouring development application. Read their statement below and "
        "extract every distinct concern it raises as a structured entry: `category` "
        "(one of height_bulk, privacy_overlooking, overshadowing, trees_landscape, "
        "traffic_parking, heritage_character, view_loss, property_value, noise, or "
        "other), an optional `target` naming the specific thing affected (e.g. 'the "
        "rear yard', 'my bedroom window', 'the fig tree on the boundary'), a list of "
        "`qualifiers` (short phrases like 'only in winter afternoons' or 'every "
        "weekday morning'), and `redacted_text`: the resident's own words for this "
        "concern with any personal name, phone number, or email address they "
        "mentioned replaced with [NAME], [PHONE], or [EMAIL] respectively -- never "
        "include a real name, phone number, or email address in `redacted_text`. "
        "Almost every statement raises exactly one concern; only split it into "
        "multiple entries if it clearly raises more than one distinct issue.\n\n"
        f"Resident's statement:\n{text}"
    )
    try:
        result = await client.generate(CLERK, prompt, _ConcernsExtraction)
    except ModelCallError:
        return [
            NormalisedConcern(
                category=classify_concern(text),
                target=None,
                qualifiers=[],
                redacted_text=redact_personal_information(text),
            )
        ]
    return result.output.concerns
