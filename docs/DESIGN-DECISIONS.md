# Setback — Design Decisions

Companion to `ARCHITECTURE.md`. That document states *what* is built; this one records
*why*, what alternatives were considered, and what each decision costs. Read this when a
build agent or a judge asks "why not just X instead."

---

## D1. Cloud Run Service + Cloud Run Job, not one service

**Decision:** `setback-console` (Service) and `setback-tribunal` (Job) are separate
deployables (ARCHITECTURE.md §1).

**Alternatives considered:**
- *One FastAPI app, tribunal work as a background asyncio task.* Rejected: a Cloud Run
  Service instance can be scaled to zero or recycled mid-request; a background task
  running inside the same process as the HTTP server has no execution guarantee once the
  request that spawned it returns and the instance is eligible for eviction. Cloud Run
  Jobs exist specifically to guarantee an execution runs to completion (or hits its own
  timeout) independent of HTTP traffic.
- *Cloud Functions instead of a Job for the tribunal.* Rejected: the court graph is a
  multi-model-call sequential/branching workflow that can run for minutes; Cloud Functions'
  timeout ceiling and lack of native multi-step state make it a worse fit than a Job that
  can run the whole ADK graph in one process with in-memory node state.

**Cost of this decision:** two deployables to build, deploy, and wire together (console
triggers the job via `roles/run.invoker`) instead of one. Judged worth it for the
latency-isolation and least-privilege reasons in ARCHITECTURE.md §1.

---

## D2. ADK graph with structural (typed) disjointness, not prompted disjointness

**Decision:** Clause/Evidence reviewer separation is enforced via distinct Pydantic input
types (`ClauseSlice`/`EvidenceSlice`) that are structurally incapable of carrying the
other reviewer's material, verified by a unit test — not via a system prompt saying "only
look at X" (ARCHITECTURE.md §2).

**Why this matters for judging, not just correctness:** "Two structurally-disjoint
reviewers" is one of the two headline innovation claims in the project pitch. A prompted
separation ("please don't look at photos") is falsifiable by a judge in about one
adversarial question ("what stops the model from just describing the photo anyway if it's
in context?") — and the honest answer would be "nothing, if it were in context." Making
the separation a type-system fact (the photo is never in the `ClauseSlice`'s context
window in the first place) closes that question entirely. This is more implementation
work up front (two slice builders instead of one shared prompt-builder with a
conditional) but it's the difference between an innovation claim that's true by
construction and one that's true by hope.

**Tradeoff:** a ground that genuinely needs both legal and photographic reasoning (e.g.
"the photo shows a wall that appears to exceed the height control in clause X") cannot be
resolved by either reviewer alone — that's exactly what `AdjudicatorNode` is for. This is
intentional, not a gap: the adjudicator sees both reviewers' *conclusions*, never their
raw inputs, which keeps the disjointness property intact even at the point where the two
domains have to be reconciled.

---

## D3. Adjudicator triggers only on conflict, not on every ground

**Decision:** `AdjudicatorNode` is a conditional node — most grounds skip it entirely
(ARCHITECTURE.md §2).

**Why:** Two independent cost pressures. First, the $2/run budget ledger (ARCHITECTURE.md
§4) — `gemini-3.7-flash` is the more expensive tier; calling it for every ground rather
than only contested ones could exhaust a case's budget before the gate and composer ever
run. Second, and more important for the pitch: the adjudicator is a *disagreement
resolver*, and running it unconditionally over already-agreeing findings would dilute
"adjudicator on split" into "adjudicator on everything," undermining the architectural
story that the split-detection itself is meaningful signal.

**Alternative considered:** always run the adjudicator as a final polish pass over every
ground. Rejected for both reasons above, and because it would blur the conservative-
default story — if the adjudicator touches everything, "conservative default on
unresolved split" stops being a distinguishing behavior and becomes just "what the
adjudicator does."

---

## D4. Deterministic content-hash IDs for grounds and evidence, ULIDs for events/ledger

**Decision:** `ground_id`/`anchor_id` are hashes of their content; `event_id`/`call_id` are
ULIDs (ARCHITECTURE.md §3).

**Why the split:** grounds and evidence anchors are *facts about the case* — the same
ground re-derived on a retry should overwrite, not duplicate, so idempotency requires the
ID to be a function of content. Events and ledger entries are *occurrences* — two
identical-looking ledger rows (same model, same stage, different timestamp) are two real
API calls that both cost money and must both be counted; collapsing them by content-hash
would silently under-report spend. ULIDs give ordering (needed for SSE resume) without
forcing content-based dedup where dedup would be wrong.

**Alternative considered:** Firestore auto-generated IDs everywhere. Rejected: auto-IDs
make every write non-idempotent, which would have required an explicit
"has-this-ground-already-been-written" check before every write anyway — the content hash
gets that check for free via `set(..., merge=True)`.

---

## D5. Circuit breaker degrades tier before it opens fully

**Decision:** three consecutive stage failures degrades `gemini-3.7-flash` →
`gemini-3.5-flash-lite` first; only a failure at the already-degraded tier fully opens the
breaker (ARCHITECTURE.md §4).

**Why:** most transient failures in practice are quota pressure (429 Dynamic Shared
Quota) or a momentarily overloaded larger model, not a fundamental inability to do the
task. Falling back to the cheaper, less loaded tier before giving up on a ground entirely
gives a case a real chance to complete instead of the resident silently losing a ground
that a second attempt at a different tier would have found fine. It also happens to save
money on repeated retries, but that's a secondary benefit — the primary one is graceful
degradation over hard failure.

**Alternative considered:** flat retry-and-give-up with no tier change. Rejected: it
either retries the exact request that just failed (likely to fail the same way if the
cause is model-side overload) or gives up too early on a ground that a smaller/cheaper
model could still have resolved.

---

## D6. Stuck-case recovery: designed as a sweeper, never built — docs-truth correction

**What this entry originally claimed:** a standalone Cloud Scheduler → Cloud Run Function
("the sweeper"), living outside the ADK graph entirely, would scan `cases` for a `status`
stuck in a running state past a timeout and mark it `failed` — the reasoning being that a
watchdog living *inside* the thing it watches can't detect that thing crashing, so it has
to be a separate execution context checking Firestore state from outside to be a
meaningful safety net rather than a comforting illusion of one.

**Docs-truth correction (wave 6):** that reasoning still holds as an argument for *why* a
sweeper should be architected this way if one is ever built — but no sweeper was built.
There is no `sweeper/` directory, no Cloud Scheduler job, and no code anywhere in this
repo that watches a running case from outside its own job execution. A full-tree grep for
`sweeper` finds only two comments in `job/main.py` pointing forward to
`ARCHITECTURE.md` §4, and that section's own subsection on this exact topic. This was a
genuine design-intent-vs-shipped gap, not a deliberate MVP cut recorded honestly at the
time — it was written up as if built, which is worse than a gap left silent, because it
told a judge a specific, checkable claim that a grep disproves.

**What actually covers case-run failure today**, none of it a sweeper:
per-stage circuit breakers (the adjudicator's only — see D5), the conservative-default
gate (an unresolved conflict or citation is refused, never guessed — ARCHITECTURE.md §2,
§6), the $2/run ledger self-abort (ARCHITECTURE.md §4), and `--session-affinity` on
`setback-console` (mitigates one specific interview-side hazard, unrelated to a stuck
tribunal job). None of these detects or recovers a crashed/OOM-killed/timed-out job
execution — that case is left stuck with no automated recovery, and needs a manual
re-trigger by `case_id` (safe, by the resume semantics in ARCHITECTURE.md §3, but not
automatic). Building the actual sweeper described above remains a reasonable next step;
it just isn't done, and this entry now says so.

---

## D7. One model call site (`models/client.py`) as the design intent — reconciled against
what actually shipped

**Decision:** every model call in the codebase was designed to go through one function
(ARCHITECTURE.md §7).

**Why:** the budget ledger, the circuit breaker, and the retry/backoff logic all need to
see *every* call to be trustworthy. If each node constructed its own `genai.Client` and
called `generate_content` directly, the $2/run abort (ARCHITECTURE.md §4) would only be as
strong as the discipline of whoever wrote the last node — one node that forgets to check
the ledger first is a silent budget leak that's invisible until the bill arrives. Routing
everything through one function makes the enforcement structural: there is no code path to
a model that doesn't pass through the budget check, because there is no second way to call
a model at all.

**Docs-truth correction (wave 4):** this is the one place the "single call site" claim
didn't hold up against the shipped code, and it's recorded honestly here rather than left
as a quiet overstatement. `court/graph.py`'s three `google.adk.agents.Agent` nodes (the
Clause/Evidence Reviewers, the Adjudicator) call Vertex AI through ADK's own internal
`genai.Client`, constructed and owned by ADK itself — they never pass through
`models.client.ModelClient.generate(...)`. This was invisible to the ledger for most of
the build (`job/pipeline.py`'s own docstring flagged it as a "known gap, not silently
swept under the rug"). This wave closes the *accounting* gap without changing the
*transport*: `court/graph.py` now reads `usage_metadata` straight off each stage's ADK
event (the same field a direct `ModelClient` call already reads, confirmed live) and books
it against the same `Ledger`, so every call is metered again even though it isn't all
funneled through one function. The originally-intended single-call-site property could
still be recovered later by wrapping ADK's `Agent` around a `ModelClient`-backed custom
`BaseLlm` instead of a bare model-id string — not attempted this wave, since the
accounting fix was the higher-priority half of the gap (a metered-but-two-path system is
honest; an unmetered court graph was the actual risk).

**Cost:** every new node type going through `ModelClient` has to fit its interface
(`generate(tier, prompt, response_model, *, system_instruction=None, temperature=None) ->
ModelResult`) rather than doing anything bespoke with the genai SDK — judged an acceptable
constraint for a hackathon-scale codebase where "every call is metered" is worth more than
"every node is fully free-form." ADK's own agents are the one place that constraint wasn't
actually enforced, for the reason above.

---

## D8. Legal relevance list as data (a plain Python dict), not a model classification

**Decision:** `classify_relevance(category)` (`gate/relevance.py`) is a lookup against a
static list of s4.15 categories, not an LLM call asked "is this planning-relevant?"
(ARCHITECTURE.md §6).

**Why:** this is the deterministic gate the whole pitch's trust story rests on
("deterministic s4.15 gate that refuses non-planning grounds"). A gate implemented as
another model call is not deterministic — it has its own hallucination surface, its own
non-planning-relevant confident-sounding wrong answer, and would need its *own* gate to be
trustworthy, which is an infinite regress. Making the list data means a judge (or a
resident, or a council officer) can read the source directly and verify exactly what
Setback considers admissible, with no model in the loop for this specific check at all.

**Docs-truth correction (wave 6):** this entry originally said the list lived in a
`shared/s415_grounds.yaml` file, version-controlled and mirrored into a Firestore config
doc for runtime checkability. Neither exists — see ARCHITECTURE.md §6's docs-truth
correction. What ships is `gate/s415.py`'s `PLANNING_HEADS`/`NON_PLANNING_GROUNDS`, two
plain Python `dict[str, RelevanceRuling]` constants. The "data, not a model call, judge
can verify it directly" property this decision is actually about still holds — reading a
Python dict is no less verifiable than reading a YAML file — but the specific file path
this entry named never existed, and is corrected here rather than left as a dangling
reference to a file a judge would search for and not find.

**Alternative considered:** ask the reviewer models to self-tag relevance and trust that
tag. Rejected outright — that's exactly the "trust the LLM to police itself" pattern the
gate exists to avoid. The category tag *is* still produced by the reviewer LLM (it has to
decide which head of consideration a ground falls under), but *whether that category is
admissible at all* is a pure function over a fixed list, not a further model judgment.

---

## D9. Provenance grade (A/B/C) set once at ingest, never re-derived

**Decision:** evidence trust grading happens exactly once, in `IngestNode`, and is treated
as immutable data by every downstream node (ARCHITECTURE.md §7).

**Why:** if any reviewer or the adjudicator could adjust a provenance grade based on how
the evidence read in context, the grade would stop meaning "how was this evidence
sourced" and start meaning "how convincing did the model find it" — collapsing two
genuinely different questions into one. Keeping provenance as a value fixed at the
deterministic ingest boundary means the gate and composer can always answer "how much do
we actually know this is true" independent of how persuasively any node downstream wrote
about it.

---

## D10. What was deliberately not re-litigated here

Multi-council genericization, auth, PDF export, live maps, Job autoscaling, per-user
Firestore rules, dead-letter queues, and token-level streaming are cut for MVP
(ARCHITECTURE.md §8) purely on a time-budget basis, not because they're architecturally
hard to add later — each fits cleanly into an existing seam (`ingest/` adapters for a
second council, `ComposerPort` for a PDF adapter, IAM conditions for per-user Firestore
rules) without a redesign. They're listed as decisions here only to record that the seam
was considered and left open, not that the capability was overlooked.
