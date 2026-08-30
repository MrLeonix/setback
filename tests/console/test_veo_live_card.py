"""Tests for the judge-gated LIVE Veo illustration card on the case page
(wave 13, founder-authorized) -- the "generating" placeholder and the
"ready" full card, both distinct from `evidence.illustration`'s own
pre-generated static-clip card (`test_illustration_card.py`).

Unit-level against `render_case_page` directly, exactly like
`test_illustration_card.py`.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from setback.console.app import render_case_page
from setback.evidence.illustration import ILLUSTRATION_LABEL, OVERSHADOWING_SIMULATION_CLIPS
from setback.evidence.veo_live import VEO_LIVE_COST_NOTE, VEO_LIVE_GENERATING_MESSAGE
from setback.state.firestore import CaseEvent, CaseRecord

_JUDGE_CASE_ID = "1" * 32
_NON_ALLOWLISTED_ANOTHER_CASE_ID = "2" * 32


def _case(case_id: str) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        application_number="DA2026/9999",
        resident_session="session-1",
        created_at=datetime(2026, 8, 31, tzinfo=UTC),
    )


def _event(
    case_id: str, event_type: str, payload: dict[str, object], sequence: int = 0
) -> CaseEvent:
    return CaseEvent(
        event_id=f"{event_type}:{sequence}",
        case_id=case_id,
        event_type=event_type,
        payload=payload,
        sequence=sequence,
        recorded_at=datetime(2026, 8, 31, tzinfo=UTC),
    )


def _judge_case_created(case_id: str) -> CaseEvent:
    return _event(
        case_id,
        "case_created",
        {"application_number": "DA2026/9999", "public_origin": False, "judge_origin": True},
    )


def _public_case_created(case_id: str) -> CaseEvent:
    return _event(
        case_id,
        "case_created",
        {"application_number": "DA2026/9999", "public_origin": True, "judge_origin": False},
    )


def _overshadowing_ground_event(case_id: str) -> CaseEvent:
    return _event(
        case_id,
        "ground_category_assigned",
        {
            "ground_id": "ground-1",
            "category": "environmental_and_social_impacts",
            "concern_type": "overshadowing",
            "evidence_document_ids": [],
        },
        sequence=1,
    )


def _generating_event(case_id: str) -> CaseEvent:
    return _event(case_id, "illustration_generating", {}, sequence=2)


def _ready_event(case_id: str, document_id: str = "veo-live-illustration") -> CaseEvent:
    return _event(case_id, "illustration_ready", {"document_id": document_id}, sequence=3)


def _failed_event(case_id: str) -> CaseEvent:
    return _event(case_id, "illustration_failed", {"reason": "boom"}, sequence=2)


def _panel(page_html: str, tab_id: str) -> str:
    start = page_html.index(f'id="panel-{tab_id}"')
    rest = page_html[start:]
    next_panel = rest.find('<div role="tabpanel"', 1)
    return rest[:next_panel] if next_panel != -1 else rest


# --- generating placeholder ---------------------------------------------------


def test_generating_card_renders_for_a_judge_origin_case_with_no_ready_event() -> None:
    page = render_case_page(
        _case(_JUDGE_CASE_ID),
        [],
        [_judge_case_created(_JUDGE_CASE_ID), _generating_event(_JUDGE_CASE_ID)],
    )

    assert VEO_LIVE_GENERATING_MESSAGE in page
    assert "<video" not in _panel(page, "evidence")


def test_generating_card_appears_in_the_evidence_panel() -> None:
    page = render_case_page(
        _case(_JUDGE_CASE_ID),
        [],
        [_judge_case_created(_JUDGE_CASE_ID), _generating_event(_JUDGE_CASE_ID)],
    )

    assert VEO_LIVE_GENERATING_MESSAGE in _panel(page, "evidence")


def test_generating_card_absent_without_a_generating_event() -> None:
    page = render_case_page(_case(_JUDGE_CASE_ID), [], [_judge_case_created(_JUDGE_CASE_ID)])

    assert VEO_LIVE_GENERATING_MESSAGE not in page


def test_generating_card_absent_for_a_public_origin_case_even_with_a_generating_event() -> None:
    """Defense in depth: even if a generating event somehow existed on a
    public-origin case (should never happen -- the job-side gate requires
    judge_origin before it ever emits one), the console must not render the
    live card for it."""
    page = render_case_page(
        _case(_JUDGE_CASE_ID),
        [],
        [_public_case_created(_JUDGE_CASE_ID), _generating_event(_JUDGE_CASE_ID)],
    )

    assert VEO_LIVE_GENERATING_MESSAGE not in page


# --- ready (full) card ---------------------------------------------------------


def test_ready_card_renders_the_video_pointing_at_the_document_route() -> None:
    page = render_case_page(
        _case(_JUDGE_CASE_ID),
        [],
        [
            _judge_case_created(_JUDGE_CASE_ID),
            _ready_event(_JUDGE_CASE_ID, "veo-live-illustration"),
        ],
    )

    assert "<video" in page
    assert f"/api/cases/{_JUDGE_CASE_ID}/documents/veo-live-illustration" in page


def test_ready_card_carries_the_mandatory_caption() -> None:
    page = render_case_page(
        _case(_JUDGE_CASE_ID),
        [],
        [_judge_case_created(_JUDGE_CASE_ID), _ready_event(_JUDGE_CASE_ID)],
    )

    assert ILLUSTRATION_LABEL in page


def test_ready_card_carries_the_live_cost_line_not_the_pregenerated_one() -> None:
    page = render_case_page(
        _case(_JUDGE_CASE_ID),
        [],
        [_judge_case_created(_JUDGE_CASE_ID), _ready_event(_JUDGE_CASE_ID)],
    )

    assert html.escape(VEO_LIVE_COST_NOTE) in page


def test_ready_card_replaces_the_generating_placeholder() -> None:
    page = render_case_page(
        _case(_JUDGE_CASE_ID),
        [],
        [
            _judge_case_created(_JUDGE_CASE_ID),
            _generating_event(_JUDGE_CASE_ID),
            _ready_event(_JUDGE_CASE_ID),
        ],
    )

    assert VEO_LIVE_GENERATING_MESSAGE not in page
    assert "<video" in page


def test_no_card_at_all_after_a_failure_with_no_ready_event() -> None:
    page = render_case_page(
        _case(_JUDGE_CASE_ID),
        [],
        [_judge_case_created(_JUDGE_CASE_ID), _failed_event(_JUDGE_CASE_ID)],
    )

    assert VEO_LIVE_GENERATING_MESSAGE not in page
    assert "<video" not in _panel(page, "evidence")


def test_ready_card_absent_for_a_public_origin_case_even_with_a_ready_event() -> None:
    page = render_case_page(
        _case(_JUDGE_CASE_ID),
        [],
        [_public_case_created(_JUDGE_CASE_ID), _ready_event(_JUDGE_CASE_ID)],
    )

    assert "<video" not in _panel(page, "evidence")


# --- the allowlisted static-clip demo cases stay byte-identical --------------


def test_live_card_never_renders_on_an_allowlisted_demo_case() -> None:
    """The pre-generated static-clip card (`evidence.illustration`) must be
    the only illustration card ever shown on one of the three canonical
    demo cases, even for a judge_origin session with a ready live-event
    somehow present -- the render layer's own mirror of `job.pipeline.
    _is_veo_live_excluded`."""
    allowlisted_case_id = next(iter(OVERSHADOWING_SIMULATION_CLIPS))
    clip = OVERSHADOWING_SIMULATION_CLIPS[allowlisted_case_id]

    page = render_case_page(
        _case(allowlisted_case_id),
        [],
        [
            _judge_case_created(allowlisted_case_id),
            _overshadowing_ground_event(allowlisted_case_id),
            _ready_event(allowlisted_case_id, "some-other-live-document-id"),
        ],
    )

    assert clip.static_path in page
    assert "some-other-live-document-id" not in page
    assert VEO_LIVE_GENERATING_MESSAGE not in page
