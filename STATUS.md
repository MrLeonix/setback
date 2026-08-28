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

## Deploy (this checkpoint)

`Dockerfile` and `deploy.sh` (repo root) now exist and have been executed
live against `vexcourt-agent`, `us-central1`. One image, two Cloud Run
deployables, per `docs/ARCHITECTURE.md` §1/§5:

- **Image**: `python:3.12-slim`, dependencies installed via `uv sync
  --frozen` against the committed `uv.lock` (never a fresh resolve), runs
  as a fixed-uid non-root user. Built and pushed via Cloud Build to a new
  Artifact Registry repo `setback` (`us-central1-docker.pkg.dev/
  vexcourt-agent/setback/setback`) with a `keep-last-3` cleanup policy
  applied.
- **`setback-console`** (Cloud Run Service): `min-instances=0`,
  `max-instances=3`, `--cpu-throttling` (request-based billing),
  `sa-console` identity, `--allow-unauthenticated` (no auth system exists
  per the MVP cut list — the docket board is meant to be publicly
  reachable), `SETBACK_GCP_PROJECT=vexcourt-agent`. No Secret Manager
  reference — the Maps secret is read only by the tribunal pipeline
  (`evidence/imagery.py`), never the console, so `--clear-secrets` is
  passed explicitly on every deploy to guarantee that stays true. Live at
  `https://setback-console-956646636969.us-central1.run.app` (revision
  `setback-console-00003-qj6`), verified `curl` returns HTTP 200 with the
  real server-rendered docket-board HTML (`<title>Setback -- Docket
  Board</title>`, 508 bytes).
- **`setback-tribunal`** (Cloud Run Job): 1 vCPU / 2GiB, `--task-timeout
  1800s`, `--max-retries=1`, `sa-orchestrator` identity, `MAPS_API_KEY`
  wired by `--set-secrets` reference to the existing `maps-api-key`
  secret (never inlined, never printed). Command overridden to `python -m
  setback.job.main` on the same image the console uses.
- **IAM, least privilege, resource-scoped (not project-level)**:
  `sa-console` was granted `roles/run.invoker` on the `setback-tribunal`
  job *only* (verified via `gcloud run jobs get-iam-policy`); `sa-
  orchestrator` was granted `roles/secretmanager.secretAccessor` on the
  `maps-api-key` secret *only* (verified via `gcloud secrets get-iam-
  policy`). Neither grant is project-wide; neither SA was touched beyond
  these two resource-scoped bindings.
  **Pre-existing, not touched this checkpoint**: `sa-console` already
  carried a project-level `roles/run.jobsExecutorWithOverrides` binding
  before this work package ran (set when the SA was originally
  provisioned, outside this repo's code). That role is broader than the
  resource-scoped `run.invoker` this checkpoint added and would be worth
  narrowing in a future pass — flagged here rather than silently left
  for a judge to find, but left untouched since removing a pre-existing
  project-level IAM binding wasn't in this work package's scope and
  wasn't requested.

**Job-execution wiring proof (live, this checkpoint)**: seeded one
fixture case directly against the real `vexcourt-agent` Firestore via
`FirestoreCaseStore.create_case` (`application_number=PAN-661190`,
mirroring the demo DA), then ran `gcloud run jobs execute setback-
tribunal --update-env-vars=CASE_ID=<id> --wait`. The execution completed
(container exited, Cloud Run recorded a terminal execution state) and
Firestore shows exactly the two events `run_job`'s contract promises:
`case_created` then `job_failed` with
`error="the review pipeline (court/gate/dispatch) is not yet wired into
the job"`. This is `job/main.py`'s existing, pre-this-checkpoint
`_RealPipelineRunner` stub (see its docstring: "Deliberately not
implemented yet") firing exactly as designed — the job package was never
updated to call `court.graph`/`gate.validator`/`dispatch.composer` after
those packages landed this wave, which is also what STATUS.md's own
"What remains: Smoke test" bullet already flagged as outstanding. The
deploy work package's job here was the *wiring* (image, env vars, ADC,
Firestore access, IAM, controlled non-crashing failure path) — all of
which is now verified live end-to-end — not wiring the pipeline itself,
which is `job/main.py`'s lane, not `deploy.sh`'s. **Flagging prominently
rather than quietly reporting success**: a full live rehearsal of
`setback-tribunal` will still fail until a future checkpoint replaces
`_RealPipelineRunner` with a real one.

**Live cost**: one Cloud Run Job execution (~90s of 1 vCPU/2GiB compute)
against one seeded Firestore case. Zero model calls were made — the
pipeline stub raises before any `ModelClient` call, confirmed by reading
back `cases/{case_id}/ledger` (0 entries) after the run. Cost is Cloud
Run Job compute seconds only, on the order of a fraction of a cent, well
inside the "~one case, cents" live budget for this work package.

**Security note**: no secret values were read, printed, or embedded
anywhere in `Dockerfile`/`deploy.sh`/this checkpoint's commands — the
Maps secret is referenced by name (`maps-api-key:latest`) only, ADC
handles all GCP auth, and no personal identifier was sent to any external
service (all outbound calls in this checkpoint were `gcloud`/Cloud Build/
Firestore SDK calls against the project's own APIs, and one plain `curl`
to the deployed service's own `*.run.app` URL with a neutral
`setback/0.1.0` User-Agent).

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

- **Deploy** — done this checkpoint: see "Deploy (this checkpoint)" above.
  `make deploy` itself is still the stub printed by the Makefile (the
  Makefile is out of this work package's lane) — use `./deploy.sh`
  directly.
- **Smoke test** — an end-to-end run against the frozen NSW fixtures
  (interview → ingest → court graph → gate → compose) has not yet been
  exercised as a single pipeline; each package is unit-tested in
  isolation with fakes. This checkpoint's live job execution reconfirmed
  the gap concretely: `job/main.py`'s `_RealPipelineRunner` is still an
  intentional stub (raises `NotImplementedError`) that was never updated
  to call `court`/`gate`/`dispatch` after those packages landed — wiring
  that in is a prerequisite for the smoke test, and for a `setback-
  tribunal` execution to ever reach `status=composed` for real.
- **Video assets** — no demo video or screen capture has been produced.
- **README final** — two literal `[TO INSERT: ...]` placeholders
  (architecture diagram, cloud spin-up commands, verbatim hackathon
  model-eligibility wording) noted at the wave-2 checkpoint are still
  unresolved; the architecture diagram placeholder can now point at
  `docs/ARCHITECTURE.md` §9's mermaid diagram.
- **Veo feature** — not yet scoped or built.
