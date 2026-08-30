"""The overshadowing-simulation illustration: a pre-generated Veo clip
attached, at render time only, to the two canonical film cases whose real
elevations drawing it was conditioned on.

**Founder-approved, wave 12** (`/Users/leo/Desktop/setback-hackathon/veo/
RECOMMENDATION.md`): `clip-3.mp4` cleared the brief's own per-axis stop bar
and is honest to attach to the two canonical film cases because it was
generated from the real DA's own elevations drawing.

**Config/data attachment only -- no runtime generation, no pipeline
wiring.** This module owns exactly one thing: a small, static
`case_id -> SimulationClip` lookup, gated additionally on the case actually
having raised an overshadowing concern (mirroring the existing Street View
grade-B fallback's own conditional-rendering pattern in
`console.app._render_document_uploaded_item`). It is deliberately never
imported by `evidence.dossier`, `evidence.grounding`, `job.pipeline`, or
`gate.validator` -- see the module-level structural-exclusion note below.

**Structural exclusion (never citable).** The clip must never become an
anchor, never be graded, and never be visible to grounding or adjudication.
This module achieves that by construction rather than by convention: a
:class:`SimulationClip` is not, and cannot become, an
:class:`~setback.evidence.dossier.EvidenceAnchor` or a
:class:`~setback.evidence.dossier.SourceDocument` -- there is no function
here that builds either, and no call site anywhere in this codebase passes
a `SimulationClip` (or its `static_path`) into
:func:`~setback.evidence.dossier.build_dossier`'s `plan_documents`/
`photo_documents`, the only two ways a document ever enters the anchor
manifest. `tests/evidence/test_illustration.py` pins this with a real,
built dossier rather than trusting the docstring.

**No on-demand generation this wave.** `static_path` points at a static
asset served by the console's existing `/static` mount
(`console/static/illustrations/`) -- the simplest correct storage choice
for a single pre-generated demo asset with zero per-case variation and zero
runtime cost, rather than wiring a new GCS upload path for one fixed file.
A future wave that generates a per-case clip from that case's own real
elevation would need `evidence.storage.GcsEvidenceStore` (or an equivalent
durable store) instead; see RECOMMENDATION.md's "not attempted" section.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

ILLUSTRATION_LABEL: Final[str] = "AI-generated illustration — not evidence"
"""The mandatory, non-dismissible caption (RECOMMENDATION.md item 2) shown
directly on/under the clip -- never omitted, never a dismissible toast."""

ILLUSTRATION_EXPLAINER: Final[str] = (
    "A synthetic, computer-generated preview of the overshadowing this "
    "development could cause, built from the applicant's own elevation "
    "drawing -- not a photograph, and not evidence considered by the "
    "tribunal or adjudicator."
)
"""The one-line explainer required alongside the mandatory label."""

ILLUSTRATION_COST_NOTE: Final[str] = (
    "Pre-generated with Veo 3.1 · one-time cost US$1.60 · not part of this case's run cost"
)
"""Founder-approved cost-disclosure line (wave-12 instruction 2) shown on
the Veo card. $1.60 is this specific clip's real one-time generation cost
(8s x $0.20/s, per the veo spend ledger) -- a fixed, pre-generation cost of
the one shared clip, never a per-case or per-run figure, and never wired to
`state.ledger.Ledger`/the case's own run-cost total (deliberately a
different module, a different number, and a different sentence)."""


@dataclass(frozen=True, slots=True)
class SimulationClip:
    """One pre-generated illustration clip attached to a case.

    Deliberately carries no `document_id`/anchor identity of the kind
    `evidence.dossier.SourceDocument`/`EvidenceAnchor` use -- see the
    module docstring's structural-exclusion note. `clip_id` exists only for
    this module's own config lookup and its own tests; it is never a
    dossier document id.
    """

    clip_id: str
    static_path: str
    caption: str


_OVERSHADOWING_SIMULATION_CLIP: Final[SimulationClip] = SimulationClip(
    clip_id="overshadowing-simulation-clip-3",
    static_path="/static/illustrations/overshadowing-simulation.mp4",
    caption=(
        "A simulated view of the growing shadow this development could cast over "
        "the neighbouring yard, based on the applicant's own elevation drawing."
    ),
)

OVERSHADOWING_SIMULATION_CLIPS: Final[Mapping[str, SimulationClip]] = {
    # The two canonical, read-only film cases (STATUS.md/SMOKE.md) -- both
    # are the same real DA, so the one clip (conditioned on that DA's own
    # elevations) is honest to attach to either. Never re-generate a clip
    # per case this wave (wave-12 instruction 3: no on-demand generation).
    "cc9bfc59084fd7cac527c479f0e71996": _OVERSHADOWING_SIMULATION_CLIP,  # DA2026/0412-FILM2
    "aeff0460678e76feceb7a5a7af934d31": _OVERSHADOWING_SIMULATION_CLIP,  # real-DA
    # the founder's single-demo film case
    "1f4b7367fd30c089173ef09d7e8383a4": _OVERSHADOWING_SIMULATION_CLIP,
}


def simulation_clip_for_case(
    case_id: str, *, has_overshadowing_ground: bool
) -> SimulationClip | None:
    """The :class:`SimulationClip` to attach to `case_id`, or `None`.

    Two independent conditions must both hold, mirroring the Street View
    grade-B fallback's own conditional-rendering pattern (never rendered
    from case identity alone, and never rendered just because a ground
    category exists on an arbitrary case):

    1. `case_id` is one of the pre-configured demo cases this specific
       clip was actually conditioned on (`OVERSHADOWING_SIMULATION_CLIPS`).
    2. `has_overshadowing_ground` is true -- the case actually raised an
       overshadowing concern (a resident's own `ground_category_assigned`
       event), so the card only ever appears where the ground it
       illustrates is actually in play.
    """
    if not has_overshadowing_ground:
        return None
    return OVERSHADOWING_SIMULATION_CLIPS.get(case_id)


__all__ = [
    "ILLUSTRATION_COST_NOTE",
    "ILLUSTRATION_EXPLAINER",
    "ILLUSTRATION_LABEL",
    "OVERSHADOWING_SIMULATION_CLIPS",
    "SimulationClip",
    "simulation_clip_for_case",
]
