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

## D6. Sweeper lives outside the ADK graph as a separate scheduled process

**Decision:** the sweeper is a standalone Cloud Scheduler → Cloud Run Function, not a node
or a wrapper around the graph (ARCHITECTURE.md §4).

**Why:** a watchdog that lives *inside* the thing it's watching can't detect that thing
crashing — if the sweeper were a node in the same process as the graph it's monitoring, an
OOM kill or hard Cloud Run Job timeout takes the sweeper down with the graph, which is
exactly the failure mode it exists to catch. It has to be a separate execution context on
its own schedule, checking Firestore state from outside, to be a meaningful safety net
rather than a comforting illusion of one.

---

## D7. Single model call site (`llm/client.py`) rather than per-node client construction

**Decision:** every model call in the codebase goes through one function
(ARCHITECTURE.md §7).

**Why:** the budget ledger, the circuit breaker, and the retry/backoff logic all need to
see *every* call to be trustworthy. If each node constructed its own `genai.Client` and
called `generate_content` directly, the $2/run abort (ARCHITECTURE.md §4) would only be as
strong as the discipline of whoever wrote the last node — one node that forgets to check
the ledger first is a silent budget leak that's invisible until the bill arrives. Routing
everything through one function makes the enforcement structural: there is no code path to
a model that doesn't pass through the budget check, because there is no second way to call
a model at all.

**Cost:** every new node type has to fit the call site's interface
(`call(model, contents, tier, case_id, stage) -> StructuredResponse`) rather than doing
anything bespoke with the genai SDK — judged an acceptable constraint for a hackathon-scale
codebase where "every call is metered" is worth more than "every node is fully free-form."

---

## D8. Legal relevance list as data (YAML/Firestore doc), not a model classification

**Decision:** `is_planning_relevant(category)` is a lookup against a static list of s4.15
categories, not an LLM call asked "is this planning-relevant?" (ARCHITECTURE.md §6).

**Why:** this is the deterministic gate the whole pitch's trust story rests on
("deterministic s4.15 gate that refuses non-planning grounds"). A gate implemented as
another model call is not deterministic — it has its own hallucination surface, its own
non-planning-relevant confident-sounding wrong answer, and would need its *own* gate to be
trustworthy, which is an infinite regress. Making the list data means a judge (or a
resident, or a council officer) can read `shared/s415_grounds.yaml` directly and verify
exactly what Setback considers admissible, with no model in the loop for this specific
check at all.

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
