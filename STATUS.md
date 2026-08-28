# STATUS — integration checkpoint (2026-08-29, wave 3)

Handover for the final wave of work packages. Tree is green: **313/313 tests
pass, ruff clean (check + format), mypy strict clean.** Pushed to
`origin/main` at (see latest commit on `main`).

## What exists

Everything from the wave-2 checkpoint (`ingest`, `gate`, `state`, model
client, breakers, ledger — see `git show 0020e94:STATUS.md` for that
writeup), plus this checkpoint's five incoming work packages, now merged,
tested, committed:

**Evidence** (`src/setback/evidence/{dossier,grounding,imagery}.py`):
- `dossier.py` — `build_dossier` assembles a `CaseDossier` from ingest's
  `DevelopmentApplicationRecord`/`PlanningControls`/`DcpDocument` plus
  rendered PDF pages (`pypdfium2` + `Pillow`), and adapts that into the
  `gate` package's own local dossier shape via a small, tested adapter
  function (the gate defines its own `CaseDossier`/`CaseDocument`/
  `PlanningControl`, per wave 2's interface note). `ProvenanceGrade` keeps
  its enum name, module path, and string values (`A`/`B`/`C`) unchanged —
  `state/firestore.py` continues to import it with no changes needed.
- `grounding.py` — `ground_elements` implements the proven bbox-grounding
  spike: `gemini-3.5-flash-lite`, thinking `MINIMAL`, `temperature=0`,
  `response_mime_type=application/json`, 1024px-wide resized PNGs sent to
  the model, boxes mapped back from normalized 0-1000 coordinates to true
  page/photo pixels. The defensive parser accepts both `box` and `box_2d`
  keys and re-sorts `ymin`/`ymax`, `xmin`/`xmax` (measured ~5% malformed
  rate). Adjudicator-contested citations route through a second pass on
  `gemini-3.7-flash` LOW.
- `imagery.py` — Street View fallback per the spike: metadata endpoint
  first (free), then image fetch only on a hit; tags provenance grade `B`
  with visible `"archival Street View, (c) Google, <date>"` attribution.
  Solar API and Aerial View are cut, per the spike's explicit decision —
  nothing built for them.
- `tests/evidence/live_demo.py` is a manual, non-pytest-collected script
  (no `test_` prefix, mirrors `tools/fetch_fixtures.py`'s convention) that
  makes exactly one live model call within this package's budget, and
  produced the checked-in `tests/fixtures/nsw/annotated/
  elevations-page1-grounded.png` demo asset.

**Court** (`src/setback/court/{graph,bench,roles,tally}.py`):
- `roles.py` — `google.adk.agents.Agent`-based `ClauseReviewerNode`/
  `EvidenceReviewerNode`/`AdjudicatorNode`. `ClauseSlice`/`EvidenceSlice`
  are the *only* constructors of each reviewer's input and are
  structurally incapable of carrying the other's material (no field can
  hold an image part on `ClauseSlice`; no field can hold clause text on
  `EvidenceSlice`).
- `bench.py` — circuit-breaker-backed tier selection (degrade before open),
  reusing `state.breakers.CircuitBreaker`/`DegradingBreaker` as-is.
- `tally.py` — reconciles the two reviewers' `GroundFinding`s by
  `clause_ref`; flags a conflict for adjudication only when they actually
  disagree or confidence is below threshold.
- `graph.py` — wires the ADK workflow per the verified live spike exactly:
  parallel fan-out is the nested-tuple edge form
  (`(from_node, (branch_a, branch_b))` and `((branch_a, branch_b), to_node)`),
  every parallel branch has its own explicit edge into the `JoinNode`,
  and reviewer output is parsed from `event.content.parts[].text` against
  the Pydantic `output_schema` — `event.output` is never relied on (the
  spike confirmed it gets cleared).
- `tests/court/test_graph.py` asserts the join received exactly its two
  expected predecessors, and — the spike's specific regression guard
  against the wrong-but-silently-working sequential-chain construction —
  that reviewer output comes from two distinct model events.
- `tests/test_slice_disjointness.py` is the judge-checkable proof: for
  every fixture case, serializing a `ClauseSlice` to genai `Content` parts
  never produces an `inline_data`/`file_data` part, and serializing an
  `EvidenceSlice` never produces a text part matching a clause-number
  regex.

**Dispatch** (`src/setback/dispatch/composer.py`):
- `CouncilSubmissionAdapter` and `PlainEnglishRefusalAdapter` both consume
  the same gated `Ground`/`Evidence` objects sourced from
  `gate.validator.GateDecision`/`GateStatus`, with no duplicated logic.
  Legal content is templated from `gate.s415`'s reference lists, not
  generated; `gemma-4-26b-a4b-it-maas` polishing (via `ModelClient`) is
  confined to resident-facing prose only. Golden fixtures
  (`tests/dispatch/golden/*.{md,html}`) pin the exact output shape.

**Interview** (`src/setback/interview/flow.py`):
- Resident-facing interview state machine over the `INTERVIEW` model tier:
  collects address/council/DA context and supports an early real-DA
  confirmation lookup before a full tribunal run is triggered.

**Console** (`src/setback/console/app.py` + `static/{app.js,style.css}`):
- FastAPI routes over `interview.flow` and `state.firestore`'s `CaseStore`
  port; resident photo/document upload via
  `ingest.tracker.UserUploadedDocumentSource`; an SSE endpoint for case
  status events. A minimal static chat UI is served alongside.

**Job** (`src/setback/job/main.py`):
- Cloud Run Job entry point: reads case state via
  `state.firestore.resume_case`, skips already-completed stages, and
  drives the court graph to completion or to a budget/breaker-forced
  conservative default.

## Dependencies added this checkpoint

- `pypdfium2>=4.30,<5` and `pillow>=11.0,<12` (`pyproject.toml`, `uv.lock`)
  — needed by `evidence/dossier.py` (PDF page rasterization) and
  `evidence/grounding.py` (image resize + overlay annotation). Reported by
  the wave-3 evidence builder; added by this integration checkpoint.

## Docs moved into the repo this checkpoint

- `docs/ARCHITECTURE.md` and `docs/DESIGN-DECISIONS.md` — component map,
  ADK court graph, Firestore schema, failure handling, credential model,
  the deterministic s4.15 gate spec, and the design-decision log with
  alternatives-considered, now shipped alongside `docs/data-sources.md`
  for judges to read directly rather than living only in scratch notes.
  No relative links needed adjusting — both docs reference each other by
  plain filename, not markdown links.

## Test count

**313 passed**, 0 skipped, 0 xfailed (up from 170 at the wave-2 checkpoint).

New this checkpoint: `evidence/test_{dossier,grounding,imagery}.py`,
`court/test_{graph,bench,roles,tally}.py`, `test_slice_disjointness.py`,
`dispatch/test_composer.py`, `interview/test_flow.py`,
`console/test_app.py`, `job/test_main.py`.

## Verification run (verbatim)

```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 69%]
........................................................................ [ 92%]
.........................                                                [100%]
313 passed, 2 warnings in 11.54s

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
68 files already formatted

$ uv run mypy
Success: no issues found in 33 source files
```

(The two warnings are a `starlette`/`httpx` deprecation notice from
FastAPI's `TestClient` and an ADK `BaseAgentConfig` deprecation notice —
both from third-party libraries, neither actionable from this repo's code.)

## Security diff check (this checkpoint)

Grepped the staged diff before every commit for the user's email localpart,
common credential patterns (`AIza...`, `ghp_...`, `AKIA...`,
`api_key=...`, `BEGIN ... PRIVATE KEY`, etc.), and internal hostnames
(`kratos`, `mimir`, home-directory paths). Zero hits — the only matches
were `@pytest.fixture`/`@pytest.mark.asyncio`/`@respx.mock` decorators,
which the email regex flags as false positives. Nothing was redacted or
withheld from any commit.

## Collisions resolved this checkpoint

- None between the five incoming packages: `evidence`, `court`,
  `dispatch`, `interview`, `console`, and `job` each touched disjoint
  files. Cross-package imports (`court.roles` → `evidence.dossier.
  ProvenanceGrade`; `evidence.dossier` → `gate.validator`/`gate.s415`/
  `ingest.onlineda`/`ingest.spatial`; `dispatch.composer` → `gate.s415`/
  `gate.validator`/`models.client`; `console.app` → `ingest.tracker`/
  `interview.flow`/`state.firestore`; `job.main` → `state.firestore`) all
  match the dependency direction in `docs/ARCHITECTURE.md` — no package
  reached into another's private internals.
- No `xfail`s were needed — nothing required deferring.

## Known issues / things worth attention before submission

- Carried over from wave 2, still true: `src/setback/state/firestore.py`
  is a large module (~1000 lines) — still a candidate to split if it grows
  further.
- Carried over from wave 2, still true: `gate/s415.py` documents a pending
  (Bill passed 2025-11-11, not yet confirmed commenced) amendment to
  s4.15(1)(b) — not encoded; worth checking before the submission date if
  the amendment has since commenced.
- `tests/evidence/live_demo.py` makes one real model call when run
  manually (`uv run python tests/evidence/live_demo.py`) — never collected
  by pytest, never run in CI, and well inside the package's documented
  4-call live budget. Its output PNG is already checked in, so re-running
  it is optional.
- Solar API / Aerial View remain explicitly cut per the spike — do not
  resurrect without a fresh product decision.

## What remains

- **Deploy** — `make deploy` is still a documented stub; no Cloud Run /
  Terraform wiring yet. GCP project (`vexcourt-agent`) and service
  accounts exist per the architecture doc but are not yet referenced by
  any code, and secrets are not yet created (`secretAccessor` is unbound).
- **Smoke test** — an end-to-end run against the frozen NSW fixtures
  (interview → ingest → court graph → gate → compose) has not yet been
  exercised as a single pipeline; each package is unit-tested in
  isolation with fakes.
- **Video assets** — no demo video or screen capture has been produced.
- **README final** — two literal `[TO INSERT: ...]` placeholders
  (architecture diagram, cloud spin-up commands, verbatim hackathon
  model-eligibility wording) noted at the wave-2 checkpoint are still
  unresolved; the architecture diagram placeholder can now point at
  `docs/ARCHITECTURE.md` §9's mermaid diagram.
- **Veo feature** — not yet scoped or built.
