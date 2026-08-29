# Setback — Architecture

Google All Things Agentic Hackathon submission. GCP project `vexcourt-agent` (display
name "Setback"). Stack: Python 3.12, `google-adk==2.8.0` (graph Workflow API),
`google-genai==2.20.0` on Vertex AI (ADC, `location=global`). Models: `gemini-3.5-flash-lite`
(thinking `MINIMAL`) as the default worker tier, `gemini-3.7-flash` (thinking `LOW`) as the
adjudicator / escalation tier, `gemma-4-26b-a4b-it-maas` for non-legal prose polishing.
Storage: Firestore, `setback-au` (`australia-southeast1`) as of this wave — see §3.
Uploads: GCS (`evidence/storage.py`'s `GcsEvidenceStore`), replacing the earlier
console-in-memory upload store. Compute: Cloud Run (service + job), `australia-southeast1`
as of this wave (was `us-central1`; see §3 and `deploy.sh`).

This document is the spec the build executes against and the judges read. Names below
are the literal module/collection/field names the code uses — treat them as contracts,
not suggestions. **Docs-truth note (wave 4):** the table and prose below were reconciled
against the actual `src/setback/` tree as it stands this wave; a handful of narrower
claims further down are known to still describe the original design intent rather than
what shipped, and are called out inline as "**Docs-truth note**"/"**Docs-truth
correction**" paragraphs (see §2, §4, §5, and §7) rather than silently asserted as true.

---

## 1. Component map

Three deliberately separate deployables, plus a shared library of deterministic modules
that both call into. Everything lives under one Python package, `src/setback/` — there
is no separate `shared/` tree; `console/` and `job/` are simply two entry points into it.

| Component | Type | Repo path | Talks to |
|---|---|---|---|
| `setback-console` | Cloud Run **Service** (FastAPI, ASGI, scale-to-zero) | `src/setback/console/` | Firestore (`cases`, `events`), Vertex AI (interview turns only), triggers `setback-tribunal` |
| `setback-tribunal` | Cloud Run **Job** (one execution per case run) | `src/setback/job/` (`main.py` entry point, `pipeline.py`'s `RealPipelineRunner` for the actual run) | Firestore (all collections), Vertex AI (review/adjudication/composition/grounding calls), OnlineDA / ePlanning / eTrack (via `ingest/`), GCS (uploaded evidence) |
| `ingest/` | deterministic library, no LLM calls | `src/setback/ingest/` (`onlineda.py`, `spatial.py`, `tracker.py`) | OnlineDA API, ePlanning `layerintersect`, council eTrack `FileDownload.ashx` |
| `evidence/` | mixed: `dossier.py`/`storage.py` are deterministic; `grounding.py` calls a model | `src/setback/evidence/` | GCS (`storage.py`'s `GcsEvidenceStore`), Firestore evidence anchors, bbox math, provenance grading, Vertex AI (grounding only) |
| `court/` | the ADK graph (§2) | `src/setback/court/` (`graph.py`, `roles.py`, `bench.py`, `tally.py`) | Vertex AI directly, via ADK's own `Agent`/`genai.Client` transport — **not** `models/client.py` (see §7's docs-truth note) |
| `gate/` | deterministic library, no LLM calls | `src/setback/gate/` (`s415.py`, `relevance.py`, `validator.py`) | Firestore (via the caller-supplied dossier), no I/O of its own |
| `dispatch/` | output composition | `src/setback/dispatch/composer.py` | `models/client.py` (resident-facing prose polish only) |
| `models/` | the sole call site for `ModelClient`-routed calls | `src/setback/models/client.py` | Vertex AI (`google-genai`, Gemini tiers), Vertex's OpenAI-compatible endpoint (Gemma MaaS tier) |
| `state/` | Firestore/ledger/breaker persistence | `src/setback/state/` (`firestore.py`, `ledger.py`, `breakers.py`) | Firestore |

**Why console and tribunal are decoupled (not one FastAPI app doing everything):**

1. **Latency shape mismatch.** The interview is interactive request/response — a resident
   typing answers, expecting sub-second turns. A full court run (ingest → two reviewers →
   possible adjudication → gate → compose) is a multi-minute, multi-model-call batch job.
   Putting both in one HTTP handler means either the interview blocks behind a Cloud Run
   request timeout, or the batch work degrades interview latency under load.
2. **Independent failure/retry domains.** The tribunal job can crash, retry, or be killed
   by Cloud Run Job's max execution time without taking the resident's chat session down
   with it. The console process is expected to be robust and long-lived per session;
   the job is expected to be disposable and retryable.
3. **Least privilege.** The tribunal job's service account needs Vertex AI review/adjudication
   scopes and outbound egress to three external NSW government hosts. The console's service
   account needs only Firestore read/write on `cases`/`events` and a narrow Vertex AI scope
   for interview chat, plus permission to start the job. Neither SA needs the other's full
   permission set (see §5).
4. **Independent scaling.** Console scales on concurrent chat sessions (cheap, bursty).
   Tribunal scales on cases-in-review (expensive, one-at-a-time is fine for MVP — see §8).

`ingest/` and `evidence/` are plain Python packages with no ADK/genai dependency, imported
by both deployables: console uses them for quick synchronous lookups during the interview
(e.g. confirming an address resolves to a real DA before starting a case); tribunal uses
them for the bulk fetch-and-anchor pass in `IngestNode`. Keeping them dependency-free and
side-effect-explicit (they take a Firestore client as an argument, never construct one
globally) is what makes them independently unit-testable without a live GCP project.

---

## 2. The ADK court graph

Defined in `src/setback/court/graph.py` using ADK 2.8.0's graph Workflow API. One case
run = one graph execution.

### Nodes

| Node | LLM? | Model tier | Reads | Never reads |
|---|---|---|---|---|
| `IngestNode` | no | — | OnlineDA, ePlanning, eTrack, resident photo uploads | — |
| `ClauseReviewerNode` | yes | `gemini-3.5-flash-lite` MINIMAL → escalates to `gemini-3.7-flash` LOW on breaker | LEP/DCP clause text, zoning controls (height/FSR), the s4.15(1) heads-of-consideration list | photos, plans, bounding boxes |
| `EvidenceReviewerNode` | yes | same tiers as above | resident photos, architectural plans/elevations, bbox-anchored image crops | legislation text, clause numbers, DCP text |
| `AdjudicatorNode` | yes | `gemini-3.7-flash` LOW only (never degrades further) | the **structured findings** (not raw evidence) from both reviewers, only when triggered | raw photos or raw clause text directly — it adjudicates conclusions, not source material |
| `S415GateNode` | no | — | every `GroundFinding`, the `s415_grounds` reference list, Firestore evidence/clause existence | — |
| `ComposerNode` | partial | `gemma-4-26b-a4b-it-maas` for resident-facing prose only | gated grounds, evidence anchors | — (legal content is templated, not generated) |

### Edges

```
IngestNode ──┬──> ClauseReviewerNode ───┐
             └──> EvidenceReviewerNode ─┴──> [conflict?] ──yes──> AdjudicatorNode ──> S415GateNode ──> ComposerNode
                                                  └──no───────────────────────────────────^
```

`ClauseReviewerNode` and `EvidenceReviewerNode` run **in parallel** (ADK fan-out) against
the same `IngestNode` output, then fan back in on ground identity (same `clause_ref`).
`AdjudicatorNode` is a **conditional** node: it only fires when the two reviewers'
`GroundFinding`s for the same ground disagree (one asserts non-compliance, the other is
silent or contradicts it) or either reviewer emits a confidence below threshold. If the
reviewers agree, the ground passes straight to `S415GateNode` — most grounds never touch
the adjudicator, which keeps the $/run ledger down (see §4).

### Structural disjointness — not just prompted, enforced

The "Clause Reviewer never sees photos, Evidence Reviewer never sees legislation" rule is
implemented as a **type boundary**, not a system-prompt instruction:

- `build_clause_slice(case) -> ClauseSlice` (Pydantic model: `clauses: list[ClauseText]`,
  `controls: ZoningControls`, `s415_categories: list[str]`) is the *only* function that can
  construct a `ClauseReviewerNode` input. `ClauseSlice` has no field capable of holding an
  image part.
- `build_evidence_slice(case) -> EvidenceSlice` (Pydantic model: `photos: list[ImageAnchor]`,
  `plans: list[ImageAnchor]`) is the *only* function that can construct an
  `EvidenceReviewerNode` input. `EvidenceSlice` has no field capable of holding clause text.
- A unit test (`tests/test_slice_disjointness.py`) asserts, for every fixture case, that
  serializing a `ClauseSlice` to the genai `Content` parts list never produces an
  `inline_data`/`file_data` part, and that serializing an `EvidenceSlice` never produces a
  `text` part matching a clause-number regex (`s\d+\.\d+|cl\.\s?\d+`). This is a judge-checkable
  test, not a prompting claim.

### Conservative default on unresolved split

If `AdjudicatorNode` cannot resolve a conflict with confidence above threshold (rare —
its job is specifically to resolve exactly this), the ground is marked
`status=gated_out`, `refusal_reason=low_confidence_unadjudicated`, and is **excluded**
from the submission rather than guessed at. The plain-English refusal output says so
explicitly. Setback never asserts a planning ground it isn't confident in — silence is
always the safe failure direction here, never fabrication.

### The court's stance is checked before the gate ever sees a citation

A `CourtVerdict` carries a `stance` (`support`/`reject`) that is orthogonal to the s4.15
gate's own concern (§6): the gate only ever asks "does this citation resolve", never "did
the tribunal actually believe this ground". A planning-relevant ground the court rejected
— unanimously, or on adjudication — could otherwise still ship purely because it happened
to carry a citation that resolves. `job/pipeline.py` closes that gap explicitly: any
statutorily relevant `CandidateGround` whose `CourtVerdict.stance` is `reject` is
synthesized into a permanent `REFUSED_UNSUBSTANTIATED` decision *before* it is ever handed
to `gate.validator.validate_ground` — the gate itself has no `stance` field on
`CandidateGround` and cannot express this check on its own. An irrelevant ground (e.g.
property value) still always keeps its specific, permanent s4.15 "not a listed matter"
explanation regardless of the court's stance — irrelevance is categorical, "the tribunal
didn't believe it" is a distinct and more specific reason, and the resident is shown
whichever one actually applies rather than a merged, vaguer one.

### Ledger truth: every model call in the graph is now metered

`ClauseReviewerNode`/`EvidenceReviewerNode`/`AdjudicatorNode` are `google.adk.agents.Agent`
instances, which call Vertex AI through ADK's own internal `genai.Client` — never through
`models/client.py` (see §7's docs-truth note on the "single call site" claim). Earlier in
this build that meant none of the court's token usage ever reached
`state.ledger.Ledger`, silently understating a run's real cost. `court/graph.py` now
extracts each stage's usage straight from the run's own ADK event stream — `Event` extends
ADK's `LlmResponse`, which carries the same `usage_metadata` field a direct `ModelClient`
call already reads — and books it against a caller-supplied `Ledger` (`run_court`/
`run_court_verbose`'s `ledger=` parameter). Confirmed live (one real Vertex call): a real
`Agent`-driven reviewer's event does populate `usage_metadata`, exactly like a direct
`genai` call. When it genuinely doesn't (measured offline against the test suite's
`BaseLlm` doubles, which report none), the booked usage falls back to a `len(text) // 4`
character-count estimate and is recorded with `estimated=true` on the ledger entry — an
honest, labelled guess, never a silent zero or a fabricated precise number.

---

## 3. Firestore schema

Project: `vexcourt-agent`. All collection/field names below are literal.

**Database and region (wave 4 change).** The project's `(default)` Firestore database
is `us-central1` and — like every GCP project's default database — immutable once
created; it cannot be moved. Rather than leave the whole system pinned to that region,
this wave adds a second, **named** Firestore database, `setback-au`, in
`australia-southeast1` (the region actually appropriate for an NSW-council-facing
product), and both deployables' Firestore client construction (`state/firestore.py`)
targets it by name. The `(default)` database is left in place, unused, rather than
deleted (Firestore databases cannot be deleted while any Cloud Run revision still
references them during a cutover, and there is no benefit to forcing that race under a
deadline). `deploy.sh`'s region defaults move to `australia-southeast1` to match.

**Uploads move out of Firestore/memory and into GCS.** A resident's uploaded
photo/document bytes previously lived only in the console process's in-memory
`ingest.tracker.UserUploadedDocumentSource` — invisible to a `setback-tribunal` Cloud Run
Job execution, which runs in a separate container (this was flagged as a known gap in the
wave-3 checkpoint's `SMOKE.md`). `evidence/storage.py`'s `GcsEvidenceStore` (implementing
the same `ingest.tracker.DocumentSource` protocol) now backs uploads with a real,
shared object at `cases/{case_id}/uploads/{sha256}.{ext}` in a GCS bucket
(`config.GCS_BUCKET`), reachable by both deployables regardless of which container
received the original upload.

```
cases/{case_id}
  case_id            = slug("{council}-{da_number}")           # e.g. "georges-river-PAN-661190"
  address, council, da_number, exhibition_end
  status             ∈ {interview, ingesting, reviewing, adjudicating, gated, composed, failed, budget_exceeded}
  created_at, updated_at
  budget_used_usd, budget_ceiling_usd   # ceiling = 2.00 per run (see §4)
  ingest_complete_at (nullable)          # resume marker

cases/{case_id}/grounds/{ground_id}
  ground_id          = sha256(clause_ref + normalize(ground_text))[:16]   # deterministic
  clause_ref, category         # category ∈ s415_grounds reference list, §6
  raised_by          ∈ {clause, evidence, both}
  status             ∈ {proposed, adjudicated, gated_in, gated_out}
  rationale
  cited_evidence_ids: list[str]     # anchor_ids, checked by the gate
  refusal_reason     ∈ {null, non_planning_ground, unresolved_citation, low_confidence_unadjudicated, budget_exceeded}
  refusal_explanation (nullable, plain English)

cases/{case_id}/evidence/{anchor_id}
  anchor_id          = sha256(source_doc + page + bbox_tuple)[:16]    # deterministic
  source_doc, page, bbox: [x0, y0, x1, y1]
  provenance_grade   ∈ {A, B, C}   # A = official council doc, B = verified applicant plan, C = unverified resident photo
  extracted_text_or_caption
  fetched_at

cases/{case_id}/events/{event_id}
  event_id           = ULID()      # monotonic, sortable — drives SSE resume
  node, stage_status ∈ {started, completed, failed, degraded}
  at

cases/{case_id}/breakers/{stage}
  stage              ∈ {clause_reviewer, evidence_reviewer, adjudicator}
  fail_count, state  ∈ {closed, open, half_open}
  opened_at (nullable), degraded_model: bool

cases/{case_id}/ledger/{call_id}
  call_id            = ULID()
  model, input_tokens, output_tokens, cost_usd, stage, at

config/s415_grounds        # single doc, the deterministic legal relevance list, §6
```

**Deterministic IDs, idempotent writes.** `ground_id` and `anchor_id` are content hashes,
not auto-IDs — re-running `IngestNode` or a reviewer after a crash writes the *same*
document ID with `set(..., merge=True)`, so retries never duplicate a ground or an
evidence anchor. `event_id` and `call_id` are ULIDs (need ordering, not idempotency —
each represents a discrete occurrence, duplicates there are harmless to detect and drop
via the sweeper if they ever occur).

**Resume semantics.** On start, `setback-tribunal` reads `cases/{case_id}`. If
`ingest_complete_at` is set and the `evidence` subcollection is non-empty, `IngestNode` is
skipped and the job resumes at the reviewer fan-out. If `status` is already `gated` or
`composed`, the job exits immediately (no-op) — this makes re-triggering a job for an
already-finished case safe, which matters because the sweeper (§4) and manual retries both
re-trigger by case ID, not by run ID.

---

## 4. Failure handling

### Circuit breakers (per stage, per case)

**Docs-truth note:** the schema below (`cases/{case_id}/breakers/{stage}`, one document
per stage) is the original design intent and is what `state/breakers.py`'s
`CircuitBreaker`/`DegradingBreaker` support generically. As actually wired in
`court/graph.py`/`job/pipeline.py` today, only `AdjudicatorNode` has a real breaker behind
it (`court.bench.AdjudicationBench`, backed by `cases/{case_id}/breakers/adjudicator`,
persisted via `store.save_breaker`) — `ClauseReviewerNode`/`EvidenceReviewerNode` currently
run at a fixed tier (`gemini-3.5-flash-lite`, from `RealPipelineRunner`'s constructor
defaults) with no breaker wired at all, so a reviewer failure propagates as a hard error
for that ground's run rather than degrading. This is a real gap against the original
design below, not an intentional simplification, and is left for a future pass rather
than silently claimed as built:

- If the adjudicator stage was calling `gemini-3.7-flash` and its breaker opens, it
  **degrades** to skipping the call entirely (`AdjudicationBench.tier()` returns `None`)
  rather than to a lower model tier — there is no lower tier than `gemini-3.7-flash` for
  the adjudicator to fall back to (`court/bench.py`'s module docstring is explicit about
  this). The ground then routes straight to the conservative default (`gated_out`,
  `refusal_reason=low_confidence_unadjudicated`) rather than retrying indefinitely.
- A reviewer stage's own per-stage breaker/degrade-to-cheaper-tier behaviour described
  below for `clause_reviewer`/`evidence_reviewer` is **not yet built** (see the note
  above) — treat it as the target design, not a current guarantee, until it lands:
  - If the stage was calling `gemini-3.7-flash`, it **degrades** to `gemini-3.5-flash-lite`
    and half-opens (retries once at the lower tier before re-opening).
  - If the stage was already on `gemini-3.5-flash-lite` (the default), the breaker opens
    fully — the stage is forced to its conservative default (ground `gated_out`,
    `refusal_reason=low_confidence_unadjudicated`) and the case proceeds without that
    ground rather than retrying indefinitely.

### Retry/backoff on 429 (Dynamic Shared Quota)

Handled *inside* `llm/client.py`, the single call site (§7), **before** a failure counts
against the breaker: exponential backoff with jitter, base 1s, cap 30s, max 5 attempts.
Only after all 5 attempts fail does it register as one breaker failure. This separates
"quota hiccup, retry transparently" from "stage is actually unhealthy, degrade."

### What happens when a worker agent loops or hallucinates (rubric question, answered directly)

- **Loops:** every node has a hard `max_tool_calls` / `max_turns` counter enforced in
  Python state (not a prompt instruction asking the model to stop) — e.g. 4 for the
  reviewers. Hitting the counter is treated as a stage failure and increments the breaker;
  it is never silently retried past the limit. There is no code path that can call a model
  in an unbounded loop — the counter is checked before every call, in the same function
  that checks the budget ledger.
- **Hallucinates:** three independent nets, not one:
  1. **Schema validation.** Every model call requests structured output against a Pydantic
     schema; a parse failure is treated as a stage failure (feeds the breaker), not
     silently coerced.
  2. **Citation resolution** (`S415GateNode`, deterministic — §6). Any `GroundFinding`
     citing a `clause_ref` or `anchor_id` that doesn't exist in Firestore, or a bbox
     outside the source image's bounds, is auto-rejected regardless of how plausible the
     surrounding text reads. A model cannot invent a citation into existence.
  3. **Conservative adjudication default** (§2). An unresolved conflict is dropped from
     the submission, never guessed.

### Sweeper (outside the agent loop)

A separate Cloud Scheduler-triggered Cloud Run function (`sweeper/main.py`), **not** part
of the ADK graph or any node — it never calls a model. It scans `cases` for `status` stuck
in a running state (`ingesting`/`reviewing`/`adjudicating`) for longer than 10 minutes
(covers a crashed job, an OOM kill, or a Cloud Run Job hard timeout that the job process
itself never got to handle) and marks the case `failed` with an events entry, so
`setback-console`'s SSE stream terminates cleanly for the resident instead of hanging
indefinitely waiting for an event that will never arrive.

### $2/run ledger abort

`state.ledger.Ledger` accumulates cost for a run and self-aborts past its ceiling: booking
a call whose cost would push the running total past **$2.00** (`DEMO_RUN_BUDGET_CEILING_USD`,
tracked separately from the hackathon-wide $62 ceiling, which is a manual dashboard check
across all cases, not an automated gate) raises `BudgetExceededError` *before* the call is
counted, rather than discovering the overage after the fact. This is a hard stop, not a
warning — no stage can book spend past it.

**Docs-truth note:** as of this wave, every stage that can reach the ledger does —
`models/client.py`-routed calls (interview, clerk extraction, grounding, composer polish)
book directly; the ADK court stages (reviewers, adjudicator) book via `court/graph.py`'s
event-stream extraction (§2). The one caller-side step still required for the court
stages' bookings to actually land in a live run is `job/pipeline.py` passing its
`Ledger` instance through to `run_court_verbose(..., ledger=...)` — `court/graph.py`
added the parameter and the extraction logic this wave, but `job/pipeline.py` is a
different work package's lane and had not yet been updated to pass it as of this
checkpoint (see this doc's revision history / the integrator's notes for status).

---

## 5. Credential security

- **ADC everywhere.** Both `setback-console` and `setback-tribunal` authenticate to Vertex
  AI and Firestore purely via Application Default Credentials (the Cloud Run service
  identity). Zero API keys appear in code, environment variables, or config for any Google
  API.
- **Service accounts, least privilege, one per deployable:**
  - `setback-console-sa`: `roles/datastore.user` (scoped in application logic to
    `cases`/`events` only — Firestore has no native per-collection IAM, so this is enforced
    in the repository layer, §7, not IAM alone), `roles/aiplatform.user` (interview chat
    calls only), `roles/run.invoker` on the `setback-tribunal` job (to trigger it). No
    external egress permission needed beyond calling the job.
  - `setback-tribunal-sa`: `roles/datastore.user` (full case subcollections),
    `roles/aiplatform.user` (review/adjudication/composition calls), outbound egress to
    `onlineda.*`, `api.apps1.nsw.gov.au`, and `etrack.georgesriver.nsw.gov.au` (no IAM
    role for this — it's network egress, not a GCP permission — but it is the only SA
    whose runtime has a reason to reach those hosts). `roles/secretmanager.secretAccessor`
    scoped to exactly the Maps secret, if and when one exists (see below).
  - Neither SA is `roles/editor` or `roles/owner`. Neither SA can read the other's scope
    it doesn't need.
- **The one API key that exists (Maps → Street View fallback):** zoning itself is still
  resolved via the ePlanning `layerintersect` API directly, never a rendered map (see §8) —
  but `evidence/imagery.py`'s Street View fallback (the resident's "away from home" or
  no-photo-available case) does call the Maps Platform Street View API, so this key is
  real, not merely a documented contingency. It is Secret-Manager-referenced only (never a
  literal in code, read at call time via an injectable `secret_accessor`, mirroring
  `models/client.py`'s ADC token-provider pattern), under the secret's actual live name on
  project `vexcourt-agent`: **`maps-api-key`** — not `setback-maps-key`, an earlier
  placeholder name from before the secret was created, corrected in this pass.
  `setback-tribunal` (the only deployable that calls it) receives it via
  `--set-secrets MAPS_API_KEY=maps-api-key:latest` at deploy time; `setback-console` never
  receives it (`--clear-secrets` is passed explicitly on every console deploy to guarantee
  that stays true, since the console never calls Street View).

---

## 6. The deterministic s4.15 gate

`S415GateNode` is pure Python — no model call, fully unit-testable, fully auditable by a
judge reading the code in under a minute.

### The legal relevance list, as data

`config/s415_grounds` (a single Firestore doc, mirrored from `shared/s415_grounds.yaml` at
deploy time so it's both version-controlled and runtime-checkable) enumerates the EP&A Act
s4.15(1) heads of consideration as category codes:

```yaml
- code: a_planning_instruments     # s4.15(1)(a) — LEPs / environmental planning instruments
- code: a1_draft_instruments       # s4.15(1)(a1)
- code: b_dcp                      # s4.15(1)(b) — development control plans
- code: c_impacts                  # s4.15(1)(c) — natural/built/social/economic impacts
- code: d_site_suitability         # s4.15(1)(d)
- code: e_submissions              # s4.15(1)(e) — public submissions
- code: f_public_interest          # s4.15(1)(f)
```

Every `GroundFinding` a reviewer produces must be tagged with exactly one `category` from
this list. `is_planning_relevant(category) -> bool` is a pure lookup against this list —
not a model judgment call. A ground whose category isn't on the list (or whose category
tag is missing/malformed) is rejected with `refusal_reason=non_planning_ground` before
anything else is checked. This is what stops Setback from ever forwarding a "the new
owners seem unfriendly" or "I don't like their car" style objection into a formal
submission — it's structurally impossible, not just discouraged by prompting.

### Citation resolution

For every `cited_evidence_id`/`clause_ref` on a ground, the gate checks:

1. The document/anchor actually exists in `cases/{case_id}/evidence/{anchor_id}` (for
   evidence) or matches a known clause number pattern actually present in the ingested
   LEP/DCP text (for clause refs) — not just pattern-shaped, actually resolvable back to
   ingested content.
2. For evidence anchors specifically: the bbox falls within the source image's actual
   pixel bounds (guards against a coordinate hallucination that happens to look valid).

Any failure here rejects the ground with `refusal_reason=unresolved_citation`, regardless
of how confident or well-written the surrounding rationale text is.

**Scope boundary, made explicit:** this gate is a citation/relevance filter only — it has
no concept of whether the court (§2) actually found a ground well-founded. That check
(a rejected ground must never ship purely because a citation resolves) happens one layer
up, in `job/pipeline.py`, before a court-rejected-but-relevant ground is ever handed to
`validate_ground` at all — see §2's "The court's stance is checked before the gate ever
sees a citation".

### Refusal semantics

A rejected ground is never silently dropped — it's written to Firestore with
`status=gated_out`, a `refusal_reason` enum value, and a `refusal_explanation` string.
`ComposerNode`'s `PlainEnglishRefusalAdapter` (§7) surfaces every rejected ground verbatim
in the "what I refused and why" section of the output, so the resident sees exactly what
Setback declined to include and why — this is a product feature (trust/transparency), not
just an internal log.

---

## 7. Design patterns actually used, and why

Written so a judge scoring "clean, modularized, maintainable" finds the answer fast.

- **A sole call site for `ModelClient`-routed calls, plus one further call path this pass
  reconciled the ledger against** (docs-truth correction — the original design intended
  literally one call site, and this is the one substantive place reality now diverges from
  it, tracked openly rather than left as a false claim). `models/client.py`'s
  `ModelClient.generate(tier, prompt, response_model, ...)` is still the only place the
  interview, `setback.clerk`'s extraction calls, `evidence/grounding.py`'s grounding calls,
  and `dispatch/composer.py`'s resident-facing prose polish talk to a model — retry/
  backoff and structured-output validation live here exactly once for all of those.
  `court/graph.py`'s three `google.adk.agents.Agent` nodes (the two reviewers, the
  adjudicator) are the exception: ADK constructs and owns its own `genai.Client`
  internally, so those calls never pass through `ModelClient` at all — there is a second
  place in the codebase a model request is actually constructed. This was invisible to the
  budget ledger until this wave (§2's "Ledger truth" note): `court/graph.py` now closes
  that gap by extracting usage straight from the ADK event stream and booking it against
  the same `Ledger`, so the *accounting* is unified again even though the *transport* is
  not. Unifying the transport itself (routing ADK's `Agent` through a `ModelClient`-backed
  custom `BaseLlm`, so there is truly only one call site) is a natural follow-up, not
  attempted this wave.
- **Ports & adapters for output composition** (`dispatch/composer.py`). `ComposerPort` is
  an interface consumed by the composition step; `CouncilSubmissionAdapter` produces the
  formal council-format document, `PlainEnglishRefusalAdapter` produces the resident-facing
  refusal summary. Both consume the same gated `Ground`/`Evidence` domain objects with no
  duplicated logic. Adding a third output (e.g. a PDF export adapter) later is a new class
  implementing the port, not a graph change.
- **Evidence provenance grading as a value object** (`evidence.dossier.ProvenanceGrade`, a
  `StrEnum` with values `"A"`/`"B"`/`"C"`, attached once at dossier-build time and never
  re-derived downstream). "How much do we trust this fact" has exactly one place it's
  decided and flows unchanged through reviewers, the gate, and the composer — no LLM node
  ever re-grades evidence trust. **Docs-truth note:** the *labels* backing those three
  letter grades are `RESIDENT_PHOTO` / `STREET_VIEW_SOLAR_FALLBACK` / `DOCUMENTS_ONLY`, a
  different taxonomy than this document's earlier "A = official council doc, B = verified
  applicant plan, C = unverified resident photo" description (no "official council doc"
  grade exists in the shipped enum at all) — the *pattern* (graded once, immutable
  downstream) held up; the specific grade semantics changed during the build and this doc
  had not been reconciled to the new labels until this pass. Confirm against
  `evidence/dossier.py`'s `ProvenanceGrade` directly if the exact grading rationale matters
  for judging, rather than trusting this paragraph's summary of it.
- **A port for Firestore case state, not a per-collection repository layer**
  (`state.firestore.CaseStore`, an abstract port with a `FirestoreCaseStore` production
  implementation). Deterministic-ID generation and idempotent writes live behind this one
  port; node/job code never imports the Firestore SDK directly. **Docs-truth correction:**
  an earlier design considered separate `CaseRepo`/`GroundRepo`/`EvidenceRepo` classes
  under a `shared/repo/` package — that specific shape was not built; the single
  `CaseStore` port (covering cases, grounds, evidence anchors, events, breakers, and the
  ledger together) is what actually ships, and is what makes the resume semantics (§3)
  testable without a live Firestore emulator running for every unit test.
- **Degrade-not-halt for the adjudication tier, not a general tier-selection strategy.**
  `court.bench.AdjudicationBench` (wrapping `state.breakers.DegradingBreaker`) decides
  whether `AdjudicatorNode` is called at `gemini-3.7-flash` or skipped straight to the
  conservative default — a binary "call or skip" choice, since the adjudicator has no
  lower tier to fall back to. **Docs-truth correction:** an earlier design (`select_tier(
  stage, breaker_state) -> ModelTier` over a general `shared/llm/tier.py`) intended this
  pattern to also degrade the two reviewers between `gemini-3.7-flash` and
  `gemini-3.5-flash-lite`; as built, the reviewers run at a fixed tier with no breaker
  behind them at all (see §4's docs-truth note on circuit breakers) — only the adjudicator
  actually has degrade-not-halt wiring today.

---

## 8. MVP cut list (deliberately not built)

| Cut | One-line reason |
|---|---|
| Multi-council genericization | One demo case (Georges River), 74h window — `ingest/` adapters are hardcoded to eTrack/ePlanning's actual response shapes, not a general council plugin system. |
| User auth / multi-tenant login | Single-session-per-case-link is enough for a hackathon demo; no resident PII protection surface to build auth around yet. |
| PDF rendering of the final submission | Output is formatted Markdown/HTML matching the council's submission fields — a PDF adds a rendering dependency with zero judging upside. |
| Live map / rendered-map UI | Zoning is resolved via the ePlanning `layerintersect` API without needing a rendered map. (The Maps key itself *is* used — §5 — but only for the Street View fallback still-image fetch, not for any interactive map UI.) |
| Cloud Run Job concurrency / autoscaling tuning | Demo processes one case at a time; not a cost or latency concern inside the hackathon window. |
| Firestore security rules per resident user | No auth system to hang per-user rules off yet; service-level IAM is the only access boundary in MVP. |
| Dead-letter queue / retry-forever infra | The sweeper (§4) plus per-stage breakers cover every failure mode the demo can hit without extra queueing infrastructure. |
| Token-by-token streamed model output to the frontend | SSE streams stage-*completion* events, not token deltas — a reviewer's full structured JSON is more useful mid-stream than partial tokens, and far simpler to implement reliably under a deadline. |

---

## 9. Architecture diagram

```mermaid
flowchart TB
  subgraph FE["Frontend"]
    UI["Browser SPA<br/>(chat + SSE client)"]
  end

  subgraph CONSOLE["setback-console — Cloud Run Service (FastAPI)"]
    API["Interview API + SSE endpoint"]
  end

  subgraph TRIBUNAL["setback-tribunal — Cloud Run Job (ADK graph)"]
    Ingest["IngestNode<br/>(deterministic)"]
    Clause["ClauseReviewerNode"]
    Evidence["EvidenceReviewerNode"]
    Adj["AdjudicatorNode<br/>(conditional)"]
    Gate["S415GateNode<br/>(deterministic)"]
    Compose["ComposerNode"]
  end

  subgraph VERTEX["Vertex AI — ADC, location=global"]
    FL["gemini-3.5-flash-lite<br/>(MINIMAL) — default"]
    F37["gemini-3.7-flash<br/>(LOW) — adjudicator / escalation"]
    Gemma["gemma-4-26b-a4b-it-maas<br/>(resident-facing prose only)"]
  end

  subgraph FS["Firestore — vexcourt-agent"]
    Cases[("cases")]
    Grounds[("grounds")]
    Evid[("evidence anchors")]
    Events[("events")]
    Breakers[("breakers")]
    Ledger[("token ledger")]
  end

  subgraph EXT["External NSW sources (keyless)"]
    OnlineDA["OnlineDA API"]
    ePlanning["ePlanning layerintersect"]
    eTrack["Council eTrack<br/>FileDownload.ashx"]
  end

  UI <-->|"chat turns, SSE stream"| API
  API -->|"create/read"| Cases
  API -->|"interview turn"| FL
  API -->|"trigger execution"| Ingest

  Ingest --> OnlineDA
  Ingest --> ePlanning
  Ingest --> eTrack
  Ingest -->|"write anchors"| Evid
  Ingest -->|"status"| Cases

  Ingest --> Clause
  Ingest --> Evidence
  Clause -->|"clause+DCP text only"| FL
  Clause -.->|"breaker escalation"| F37
  Evidence -->|"photos+plans only"| FL
  Evidence -.->|"breaker escalation"| F37
  Clause -->|"GroundFinding"| Grounds
  Evidence -->|"GroundFinding"| Grounds

  Grounds -->|"on conflict"| Adj
  Adj --> F37
  Adj -->|"resolved / conservative default"| Grounds

  Grounds --> Gate
  Evid -->|"citation check"| Gate
  Gate -->|"gated_in / gated_out"| Grounds

  Gate --> Compose
  Compose -->|"refusal prose"| Gemma
  Compose -->|"submission + refusal doc"| Cases

  Cases -->|"status change"| Events
  Events -->|"SSE"| API

  TRIBUNAL -.->|"per-stage state"| Breakers
  TRIBUNAL -.->|"per-call cost"| Ledger
```

Solid arrows are the main data path; dashed arrows are escalation/observability side
channels (breaker upgrades, ledger writes). The `FS` (Firestore) box is the named
`setback-au` database in `australia-southeast1` as of this wave (§3), not the project's
original `(default)` database; a GCS bucket for uploaded evidence (§3) sits alongside it
and isn't drawn separately above.
