"""Statutory data for the s4.15 relevance gate.

Encodes s4.15(1) of the *Environmental Planning and Assessment Act 1979*
(NSW) — the exhaustive statutory list of matters a consent authority "is to
take into consideration" when determining a development application — plus
an explicit list of grounds residents commonly raise that are **not** matters
under that section, each with a plain-English explanation of why.

This module is pure data: no model calls, no I/O, nothing that can fail at
runtime beyond a dict lookup. It is the ground truth the rest of the s4.15
gate (:mod:`setback.gate.relevance`, :mod:`setback.gate.validator`)
classifies candidate objection grounds against.

Sourcing
--------
The primary source, https://legislation.nsw.gov.au/view/html/inforce/current/act-1979-203,
returned ``HTTP 403 Forbidden`` (Cloudflare bot-protection challenge) on every
attempt on 2026-08-29, including via a headless-render proxy, both for the
Angular single-page HTML view and the PDF endpoint. AustLII
(austlii.edu.au / classic.austlii.edu.au) was likewise 403-blocked with a bot
challenge on the same date.

The verbatim wording below was instead sourced from a Georges River-adjacent
council's own business paper, which quotes s4.15(1) directly from the
in-force Act for its councillors:

    Bathurst Regional Council, Ordinary Council Meeting business paper,
    17 April 2024, item "9.2.1 Section 4.15 of the Environmental Planning
    and Assessment Act 1979" — accessed 2026-08-29.
    https://www.bathurst.nsw.gov.au/files/content/public/v/1/minutes-and-agendas/17-april-2024/17-april-2024/reports/9.2.1-section-4.15-of-the-environmental-planning-and-assessment-act-1979/9-2-1-section-4-15-of-the-environmental-planning-and-ass.pdf

**Pending amendment (not encoded here)**: per HWL Ebsworth, "Major reforms
confirmed for the NSW planning system" (14 November 2025), a Bill passed the
NSW Parliament on 11 November 2025 that would insert "significant" before
"likely impacts" in s4.15(1)(b) (a "proportionate and risk-based" narrowing).
As at the date this module was written the Bill was reported as awaiting
assent with no commencement date confirmed by any source this module's
author could reach. The wording below is the current, unamended text.
**If that amendment has since commenced, `ENVIRONMENTAL_AND_SOCIAL_IMPACTS`
below is stale and should be updated** — this is a known gap for the
integrator, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass

ACT_CITATION = "Environmental Planning and Assessment Act 1979 (NSW)"
"""Short title used to build statutory-basis strings."""


@dataclass(frozen=True)
class RelevanceRuling:
    """A statutory ruling on whether a ground category is a s4.15(1) matter.

    Attributes:
        category: The stable identifier a candidate ground is tagged with
            (e.g. ``"site_suitability"``, ``"property_value"``).
        relevant: True for the five s4.15(1) heads of consideration; False
            for the explicit non-planning list.
        explanation: A one-paragraph, plain-English explanation suitable for
            showing directly to the resident lodging the objection.
        statutory_basis: The citable statutory reference backing the ruling.
    """

    category: str
    relevant: bool
    explanation: str
    statutory_basis: str


# --- s4.15(1): matters a consent authority IS to consider -------------------
#
# Verbatim chapeau (Bathurst Regional Council business paper, 17 April 2024):
#   "In determining a development application, a consent authority is to
#   take into consideration such of the following matters as are of
#   relevance to the development the subject of the development
#   application:"

PLANNING_HEADS: dict[str, RelevanceRuling] = {
    "epi_dcp_provisions": RelevanceRuling(
        category="epi_dcp_provisions",
        relevant=True,
        explanation=(
            "This ground points to a specific clause of a planning instrument that applies "
            "to the site — a State Environmental Planning Policy (SEPP), the Local "
            "Environmental Plan (LEP), a Development Control Plan (DCP), or a planning "
            "agreement under s7.4 — and argues the proposal does not comply with it. "
            "Section 4.15(1)(a) requires the consent authority to weigh exactly this."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1)(a)",
    ),
    "environmental_and_social_impacts": RelevanceRuling(
        category="environmental_and_social_impacts",
        relevant=True,
        explanation=(
            "This ground describes a likely impact of the proposed development itself — on "
            "the natural environment (e.g. trees, drainage, habitat), the built environment "
            "(e.g. overshadowing, privacy, streetscape), or the social or economic character "
            "of the locality. Section 4.15(1)(b) requires the consent authority to consider "
            "exactly this: 'the likely impacts of that development, including environmental "
            "impacts on both the natural and built environments, and social and economic "
            "impacts in the locality'."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1)(b)",
    ),
    "site_suitability": RelevanceRuling(
        category="site_suitability",
        relevant=True,
        explanation=(
            "This ground argues the site itself is not suitable for the kind of development "
            "proposed — for example because of its slope, flood or bushfire risk, "
            "contamination, or access constraints. Section 4.15(1)(c) requires the consent "
            "authority to consider 'the suitability of the site for the development'."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1)(c)",
    ),
    "public_submissions": RelevanceRuling(
        category="public_submissions",
        relevant=True,
        explanation=(
            "This ground draws on matters raised in submissions made on the exhibited "
            "application. Section 4.15(1)(d) requires the consent authority to consider "
            "'any submissions made in accordance with this Act or the regulations'."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1)(d)",
    ),
    "public_interest": RelevanceRuling(
        category="public_interest",
        relevant=True,
        explanation=(
            "This ground appeals to the public interest in the proposal's determination — "
            "the broadest of the five heads, but still a genuine statutory one. Section "
            "4.15(1)(e) requires the consent authority to consider 'the public interest'."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1)(e)",
    ),
}
"""The five s4.15(1) heads of consideration, keyed by a stable category id."""


# --- Explicit non-planning grounds -------------------------------------------

NON_PLANNING_GROUNDS: dict[str, RelevanceRuling] = {
    "property_value": RelevanceRuling(
        category="property_value",
        relevant=False,
        explanation=(
            "A development's effect on nearby property values is not a matter listed in "
            "s4.15(1). NSW courts have consistently held that diminution in property value "
            "is not, by itself, a relevant planning consideration — it is a private "
            "financial consequence, not an environmental, social, or planning-instrument "
            "impact of the development. This ground cannot be submitted as an objection."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1) (not a listed matter)",
    ),
    "private_view_loss": RelevanceRuling(
        category="private_view_loss",
        relevant=False,
        explanation=(
            "Loss of a private view is not, by itself, a matter under s4.15(1) — there is no "
            "general right to retain an existing view. View loss becomes a planning matter "
            "only where it is picked up by a documented planning control that applies to the "
            "site, such as a DCP view-sharing provision or a mapped scenic-protection clause "
            "in the LEP: in that case the ground should be raised under the relevant "
            "instrument provision (s4.15(1)(a)) or as a built-environment impact "
            "(s4.15(1)(b)), citing that specific control, not raised as bare view loss."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1) (not a listed matter, absent a control hook)",
    ),
    "commercial_competition": RelevanceRuling(
        category="commercial_competition",
        relevant=False,
        explanation=(
            "Commercial competition or trade injury to an existing business is not a matter "
            "under s4.15(1). Planning law does not protect competitors from lawful competing "
            "development, and objections raised purely to protect market share have "
            "repeatedly been rejected by NSW courts as an irrelevant consideration. This "
            "ground cannot be submitted as an objection."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1) (not a listed matter)",
    ),
    "applicant_personal_circumstances": RelevanceRuling(
        category="applicant_personal_circumstances",
        relevant=False,
        explanation=(
            "The personal characteristics, motives, wealth, or conduct of the applicant "
            "(as distinct from the development itself and its impacts) are not matters "
            "under s4.15(1). Assessment is of the proposal on its planning merits, not of "
            "who is applying. This ground cannot be submitted as an objection."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1) (not a listed matter)",
    ),
    "neighbourhood_character_no_control_hook": RelevanceRuling(
        category="neighbourhood_character_no_control_hook",
        relevant=False,
        explanation=(
            "A bare assertion that a development 'doesn't suit the neighbourhood character' "
            "is not itself a s4.15(1) matter — character is only a planning consideration to "
            "the extent a planning instrument gives it content (e.g. an LEP objective, or a "
            "DCP desired-character or built-form control) for this zone. Without naming that "
            "control, this ground is an unanchored preference, not a submission under "
            "s4.15(1)(a) or (b). Cite the specific instrument clause that describes the "
            "desired character to raise this as a planning-relevant ground instead."
        ),
        statutory_basis=f"{ACT_CITATION} s4.15(1) (not a listed matter, absent a control hook)",
    ),
}
"""Grounds residents commonly raise that are not s4.15(1) matters, with why."""
