"""The Collaborative Partner interview loop.

This is a **state machine, not free chat**: at every point the resident is
in exactly one :class:`InterviewStage`, and that stage alone decides what
question comes next. The only thing a model ever does is phrase that
decision naturally (:class:`QuestionComposer`, one ``INTERVIEW``-tier call
per turn via :class:`ModelQuestionComposer`) -- it never decides *what* to
ask, only *how* to say it. This split is what keeps the loop testable
without any live model call: every test in this package injects a fake
composer and asserts on `flow.stage` and the structured `RaisedConcern`
data, never on generated wording.

The happy path for one concern is::

    OPENING -> CLARIFYING -> REQUESTING_EVIDENCE -> CONFIRMING -> ASK_MORE

A "no" at CONFIRMING loops back to CLARIFYING with the resident's pushback
recorded as durable context (`RaisedConcern.disputed_confirmations`) rather
than discarded -- the next clarifying question is composed with that
context in view. A "yes" at ASK_MORE resets to OPENING for a second
concern; a "no" ends the interview at DONE.

Separately, :func:`capture_refusal_feedback` handles a different kind of
feedback: a resident pushing back on a *gate refusal* after the tribunal
has run. That isn't part of the elicitation state machine above (it can
happen long after the interview is DONE, triggered from the case page) --
it re-renders the refusal explanation acknowledging the pushback and
records both durably on the case via the same :class:`~setback.state.
firestore.CaseStore` port everything else in Setback persists through.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from setback.config import INTERVIEW

if TYPE_CHECKING:
    from setback.models.client import ModelClient
    from setback.state.firestore import CaseStore

# --- concern classification: deterministic, no model call --------------------


class ConcernType(StrEnum):
    """The presenting concern types the interview recognises.

    This is the interview's own light triage -- a fixed, keyword-matched
    classification used purely to pick which targeted clarifying question
    to ask next. It is deliberately *not* the s4.15(1) category a ground is
    later tagged with (that is the reviewers'/gate's job, over richer
    evidence than one opening sentence); several of these concern types
    (e.g. `PROPERTY_VALUE`, `VIEW_LOSS`) map to categories the gate refuses
    outright, and that is fine -- the interview's job is to draw the
    resident out, not to pre-judge relevance.
    """

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


# Order matters: checked top to bottom, first match wins. Overshadowing is
# checked before height/bulk since both mention height-adjacent words but
# "shade"/"sun" are the more specific signal.
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


_CLARIFYING_INSTRUCTIONS: dict[ConcernType, str] = {
    ConcernType.HEIGHT_BULK: (
        "Ask how many extra storeys or metres taller than the resident expected the new "
        "building appears, and from where they observed it."
    ),
    ConcernType.PRIVACY_OVERLOOKING: (
        "Ask which specific rooms or outdoor areas of their home would be visible from the "
        "new building, and at what distance."
    ),
    ConcernType.OVERSHADOWING: (
        "Ask which part of their property loses sun, at roughly what time of day, and in "
        "which season it is worst."
    ),
    ConcernType.TREES_LANDSCAPE: (
        "Ask which specific trees or vegetation are affected and whether they are on the "
        "resident's property, the applicant's, or a nature strip/boundary."
    ),
    ConcernType.TRAFFIC_PARKING: (
        "Ask whether the concern is about on-street parking availability, traffic volume, "
        "or vehicle access/safety, and at what times it is worst today."
    ),
    ConcernType.HERITAGE_CHARACTER: (
        "Ask whether the property or street is heritage-listed or in a conservation area, "
        "and what specifically about the streetscape they feel is affected."
    ),
    ConcernType.VIEW_LOSS: (
        "Ask what exactly is currently visible from which room or outdoor space, and "
        "whether any part of that view would remain."
    ),
    ConcernType.PROPERTY_VALUE: (
        "Ask what makes them expect a value impact, while gently noting that property "
        "value alone is unlikely to be an admissible planning ground on its own."
    ),
    ConcernType.NOISE: (
        "Ask whether the concern is about construction-phase noise or ongoing noise once "
        "the development is finished, and at what times it is worst."
    ),
    ConcernType.OTHER: (
        "Ask the resident to say a bit more about what specifically concerns them, in "
        "their own words."
    ),
}


def _affirms(text: str) -> bool:
    lowered = text.lower()
    return any(
        word in lowered for word in ("yes", "yeah", "yep", "correct", "that's right", "right")
    )


def _declines(text: str) -> bool:
    lowered = text.lower()
    if _affirms(lowered):
        return False
    return any(word in lowered for word in ("no", "none", "nope", "skip", "don't have", "nothing"))


# --- transcript and composer -------------------------------------------------


class InterviewStage(StrEnum):
    """A resident's current position in the interview state machine.

    Forward flow for one concern: OPENING -> CLARIFYING ->
    REQUESTING_EVIDENCE -> CONFIRMING -> ASK_MORE. A disputed confirmation
    loops CONFIRMING back to CLARIFYING; a "yes" at ASK_MORE loops back to
    OPENING for a new concern; a "no" there ends at DONE (terminal).
    """

    OPENING = "opening"
    CLARIFYING = "clarifying"
    REQUESTING_EVIDENCE = "requesting_evidence"
    CONFIRMING = "confirming"
    ASK_MORE = "ask_more"
    DONE = "done"


@dataclass(frozen=True)
class InterviewTurn:
    """One system-composed message shown to the resident, tagged with the
    stage it was asked in (so a caller can render/log it without having to
    re-derive the stage from the text)."""

    stage: InterviewStage
    prompt: str


@dataclass
class RaisedConcern:
    """One concern the resident has raised, accumulated across turns."""

    concern_type: ConcernType
    initial_statement: str
    clarification: str | None = None
    evidence_document_ids: tuple[str, ...] = ()
    disputed_confirmations: tuple[str, ...] = ()
    confirmed: bool = False


@dataclass(frozen=True)
class ResidentFeedback:
    """The result of capturing a resident's pushback on a gate refusal."""

    ground_id: str
    pushback: str
    re_rendered_explanation: str


class QuestionComposer(Protocol):
    """Composes the natural-language wording for one interview turn.

    The state machine decides *what* instruction to compose (deterministic
    control); this protocol is the seam for the *how* (one model call).
    """

    async def compose(self, *, instruction: str, context: Sequence[InterviewTurn]) -> str: ...


class _ComposedTurn(BaseModel):
    """Structured-output schema for the one flash-lite call per turn."""

    message: str


class ModelQuestionComposer:
    """The production :class:`QuestionComposer`: one ``INTERVIEW``-tier call
    (via :class:`~setback.models.client.ModelClient`, the sole model call
    site) per turn."""

    def __init__(self, client: ModelClient) -> None:
        self._client = client

    async def compose(self, *, instruction: str, context: Sequence[InterviewTurn]) -> str:
        transcript = "\n".join(f"[{turn.stage.value}] {turn.prompt}" for turn in context)
        prompt = (
            "You are Setback's Collaborative Partner, helping a resident write an "
            "objection to a neighbouring development application. Compose the single "
            "next thing to say to them: warm, plain English, 1-3 sentences, no legal "
            "jargon, and follow this instruction exactly without asking anything else:\n\n"
            f"{instruction}\n\n"
            f"Conversation so far:\n{transcript or '(nothing yet)'}"
        )
        result = await self._client.generate(INTERVIEW, prompt, _ComposedTurn)
        return result.output.message


# --- the state machine --------------------------------------------------------


class InterviewFlow:
    """The per-case Collaborative Partner interview state machine.

    One instance tracks one resident's interview from open to done. Every
    method that advances state is `async` because composing a turn's
    wording is (in production) a model call; the transition it produces is
    always deterministic given the current stage and the resident's answer.
    """

    def __init__(self, *, composer: QuestionComposer) -> None:
        self._composer = composer
        self.stage: InterviewStage = InterviewStage.OPENING
        self.transcript: list[InterviewTurn] = []
        self.concerns: list[RaisedConcern] = []
        self._current: RaisedConcern | None = None

    async def _ask(self, stage: InterviewStage, instruction: str) -> InterviewTurn:
        message = await self._composer.compose(instruction=instruction, context=self.transcript)
        turn = InterviewTurn(stage=stage, prompt=message)
        self.stage = stage
        self.transcript.append(turn)
        return turn

    async def start(self) -> InterviewTurn:
        """Open the interview with the what-worries-you question."""
        return await self._ask(
            InterviewStage.OPENING,
            "Ask the resident, warmly and simply, what worries them about the "
            "development application next door.",
        )

    async def submit(self, answer: str) -> InterviewTurn:
        """Advance the state machine with the resident's answer to the
        current stage's question, and return the next turn."""
        self.transcript.append(InterviewTurn(stage=self.stage, prompt=answer))
        if self.stage is InterviewStage.OPENING:
            return await self._handle_opening(answer)
        if self.stage is InterviewStage.CLARIFYING:
            return await self._handle_clarifying(answer)
        if self.stage is InterviewStage.REQUESTING_EVIDENCE:
            return await self._handle_requesting_evidence(answer)
        if self.stage is InterviewStage.CONFIRMING:
            return await self._handle_confirming(answer)
        if self.stage is InterviewStage.ASK_MORE:
            return await self._handle_ask_more(answer)
        raise RuntimeError(f"interview is already {self.stage.value}: nothing left to submit")

    async def _handle_opening(self, answer: str) -> InterviewTurn:
        concern_type = classify_concern(answer)
        self._current = RaisedConcern(concern_type=concern_type, initial_statement=answer)
        return await self._ask(InterviewStage.CLARIFYING, _CLARIFYING_INSTRUCTIONS[concern_type])

    async def _handle_clarifying(self, answer: str) -> InterviewTurn:
        assert self._current is not None
        self._current.clarification = answer
        return await self._ask(
            InterviewStage.REQUESTING_EVIDENCE,
            "Ask the resident if they have any photos, plans, or documents that show this, "
            "and to upload them if so -- reassure them it's fine if they don't.",
        )

    async def _handle_requesting_evidence(self, answer: str) -> InterviewTurn:
        assert self._current is not None
        if not _declines(answer):
            # Free-text elaboration rather than an upload -- keep it as context.
            self._current.clarification = f"{self._current.clarification}\n{answer}".strip()
        return await self._ask_confirmation()

    async def record_evidence_upload(self, document_id: str) -> InterviewTurn:
        """Record an uploaded photo/document id against the concern being
        discussed. Called by the console's upload endpoint, independent of
        `submit()`'s text answers. Safe to call at any stage: before a
        concern exists yet, it is a no-op append that doesn't advance the
        state machine (the resident jumped ahead of the flow)."""
        if self._current is None:
            return self.transcript[-1] if self.transcript else await self.start()
        self._current.evidence_document_ids = (*self._current.evidence_document_ids, document_id)
        if self.stage is InterviewStage.REQUESTING_EVIDENCE:
            return await self._ask_confirmation()
        return self.transcript[-1]

    async def _ask_confirmation(self) -> InterviewTurn:
        assert self._current is not None
        concern = self._current
        summary = (
            f"Confirm your understanding back to the resident in one or two sentences: they "
            f"raised '{concern.initial_statement}', clarified as '{concern.clarification}', "
            f"with {len(concern.evidence_document_ids)} supporting file(s) attached. Ask them "
            "to confirm this is right or correct anything you got wrong."
        )
        return await self._ask(InterviewStage.CONFIRMING, summary)

    async def _handle_confirming(self, answer: str) -> InterviewTurn:
        assert self._current is not None
        if _affirms(answer):
            self._current.confirmed = True
            self.concerns.append(self._current)
            self._current = None
            return await self._ask(
                InterviewStage.ASK_MORE,
                "Ask the resident if there's anything else about the development that "
                "concerns them.",
            )
        self._current.disputed_confirmations = (*self._current.disputed_confirmations, answer)
        instruction = _CLARIFYING_INSTRUCTIONS[self._current.concern_type]
        return await self._ask(
            InterviewStage.CLARIFYING,
            f"{instruction} The resident just said your last summary wasn't quite right "
            f"(they said: '{answer}') -- acknowledge that briefly before asking again.",
        )

    async def _handle_ask_more(self, answer: str) -> InterviewTurn:
        if _declines(answer):
            return await self._ask(
                InterviewStage.DONE,
                "Thank the resident warmly for walking through their concerns and let them "
                "know their submission is being prepared.",
            )
        return await self.start()


# --- durable refusal-feedback capture ----------------------------------------


def _refusal_feedback_event_id(ground_id: str, pushback: str) -> str:
    digest = hashlib.sha256(pushback.encode()).hexdigest()[:16]
    return f"refusal-feedback:{ground_id}:{digest}"


async def capture_refusal_feedback(
    *,
    store: CaseStore,
    composer: QuestionComposer,
    case_id: str,
    ground_id: str,
    original_explanation: str,
    pushback: str,
) -> ResidentFeedback:
    """Durably capture a resident's pushback on a gate refusal.

    Distinct from the elicitation state machine above: this fires after the
    tribunal has already gated a ground out, whenever the resident disputes
    the refusal on the case page. It re-renders the refusal explanation
    acknowledging the pushback (one model call via `composer`) and then
    persists both the pushback and the re-rendered explanation as a durable
    :class:`~setback.state.firestore.CaseEvent` -- content-hash keyed on
    `(ground_id, pushback)` so replaying an already-recorded pushback is a
    no-op, matching every other idempotent write in this system.

    The gate's ruling itself never changes here -- this only records the
    resident's preference for the record and gives them a clear,
    acknowledging restatement, per the product's transparency commitment.
    """
    message = await composer.compose(
        instruction=(
            "The resident disagrees with why one of their grounds was refused. "
            f"The original refusal explanation was: {original_explanation!r}. Their "
            f"response was: {pushback!r}. In two or three sentences: acknowledge their "
            "point respectfully, restate clearly why the ground could not be "
            "included, and let them know their disagreement has been recorded. Do "
            "not imply the decision might change."
        ),
        context=(),
    )
    feedback = ResidentFeedback(
        ground_id=ground_id, pushback=pushback, re_rendered_explanation=message
    )
    await store.append_event(
        case_id,
        _refusal_feedback_event_id(ground_id, pushback),
        "resident_refusal_feedback",
        payload={
            "ground_id": ground_id,
            "pushback": pushback,
            "original_explanation": original_explanation,
            "re_rendered_explanation": message,
        },
    )
    return feedback
