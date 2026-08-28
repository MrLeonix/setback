# STATUS — integration checkpoint (2026-08-29)

Handover for the next wave of work packages. Tree is green: **56/56 tests
pass, ruff clean, mypy strict clean.** Pushed to `origin/main` at `fdf8e54`.

## What exists

### Scaffold (from the initial commit, `6f13137`)
- `pyproject.toml` — Python 3.12, uv-managed, pins per INFRA
  (`google-adk==2.8.0`, `google-genai==2.20.0`), ruff + mypy strict config.
- Package skeleton under `src/setback/`: `console`, `court`, `dispatch`,
  `evidence`, `gate`, `ingest`, `interview`, `job`, `models`, `state`, plus
  `config.py`. Every module below is still a documented `NotImplementedError`
  stub **except** the ones this checkpoint filled in (see below) — they are
  intentionally left for their owning work packages; do not treat their
  presence as "already started."
- `config.py`: model tiers (`INTERVIEW`/`BENCH`/`CLERK`), Vertex location,
  GCP project/bucket defaults, budget ceilings, demo-case constants
  (PAN-661190, Georges River Council, 65A Vista Street).
- `Makefile`, `.env.example`, `README.md`, `LICENSE`, `DISCLOSURE.md`.

### This checkpoint's two incoming work packages (now merged, tested, committed)

**NSW fixtures pack** (`tools/fetch_fixtures.py`, `tests/fixtures/nsw/`,
`docs/data-sources.md`, `tests/test_fetch_fixtures.py`, root `conftest.py`):
- Live-fetches and freezes the three keyless NSW data sources for the demo
  case (PAN-661190 / DA2026-0359, 65A Vista Street, Sans Souci):
  OnlineDA open-data API, ePlanning spatial API (address → propId →
  layerintersect zoning layers → applicable DCPs), and Georges River
  Council's eTrack ASP.NET WebForms public document register (address →
  search postback → document list → PDF download).
  - Frozen fixtures include the two exhibited PDFs referenced by the demo
    (Statement of Environmental Effects, Elevations), each ~1.5MB, committed
    directly since both are under the 8MB large-fixture threshold.
- `docs/data-sources.md` documents each endpoint's exact request shape
  (including undocumented gotchas: OnlineDA's header-based filter contract,
  eTrack's WebForms viewstate/postback dance and gzip-on-empty-body quirk)
  and licence.
- Parsing helpers (`parse_onlineda_record`, `parse_prop_id`,
  `parse_zoning_layers`, `parse_dcp_plans`, `parse_etrack_documents`,
  `find_document`) are unit-tested fully offline against the frozen
  fixtures — the fetch script itself is never invoked in tests or CI.

**Model/state core** (`src/setback/models/client.py`,
`src/setback/state/breakers.py`, `src/setback/state/ledger.py`):
- `ModelClient` is the sole model call site: routes Gemini tiers through the
  `google-genai` Vertex SDK and the Gemma MaaS tier (matched by a `-maas`
  model-name suffix) through Vertex's OpenAI-compatible endpoint over
  `httpx`. Validates every reply into a caller-supplied Pydantic model,
  retries 429/5xx with exponential backoff + jitter, surfaces thinking
  tokens on `TokenUsage` (they bill at the output rate). All dependencies
  (genai client, httpx client, token provider, sleep) are injectable, so the
  test suite runs fully offline — a fake `genai.Client`-shaped stand-in plus
  `respx` against the real `httpx` transport, no ADC, no network.
- `CircuitBreaker` / `DegradingBreaker`: domain-agnostic, degrade-not-halt
  failure tracking per named stage (closed → open → half-open probe), with
  a primary/fallback value pair a caller picks between via `current()`.
  Knows nothing about models or prompts — any future stage can reuse it.
- `Ledger`: prices every call at Vertex list rates
  (`PRICING_USD_PER_MILLION_TOKENS`), bills thinking tokens at the output
  rate, and raises `BudgetExceededError` *before* booking a call that would
  breach the run's ceiling (the ledger's totals are left unchanged on
  rejection, so a caller can degrade and retry cheaper).

## Test count

**56 passed**, 0 skipped, 0 xfailed. (`uv run pytest -q` →
`56 passed in 2.07s` at the time of this checkpoint.)

Breakdown: `test_config.py` (pre-existing), `test_fetch_fixtures.py` (fixture
parsing), `models/test_client.py` (ModelClient), `state/test_breakers.py`,
`state/test_ledger.py`.

## Verification run (verbatim)

```
$ uv run pytest -q
........................................................                 [100%]
56 passed in 2.07s

$ uv run ruff check .
All checks passed!

$ uv run mypy
Success: no issues found in 29 source files
```

## Collisions resolved this checkpoint

- `.gitignore` ignored `tests/fixtures/large/`, but `fetch_fixtures.py`'s
  `LARGE_DOCS_DIR` actually writes oversized documents to `fixtures-large/`
  at the repo root. Not currently triggered (both frozen PDFs are under the
  8MB threshold, so they live under `tests/fixtures/nsw/docs/` and are
  committed), but would have silently committed a multi-MB binary the first
  time a larger exhibited document was fetched. Fixed the ignore path to
  match the script (`fdf8e54`).
- No other collisions: the fixtures pack and the model/state core touched
  disjoint files and share no runtime coupling yet beyond `Ledger` importing
  `TokenUsage` from `models/client.py`, which is the intended dependency
  direction.

## Known issues / things worth the next wave's attention

- `Ledger.DEFAULT_RUN_CEILING_USD` (2.0) duplicates
  `config.DEMO_RUN_BUDGET_CEILING_USD` (2.0) as an independent literal
  rather than importing it — noted in the ledger module's own docstring as
  intentional decoupling (state/ shouldn't need to import config's demo-case
  constants), but worth a second opinion once a real caller wires them
  together, in case they're meant to be the same value by construction
  rather than by coincidence.
- `tools/fetch_fixtures.py` is exercised by tests only through its parsing
  functions; `main()` and the live-network helpers (`fetch_onlineda`,
  `_submit_etrack_search`, etc.) are untested by design (would require live
  network or extensive httpx mocking of a three-step WebForms dance for a
  script that's explicitly "run manually, never in CI"). If a future
  package needs to touch this script, that's the one area with no safety
  net.
- No `xfail`s were needed — nothing required deferring.

## What remains (next waves)

Everything below is still a stub (`NotImplementedError`) from the original
scaffold, untouched by this checkpoint:

- **Interview flow** (`src/setback/interview/flow.py`) — resident-facing
  interview over `INTERVIEW` tier.
- **Ingest** (`src/setback/ingest/{onlineda,spatial,tracker}.py`) — the
  live callers that will consume the NSW fixtures pack's parsing helpers
  and hit the real APIs at runtime.
- **Evidence dossier** (`src/setback/evidence/dossier.py`).
- **Court graph** (`src/setback/court/{graph,bench,roles,tally}.py`) — the
  ADK workflow wiring the two structurally-disjoint reviewers + adjudication
  bench; will consume `ModelClient`, `CircuitBreaker`/`DegradingBreaker`,
  and `Ledger` from this checkpoint.
- **Citation gate** (`src/setback/gate/validator.py`) — the deterministic
  pre-dispatch check.
- **Dispatch** (`src/setback/dispatch/composer.py`) — submission + refusal
  explainer composition.
- **Console** (`src/setback/console/app.py`) — currently a bare `FastAPI()`
  app with no routes.
- **Job entry point** (`src/setback/job/main.py`) — Cloud Run job driving
  the end-to-end pipeline.
- **Firestore state** (`src/setback/state/firestore.py`) — case-state
  persistence; `make demo-reset` depends on this landing.
- **Deploy** — `make deploy` is a documented stub; no Cloud Run / Terraform
  wiring yet. GCP project (`vexcourt-agent`) and service accounts exist per
  the INFRA file but are not yet referenced by any code, and secrets are
  not yet created (secretAccessor is unbound).
- **Docs** — README has two literal `[TO INSERT: ...]` placeholders
  (architecture diagram, cloud spin-up commands, verbatim hackathon
  model-eligibility wording).
- **Demo** — no end-to-end run has happened yet; nothing to point a judge at
  until interview → ingest → court → gate → dispatch are wired together.

## Deadline context

Hackathon submission deadline: Tue 2026-09-01 10:00 AEST. Today: Sat
2026-08-29. Budget ceiling: $62 total model spend (untouched so far — every
test in this checkpoint runs offline against fixtures/fakes, zero live model
calls made).
