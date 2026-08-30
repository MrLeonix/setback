"""Tests for the overshadowing-simulation `<video>` card on the case page
(RECOMMENDATION.md's minimal integration plan, wave 12).

Unit-level against `render_case_page` directly -- no `TestClient`/
`create_app` plumbing needed, and (per the wave-12 brief) the two canonical
film cases this feature actually targets are read-only, never re-run: these
tests construct a `CaseRecord` with the canonical case id directly rather
than driving a real interview through `create_app` to reach it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from setback.console.app import render_case_page
from setback.evidence.illustration import ILLUSTRATION_LABEL, OVERSHADOWING_SIMULATION_CLIPS
from setback.state.firestore import CaseEvent, CaseRecord

_FILM2_CASE_ID = "cc9bfc59084fd7cac527c479f0e71996"
_NON_DEMO_CASE_ID = "0" * 32


def _case(case_id: str) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        application_number="DA2026/0412-FILM2",
        resident_session="session-1",
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
    )


def _overshadowing_ground_category_event(case_id: str) -> CaseEvent:
    return CaseEvent(
        event_id=f"ground-category:{case_id}",
        case_id=case_id,
        event_type="ground_category_assigned",
        payload={
            "ground_id": "ground-1",
            "category": "environmental_and_social_impacts",
            "concern_type": "overshadowing",
            "evidence_document_ids": [],
        },
        sequence=0,
        recorded_at=datetime(2026, 8, 30, tzinfo=UTC),
    )


def _height_bulk_ground_category_event(case_id: str) -> CaseEvent:
    return CaseEvent(
        event_id=f"ground-category:{case_id}",
        case_id=case_id,
        event_type="ground_category_assigned",
        payload={
            "ground_id": "ground-1",
            "category": "environmental_and_social_impacts",
            "concern_type": "height_bulk",
            "evidence_document_ids": [],
        },
        sequence=0,
        recorded_at=datetime(2026, 8, 30, tzinfo=UTC),
    )


def test_simulation_card_renders_on_a_known_demo_case_with_overshadowing_ground() -> None:
    html = render_case_page(
        _case(_FILM2_CASE_ID), [], [_overshadowing_ground_category_event(_FILM2_CASE_ID)]
    )

    assert ILLUSTRATION_LABEL in html
    clip = OVERSHADOWING_SIMULATION_CLIPS[_FILM2_CASE_ID]
    assert clip.static_path in html
    assert "<video" in html


def test_simulation_card_absent_without_an_overshadowing_ground() -> None:
    html = render_case_page(
        _case(_FILM2_CASE_ID), [], [_height_bulk_ground_category_event(_FILM2_CASE_ID)]
    )

    assert ILLUSTRATION_LABEL not in html
    assert "<video" not in html


def test_simulation_card_absent_with_no_grounds_at_all() -> None:
    html = render_case_page(_case(_FILM2_CASE_ID), [], [])

    assert ILLUSTRATION_LABEL not in html
    assert "<video" not in html


def test_simulation_card_absent_on_a_non_demo_case_even_with_overshadowing_ground() -> None:
    html = render_case_page(
        _case(_NON_DEMO_CASE_ID), [], [_overshadowing_ground_category_event(_NON_DEMO_CASE_ID)]
    )

    assert ILLUSTRATION_LABEL not in html
    assert "<video" not in html


def _panel(html: str, tab_id: str) -> str:
    """The raw markup for one `_render_section_panel` tabpanel, from its own
    `id="panel-{tab_id}"` marker up to (but excluding) the next tabpanel
    div -- panels are emitted back-to-back with no separating whitespace,
    so this is the only reliable way to isolate one panel's content."""
    start = html.index(f'id="panel-{tab_id}"')
    rest = html[start:]
    next_panel = rest.find('<div role="tabpanel"', 1)
    return rest[:next_panel] if next_panel != -1 else rest


def test_simulation_card_appears_in_the_evidence_panel_not_the_overlay_panel() -> None:
    html = render_case_page(
        _case(_FILM2_CASE_ID), [], [_overshadowing_ground_category_event(_FILM2_CASE_ID)]
    )

    assert ILLUSTRATION_LABEL in _panel(html, "evidence")
    assert ILLUSTRATION_LABEL not in _panel(html, "overlay")
