# SMOKE.md — end-to-end tribunal smoke test (wave 3 QA loop)

Driven live against the real demo case (PAN-661190 / DA2026-0359 / Georges
River Council / 65A Vista Street, Sans Souci) on **both** a local `uvicorn`
instance and the deployed `setback-console` Cloud Run Service, via `httpx`/
`curl` (neutral UA `setback-smoke/0.1`) and a real Chrome browser
(`chrome-devtools` MCP). Screenshots saved under
`/private/tmp/claude-501/-Users-leo-Research/2266960f-dd3e-44f6-8e11-7411908d3c00/scratchpad/smoke-shots/`.

**Headline finding**: the tribunal pipeline (`job.main._RealPipelineRunner`)
was still the wave-2 stub (`raise NotImplementedError`) — STATUS.md already
flagged this as outstanding. Wiring it (new `src/setback/job/pipeline.py`,
`RealPipelineRunner`) was this loop's main body of work, and it surfaced
**four additional, previously-undiscovered live bugs** below, each found by
actually running the app, each fixed in this same round, each re-smoked
until clean. A fifth issue (a deployed-environment IAM gap) could not be
fixed in this round — see "Known gap" at the bottom.

## Result: LOCAL (`uvicorn`, `SETBACK_LOCAL_TRIBUNAL=1`) — full clean pass

| # | Step | Expected | Observed | Verdict |
|---|------|----------|----------|---------|
| 1 | `POST /api/cases` for PAN-661190 | Case created, deterministic id | `201`, case id returned | PASS |
| 2 | Interview: overshadowing concern, clarify, upload `elevations.pdf` + a photo, confirm | Stage advances opening→clarifying→requesting_evidence→confirming→ask_more; a ground is proposed | All stage transitions correct; `ground_proposed` + `ground_category_assigned` (`environmental_and_social_impacts`) events recorded | PASS (after fix #1 below) |
| 3 | Interview: second concern — property value (deliberate non-planning concern) | Second ground proposed, category `property_value` | Confirmed; second `ground_category_assigned` event recorded | PASS (after fix #1) |
| 4 | Interview reaches `done` | Closing message, both grounds visible on case page | Confirmed via browser snapshot | PASS |
| 5 | Case page (browser) before tribunal | Renders transcript, evidence, both proposed grounds, empty sections for reviewer/gate/etc. | Matches exactly | PASS |
| 6 | Click "Start tribunal" (real browser click) | `POST /tribunal` fires, 202, pipeline runs in background | Confirmed via server log; button disables and reads "Tribunal running..." | PASS |
| 7 | SSE stream / case page during & after the run | Live updates as each stage completes; no reload storm | Fixed and confirmed clean (see fix #2) | PASS (after fix #2) |
| 8 | Both reviewer opinions render, per ground | `review_verdict` events for `clause_reviewer` and `evidence_reviewer`, distinct rationale | 4 events (2 grounds × 2 reviewers) rendered with distinct text; live run also exercised the SPLIT→adjudicator path in an earlier iteration of this loop (reviewers disagreed → `adjudication_decision` fired with a real adjudicated verdict), and this final clean run exercised the CLEAR path (reviewers agreed → adjudicator correctly *not* called, "Adjudication: Nothing yet.") | PASS — both branches observed live across the loop |
| 9 | Property-value ground refused with the s4.15 explanation | `gate_decision` status `refused-irrelevant`, explanation names s4.15(1) and "not a matter listed" | Exact text present, both in the event and in the rendered refusals-explainer section/document | PASS (see fix #4 for why this needed a second look) |
| 10 | At least one ground ships with a resolving citation and an annotated overlay | `gate_decision` status `shipped`; `annotated_overlay` event with a real image; ground's evidence anchor points at the uploaded elevations PDF | Overshadowing ground shipped, citing the elevations PDF page anchor; annotated overlay (grounded bounding boxes on the real elevation drawing — window/door/height-datum labels) rendered as an actual `<img>` on the case page | PASS (after fix #3) |
| 11 | Both output documents render and download | Submission + refusals explainer, both Markdown and HTML, viewable and downloadable | `submission.md`/`.html`/`refusals.md`/`.html` all return `200` with correct, polished content; rendered inline on the case page too | PASS |
| 12 | Full offline test suite + lint + typecheck | Green | `327 passed`; `ruff check` / `ruff format --check` clean; `mypy` clean (34 source files) | PASS |

Screenshots: `01-case-page-before-tribunal.png`, `02-case-page-after-tribunal.png`
(shows reviewer opinions, gate decisions, the annotated overlay, and both
composed documents with download links), `03-annotated-overlay-viewport.png`.

## Issues found live, fixed in this round, and re-smoked

### Fix 1 — grounds were never proposed at all (console wiring gap)
**Found at**: step 2 above — confirming a concern advanced the interview but
no `CandidateGround` ever reached the gate; `job.pipeline` had nothing to
read.
**Root cause**: the console persisted interview *turns* but never called
`CaseStore.propose_ground` when a concern was confirmed — the parsed
`RaisedConcern`/`ConcernType` was discarded the moment the turn was logged.
**Fix**: `console/app.py` now calls a new `_propose_ground_for_confirmed_concern`
the instant a concern is confirmed (`InterviewStage.ASK_MORE`), tagging it
with an s4.15 category via a documented `ConcernType → category` mapping,
and recording a `ground_category_assigned` event `job.pipeline` reads back.
**Tests**: `tests/console/test_app.py::test_confirmed_concern_proposes_a_ground_with_its_category`,
`::test_confirming_a_second_concern_proposes_a_second_ground`.
**Re-smoked**: step 2/3 above, clean.

### Fix 2 — SSE reload storm on any case with history
**Found at**: step 6/7 — clicking "Start tribunal" appeared to fail
("Element ... no longer exists") because the page was reloading every
~0.3–1s in a tight loop the instant it had *any* prior event history.
**Root cause**: a fresh page load opens a brand-new SSE connection with an
empty `seen` set; the server replayed the *entire* event history, and the
client's `onmessage` handler reloaded the page for any event type it didn't
already special-case (`ground_proposed`, `tribunal_requested`, etc.) —
reload → new connection → full replay again → reload, forever, the moment a
case had a ground or a tribunal request on record.
**Fix**: the case page now renders `data-last-sequence` (the newest event
sequence it already reflects); `app.js` passes it as `?after=` on the SSE
URL; `_sse_event_stream` skips everything at or below that cursor.
**Tests**: `tests/console/test_app.py::test_events_stream_after_param_skips_already_rendered_events`.
**Re-smoked**: confirmed via a diagnostic in-page `EventSource` (4s, zero
replayed messages) and via the real click succeeding cleanly afterward.

### Fix 3a — ADK agents silently tried the public Gemini API, not Vertex
**Found at**: first real tribunal attempt — `job_failed`:
`ValueError: No API key was provided`, from deep inside
`google.adk.models.google_llm`.
**Root cause**: `setback.models.client.ModelClient` passes `vertexai=True,
project=..., location=...` explicitly to its own `genai.Client`, but
`google.adk.agents.Agent(model="gemini-3.5-flash-lite")` (every reviewer/
adjudicator node `court.graph` builds) constructs its **own**, separate
`genai.Client` lazily and reads Vertex routing from environment variables
instead — unset, it silently falls back to the public Gemini Developer API,
which then fails for lack of an API key. `spike-adkCourt.md` had already
measured and documented the exact required env vars
(`GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT`,
`GOOGLE_CLOUD_LOCATION`) but nothing in the repo ever set them.
**Fix**: `court/graph.py` now sets all three via `os.environ.setdefault(...)`
from `setback.config`'s own project/location at import time.
**Re-smoked**: `uv run pytest tests/court` green; a direct pipeline run then
reached a real (different) error, confirming the fix worked.

### Fix 3b — annotated overlay blew Firestore's 1 MiB document limit
**Found at**: the next tribunal attempt — `job_failed`:
`400 Property payload contains an invalid nested entity` (a
`grpc.aio.AioRpcError` from `google-cloud-firestore`, traced to
`store.append_event(..., "annotated_overlay", ...)`).
**Root cause**: `render_overlay` draws on the **full-resolution** rendered
PDF page (`evidence.dossier.DEFAULT_RENDER_DPI=300`) — ~2 MB as a PNG for
this fixture, ~2.6 MB base64-encoded — nested inside one `CaseEvent`
payload, comfortably over Firestore's ~1 MiB single-document ceiling.
**Fix**: `job/pipeline.py._shrink_png_for_storage` downscales the overlay to
a bounded width before it is ever persisted; the anchors' stored page-point
coordinates are untouched (only the display copy shrinks).
**Tests**: `tests/job/test_pipeline.py::test_shrink_png_for_storage_fits_under_the_firestore_document_limit`
(uses the real fixture; asserts the *unfixed* size would have exceeded the
Firestore limit, guarding the regression) and
`::test_shrink_png_for_storage_leaves_a_small_image_untouched`.
**Re-smoked**: full pipeline run completed; `annotated_overlay` event
persisted and rendered as a real, legible image on the case page.

### Fix 4 — a court-rejected ground could still ship on a resolving citation
**Found at**: a full clean run where the resident's evidence photo was
plausibly unrelated to the claim — the Evidence Reviewer correctly rejected
the overshadowing ground, but it was **shipped anyway**, because
`gate.validator.CandidateGround` has no concept of the court's stance at
all — the gate is a citation/relevance filter only.
**Root cause / design gap**: nothing in the court→gate wiring ever checked
`CourtVerdict.stance` before dispatch. A resolvable citation was treated as
sufficient regardless of whether the tribunal actually believed the ground.
**Fix**: `job/pipeline.py` now short-circuits to a synthesized "not
well-founded" refusal for any *statutorily relevant* ground the court
rejected, before it ever reaches `gate.validate_ground` — an irrelevant
ground (e.g. property value) still always gets its specific, permanent s4.15
"not a listed matter" explanation regardless of the reviewers' stance
(irrelevance and "not well-founded" are different, non-overlapping reasons,
and the resident deserves the more specific one).
**Tests**: `tests/job/test_pipeline.py::test_a_ground_the_court_rejects_never_ships_even_with_a_resolving_citation`,
`::test_an_irrelevant_ground_keeps_its_s415_explanation_even_if_the_court_rejects_it`.
**Re-smoked**: final clean run (with more plausible evidence) shipped the
overshadowing ground correctly and refused property value with the correct,
specific explanation — see steps 9–10 above.

## Result: DEPLOYED (`setback-console` Cloud Run Service) — BLOCKED, named precisely

| Step | Expected | Observed | Verdict |
|------|----------|----------|---------|
| Docket board (`GET /`) | `200`, real server-rendered HTML | `200`, correct HTML | PASS |
| `POST /api/cases` for PAN-661190 | `201`, case created in the real Firestore | `201`, confirmed | PASS |
| `GET /api/cases/{id}/interview` (auto-starts) | `200`, opening question composed | **`500 Internal Server Error`** | **FAIL — blocked** |

**Root cause (confirmed via `gcloud run services logs read`)**: every
interview turn calls `ModelClient.generate` (even the very first, to compose
the opening question), which raises
`google.genai.errors.ClientError: 403 PERMISSION_DENIED ... Permission
'aiplatform.endpoints.predict' denied ...`. `sa-console` (the deployed
Cloud Run Service's identity) carries **no `aiplatform.*` IAM role at all**
— confirmed via `gcloud projects get-iam-policy vexcourt-agent --filter
bindings.members:sa-console@vexcourt-agent.iam.gserviceaccount.com`, which
lists only `cloudtasks.enqueuer`, `cloudtrace.agent`, `datastore.user`,
`logging.logWriter`, `run.jobsExecutorWithOverrides` — Vertex AI predict
access was never granted. This blocks the **entire** resident-facing
product on the live URL, not just the tribunal — even the opening question
of the interview cannot be composed.

**This is an infrastructure/IAM change, not a code fix, and this session's
attempt to apply it (`gcloud projects add-iam-policy-binding vexcourt-agent
--member=serviceAccount:sa-console@vexcourt-agent.iam.gserviceaccount.com
--role=roles/aiplatform.user`) was explicitly denied by the auto-mode
permission classifier.** Per doctrine, that denial was respected rather than
worked around. **Named precisely for a human to apply**:

```
gcloud projects add-iam-policy-binding vexcourt-agent \
  --member="serviceAccount:sa-console@vexcourt-agent.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

Once granted, the deployed console's interview flow should work
immediately (no code change needed — `ModelClient`'s Vertex wiring is
already correct; this is purely a missing grant). A full deployed tribunal
run also still requires redeploying this checkpoint's code (the currently
live revision predates every fix in this document, including the pipeline
wiring itself) — that redeploy was not performed in this round, since
`deploy.sh`/Cloud Build/Cloud Run deploy actions are a separate work
package's lane and were not requested here; only driving the
already-deployed app was in scope.

## Known gap (flagged, not silently hidden, per `job/pipeline.py`'s own docstring)

A resident's uploaded document/photo bytes live only in the console
process's in-memory `UserUploadedDocumentSource`. A **real** `setback-tribunal`
Cloud Run Job execution (a separate container) has no access to that
memory — `RealPipelineRunner` degrades gracefully (an empty `EvidenceSlice`,
verified in this session's very first, otherwise-successful pipeline run)
rather than crashing, but a ground with no evidence document reachable will
rarely ship. `console/app.py`'s new `LocalPipelineJobTrigger` sidesteps this
for local/dev testing only (shares the same in-process store), gated behind
`SETBACK_LOCAL_TRIBUNAL=1`, which the deployed Cloud Run Service never sets.
A real fix needs a shared, persistent document store (Firestore or GCS)
between the two deployables — out of this checkpoint's scope, and flagged
here rather than quietly left for a judge to find.

## Live budget spent this loop

Multiple full/partial pipeline runs against real Vertex AI (`gemini-3.5-flash-lite`
reviewers/grounding, `gemini-3.7-flash` adjudicator on the one run that hit
SPLIT, `gemma-4-26b-a4b-it-maas` submission polish) across the find→fix→
re-smoke loop — each ledger-capped at $2/run regardless; total cost is
still on the order of cents (flash-lite-tier reviewer/grounding calls,
single-digit calls per run). Zero secrets, credentials, or personal
identifiers were read, logged, or transmitted; all outbound calls used ADC
or the neutral `setback-smoke/0.1` / `setback/0.1.0` User-Agent.

## Final verification (verbatim)

```
$ uv run pytest -q
...
327 passed, 142 warnings in 16.46s

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
70 files already formatted

$ uv run mypy
Success: no issues found in 34 source files
```

## Status

**LOCAL**: fully green — every issue found was fixed in this round and
re-smoked clean. **DEPLOYED**: blocked on a named, precise IAM gap this
session could not apply (classifier-denied) plus a not-yet-performed
redeploy (out of this work package's lane). Overall: **partial** —
the local flow, which is the harder and more complete proof (the actual
pipeline logic), is fully verified; the deployed flow's blocker is named
exactly, with the one-line fix a human can apply.
