# STATUS — integration checkpoint (2026-08-29, wave 2)

Handover for the next wave of work packages. Tree is green: **170/170 tests
pass, ruff clean (check + format), mypy strict clean.** Pushed to
`origin/main` at `587219d`.

## What exists

### Scaffold (from the initial commit, `6f13137`)
- `pyproject.toml` — Python 3.12, uv-managed, pins per INFRA
  (`google-adk==2.8.0`, `google-genai==2.20.0`), ruff + mypy strict config.
- Package skeleton under `src/setback/`: `console`, `court`, `dispatch`,
  `evidence`, `gate`, `ingest`, `interview`, `job`, `models`, `state`, plus
  `config.py`. Every module not called out below remains a documented
  `NotImplementedError` stub — intentionally left for its owning work
  package; do not treat its presence as "already started."
- `Makefile`, `.env.example`, `README.md`, `LICENSE`, `DISCLOSURE.md`.

### Wave 1 (previous checkpoint, `fdf8e54`)

**NSW fixtures pack** (`tools/fetch_fixtures.py`, `tests/fixtures/nsw/`,
`docs/data-sources.md`) — froze the three keyless NSW data sources for the
demo case; see `docs/data-sources.md` for exact request shapes and gotchas.

**Model/state core** (`src/setback/models/client.py`,
`src/setback/state/breakers.py`, `src/setback/state/ledger.py`) —
`ModelClient` (sole model call site), `CircuitBreaker`/`DegradingBreaker`
(degrade-not-halt failure tracking), `Ledger` (per-run spend ceiling,
self-aborts before booking an over-budget call). See wave 1's STATUS
history (`git show fdf8e54:STATUS.md`) for the full writeup.

### Wave 2 (this checkpoint's three incoming work packages, now merged, tested, committed)

**Ingest** (`src/setback/ingest/{onlineda,spatial,tracker}.py`):
- `fetch_development_application` — the live OnlineDA caller, sending the
  three-header filter contract documented in `docs/data-sources.md`.
- `resolve_site` — the address → propId → layerintersect → DCP chain,
  returning `PlanningControls` (zone, height, FSR, lot size, heritage
  flags), each value carrying its source LEP name and legislation.nsw.gov.au
  URL for direct citation, plus the applicable DCP document list.
- `DocumentSource` — a Protocol port implemented by `EtrackDocumentSource`
  (Georges River Council's eTrack WebForms search-postback-then-scrape flow)
  and `UserUploadedDocumentSource` (the universal resident-upload fallback,
  and the only source a council with no tracker at all needs). Any future
  council tracker vendor (e.g. ePathway) plugs in as an equally first-class
  implementation of the same port.
- All three clients retry once on a transient failure (connection error,
  timeout, or 500/502/503/504) and raise a typed, module-specific error
  otherwise. Tests replay the frozen NSW fixtures via `respx` against the
  real `httpx` transport — no live network calls anywhere in the suite.

**Gate** (`src/setback/gate/{s415,relevance,validator}.py`):
- `s415.py` — pure statutory data: the five s4.15(1) EP&A Act heads of
  consideration and five explicit non-planning grounds residents commonly
  raise (property value, private view loss, commercial competition,
  applicant personal circumstances, unanchored neighbourhood character),
  each with a plain-English explanation and citable statutory basis. Notes
  a pending (not-yet-commenced, per the module's sourcing note) legislative
  amendment to s4.15(1)(b) as a known gap for a future update.
- `relevance.py` — `classify_relevance` looks a ground's category up
  against that data; an unrecognised category is conservatively refused
  rather than defaulting to shipping.
- `validator.py` — `validate_ground`, the full deterministic pre-dispatch
  gate: irrelevant grounds refuse immediately; relevant grounds must have
  every citation resolve against a `CaseDossier` (document exists, page in
  range, bbox in page bounds, quoted control value matches the case's
  actual value). Reuses `CircuitBreaker` as-is (no new abstraction) to
  escalate a ground whose citations fail three times running from
  refusal to a `FLAGGED`-for-human-review status.
- Zero model calls anywhere in this package's decision path, by design.

**State: Firestore case store** (`src/setback/state/firestore.py`):
- `CaseStore` — a narrow structural `Protocol` — plus two implementations
  that satisfy it identically: `InMemoryCaseStore` (the fully offline test
  double used throughout the suite) and `FirestoreCaseStore` (a thin adapter
  over `google.cloud.firestore.AsyncClient`, exercised in tests only via an
  injected fake — never against a live project).
- Lifecycle validation (ground status transitions) and idempotency
  (deterministic case ids via `case_id_for`, natural-key event dedup) live
  in module-level pure helpers shared by both backends, so they're
  guaranteed to agree on behaviour and a run interrupted mid-case can
  replay its exact operation sequence and resume rather than fork or
  duplicate state.
- `resume_case` assembles a `ResumeState` (case, grounds, events, restored
  `Ledger`, restored `CircuitBreaker`s, heartbeats) from a store for a given
  case id — safe to call even when the case doesn't exist yet.
- Breaker/ledger snapshots are restored through each class's own public API
  only (never by reaching into private fields), and a restored ledger is
  re-priced from stored token counts rather than trusting a stored cost
  figure that could drift from a live pricing-table update.

## Config and ledger fixes made at this checkpoint

- **`config.GCP_PROJECT` default corrected** (`e349351`): was `setback-app`
  (never a real project), now `vexcourt-agent` — the hackathon's actual GCP
  project id; its Cloud Console *display name* is "Setback", which is what
  produced the mismatch (project ids are immutable post-creation, so the id
  itself was never renamed to match). `SETBACK_GCP_PROJECT` still overrides
  it. `tests/test_config.py` updated to match.
- **`Ledger.DEFAULT_RUN_CEILING_USD` deduplicated** (`587219d`): now imports
  `config.DEMO_RUN_BUDGET_CEILING_USD` instead of repeating `2.0` as an
  independent literal (flagged as a "second opinion" item in wave 1's
  STATUS). Confirmed no circular import risk (`config` imports neither
  `state` nor `models`), so the two ceilings can no longer silently drift
  apart.

## Test count

**170 passed**, 0 skipped, 0 xfailed.

Breakdown: `test_config.py`, `test_fetch_fixtures.py`, `models/test_client.py`,
`state/test_breakers.py`, `state/test_ledger.py` (all wave 1), plus this
checkpoint's `ingest/test_{onlineda,spatial,tracker}.py`,
`gate/test_{relevance,validator}.py`, `state/test_firestore.py`.

## Verification run (verbatim)

```
$ uv run pytest -q
........................................................................ [ 42%]
........................................................................ [ 84%]
..........................                                               [100%]
170 passed in 3.72s

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
48 files already formatted

$ uv run mypy
Success: no issues found in 31 source files
```

## Collisions resolved this checkpoint

- None between the three incoming packages: `ingest`, `gate`, and
  `state/firestore.py` touched disjoint files. `state/firestore.py` imports
  `evidence.dossier.ProvenanceGrade` (a plain enum, not the still-stubbed
  `build_dossier` function) and `models.client.TokenUsage` — both are the
  intended dependency direction, not a lane violation.
- Ran `ruff format` across the tree (not part of `make lint`, which is
  `ruff check` only, but worth keeping tidy): reformatted 5 files with
  long single-line `respx`/`await` calls introduced by this wave's tests;
  no behaviour change.
- No `xfail`s were needed — nothing required deferring.

## Known issues / things worth the next wave's attention

- `tools/fetch_fixtures.py` is still exercised by tests only through its
  parsing functions (unchanged from wave 1) — untested by design, script is
  run manually and never in CI.
- `src/setback/state/firestore.py` is a large module (~1000 lines): domain
  types, port, two full implementations, and their (de)serialization
  helpers all live together. It reads cleanly today (clearly sectioned,
  each implementation trivially diffable against the other) but is a
  candidate to split (e.g. domain types / port / in-memory / Firestore
  adapter into separate files) if it grows further before the deadline.
- `FirestoreCaseStore.append_event`'s sequence numbering counts existing
  events on every append (documented in its own inline comment as an
  accepted O(n)-per-append tradeoff at this project's per-case scale, with
  a known race window assuming one job writes to a case at a time) — fine
  for the hackathon demo, worth revisiting if `console`/multi-writer access
  lands later.
- `gate/s415.py` documents a pending (Bill passed 2025-11-11, not yet
  confirmed commenced) amendment to s4.15(1)(b) that would narrow
  "likely impacts" to "significant likely impacts" — not encoded; flagged
  in the module's own sourcing note as a gap to check before the
  hackathon's submission date if the amendment has since commenced.

## What remains (next waves)

Still a stub (`NotImplementedError`) from the original scaffold:

- **Interview flow** (`src/setback/interview/flow.py`) — resident-facing
  interview over `INTERVIEW` tier.
- **Evidence dossier** (`src/setback/evidence/dossier.py`) — `build_dossier`
  itself; `ProvenanceGrade`/`EvidenceAnchor` already exist and are consumed
  by `state/firestore.py`.
- **Court graph** (`src/setback/court/{graph,bench,roles,tally}.py`) — the
  ADK workflow wiring the two structurally-disjoint reviewers + adjudication
  bench; will consume `ModelClient`, `CircuitBreaker`/`DegradingBreaker`,
  `Ledger`, and now the `gate` package's `CandidateGround`/`CaseDossier`
  shapes and `state.firestore`'s `CaseStore` port.
- **Dispatch** (`src/setback/dispatch/composer.py`) — submission + refusal
  explainer composition; will consume `gate.validator.GateDecision`.
- **Console** (`src/setback/console/app.py`) — currently a bare `FastAPI()`
  app with no routes.
- **Job entry point** (`src/setback/job/main.py`) — Cloud Run job driving
  the end-to-end pipeline; can now wire real `ingest` calls to a real
  `FirestoreCaseStore` via `SETBACK_GCP_PROJECT=vexcourt-agent` (the
  corrected default no longer even needs the env var, though setting it
  explicitly in deploy config is still good practice).
- **Deploy** — `make deploy` is a documented stub; no Cloud Run / Terraform
  wiring yet. GCP project (`vexcourt-agent`) and service accounts exist per
  the INFRA file but are not yet referenced by any code, and secrets are
  not yet created (secretAccessor is unbound).
- **Docs** — README has two literal `[TO INSERT: ...]` placeholders
  (architecture diagram, cloud spin-up commands, verbatim hackathon
  model-eligibility wording).
- **E2E demo** — no end-to-end run has happened yet. After this checkpoint,
  ingest → gate and state persistence are wired-capable in isolation, but
  nothing yet calls them in sequence; interview and court graph are still
  needed before there's a full pipeline to point a judge at.

## Deadline context

Hackathon submission deadline: Tue 2026-09-01 10:00 AEST. Today: Sat
2026-08-29. Budget ceiling: $62 total model spend (untouched so far — every
test across both checkpoints runs offline against fixtures/fakes, zero live
model calls made).
