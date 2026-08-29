"""Tests for setback.clerk: the Gemma Clerk (classify_document,
normalise_concerns) and their deterministic fallbacks.

Fully offline: every Gemma MaaS call goes through `respx` against the real
httpx transport, exactly like `tests/models/test_client.py` -- a real
`ModelClient` is constructed with a fake `token_provider`, never a live
credential. No network, no ADC.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from google.genai import errors

from setback.clerk import (
    ConcernType,
    DocumentKind,
    NormalisedConcern,
    classify_concern,
    classify_document,
    normalise_concerns,
    redact_personal_information,
)
from setback.models.client import ModelClient, RetryPolicy, _maas_base_url

_NO_RETRY = RetryPolicy(max_attempts=1)
_URL = _maas_base_url("test-project", "global") + "/chat/completions"


def _client() -> ModelClient:
    return ModelClient(
        project="test-project",
        location="global",
        token_provider=lambda: "fake-token",
        retry_policy=_NO_RETRY,
    )


def _mock_success(json_content: str) -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json_content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )


def _mock_failure() -> None:
    respx.post(_URL).mock(side_effect=[httpx.Response(503, json={"error": "unavailable"})] * 5)


# --- classify_document: keyword fallback (pure, no model) --------------------


@pytest.mark.parametrize(
    ("filename", "first_page_text", "expected"),
    [
        ("elevations.pdf", "NORTH ELEVATION\nSCALE 1:100", DocumentKind.ELEVATIONS),
        ("site-plan.pdf", "SITE PLAN\nLot 4 DP232626", DocumentKind.SITE_PLAN),
        (
            "see.pdf",
            "Statement of Environmental Effects\nProposed alterations",
            DocumentKind.SEE,
        ),
        ("shadow.pdf", "Shadow Diagram - 21 June 9am", DocumentKind.SHADOW_DIAGRAM),
        ("survey.pdf", "Detail and Level Survey\nRegistered Surveyor", DocumentKind.SURVEY),
        ("basix.pdf", "BASIX Certificate No. 12345", DocumentKind.BASIX),
        ("wmp.pdf", "Waste Management Plan", DocumentKind.WASTE),
        ("random-doc.pdf", "Nothing recognisable here", DocumentKind.OTHER),
    ],
)
async def test_classify_document_falls_back_to_keyword_classifier_on_model_error(
    filename: str, first_page_text: str, expected: DocumentKind
) -> None:
    with respx.mock:
        _mock_failure()
        kind = await classify_document(filename, first_page_text, client=_client())
    assert kind is expected


@respx.mock
async def test_classify_document_uses_model_output_when_available() -> None:
    _mock_success('{"kind": "shadow_diagram"}')

    kind = await classify_document(
        "IMG_0042.pdf", "Ambiguous scanned page with no obvious keywords", client=_client()
    )

    assert kind is DocumentKind.SHADOW_DIAGRAM


# --- classify_concern (pure, deterministic -- moved here from interview.flow) -


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The new building is way too tall and blocks the sky", ConcernType.HEIGHT_BULK),
        ("It's going to shade my whole backyard all winter", ConcernType.OVERSHADOWING),
        ("I just have a bad feeling about this", ConcernType.OTHER),
    ],
)
def test_classify_concern(text: str, expected: ConcernType) -> None:
    assert classify_concern(text) == expected


# --- redact_personal_information (pure, deterministic) -----------------------


def test_redacts_email_address() -> None:
    result = redact_personal_information("Contact me at jane.smith@example.com about this.")
    assert "jane.smith@example.com" not in result
    assert "[EMAIL]" in result


def test_redacts_au_mobile_number() -> None:
    result = redact_personal_information("Call me on 0412 345 678 if you have questions.")
    assert "0412 345 678" not in result
    assert "[PHONE]" in result


def test_redacts_au_landline_number() -> None:
    result = redact_personal_information("You can reach me at (02) 9123 4567 any time.")
    assert "9123 4567" not in result
    assert "[PHONE]" in result


def test_redacts_self_introduced_name() -> None:
    result = redact_personal_information("My name is Jane Smith and I live next door.")
    assert "Jane Smith" not in result
    assert "[NAME]" in result
    assert "I live next door" in result


def test_leaves_ordinary_text_untouched() -> None:
    text = "The new second storey will overshadow my entire garden."
    assert redact_personal_information(text) == text


# --- normalise_concerns -------------------------------------------------------


async def test_normalise_concerns_falls_back_deterministically_on_model_error() -> None:
    text = "My name is Jane Smith, call me on 0412 345 678 -- the noise is unbearable."
    with respx.mock:
        _mock_failure()
        concerns = await normalise_concerns(text, client=_client())

    assert len(concerns) == 1
    concern = concerns[0]
    assert isinstance(concern, NormalisedConcern)
    assert concern.category is ConcernType.NOISE
    assert concern.target is None
    assert concern.qualifiers == []
    assert "Jane Smith" not in concern.redacted_text
    assert "0412 345 678" not in concern.redacted_text


@respx.mock
async def test_normalise_concerns_uses_model_output_when_available() -> None:
    _mock_success(
        '{"concerns": [{"category": "overshadowing", "target": "the rear yard", '
        '"qualifiers": ["winter afternoons"], '
        '"redacted_text": "The new second storey overshadows the rear yard."}]}'
    )

    concerns = await normalise_concerns(
        "The new second storey overshadows the rear yard, worst in winter afternoons.",
        client=_client(),
    )

    assert len(concerns) == 1
    concern = concerns[0]
    assert concern.category is ConcernType.OVERSHADOWING
    assert concern.target == "the rear yard"
    assert concern.qualifiers == ["winter afternoons"]
    assert concern.redacted_text == "The new second storey overshadows the rear yard."


@respx.mock
async def test_normalise_concerns_can_return_multiple_distinct_concerns() -> None:
    _mock_success(
        '{"concerns": ['
        '{"category": "noise", "target": null, "qualifiers": [], '
        '"redacted_text": "Construction noise is unbearable."}, '
        '{"category": "property_value", "target": null, "qualifiers": [], '
        '"redacted_text": "This will also drop my property value."}'
        "]}"
    )

    concerns = await normalise_concerns(
        "Construction noise is unbearable, and this will also drop my property value.",
        client=_client(),
    )

    assert [c.category for c in concerns] == [ConcernType.NOISE, ConcernType.PROPERTY_VALUE]


async def test_normalise_concerns_error_message_never_leaks_into_a_test_failure() -> None:
    # Sanity: ModelCallError is caught, not re-raised, for the fallback to fire.
    with respx.mock:
        respx.post(_URL).mock(
            side_effect=[errors.ServerError(503, {"error": {"message": "down"}})] * 5
        )
        concerns = await normalise_concerns(
            "The traffic here is already terrible.", client=_client()
        )
    assert concerns[0].category is ConcernType.TRAFFIC_PARKING
