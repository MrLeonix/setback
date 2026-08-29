"""Tests for setback.interview.flow: the Collaborative Partner interview
state machine and durable refusal-feedback capture.

No live model calls anywhere in this module (0 budget for this work
package) -- `_FakeComposer` stands in for the one flash-lite call the real
`ModelQuestionComposer` would make, so every assertion here is about what
the STATE MACHINE decides to ask/do, not about model output quality.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from setback.clerk import NormalisedConcern
from setback.interview.flow import (
    ConcernType,
    InterviewFlow,
    InterviewStage,
    InterviewTurn,
    KeywordConcernNormaliser,
    ModelConcernNormaliser,
    RaisedConcern,
    ResidentFeedback,
    capture_refusal_feedback,
    classify_concern,
)
from setback.models.client import ModelClient, RetryPolicy, _maas_base_url
from setback.state.firestore import EventType, InMemoryCaseStore


class _FakeComposer:
    """Records every instruction it was asked to compose and returns a
    deterministic, inspectable message instead of calling a model."""

    def __init__(self) -> None:
        self.instructions: list[str] = []

    async def compose(self, *, instruction: str, context: object = ()) -> str:
        self.instructions.append(instruction)
        return f"COMPOSED: {instruction}"


# --- classify_concern (pure, deterministic) ----------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The new building is way too tall and blocks the sky", ConcernType.HEIGHT_BULK),
        (
            "I'm worried they'll be able to see straight into my bedroom",
            ConcernType.PRIVACY_OVERLOOKING,
        ),
        ("It's going to shade my whole backyard all winter", ConcernType.OVERSHADOWING),
        ("They want to remove the big fig tree on the boundary", ConcernType.TREES_LANDSCAPE),
        (
            "There's already nowhere to park and this adds six more cars",
            ConcernType.TRAFFIC_PARKING,
        ),
        ("This heritage streetscape will be ruined", ConcernType.HERITAGE_CHARACTER),
        ("I'll lose my harbour view completely", ConcernType.VIEW_LOSS),
        ("My property value will definitely drop", ConcernType.PROPERTY_VALUE),
        ("The construction noise is going to be unbearable", ConcernType.NOISE),
        ("I just have a bad feeling about this", ConcernType.OTHER),
    ],
)
def test_classify_concern(text: str, expected: ConcernType) -> None:
    assert classify_concern(text) == expected


# --- InterviewFlow: the full happy-path state machine ------------------------


async def test_start_opens_with_what_worries_you() -> None:
    flow = InterviewFlow(composer=_FakeComposer())
    turn = await flow.start()
    assert flow.stage is InterviewStage.OPENING
    assert "worr" in turn.prompt.lower() or "concern" in turn.prompt.lower()


async def test_full_happy_path_records_one_confirmed_concern() -> None:
    composer = _FakeComposer()
    flow = InterviewFlow(composer=composer)
    await flow.start()

    turn = await flow.submit("The new second storey will overshadow my entire garden.")
    assert flow.stage is InterviewStage.CLARIFYING
    assert turn.stage is InterviewStage.CLARIFYING

    turn = await flow.submit("It'll lose sun from about 11am to 3pm in winter.")
    assert flow.stage is InterviewStage.REQUESTING_EVIDENCE

    turn = await flow.record_evidence_upload("photo-1")
    assert flow.stage is InterviewStage.CONFIRMING
    assert turn.stage is InterviewStage.CONFIRMING

    turn = await flow.submit("Yes, that's right.")
    assert flow.stage is InterviewStage.ASK_MORE
    assert len(flow.concerns) == 1
    concern = flow.concerns[0]
    assert concern.concern_type is ConcernType.OVERSHADOWING
    assert concern.confirmed is True
    assert concern.evidence_document_ids == ("photo-1",)

    turn = await flow.submit("No, that's everything.")
    assert flow.stage is InterviewStage.DONE
    assert turn.stage is InterviewStage.DONE


async def test_declining_evidence_upload_skips_straight_to_confirming() -> None:
    flow = InterviewFlow(composer=_FakeComposer())
    await flow.start()
    await flow.submit("Too much traffic and nowhere to park already.")
    await flow.submit("It's bumper to bumper by 8am most weekdays.")
    assert flow.stage is InterviewStage.REQUESTING_EVIDENCE

    turn = await flow.submit("No, I don't have any photos.")
    assert flow.stage is InterviewStage.CONFIRMING
    assert turn.stage is InterviewStage.CONFIRMING


async def test_disputing_confirmation_loops_back_to_clarifying_with_feedback() -> None:
    flow = InterviewFlow(composer=_FakeComposer())
    await flow.start()
    await flow.submit("The construction noise is going to be unbearable.")
    await flow.submit("Jackhammering starts at 6am most days.")
    await flow.submit("no photos")
    assert flow.stage is InterviewStage.CONFIRMING

    turn = await flow.submit(
        "No, that's not quite it -- it's actually about the hours, not the noise itself."
    )
    assert flow.stage is InterviewStage.CLARIFYING
    assert turn.stage is InterviewStage.CLARIFYING
    # the dispute is retained as durable context for the next clarifying question
    assert flow.concerns == []
    assert flow._current is not None  # noqa: SLF001 -- internal check that state wasn't lost
    assert flow._current.disputed_confirmations == (
        "No, that's not quite it -- it's actually about the hours, not the noise itself.",
    )


async def test_ask_more_yes_starts_a_second_concern() -> None:
    flow = InterviewFlow(composer=_FakeComposer())
    await flow.start()
    await flow.submit("My harbour view will be gone.")
    await flow.submit("I can see the bridge from my kitchen right now.")
    await flow.submit("none")
    await flow.submit("yes that's correct")
    assert flow.stage is InterviewStage.ASK_MORE

    turn = await flow.submit("Yes, one more thing.")
    assert flow.stage is InterviewStage.OPENING
    assert turn.stage is InterviewStage.OPENING
    assert len(flow.concerns) == 1


async def test_transcript_accumulates_every_turn() -> None:
    flow = InterviewFlow(composer=_FakeComposer())
    await flow.start()
    await flow.submit("It'll block my view.")
    assert len(flow.transcript) >= 2  # opening turn + clarifying turn


async def test_record_evidence_upload_before_requesting_stage_is_a_noop_append() -> None:
    """Uploading a document while still at OPENING (e.g. resident jumps ahead)
    is recorded against the not-yet-created concern gracefully rather than
    crashing -- it's just queued as general context, no stage transition."""
    flow = InterviewFlow(composer=_FakeComposer())
    await flow.start()
    turn = await flow.record_evidence_upload("early-upload")
    assert flow.stage is InterviewStage.OPENING
    assert turn.stage is InterviewStage.OPENING


# --- redacted_text: persisted, PII-stripped, default normaliser -------------


async def test_confirmed_concern_carries_redacted_text_with_pii_stripped() -> None:
    flow = InterviewFlow(composer=_FakeComposer())
    await flow.start()
    await flow.submit("My name is Jane Smith, email jane@example.com -- this is way too noisy.")
    await flow.submit("Jackhammering starts at 6am, call me on 0412 345 678 to discuss.")
    await flow.submit("no photos")
    await flow.submit("yes that's right")

    assert len(flow.concerns) == 1
    concern = flow.concerns[0]
    assert "Jane Smith" not in concern.redacted_text
    assert "jane@example.com" not in concern.redacted_text
    assert "0412 345 678" not in concern.redacted_text
    assert "[NAME]" in concern.redacted_text
    assert "[EMAIL]" in concern.redacted_text
    assert "[PHONE]" in concern.redacted_text
    # the raw fields are untouched -- redaction is additive, not destructive
    assert "Jane Smith" in concern.initial_statement


async def test_default_normaliser_is_the_offline_keyword_test_double() -> None:
    flow = InterviewFlow(composer=_FakeComposer())
    assert isinstance(flow._concern_normaliser, KeywordConcernNormaliser)  # noqa: SLF001


# --- ConcernNormaliser wiring: a model-backed normaliser drives category ----


class _FakeConcernNormaliser:
    """A `ConcernNormaliser` test double that returns a canned category,
    proving the state machine actually consults the injected normaliser
    rather than always falling back to `classify_concern`."""

    def __init__(self, concerns: list[NormalisedConcern]) -> None:
        self._concerns = concerns
        self.calls: list[str] = []

    async def normalise(self, text: str) -> list[NormalisedConcern]:
        self.calls.append(text)
        return self._concerns


async def test_injected_normaliser_overrides_the_keyword_classification() -> None:
    # Keyword classification would call this OTHER (no recognised keyword);
    # the injected normaliser insists it's actually about heritage.
    normaliser = _FakeConcernNormaliser(
        [
            NormalisedConcern(
                category=ConcernType.HERITAGE_CHARACTER,
                target="the front facade",
                qualifiers=[],
                redacted_text="It just doesn't feel right for this street.",
            )
        ]
    )
    flow = InterviewFlow(composer=_FakeComposer(), concern_normaliser=normaliser)

    await flow.start()
    turn = await flow.submit("It just doesn't feel right for this street.")

    assert normaliser.calls == ["It just doesn't feel right for this street."]
    assert flow._current is not None  # noqa: SLF001
    assert flow._current.concern_type is ConcernType.HERITAGE_CHARACTER  # noqa: SLF001
    assert flow._current.redacted_text == "It just doesn't feel right for this street."  # noqa: SLF001
    assert turn.stage is InterviewStage.CLARIFYING


async def test_model_concern_normaliser_wires_the_real_clerk_call_offline() -> None:
    """End-to-end wiring proof, fully offline (respx, no live model): a
    `ModelConcernNormaliser` backed by a real `ModelClient` drives both the
    concern's category and its redacted text."""
    url = _maas_base_url("test-project", "global") + "/chat/completions"
    with respx.mock:
        respx.post(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"concerns": [{"category": "overshadowing", '
                                    '"target": null, "qualifiers": [], '
                                    '"redacted_text": "It overshadows my garden."}]}'
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        )
        client = ModelClient(
            project="test-project",
            location="global",
            token_provider=lambda: "fake-token",
            retry_policy=RetryPolicy(max_attempts=1),
        )
        flow = InterviewFlow(
            composer=_FakeComposer(), concern_normaliser=ModelConcernNormaliser(client)
        )
        await flow.start()
        await flow.submit("It overshadows my garden, my name is Jane Smith by the way.")

    assert flow._current is not None  # noqa: SLF001
    assert flow._current.concern_type is ConcernType.OVERSHADOWING  # noqa: SLF001
    assert flow._current.redacted_text == "It overshadows my garden."  # noqa: SLF001


# --- capture_refusal_feedback: durable pushback capture ----------------------


async def test_capture_refusal_feedback_persists_a_durable_event() -> None:
    store = InMemoryCaseStore()
    composer = _FakeComposer()
    case = await store.create_case(application_number="PAN-1", resident_session="s1")

    feedback = await capture_refusal_feedback(
        store=store,
        composer=composer,
        case_id=case.case_id,
        ground_id="ground-1",
        original_explanation="Property value is not a s4.15(1) matter.",
        pushback="But my house is worth $200k less because of this!",
    )

    assert isinstance(feedback, ResidentFeedback)
    assert feedback.ground_id == "ground-1"
    assert feedback.re_rendered_explanation.startswith("COMPOSED:")
    assert (
        "pushback" not in composer.instructions[0]
    )  # sanity: real instruction text, not a repr dump

    events = await store.list_events(case.case_id)
    feedback_events = [e for e in events if e.event_type == "resident_refusal_feedback"]
    assert len(feedback_events) == 1
    payload = feedback_events[0].payload
    assert payload["ground_id"] == "ground-1"
    assert payload["pushback"] == "But my house is worth $200k less because of this!"
    assert payload["re_rendered_explanation"] == feedback.re_rendered_explanation


async def test_capture_refusal_feedback_is_idempotent_on_identical_pushback() -> None:
    store = InMemoryCaseStore()
    composer = _FakeComposer()
    case = await store.create_case(application_number="PAN-2", resident_session="s2")

    await capture_refusal_feedback(
        store=store,
        composer=composer,
        case_id=case.case_id,
        ground_id="ground-1",
        original_explanation="explanation",
        pushback="same pushback text",
    )
    await capture_refusal_feedback(
        store=store,
        composer=composer,
        case_id=case.case_id,
        ground_id="ground-1",
        original_explanation="explanation",
        pushback="same pushback text",
    )

    events = await store.list_events(case.case_id)
    feedback_events = [e for e in events if e.event_type == "resident_refusal_feedback"]
    assert len(feedback_events) == 1


async def test_capture_refusal_feedback_distinct_pushbacks_both_recorded() -> None:
    store = InMemoryCaseStore()
    composer = _FakeComposer()
    case = await store.create_case(application_number="PAN-3", resident_session="s3")

    await capture_refusal_feedback(
        store=store,
        composer=composer,
        case_id=case.case_id,
        ground_id="ground-1",
        original_explanation="explanation",
        pushback="first objection",
    )
    await capture_refusal_feedback(
        store=store,
        composer=composer,
        case_id=case.case_id,
        ground_id="ground-1",
        original_explanation="explanation",
        pushback="second, different objection",
    )

    events = await store.list_events(case.case_id)
    feedback_events = [e for e in events if e.event_type == "resident_refusal_feedback"]
    assert len(feedback_events) == 2


def test_event_type_enum_unaffected_by_this_module() -> None:
    # Guards against accidentally shadowing state's own EventType constants.
    assert EventType.CASE_CREATED == "case_created"


# --- InterviewFlow.resume: rebuilding state from a persisted transcript -----
# (wave-9 fix for the documented cold-start no-resume bug: a fresh process
# with no in-memory InterviewFlow used to call `.start()` unconditionally
# and append a duplicate, differently-worded opening turn on top of what a
# case already had durably stored -- LEO-FEEDBACK-UIUX.md §2.)


def test_resume_rebuilds_transcript_and_stage_with_no_composer_call() -> None:
    composer = _FakeComposer()
    transcript = [
        InterviewTurn(stage=InterviewStage.OPENING, prompt="What worries you?"),
        InterviewTurn(stage=InterviewStage.OPENING, prompt="It overshadows my yard."),
        InterviewTurn(stage=InterviewStage.CLARIFYING, prompt="When does it lose sun?"),
    ]
    flow = InterviewFlow.resume(composer=composer, transcript=transcript)
    assert flow.transcript == transcript
    assert flow.stage is InterviewStage.CLARIFYING
    assert flow.concerns == []
    assert composer.instructions == []  # never calls the composer to rebuild


def test_resume_with_empty_transcript_starts_at_opening() -> None:
    flow = InterviewFlow.resume(composer=_FakeComposer(), transcript=[])
    assert flow.stage is InterviewStage.OPENING
    assert flow.transcript == []


def test_resume_carries_forward_confirmed_concerns_and_current_concern() -> None:
    composer = _FakeComposer()
    confirmed = RaisedConcern(
        concern_type=ConcernType.OVERSHADOWING,
        initial_statement="It overshadows my yard.",
        confirmed=True,
        redacted_text="It overshadows my yard.",
    )
    current = RaisedConcern(
        concern_type=ConcernType.NOISE,
        initial_statement="It's loud too.",
        redacted_text="It's loud too.",
    )
    flow = InterviewFlow.resume(
        composer=composer,
        transcript=[InterviewTurn(stage=InterviewStage.CLARIFYING, prompt="It's loud too.")],
        concerns=[confirmed],
        current=current,
    )
    assert flow.concerns == [confirmed]
    assert flow._current is current


async def test_resume_can_continue_submitting_answers() -> None:
    """A resumed flow, mid-CLARIFYING, must be able to advance exactly like
    a freshly-started one -- proving `_current` is a real, usable
    `RaisedConcern`, not just a placeholder that renders but crashes the
    next `submit()`."""
    composer = _FakeComposer()
    current = RaisedConcern(
        concern_type=ConcernType.OVERSHADOWING,
        initial_statement="It overshadows my yard.",
        redacted_text="It overshadows my yard.",
    )
    flow = InterviewFlow.resume(
        composer=composer,
        transcript=[
            InterviewTurn(stage=InterviewStage.CLARIFYING, prompt="When does it lose sun?"),
        ],
        current=current,
    )
    turn = await flow.submit("In winter afternoons.")
    assert turn.stage is InterviewStage.REQUESTING_EVIDENCE
