"""Tests for setback.evidence.illustration: the pre-generated Veo
overshadowing-simulation clip attachment.

This is a config/data-level attachment, not a document/anchor -- these
tests exist specifically to pin the structural exclusion this feature
requires (RECOMMENDATION.md's item 2): the simulation must never become
citable, so it must never enter a CaseDossier's document store or anchor
manifest under any circumstance. See
`test_simulation_clip_identity_never_appears_in_a_built_dossier` below.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from setback.evidence.dossier import build_dossier
from setback.evidence.illustration import (
    ILLUSTRATION_COST_NOTE,
    ILLUSTRATION_LABEL,
    OVERSHADOWING_SIMULATION_CLIPS,
    SimulationClip,
    simulation_clip_for_case,
)
from setback.ingest.onlineda import DevelopmentApplicationRecord
from setback.ingest.spatial import PlanningControls, SourcedValue

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "nsw" / "docs"
ELEVATIONS_PDF = FIXTURES / "elevations.pdf"

# The two canonical film cases (SETBACK's read-only demo cases) -- see
# STATUS.md/SMOKE.md. Never modified or re-run by these tests; only their
# case ids are referenced, as plain strings, for the config-lookup test.
_FILM2_CASE_ID = "cc9bfc59084fd7cac527c479f0e71996"
_REAL_DA_CASE_ID = "aeff0460678e76feceb7a5a7af934d31"
# The founder's single-demo film case (filmed same-day as this wave) --
# same real DA/elevations as the two canonical film cases above, so the one
# clip is equally honest to attach here. See illustration.py's allowlist
# comment.
_FOUNDER_FILM_CASE_ID = "1f4b7367fd30c089173ef09d7e8383a4"


def test_illustration_label_is_the_mandatory_disclosure_text() -> None:
    assert ILLUSTRATION_LABEL == "AI-generated illustration — not evidence"


def test_illustration_cost_note_is_the_founder_approved_text() -> None:
    assert ILLUSTRATION_COST_NOTE == (
        "Pre-generated with Veo 3.1 · one-time cost US$1.60 · not part of this case's run cost"
    )


def test_known_demo_cases_are_configured() -> None:
    assert _FILM2_CASE_ID in OVERSHADOWING_SIMULATION_CLIPS
    assert _REAL_DA_CASE_ID in OVERSHADOWING_SIMULATION_CLIPS
    assert _FOUNDER_FILM_CASE_ID in OVERSHADOWING_SIMULATION_CLIPS
    # Same clip constant as the other two film cases (wave-12 instruction 1:
    # "same clip constant") -- never a second generated clip.
    assert (
        OVERSHADOWING_SIMULATION_CLIPS[_FOUNDER_FILM_CASE_ID]
        is OVERSHADOWING_SIMULATION_CLIPS[_FILM2_CASE_ID]
    )


def test_simulation_clip_for_case_returns_none_for_an_unknown_case() -> None:
    assert simulation_clip_for_case("some-other-case-id", has_overshadowing_ground=True) is None


def test_simulation_clip_for_case_returns_none_without_an_overshadowing_ground() -> None:
    # A known demo case id alone is not enough -- RECOMMENDATION.md's
    # instruction is to mirror the Street View grade-B conditional-render
    # pattern, which is also always gated on real case content, never on
    # case identity alone.
    assert simulation_clip_for_case(_FILM2_CASE_ID, has_overshadowing_ground=False) is None


def test_simulation_clip_for_case_returns_the_clip_for_a_known_case_with_the_ground() -> None:
    clip = simulation_clip_for_case(_FILM2_CASE_ID, has_overshadowing_ground=True)

    assert isinstance(clip, SimulationClip)
    assert clip.static_path.startswith("/static/")
    assert clip.static_path.endswith(".mp4")
    assert clip.caption


def test_simulation_clip_video_asset_exists_on_disk() -> None:
    for clip in OVERSHADOWING_SIMULATION_CLIPS.values():
        asset_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "setback"
            / "console"
            / "static"
            / clip.static_path.removeprefix("/static/")
        )
        assert asset_path.is_file(), f"missing static asset for {clip.clip_id}: {asset_path}"


# --- structural exclusion (RECOMMENDATION.md item 2 / wave-12 instruction 2) -----


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
        zone_name=None,
        height_limit_metres=SourcedValue(
            value=9.0,
            lep_name="Georges River LEP 2021",
            legislation_url="https://legislation.nsw.gov.au/lep#h",
        ),
        floor_space_ratio=None,
        lot_size_sqm=None,
        heritage_flags=(),
    )


def test_simulation_clip_identity_never_appears_in_a_built_dossier() -> None:
    """Pins the structural exclusion: build a real `CaseDossier` for a case
    that (per `simulation_clip_for_case`) DOES have a simulation clip
    attached, exactly the way the real tribunal pipeline builds one --
    never passing the clip through `build_dossier`'s `plan_documents`/
    `photo_documents` (there is no code path that does; the clip is a
    console-rendering-layer attachment only). Every identifying value the
    clip carries (`clip_id`, `static_path`) must then be entirely absent
    from the dossier's document store, its anchor manifest, and both
    disjoint court slices -- so the clip can never be cited, graded, or
    surfaced to grounding/adjudication by construction, not by convention.
    """
    clip = simulation_clip_for_case(_FILM2_CASE_ID, has_overshadowing_ground=True)
    assert clip is not None

    dossier = build_dossier(
        da_record=_da_record(),
        controls=_controls(),
        dcp_documents=[],
        plan_documents=[("elevations", "Elevations", ELEVATIONS_PDF.read_bytes())],
        photo_documents=[],
    )

    assert clip.clip_id not in dossier.documents
    assert all(anchor.source_doc != clip.clip_id for anchor in dossier.anchors.values())
    assert all(clip.static_path not in anchor.caption for anchor in dossier.anchors.values())
    assert all(clip.clip_id != clause.document_id for clause in dossier.clause_slice.clauses)
    all_image_anchors = dossier.evidence_slice.plans + dossier.evidence_slice.photos
    assert all(clip.static_path not in image.caption for image in all_image_anchors)
    assert all(image.anchor_id != clip.clip_id for image in all_image_anchors)
