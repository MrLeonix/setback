"""Composes the two dispatch deliverables from the gate's decisions.

Takes the gate's typed :class:`~setback.gate.validator.GateDecision` list —
one decision per candidate ground, already ruled ``shipped``,
``refused-irrelevant``, ``refused-unsubstantiated``, or ``flagged`` — plus
the case-level facts and per-ground narrative content, and deterministically
assembles two documents, each rendered to both Markdown and clean HTML:

1. **The submission**: a council-format objection letter. Numbered grounds
   (shipped only), each with its clause citation, document/page reference,
   and — where the ground carries one — its annotated-image reference. A
   header block carries the DA number, property address, exhibition window,
   and submitter placeholders for the resident to fill in before lodging.
   The structure follows the pattern common to NSW council "how to make a
   submission" guidance: addressee/reference block, numbered grounds each
   tied to a specific planning consideration, and a signature block — see
   :mod:`setback.gate.s415` for the underlying statutory heads each ground
   cites.
2. **The refusals explainer**: a plain-English "what I left out and why"
   for every ground that did *not* ship. Each ``refused-*`` ground gets its
   statutory basis and the gate's plain explanation, plus — where the
   category admits a fix (e.g. an unanchored view-loss or character
   complaint that could be rescued by naming a control) — an encouraging
   note on what would make it viable. ``flagged`` grounds are not refusals
   (a human hasn't looked at them yet), but they are equally absent from
   the submission, so they get their own "still under review" section
   rather than silently vanishing — a deliberate scope choice beyond the
   literal "refused grounds", made because a resident who doesn't see a
   ground anywhere would otherwise have no way to know it is still pending.

Composition itself is fully deterministic: this module never asks a model
to decide which grounds appear or what they say. An optional model client
may be given to *polish the prose* of each finished document — grammar,
flow, register — strictly after deterministic assembly; the polish prompt
demands the reviser preserve every fact, number, and heading verbatim, and
any failure (call error, or a heading list that no longer matches) discards
the polished text and keeps the deterministic original rather than risk a
silently altered ground. Tests never pass a polisher, so the whole suite
runs offline against pure string assembly.
"""

from __future__ import annotations

import html as html_lib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from pydantic import BaseModel

from setback.config import INTERVIEW, ModelConfig
from setback.gate.s415 import NON_PLANNING_GROUNDS
from setback.gate.validator import GateDecision, GateStatus
from setback.models.client import ModelCallError, ModelClient

POLISH_TIER: Final[ModelConfig] = INTERVIEW
"""The flash-lite tier used for the optional post-assembly prose polish."""

_SUBMITTER_PLACEHOLDER_BLOCK: Final[str] = (
    "[Your full name]\n[Your postal address]\n[Your email address]\n[Date]"
)

_INTRO_PARAGRAPH: Final[str] = (
    "I am writing to formally object to the above Development Application, exhibited "
    "for public comment during the period stated above. I ask that the following "
    "grounds be taken into account under section 4.15 of the *Environmental Planning "
    "and Assessment Act 1979* (NSW) in determining this application."
)

_EXPLAINER_INTRO: Final[str] = (
    "Not every ground you raised made it into the submission. This isn't a judgement "
    "on your concerns — it's a statutory filter: NSW planning law only lets a consent "
    "authority weigh certain kinds of matters (section 4.15 of the *Environmental "
    "Planning and Assessment Act 1979*), and grounds that raise a genuine planning "
    "matter still need a citation that stands up. Here is exactly what happened to "
    "each ground that didn't ship."
)

_UNDER_REVIEW_NOTE: Final[str] = (
    "These grounds are genuine planning matters, but their citations couldn't be "
    "automatically verified after repeated attempts. A person will review them before "
    "the submission is finalised, rather than refusing them outright."
)


class PolishedProse(BaseModel):
    """The model's structured reply to a polish request: revised body text."""

    polished_markdown: str


# --- inputs the gate's own output doesn't carry ------------------------------


@dataclass(frozen=True)
class CaseInfo:
    """Case-level facts shown in the submission header block."""

    da_number: str
    council: str
    property_address: str
    exhibition_start: date
    exhibition_end: date


@dataclass(frozen=True)
class GroundContent:
    """The narrative content for one ground, keyed by `ground_id` alongside
    its matching :class:`~setback.gate.validator.GateDecision`.

    The gate rules on *whether* a ground ships or is refused; it carries no
    prose statement or human-readable citation location. Whatever assembles
    the case's evidence is responsible for supplying this shape so the
    composer can render the actual paragraph and its reference line.
    """

    statement: str
    document_title: str
    page: int
    annotated_image_ref: str | None = None
    """A reference to the annotated evidence image backing this ground
    (e.g. a filename or asset id), when one exists. Omitted for grounds
    substantiated by text alone."""


@dataclass(frozen=True)
class ComposedDocument:
    """One finished document, rendered in both formats from the same content."""

    markdown: str
    html: str


@dataclass(frozen=True)
class DispatchPackage:
    """The two composed deliverables, ready to hand to the resident."""

    submission: ComposedDocument
    refusals_explainer: ComposedDocument


# --- shared small helpers -----------------------------------------------------


def _fmt_date(value: date) -> str:
    return value.strftime("%-d %B %Y")


def _shipped_grounds(decisions: Sequence[GateDecision]) -> list[GateDecision]:
    return [d for d in decisions if d.status is GateStatus.SHIPPED]


def _unshipped_grounds(decisions: Sequence[GateDecision]) -> list[GateDecision]:
    return [d for d in decisions if d.status is not GateStatus.SHIPPED]


def _content_for(ground_id: str, ground_content: Mapping[str, GroundContent]) -> GroundContent:
    content = ground_content.get(ground_id)
    if content is None:
        raise ValueError(f"no GroundContent supplied for shipped ground {ground_id!r}")
    return content


_HEADING_TRUNCATE_LIMIT: Final[int] = 80


def _truncate(text: str, *, limit: int = _HEADING_TRUNCATE_LIMIT) -> str:
    """Shorten `text` to a heading-length label, cutting on the last word
    boundary at or before `limit` chars rather than mid-word, with a
    trailing ellipsis when anything was actually cut."""
    stripped = " ".join(text.split())
    if len(stripped) <= limit:
        return stripped
    cut = stripped[:limit].rsplit(" ", 1)[0]
    return f"{cut}…" if cut else f"{stripped[:limit]}…"


def _refusal_heading(decision: GateDecision, ground_content: Mapping[str, GroundContent]) -> str:
    """A human-readable heading for an unshipped ground in the refusals
    explainer — never the raw internal `ground_id` (a content hash, e.g.
    ``ground-b72d23845dda7b8e``, meaningless and unpolished to a resident).

    Prefers a short form of the resident's own claim text, when the caller
    has supplied a `GroundContent` entry for this ground (today only
    `job/pipeline.py`'s shipped-ground path does this; an unshipped ground
    that gets one too — e.g. from a future caller change — automatically
    gets the richer heading with no composer change needed). Falls back to
    a plain-English label derived from the ground's `category`, which is
    always present on every `GateDecision` regardless of shipped status, so
    the heading is never the bare hash even when no `GroundContent` exists.
    """
    content = ground_content.get(decision.ground_id)
    if content is not None and content.statement.strip():
        return _truncate(content.statement)
    return decision.category.replace("_", " ").capitalize()


def _encouraging_note(decision: GateDecision) -> str | None:
    """A note on what would make an unshipped ground viable, where the
    category or the citation issues admit a concrete fix. Categories with no
    control hook (bare view loss, unanchored character) can be rescued by
    naming a specific planning instrument clause; unsubstantiated citations
    can be rescued by fixing the cited reference; the remaining non-planning
    categories (property value, commercial competition, applicant personal
    circumstances) have no fix — they are never planning matters regardless
    of evidence, so no encouraging note is offered for them."""
    if decision.status is GateStatus.REFUSED_UNSUBSTANTIATED:
        fixes = "; ".join(decision.citation_issues)
        return (
            "This ground is a genuine planning matter — it just needs its citation "
            f"fixed: {fixes}. Correct the reference and it can be resubmitted."
        )
    if decision.status is GateStatus.REFUSED_IRRELEVANT:
        no_hook_categories = {"private_view_loss", "neighbourhood_character_no_control_hook"}
        if decision.category in no_hook_categories:
            return (
                "This could still become a viable ground if it names a specific planning "
                "control — a DCP view-sharing provision, a scenic-protection clause, or an "
                "LEP desired-character objective that applies to this site. Find that "
                "control and cite it, and the same concern can be resubmitted as a "
                "planning-relevant ground."
            )
        if decision.category in NON_PLANNING_GROUNDS:
            return None
    return None


# --- deterministic assembly: submission ---------------------------------------


def _submission_markdown(
    case: CaseInfo, shipped: Sequence[GateDecision], ground_content: Mapping[str, GroundContent]
) -> str:
    lines = [
        "# Objection to Development Application",
        "",
        f"**To:** {case.council}",
        f"**Development Application:** {case.da_number}",
        f"**Property:** {case.property_address}",
        (
            f"**Exhibition period:** {_fmt_date(case.exhibition_start)} to "
            f"{_fmt_date(case.exhibition_end)}"
        ),
        "",
        _INTRO_PARAGRAPH,
        "",
        "## Grounds of objection",
        "",
    ]
    for index, decision in enumerate(shipped, start=1):
        content = _content_for(decision.ground_id, ground_content)
        lines.append(f"### {index}. {decision.statutory_basis}")
        lines.append("")
        lines.append(content.statement)
        lines.append("")
        lines.append(f"*Reference: {content.document_title}, page {content.page}.*")
        if content.annotated_image_ref is not None:
            lines.append(f"*Annotated evidence: {content.annotated_image_ref}.*")
        lines.append("")
    lines.append("## Submitter details")
    lines.append("")
    lines.append(_SUBMITTER_PLACEHOLDER_BLOCK)
    return "\n".join(lines).rstrip() + "\n"


def _submission_html(
    case: CaseInfo, shipped: Sequence[GateDecision], ground_content: Mapping[str, GroundContent]
) -> str:
    esc = html_lib.escape
    parts = [
        '<article class="submission">',
        "<h1>Objection to Development Application</h1>",
        '<dl class="header-block">',
        f"<dt>To</dt><dd>{esc(case.council)}</dd>",
        f"<dt>Development Application</dt><dd>{esc(case.da_number)}</dd>",
        f"<dt>Property</dt><dd>{esc(case.property_address)}</dd>",
        (
            "<dt>Exhibition period</dt><dd>"
            f"{esc(_fmt_date(case.exhibition_start))} to {esc(_fmt_date(case.exhibition_end))}"
            "</dd>"
        ),
        "</dl>",
        (
            "<p>I am writing to formally object to the above Development Application, "
            "exhibited for public comment during the period stated above. I ask that the "
            "following grounds be taken into account under section 4.15 of the "
            "<em>Environmental Planning and Assessment Act 1979</em> (NSW) in determining "
            "this application.</p>"
        ),
        "<h2>Grounds of objection</h2>",
        '<ol class="grounds">',
    ]
    for decision in shipped:
        content = _content_for(decision.ground_id, ground_content)
        parts.append("<li>")
        parts.append(f"<h3>{esc(decision.statutory_basis)}</h3>")
        parts.append(f"<p>{esc(content.statement)}</p>")
        parts.append(
            f'<p class="reference">Reference: {esc(content.document_title)}, '
            f"page {content.page}.</p>"
        )
        if content.annotated_image_ref is not None:
            parts.append(
                f'<p class="reference">Annotated evidence: {esc(content.annotated_image_ref)}.</p>'
            )
        parts.append("</li>")
    parts.append("</ol>")
    parts.append("<h2>Submitter details</h2>")
    parts.append("<address>")
    parts.extend(f"{esc(line)}<br>" for line in _SUBMITTER_PLACEHOLDER_BLOCK.split("\n"))
    parts.append("</address>")
    parts.append("</article>")
    return "\n".join(parts) + "\n"


# --- deterministic assembly: refusals explainer -------------------------------


def _refusals_explainer_markdown(
    unshipped: Sequence[GateDecision], ground_content: Mapping[str, GroundContent]
) -> str:
    refused = [d for d in unshipped if d.status is not GateStatus.FLAGGED]
    flagged = [d for d in unshipped if d.status is GateStatus.FLAGGED]

    lines = ["# What I left out, and why", "", _EXPLAINER_INTRO, ""]
    if refused:
        lines.append("## Refused grounds")
        lines.append("")
        for decision in refused:
            lines.append(f"### {_refusal_heading(decision, ground_content)}")
            lines.append("")
            lines.append(f"**Statutory basis:** {decision.statutory_basis}")
            lines.append("")
            lines.append(decision.explanation)
            lines.append("")
            note = _encouraging_note(decision)
            if note is not None:
                lines.append(f"**What would make this viable:** {note}")
                lines.append("")
    if flagged:
        lines.append("## Still under review")
        lines.append("")
        lines.append(_UNDER_REVIEW_NOTE)
        lines.append("")
        for decision in flagged:
            lines.append(f"### {_refusal_heading(decision, ground_content)}")
            lines.append("")
            lines.append(f"**Statutory basis:** {decision.statutory_basis}")
            lines.append("")
            issues = "; ".join(decision.citation_issues)
            lines.append(f"Outstanding issues: {issues}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _refusals_explainer_html(
    unshipped: Sequence[GateDecision], ground_content: Mapping[str, GroundContent]
) -> str:
    esc = html_lib.escape
    refused = [d for d in unshipped if d.status is not GateStatus.FLAGGED]
    flagged = [d for d in unshipped if d.status is GateStatus.FLAGGED]

    parts = [
        '<article class="refusals-explainer">',
        "<h1>What I left out, and why</h1>",
        f"<p>{esc(_EXPLAINER_INTRO)}</p>",
    ]
    if refused:
        parts.append("<h2>Refused grounds</h2>")
        for decision in refused:
            parts.append('<section class="refused-ground">')
            parts.append(f"<h3>{esc(_refusal_heading(decision, ground_content))}</h3>")
            parts.append(
                f"<p><strong>Statutory basis:</strong> {esc(decision.statutory_basis)}</p>"
            )
            parts.append(f"<p>{esc(decision.explanation)}</p>")
            note = _encouraging_note(decision)
            if note is not None:
                parts.append(f"<p><strong>What would make this viable:</strong> {esc(note)}</p>")
            parts.append("</section>")
    if flagged:
        parts.append("<h2>Still under review</h2>")
        parts.append(f"<p>{esc(_UNDER_REVIEW_NOTE)}</p>")
        for decision in flagged:
            parts.append('<section class="flagged-ground">')
            parts.append(f"<h3>{esc(_refusal_heading(decision, ground_content))}</h3>")
            parts.append(
                f"<p><strong>Statutory basis:</strong> {esc(decision.statutory_basis)}</p>"
            )
            issues = "; ".join(decision.citation_issues)
            parts.append(f"<p>Outstanding issues: {esc(issues)}</p>")
            parts.append("</section>")
    parts.append("</article>")
    return "\n".join(parts) + "\n"


# --- optional model polish -----------------------------------------------------


def _polish_prompt(document_kind: str, markdown: str) -> str:
    return (
        f"Below is a {document_kind}, in Markdown, already fully assembled. Improve its "
        "prose only: grammar, flow, and register. Preserve every fact, number, heading, "
        "citation, reference line, and placeholder exactly as given, in the same order, "
        "with the same Markdown structure. Do not add, remove, merge, or reorder any "
        "ground, section, or heading. Return the complete revised document as "
        "`polished_markdown`.\n\n---\n\n"
        f"{markdown}"
    )


def _looks_safe_to_use(original: str, polished: str) -> bool:
    """A conservative sanity check that the polish preserved structure: every
    heading line present in the original must still be present, in the same
    order, in the polished text. Anything less is discarded in favour of the
    deterministic original — prose polish must never silently drop a ground."""
    original_headings = [line for line in original.splitlines() if line.startswith("#")]
    polished_headings = [line for line in polished.splitlines() if line.startswith("#")]
    return original_headings == polished_headings


async def _polish(markdown: str, document_kind: str, polisher: ModelClient | None) -> str:
    if polisher is None:
        return markdown
    try:
        result = await polisher.generate(
            POLISH_TIER,
            _polish_prompt(document_kind, markdown),
            PolishedProse,
            system_instruction=(
                "You are a careful copy editor for a resident's council objection "
                "documents. You never invent, remove, or alter facts — only prose."
            ),
        )
    except ModelCallError:
        return markdown
    polished = result.output.polished_markdown
    return polished if _looks_safe_to_use(markdown, polished) else markdown


# --- public API -----------------------------------------------------------


async def compose_dispatch_package(
    decisions: Sequence[GateDecision],
    case: CaseInfo,
    ground_content: Mapping[str, GroundContent],
    *,
    polisher: ModelClient | None = None,
) -> DispatchPackage:
    """Deterministically compose the submission and the refusals explainer.

    Args:
        decisions: Every candidate ground's gate decision for this case.
        case: Case-level facts for the submission header.
        ground_content: Narrative content for each *shipped* ground, keyed
            by `ground_id`. Must contain an entry for every ground in
            `decisions` with `status == GateStatus.SHIPPED`.
        polisher: An optional model client used to polish each finished
            document's prose after deterministic assembly. Composition
            itself — which grounds appear, their citations and facts — is
            never influenced by the model; a polish that alters headings or
            fails outright is discarded in favour of the deterministic text.

    Returns:
        The two composed documents, each rendered as Markdown and HTML.

    Raises:
        ValueError: A shipped ground in `decisions` has no matching entry
            in `ground_content`.
    """
    shipped = _shipped_grounds(decisions)
    unshipped = _unshipped_grounds(decisions)

    submission_markdown = _submission_markdown(case, shipped, ground_content)
    submission_html = _submission_html(case, shipped, ground_content)
    explainer_markdown = _refusals_explainer_markdown(unshipped, ground_content)
    explainer_html = _refusals_explainer_html(unshipped, ground_content)

    submission_markdown = await _polish(
        submission_markdown, "council objection submission", polisher
    )
    explainer_markdown = await _polish(explainer_markdown, "refusals explainer", polisher)

    return DispatchPackage(
        submission=ComposedDocument(markdown=submission_markdown, html=submission_html),
        refusals_explainer=ComposedDocument(markdown=explainer_markdown, html=explainer_html),
    )
