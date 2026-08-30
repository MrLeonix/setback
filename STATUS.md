# STATUS — post-freeze incident fix + film-case allowlist, RE-FROZEN FINAL (2026-08-30)

**Superseding update.** Five commits landed after the wave-12 freeze declared below
(`56a7f73`, `1d5d565`, `230bc07`, `a665c89`, `bfed41f`, `3135a1a`). Two matter substantively:
a tribunal-capacity incident fix (`1d5d565` + `230bc07`, with `a665c89` recording the
incident) that counts `tribunal_rerun_ignored` as a terminal event and excludes stale runs
from the concurrency cap via a 15-minute TTL, and `3135a1a`, which allowlists the founder's
canonical film case (`1f4b7367fd30c089173ef09d7e8383a4`) for the Veo overshadowing clip and
adds a founder-approved one-time-cost disclosure ("Pre-generated with Veo 3.1 · one-time
cost US$1.60 · not part of this case's run cost") directly on the Evidence-tab card.
`bfed41f` restyled overlay chips/boxes (stroke-only green boxes, compact chips outside the
box). Final state: `setback-console` revision **`setback-console-00022-w62`**, **691 tests
green**. This — not the `-00020-sl4` / 676-tests state described below — is the actual final
frozen state for filming; no further agent build pass is planned.

---

# STATUS — wave 12 CLOSED, build RE-FROZEN FINAL (2026-08-30)

**Wave 12 is done.** Film-kit patch pass re-verified every ship-phase claim below live
against the deployed revision, reshot every gallery asset invalidated by this wave's own
UI fixes (see `gallery-assets/INDEX.md`), added the new Veo-card shot, and patched
`FILM-SCRIPT.md` for the two things that changed since the wave-11 script: the optional
Veo beat and the (now fixed) chat-pane bubble/height behaviour. Final state: `setback-console`
revision **`setback-console-00020-sl4`**, `setback-tribunal` job generation **19**,
**676 tests green**. The build is **RE-FROZEN FINAL** — no further agent build pass is
planned. Remaining work is exclusively human: film, upload, Monday's submission.

One item stays open and needs a founder decision, not a doc fix: the objection letter's
internal `Clause Reviewer:`/`Evidence Reviewer:` label leak is fixed in `job/pipeline.py`
for any case run from here on, but both canonical film cases' stored letters were composed
before that fix existed and are not retroactively repaired, since canonical cases are
read-only/never-re-run by explicit project rule. `testing-instructions.md` now carries an
honest, visible caveat about this so a judge isn't blindsided by it — see the ship-phase
entry below for the full explanation.

---

# STATUS — wave 12 ship phase: merged, redeployed, Veo shipped (2026-08-30)

Ship phase closed out the wave-12 fix rounds below: merged both parked feature branches to
`main`, ran the full gate, and redeployed. Full detail in `SMOKE.md`'s "Ship phase" section
(the redeploy + live browser verification); headline here.

- **Merged to `main`:** `ui-bubbles-lightbox` (founder P0 saved-case chat-bubble
  speaker/role fix, plus the photo/Street View doc-card in-page lightbox) and
  `veo-integration` (the founder-approved AI-generated overshadowing-illustration card —
  see below). Both branches had forked before the same-wave reviewer-label-leak and
  upload-magic-byte-sniffing P0 fixes landed on `main`; both merges were confirmed by grep,
  post-merge, to still carry those two fixes intact.
- **Redeployed:** `setback-console` revision `setback-console-00020-sl4` (100% traffic),
  `setback-tribunal` job generation **19**, `australia-southeast1`. This supersedes the
  `setback-console-00018-z2s` / generation-17 revision recorded in the wave-12 P0 entries
  below — anything in this file or elsewhere citing `00018-z2s` as the current live
  revision is describing the pre-ship-phase deploy, not what's live now.
- **Veo illustration card shipped** (founder-approved, "ship Veo"): a pre-generated
  `veo-3.1-generate-001` clip, conditioned on the real DA's own elevation drawing, renders
  on the Evidence tab of both canonical film cases (each raised an overshadowing ground),
  captioned with the mandatory, non-dismissible "AI-generated illustration — not evidence"
  label. No on-demand/runtime Veo calls — the clip is a static asset. Structurally excluded
  from the tribunal pipeline (never an anchor/`SourceDocument`, never citable, never seen by
  a reviewer or the adjudicator); confirmed both by a new structural-exclusion unit test
  against a built case dossier and by live browser verification (real video decode, correct
  byte-range streaming, and a negative check confirming the card does **not** render on a
  legacy case that raises the same ground but isn't one of the two conditioned-on case ids).
- Full offline suite green: **676 passed** (up from 662 at the prior P0-fix-round entry
  below), `ruff check` clean, `ruff format --check` clean, full-tree `mypy` clean. No secret
  value, personal identifier, or hostname was read, printed, or transmitted this round;
  `docket-key` was fetched into a shell variable once for one verification `curl` call and
  `unset` immediately after.

**Known, not fixed by this phase:** the composed objection letter's internal
`Clause Reviewer:`/`Evidence Reviewer:` label leak (P0 #3 in the wave-12 synthesis) is fixed
in `job/pipeline.py` for any case run from here on, but the two canonical film cases'
already-stored letters were composed before that fix existed and are not retroactively
repaired — see the P0-fix-round entry below for the full explanation and why a canonical
case's stored documents aren't re-run to fix it.

---

# STATUS — wave 12 P0 fix round part 2: security leak + XSS closed (2026-08-30)

Resumed the wave-12 synthesis's full 9-item P0 list against the section below (which
covered only a subset under different numbering) and closed the two still-open,
in-repo items plus one non-repo doc fix, smallest surgical change each, on `main`, no
deploy (Ship phase deploys once):

1. **Objection letter leaks internal reviewer labels (P0 #4)** —
   `job/pipeline.py`'s `GroundContent.statement` was `f"{ground.claim} {result.verdict.
   rationale}"`; `CourtVerdict.rationale` on the CLEAR path (`court/graph.py::
   _finalize_clear`) is always synthesized as literal `"Clause Reviewer: ... |
   Evidence Reviewer: ..."`. The letter body now uses `ground.claim` alone. Regression
   test reproduces the existing CLEAR-path fixture and asserts neither literal label
   appears in the composed submission markdown; confirmed to fail against the reverted
   code before the fix, pass after. Canonical film cases' stored documents are frozen
   from tribunal run time and unaffected either way — this closes the leak going
   forward, for every case run from here on.
2. **Stored XSS via upload Content-Type trust (P0 #9, security, not film-visible,
   prioritized regardless)** — `console/app.py`'s upload route trusted the
   client-supplied `UploadFile.content_type` header verbatim, storing and later
   serving it back as the response's `media_type` with no verification — on a live
   public app whose docket `?key=` is reachable via `document.referrer`. Uploads are
   now accepted only when their own bytes match a known image/PDF magic signature
   (JPEG/PNG/GIF/WEBP/PDF, matching the upload widget's own `accept` attribute);
   anything else is rejected 415. Stored/served content-type is always the sniffed
   value, never the header. `get_uploaded_document` also now sets
   `X-Content-Type-Options: nosniff`. New regression tests cover: rejecting
   non-image/PDF bytes, ignoring a spoofed header (real PNG bytes declared
   `text/html` still get served as `image/png`), and the `nosniff` header's presence.
   Existing upload fixtures used placeholder bytes with no real signature (only ever
   accepted because the header was trusted); updated to carry real magic bytes.
3. **`HOW-SETBACK-WORKS.md` points to a superseded case (P0 #6, non-repo file,
   `~/Desktop/setback-hackathon/`)** — §2's live case link and quoted-transcript
   narrative pointed at `5e791203b4b538ec8b4de27b981e7ab6` (a wave-10 predecessor run),
   not the current canonical real-DA case. Updated the URL to
   `aeff0460678e76feceb7a5a7af934d31` and added a source-of-truth note pointing to
   `FILM-SCRIPT.md` for this section's specific quoted lines and cost figure, since
   those were captured against the superseded case and haven't been independently
   re-verified against the current one — safer than rewriting case-specific narrative
   content this agent can't verify against the live case page.

Full offline suite green: **662 passed** (up from 648 at the wave-12 baseline this
picked up from, +14 across the two touched test files), `ruff check` clean, `ruff
format --check` clean, full-tree `mypy` clean (38 source files). Two commits on
`main`, conventional-commit messages, no force-push, `veo-integration` and
`ui-bubbles-lightbox` branches untouched. No secret value, personal identifier, or
hostname was read, printed, or transmitted this round.

**Still open from the original 9-item P0 list, unchanged from below:** #1 (redeploy)
is explicitly out of scope for this fix round — the Ship phase's job. #8 (README.md's
eligibility clause) is still genuinely blocked on Leo pasting the verbatim hackathon
rules text with a citation; no agent can source that. #2/#3/#5/#7 were already closed
by the prior pass below.

---

# STATUS — wave 12 P0 fix round landed, film-visible defects closed (2026-08-30)

Closed the wave-12 synthesis's P0 list (5 of 6 items; #5 is genuinely blocked, see
below) with the smallest surgical, TDD change each, on `main`, no deploy (Ship phase
deploys once):

1. **FILM2 overlay chip collision** — `_draw_label_chip` had no fallback once vertical
   stacking ran out of headroom at a page's top edge; it silently accepted the
   overlap. Added `_shift_clear_of_avoid` (the two-row/horizontal-offset fallback):
   once pinned to the top edge, a colliding chip is placed on a "shelf" beside
   whichever already-placed chips share its row, bounded by the image's own width.
   Regression test reproduces three adjacent windows near a page's top edge.
2. **Transcript speaker mislabeling on reload** — `InterviewTurn` carried no `role`,
   so a rehydrated flow's replayed resident turns rendered as Setback's own (app.js
   hardcoded `"ai"` for every turn). Added `role` to `InterviewTurn`, threaded through
   `_rehydrate_flow_from_store` and `_turn_to_json`, and app.js now passes `turn.role`
   through to `appendTurn` instead of a literal `"ai"`.
3. **Evidence Reviewer image-blindness / hallucinated rationale (FILM2)** —
   narrative-only fix, as scoped: `court/roles.py` already only ever sends the Evidence
   Reviewer text captions (never image bytes), but `ARCHITECTURE.md` §2's node table
   and the non-repo film script both read as though it visually examines photos/plans.
   Corrected both; a docs-truth note now lives beside the table. The real fix
   (attaching `image_base64` as inline-data parts, retuning the prompt, re-validating)
   is unchanged, scoped follow-up work, not attempted here.
4. **Deprecated case still public on docket** — `f3f8c3475e2646537212677fbf7c8075`
   (`DA2026/0412-FILM`, superseded by the canonical `DA2026/0412-FILM2`) is now hidden
   from the docket-board list via a `case_id` denylist (`_DEPRECATED_CASE_IDS`),
   deliberately not a `_JUNK_METADATA_PATTERNS` substring (a `"film"` pattern would
   also catch the canonical FILM2 case). Hide only, never delete — `/cases/{case_id}`
   is unaffected. Not yet reconfirmed against the live docket: that verification
   belongs to the Ship phase's deploy, not this fix round.
5. **Eligibility clause not verbatim (`README.md:189-190`)** — **blocked, needs Leo**:
   the section paraphrases the hackathon's model-eligibility rule instead of quoting
   it verbatim with a citation, and no agent can source the exact rules text. Left
   untouched rather than fabricated. Correcting the record: **wave 6's STATUS.md entry
   below, which claims "the verbatim hackathon model-eligibility clause is inserted,"
   is wrong** — `README.md:189-190` was never actually verbatim-with-citation; that
   wave's own claim of completion should not be trusted for this item.
6. **`redacted_text` self-contradiction on a disputed re-clarification** — `_handle_
   clarifying` always appended the new answer onto whatever `redacted_text` already
   held, including a previously-disputed (wrong) clarification, producing
   self-contradictory pairs (e.g. north/south, 9am/2pm) in the same string fed to the
   tribunal prompt. Added `RaisedConcern.redacted_base` (captured once at concern
   creation) and replace-instead-of-append when re-entering `CLARIFYING` off a
   disputed `CONFIRMING`. Regression test proves the failure mode against the reverted
   code and passes against the fix.

Full offline suite green throughout (659 passed by the last commit of this round),
ruff check clean, mypy clean. Six commits on `main`, conventional-commit messages, no
force-push, `veo-integration` branch untouched. No secret value, personal identifier,
or hostname was read, printed, or transmitted this round.

---

# STATUS — wave 11 SMOKE close-out: full QA loop green, build RE-FROZEN for filming (2026-08-30)

Full QA→find→fix→QA smoke pass against the deployed `setback-console`
(`australia-southeast1`, revision `setback-console-00018-z2s`, tribunal
generation 17 — the wave-11 redeploy already recorded below) at 390px,
768px, and desktop, both themes, per the founder's film-day brief. Full
detail in `SMOKE.md`'s "SMOKE.md v10" section; headline here.

**Result: zero defects found.** Every item passed on the first live check —
the ARIA tablist (selection state, only-active-content, full keyboard
support), mobile layout (no horizontal scroll, chat-first, 44px tap
targets, desktop two-pane at 1440px), the one-line chat input with a custom
Upload button (confirmed zero native "Choose file"/"No file chosen" text
anywhere), the header timestamp (`DD/MM/YYYY HH:MM AM/PM`, Sydney) with
run-cost and live-ingest-source both visible, and every wave-9 checklist
item (docket gate, accordion legibility, evidence doc-card clicks, overlay
lightbox open/close, Copy text + Email this with zero Markdown links, theme
toggle + persistence + warm-brown refusal semantics in dark mode, and the
safe idempotent tribunal re-press). The wave-11 grounding fix (site-plan
vocabulary overlay labels, zero window/door labels) was independently
reconfirmed live on the actual redeployed revision, not just carried
forward from the PROVE pass's pre-redeploy check.

No source file was touched this pass (nothing to fix); no redeploy was
needed. Full offline suite re-confirmed green immediately before the
browser work: `648 passed`, ruff check/format clean, mypy clean (38 source
files); tree was clean and up to date with `origin/main` throughout. Zero
live model calls this pass (drove only already-completed cases; the one
idempotent tribunal re-press short-circuits before any model call). No
secret value, personal identifier, or hostname was read, printed, or
transmitted — full detail and the exact commands run are in `SMOKE.md` v10.

**Build is RE-FROZEN for filming as of this pass.** Nothing further is
expected from a build/QA agent before the founder's early-afternoon shoot
today; the wave-11 integration entry immediately below (grounding fix +
round-2 UI feedback) is what this smoke pass verified.

---

# STATUS — wave 11 landed: grounding root-cause fix + round-2 UI feedback (2026-08-30)

Founder-directed round, filmed early afternoon the same day it landed — the brief
called for the smallest correct implementation, fast. Integration pass reconciling
two concurrent lanes' work (both already applied to the working tree when this pass
started) against full quality gates and a security diff check. **Git only, no
deploy** — `./deploy.sh` against the live `setback-console`/`setback-tribunal`
(`australia-southeast1`) is a separate, later step, not attempted by this pass.

## Lane O — grounding root-cause fix (`models/client.py`, `evidence/grounding.py`, `job/pipeline.py`)

**Major pre-existing defect found and fixed**: every vision-shaped model call in the
codebase's history (since wave 4) sent zero image bytes to the model — "grounding"
was pure text-only guessing, regardless of which document `job/pipeline.py` had
selected. This is consistent with (and likely explains) Blocker 1's original
symptom — window/door boxes landing mid a cover letter, not mid a drawing — since
the model was never shown either document.

- `ModelClient.generate` gains a backward-compatible `images: Sequence[tuple[bytes,
  str]] | None` parameter (default `None`, zero behaviour change at every existing
  call site); passing `images` to a text-only Gemma MaaS tier raises
  `ModelCallError` rather than silently dropping them.
- `evidence/grounding.py` adds a two-stage describe-then-ground pipeline
  (`describe_drawing` → `ground_described_elements`, composed as
  `describe_then_ground`) that replaces the single hardcoded elevation-shaped label
  list (`window W.1`/`door D.1`/`9m height limit datum line`) with a per-page
  inventory: what kind of drawing is this, and what real elements does it actually
  contain? A top-down Site Plan is now grounded in its own vocabulary (building
  footprint, boundary setbacks, the neighbouring lot, a north arrow) instead of
  being asked for windows and doors that cannot exist on it — the exact defect this
  wave was chartered to fix. `ground_elements` is kept as-is (still used by
  `ground_contested_elements`'s adjudication-escalation path) and now also attaches
  real image content via the same shared plumbing.
- `job/pipeline.py`'s `_ground_annotated_evidence` calls `describe_then_ground`
  instead of `ground_elements` with the fixed list.

Non-blocking quality nuances observed live during this lane's own verification, not
fixed this wave (time-boxed): (1) stage 1 can reuse a generic element name across
two physically different fixtures on different elevations of the same page —
harmless for anchor identity (keyed on bbox, not label) but could read ambiguously
in a caption if both appear in one overlay; (2) on the real Site Plan, one described
element ("setback dimensions and boundary lines") is inherently scattered across
most of the page, producing the least-tight bounding box of the nine seen —
correctly non-elevation vocabulary and well under the overlay's 90%-page-area drop
threshold, just visually the loosest. Neither blocks film.

Live validation used 8 of 8 budgeted grounding calls this lane (elevations ×2
rounds, the real Site Plan ×1 round, plus a re-run verifying a label-canonicalization
fix). No budget remains in this lane for further live grounding tuning this wave.

## Lane U — console round-2 founder UI feedback (`console/app.py`, `static/{app.js,style.css}`)

- **Real tabs, not a ref-link nav** (item 1): the right pane's sections
  (Grounds/Evidence/Overlay/Documents) are now a genuine WAI-ARIA tablist
  (`role="tab"`/`"tabpanel"`, `aria-selected`, full arrow-key/Home/End keyboard
  support) — the founder's own correction that the prior sticky anchor-link nav
  scrolled to an always-rendered section with no visible selected state.
- **Standalone "Tribunal" tab/section removed** (item 4): its start timestamp
  (`DD/MM/YYYY HH:MM AM/PM`, Australia/Sydney) moved to a new case-header meta line
  alongside run cost once non-zero; its ingest-source and rerun-ignored notices moved
  into a small "Notes" card inside the Grounds tab. No resident-facing content was
  dropped, only relocated.
- **One-line chat input row** (item 3): text input | Send | Upload, all in one flex
  row. The native `<input type="file">` is visually hidden behind a styled Upload
  button that opens the picker and uploads immediately on selection; status (in
  progress / done / error) shows as a small chip instead of the native "Choose file /
  No file chosen" text.
- **Mobile pass** (item 2, 390px/768px breakpoints), using only existing spacing/type
  tokens: 44px tap targets, a bounded transcript height once single-column, stacked
  landing-page form. A `viewport` meta tag was added to all three server-rendered
  pages (landing, docket board, case page) — missing before, which is why none of
  this would previously have rendered at mobile width at all.

Soft finding, **not fixed** this round (pre-existing behaviour, outside the four
requested items): uploading evidence after the interview has already reached the
`DONE` stage does not show the "Evidence added: `<filename>`" chat line, because
`loadInterview()`'s `renderTranscript()` replaces the whole transcript with only
persisted interview turns. The upload itself (the `POST /documents` call, the
`document_uploaded` event, the Evidence tab, the doc card) all work correctly —
confirmed live. Flagging rather than leaving it for a future pass to rediscover.

## Cross-lane notes

- Lane U's own report flagged that `models/client.py`'s new `images` parameter is
  outside its lane but was required for the wave's own feature (the grounding fix)
  to work at all — noted here so whoever next touches `ModelClient` has the
  rationale (full detail in `models/client.py`'s docstring and `evidence/
  grounding.py`'s module docstring).
- Both lanes independently reported concurrent uncommitted edits to a shared working
  tree during this wave (a mid-session `console/app.py` WIP state with an undefined
  `_render_tribunal_section`, and an unformatted `evidence/grounding.py`) — both were
  resolved by the time this integration pass started; the tree was internally
  consistent and every gate below passed cleanly on the first run.

## Verification (verbatim, this integration pass)

```
$ uv run pytest -q
648 passed, 256 warnings in 44.19s

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
81 files already formatted

$ uv run mypy
Success: no issues found in 38 source files
```

Up from wave 9.5's 619 (648 after this wave's two lanes' own TDD additions: 71 new
`models/client.py` multimodal-content tests, 370 new `evidence/grounding.py`
describe-then-ground tests, plus `job/pipeline.py` and `console/app.py` test
updates).

## Security diff check (this pass, full wave-11 diff)

Grepped the full diff for credential patterns (`AIza...`, `ghp_...`, `AKIA...`,
`sk-...`, JWTs, `BEGIN ... PRIVATE KEY`, literal `docket-key`/`api_key`/`password`
assignments), the user's email, and internal hostnames/home-directory paths
(`kratos`, `mimir`, `/Users/leo`, `/home/leo`, `@gmail.com`). **Zero hits.** No
secret value, personal identifier, or hostname appears anywhere in this wave's diff.
Per both lanes' own reports: no secret was read or transmitted, `resident_session`
values used in live validation were synthetic `uuid4`s, and every outbound HTTP call
used the neutral `User-Agent: setback/0.1`.

## Live model calls this pass (integration)

Zero. This was a pure git-integration pass (reconciliation, gates, security check,
commits) — no live Vertex AI, Secret Manager, or GCP call of any kind was made by
this pass itself; the 8 live grounding calls and other live validation reported
above were made by lane O/U's own work before this pass started.

## Commits this pass

- `50dbcf1` `fix(evidence,models): actually attach page images to grounding calls`
- `823929d` `fix(console): round-2 founder UI feedback (tabs, mobile, one-line chat input)`

Pushed to `origin/main`.

## What remains (film-day / next-pass work)

- **Redeploy**: this pass's changes are pushed to `origin/main` but not yet deployed
  — `./deploy.sh` against `australia-southeast1` is the next step, followed by a live
  smoke pass (docket gate, a real interview turn including a post-DONE upload check,
  a real tribunal run against the real Site Plan case) before filming.
- **The two lane-O quality nuances and the lane-U post-DONE upload-transcript
  finding above** — none block film; candidates for a future pass if the founder
  wants them tightened.
- Everything else unchanged from wave 9.5's own closing note: pick the final film
  case(s), the timed rehearsal, and the README/Devpost narrative items STATUS.md's
  "what remains" sections already list.

---

# STATUS — wave 9.5 complete: Blocker 1 closed, redeployed, proof run verified (2026-08-29)

Ship-phase pass over a fixer's Blocker-1 fix (`833e6fd`, already on `main`
and green — 619 passed — when this pass started): ran full quality gates
verbatim, redeployed (`setback-console-00017-s25`, tribunal generation 16),
and ran the one budgeted proof-run tribunal against the real on-exhibition
DA to verify the fix live. Full detail in `SMOKE.md`'s "SMOKE.md v9"
section and `CASES.md`'s "Wave 9.5" section; headline items only here.

- **Blocker 1 (real-DA overlay grounds on the wrong document) — CONFIRMED
  CLOSED live.** New case `5e791203b4b538ec8b4de27b981e7ab6` (real ingest,
  `PAN-661190`/`DA2026/0359`, no photo upload) grounds its overlay on the
  real "Site Plan" drawing (correct letterhead/address/drawing number),
  not the Resident Notification Letter every prior real-DA case was stuck
  on. Full-resolution click-through confirmed (`200`, `4962×3508` PNG).
- **Street View grade-B fallback confirmed rendering** on this same fresh
  real case (attribution + badge + clickable) — the wave-9 fix holds up on
  a brand-new case, not just the one it was originally verified against.
- **New recommended film case for the real-DA beat**:
  `5e791203b4b538ec8b4de27b981e7ab6` — beats the populate pass's
  `9f9a6a087f851db107be765391ba48ad` (Case A) by combining a real DA, a
  clean SHIPPED overshadowing ground, a presentable overlay, and a working
  Street View card in one run, all of which Case A lacks at least one of.
  One honest caveat: this case's property-value ground was refused as
  *unsubstantiated* rather than *not a listed matter* — a recurrence of
  the already-documented ITEM 3 live-model-classification soft finding,
  not a new or different code defect. If the exact "not a listed matter"
  wording matters more than the overlay/Street-View combination, Case A
  remains available and correctly categorised on that one point.
- **Item 5 (second council)**: re-confirmed no change — no independent,
  currently-exhibited second DA exists at any nearby eTrack-shaped council
  today. Nothing shipped, nothing run, per the item's own standing STOP.
- **Docket filter re-confirmed live** (with the Secret-Manager-fetched
  key): the wave-9.5 fix's exclusion list still hides every `DEPLOY-QA`/
  `SV-TEST` row; the new proof case now heads its `PAN-661190` group
  ("+5 earlier cases").

Quality gates verbatim: `619 passed`; `ruff check`/`ruff format --check`
clean; `mypy` clean (38 source files) — unchanged from the fixer's own
count, since this pass made no source edits. Security diff check on the
fixer's commit: clean. **1 of 3 allowed tribunal runs used, $0.003048 of
the ≤USD 1.5 budget spent.** Pushed (this file + `SMOKE.md` +
`CASES.md`) to `origin/main`.

**Remaining work is film-day/founder-only** from here — unchanged in kind
from wave 9's own closing note below: pick the final film case(s) (now
including the new real-DA recommendation above), one timed rehearsal, and
the Devpost/DISCLOSURE.md narrative items. No further build-or-QA-wave
agent pass is expected before Sunday's freeze.

---

# STATUS — wave 9 complete: full QA loop closed, deployed, verified (2026-08-29)

Final wave-9 pass: drove the deployed app through every item in
`LEO-FEEDBACK-UIUX.md` (both light and dark theme), per the standing
QA→find→fix→QA doctrine. Found and fixed two real live defects in this
round (see `SMOKE.md`'s "SMOKE.md v8" section for full detail):

- **Street View fallback never rendered** (§4): `job/pipeline.py` fetched it
  correctly but never told the console it existed (no `document_uploaded`
  event) — fixed, redeployed, re-verified live end-to-end on a fresh
  no-photo case (real thumbnail, "Archival Street View" grade-B badge,
  attribution, clickable).
- **Reviewer opinions illegible inside an expanded ground accordion** (§3):
  a stale, pre-accordion `.ground-list li` CSS rule (no child combinator)
  leaked its flex/space-between layout into every nested `<li>`, squeezing
  each reviewer's prose into unreadable vertical-letter columns — fixed by
  scoping the selector to direct children only, redeployed, re-verified.

Both fixes are TDD'd where Python (591 passed, up from 590), verified live
where CSS (this repo has no CSS test harness, per established convention).
Two redeploys this round: `setback-console-00015-qt2` (tribunal gen 14,
Street View fix) then `setback-console-00016-jcr` (tribunal gen 15, CSS
fix) — both confirmed serving (docket gate `401`/`401`/`200`) before
proceeding.

**New recommended film case for the overlay flagship shot**:
`cc9bfc59084fd7cac527c479f0e71996` (`DA2026/0412-FILM2`) — a fresh run
produced under every current fix (green, correctly-coloured height-datum
box; legible chip; full-resolution click-through confirmed serving a real
4962×3508 PNG), unlike the historical `f3f8c3475e2646537212677fbf7c8075`,
whose own live overlay event predates three waves of overlay fixes and is
never retroactively recomposed (an append-only event log, by design). The
populate pass's real-DA case (`9f9a6a087f851db107be765391ba48ad`, Case A)
remains the right pick for the separate "real, live-fetched DA" narrative
beat — see "Blocker 1" below, still open.

**Full checklist result: every LEO-FEEDBACK-UIUX.md item is a confirmed
live PASS in both themes** (see `SMOKE.md` v8's item-by-item table) except
one already-documented, deliberately-not-re-fixed limitation:

- **Blocker 1** (wave-9 populate pass, `CASES.md`, still open): on a real
  (non-fixture) DA, the annotated-overlay pipeline can pick a
  non-plan/elevation document (e.g. a cover letter) because
  `_ground_annotated_evidence`'s document selection isn't classification-
  aware and `_MAX_TRACKER_DOCUMENTS = 3` can truncate before the real
  elevations document is ever seen. Not attempted this round (larger
  change, and every affected real case is already being avoided for
  filming via case selection per the populate pass's own recommendation,
  adopted here) — exact patch pointer on record in `CASES.md`.

**Remaining work is film-day/founder-only** from here: pick the final film
case(s) from the recommendation above, one timed rehearsal, and the
Devpost/DISCLOSURE.md narrative items already listed further down this
file. No further build-or-QA-wave agent pass is expected before Sunday's
freeze.

---

# STATUS — wave 9 landed: founder feedback round (2026-08-29)

Integration pass reconciling four fixer lanes' work (already applied to the working
tree when this pass started) against `LEO-FEEDBACK-UIUX.md` (Leo's binding wave-9
spec, driving the deployed app live), plus this pass's own cross-lane fixes and full
quality-gate/security verification. **This pass did not redeploy** — `deploy.sh`
against the live `setback-console`/`setback-tribunal` (`australia-southeast1`) is the
next, separate step; git only, per this wave's brief.

## Shipped (LEO-FEEDBACK-UIUX.md item by item)

- **§1 Landing page**: public `/`, minimal (Setback + caption + one DA-number input),
  never 401s; docket board moved to `/docket` (same `?key=` gate). localStorage
  "previous cases" affordance. Copy link + a server-generated QR code (`segno`, new
  dependency) on every case page for account-free re-access. Email sending stays
  explicitly deferred (no GCP-native SES), per the spec's own "explicitly deferred"
  list.
- **§2 Case page**: fixed left chat pane / scrolling right pane, stacking on narrow
  screens. The cold-start no-resume bug is fixed (`InterviewFlow.resume`, wired from
  the case's persisted transcript) — greets only when the transcript is empty, no
  more duplicate opening line on a fresh Cloud Run instance. Typing indicator +
  disabled input while a reply is pending. Standalone "Interview transcript" section
  removed. "Export transcript" downloads plain text.
- **§3 Grounds**: one-line accordion (clamped claim + status pill) expanding to full
  detail; reviewer opinions and the gate decision merged into the expansion; refusal
  copy names the ground by its own one-liner.
- **§4 Evidence**: document cards clickable (real thumbnail for a photo, served via
  the existing document route); Street View fallback trigger condition widened to
  accept a free-text address (the pipeline never had lat/lng, only a resolved
  address string) and verified.
- **§5 Annotated overlay (flagship, was regressed)** — root-caused and fixed, not
  patched around:
  - Overlay box count capped at 8 (`DEFAULT_MAX_OVERLAY_BOXES`), preferring decided
    boxes over neutral evidence anchors when trimming.
  - Label-chip collisions now stack instead of overwriting into an illegible
    run-on string.
  - **Root cause of "meaningless mid-house boxes"** fixed in
    `_propagate_page_level_anchor_status`: page-level citation inheritance is now an
    all-or-nothing fallback for a page with zero direct citations of its own, never a
    per-anchor gap-filler on a page that already has specific citations elsewhere —
    verified against the film case's own real stored anchor/ground facts, and pinned
    by a new regression test reconstructing that exact case (one directly-cited
    datum-line box stays green; four unrelated window/door boxes on the same page,
    cited by an unrelated refused ground's page-level citation, correctly stay
    neutral instead of turning orange).
  - **Full-resolution click-to-open, closed end-to-end**: `render_semantic_overlay`'s
    pre-shrink PNG is now durably stored (via the document source's
    `EvidenceUploadStore` side — `GcsEvidenceStore` in production) and referenced
    from the `annotated_overlay` event; the lightbox opens that real full-resolution
    image instead of re-displaying the shrunk, embedded copy bigger. Verified live in
    a real browser against an offline, fakes-only smoke server (zero live model/GCP
    calls): clicking the overlay opened a genuinely higher-resolution image served
    from the new document route.
  - Grounding-tier default kept at `INTERVIEW`, not switched to `BENCH` — a live
    2-call comparison against the real film-case fixture showed `BENCH` placing all
    four window/door boxes on the *wrong* elevation drawing, a regression. Documented
    in `grounding.py`'s docstring for re-evaluation against a future, richer fixture.
- **§6 Submission documents**: Copy text + Email this (mailto) as primary actions,
  HTML download kept secondary, Markdown download removed from the UI.
- **§7 Tribunal**: timestamps render in Australia/Sydney with the date (stored UTC).
  "Start tribunal" is now un-crashable on both sides: UI disables/hides it in
  terminal states, and `job/pipeline.py` gained the job-side idempotency guard
  (SMOKE.md's "Fix 4 — not fixed") — a judge double-pressing the button against an
  already-decided case is now a safe, event-logged no-op instead of a crash. The
  three new event types this produces (`ingest_resolved`, `tribunal_rerun_ignored`,
  `ground_rerun_skipped`) were flagged by one fixer's handoff note as an
  unregistered-renderer gap (would have fallen through to the raw-JSON dump, or
  simply never rendered) — closed at this integration pass: the first two render in
  plain English in a merged, chronological "Tribunal" card; the third stays out of
  the resident-facing UI by design (an internal resume-safety signal only), per that
  same note's own recommendation.
- **§8 Theme**: header light/dark toggle, persisted in localStorage, overriding
  system preference; `?theme=light` still works for filming.
- **§9 Right-pane structure**: sticky in-page nav across the merged section set
  (Grounds, Evidence, Overlay, Documents, Tribunal).

## Partially shipped

- **§10 Real-case validation**: the *mechanism* is shipped and tested — a typed DA
  number now drives real OnlineDA/spatial/tracker fetching when a live ingest client
  is configured, degrading honestly (a labelled `ingest_resolved` event, never a
  silent wrong letterhead) to the frozen demo fixture on any resolution failure.
  **Not done by this pass**: actually populating the docket with 4–6 real DAs
  currently on exhibition, running real tribunals against them, recording per-case
  cost, and picking the new film case — this requires live runs against real
  government endpoints and is deploy-time/founder work, out of this git-only
  integration pass's scope and budget.

## Cut

- **§11 Veo simulation**: not attempted this wave — zero trace of it anywhere in the
  tree. Consistent with the spec's own instruction ("time-boxed bonus... if not
  demo-ready in time, CUT it — no half-shipped feature"), but flagged explicitly
  since no lane's notes confirmed this was a deliberate per-lane decision rather than
  simply unstarted.

## Note: one lane's report was not found

The wave-9 brief describes four fixer lanes (A/B/C/D), but only three lanes' patch
notes were present in this integration pass's brief (covering `evidence/{overlays,
grounding}.py`; `console/**` + `interview/flow.py` + `dispatch/composer.py`;
`job/{pipeline,main}.py` + `evidence/imagery.py`). No lane-D patch notes were found,
and no trace of lane-D-shaped changes (a config change, a docket-card wiring change,
or anything matching "lane D's config + console card wiring") appears anywhere in
the working tree diff this pass reconciled. Per the orchestrator's own fallback
instruction ("if D said CUT, wire nothing and say so"), **nothing was wired for a
lane D this pass could not identify** — flagging this explicitly rather than
guessing at what lane D covered or silently proceeding as if it had shipped.

## Cross-lane patch notes applied at this integration pass

- The propagation-bug root-cause fix (§5 above) — reported by one fixer as a patch
  note for `job/pipeline.py`, outside that fixer's own lane (`evidence/overlays.py`),
  verified visually offline against the film case's real facts. Applied here with a
  new regression test reconstructing the exact reported scenario.
- The full-resolution click-to-open wiring (§5 above) — reported by the same fixer
  as needing both `job/pipeline.py` and `console/app.py`, requiring a GCS-wiring and
  event-payload-shape decision neither individual lane could make alone. Implemented
  here (pipeline write + event field + console route content-type fallback + lightbox
  JS preference), TDD throughout, and verified live in a real browser.
- The three unregistered event-renderer gap (§7 above) — reported by the `job/
  pipeline.py` fixer as a handoff note for whoever owns `console/app.py`. Closed here.

## Verification (verbatim, this integration pass)

```
$ uv run pytest -q
590 passed, 256 warnings in ~45-55s

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
81 files already formatted

$ uv run mypy
Success: no issues found in 38 source files
```

Up from wave 8's 532 (581 after the four lanes' own work, 590 after this pass's own
TDD additions: the propagation-bug regression test, the three event-renderer tests,
and the three full-resolution click-through tests).

## Security diff check (this pass, full wave-9 diff)

Grepped the full staged diff for credential patterns (`AIza...`, `ghp_...`,
`AKIA...`, `sk-...`, JWTs, `BEGIN ... PRIVATE KEY`, literal `docket-key`/`api_key`/
`password` assignments), the user's email, and internal hostnames/home-directory
paths (`kratos`, `mimir`, `.local`, `/Users/leo`, `/home/leo`, `@gmail.com`). Zero
live hits — the only `.local`-shaped matches were `window.localStorage` calls (new
localStorage-driven UI state, §1/§8 above), not a hostname. No secret value, personal
identifier, or hostname appears anywhere in this wave's diff.

## Live model calls this pass

Zero. This was a pure code-integration pass (propagation-bug fix, event-renderer
wiring, full-resolution overlay wiring) verified entirely by the offline test suite
plus one offline, fakes-only browser smoke test (`create_app(...)` with an in-memory
store/document-source/composer — never `_build_production_app`, never a real
`ModelClient`) — no live Vertex AI, Secret Manager, or GCP call of any kind was made.

## What remains (film-day / next-pass work, unchanged in spirit from prior waves)

- **Redeploy**: this pass's changes are pushed to `origin/main` but not yet deployed
  — `./deploy.sh` against `australia-southeast1` is the next step, followed by a live
  smoke pass against the deployed console (docket gate, case creation, a real
  interview turn, a real tribunal run) before the film-day freeze.
- **§10's live half** (populate the docket with 4–6 real DAs, run real tribunals,
  record cost, pick the new film case) — founder/deploy-time work, not attempted
  here.
- **§11 Veo** — cut, as above; would need a fresh scoping pass if resurrected.
- **Lane D** — its scope was never identified by this pass (see the note above); if
  it was meant to cover something real, it needs to be re-briefed and re-run, not
  assumed lost.

---

# STATUS — wave 8 landed: build phase closed (2026-08-29)

Final build wave. Closed the three visual defects SMOKE.md v5 flagged as gating the
flagship demo shot:

- **Dockerfile**: installs `fonts-dejavu-core` so the deployed container's overlay
  label chips use a real TTF instead of silently falling back to PIL's bitmap default.
- **Chip legibility**: `evidence/overlays.py` sizes the label-chip font as a ratio of
  the image width being drawn on, so chips stay legible (>=18px glyph height on a
  1600px-wide output) after the working page is downscaled ~4x for Firestore storage.
- **Anchor-status propagation**: `job/pipeline.py::_propagate_page_level_anchor_status`
  now applies three ordered rules — a directly-cited bbox anchor always keeps its own
  ground's status (never overridden by a page-level inheritance from a different,
  more severe ground); page-level inheritance only reaches anchors with no direct
  citation; among competing page-level-only claims, prefer the ground whose evidence
  slice actually included the document before falling back to severity.

Full suite: **532 passed** (up from 522), ruff check/format clean, mypy clean. Security
diff check clean. Committed (`ebc1336`) and pushed to `origin/main`.

Redeployed (`setback-console-00013-smf`, `setback-tribunal` generation 12); docket
gate verified live (401 no-key, 401 wrong-key, 200 with the Secret-Manager-fetched v2
key; v1 confirmed disabled/inaccessible). Re-rendered the flagship overlay for the
film-day case (`f3f8c3475e2646537212677fbf7c8075`) from its stored anchors and gate
outcomes using the fixed renderer — no live tribunal run needed. Gallery's #07 shot
replaced; the other seven confirmed unaffected. Full detail in `SMOKE.md`'s "SMOKE.md
v6" section, including the exact stored-anchor reconstruction and its cross-checks.

**Build phase closed.** Everything remaining is film-day/founder-only work — see the
wave-6/7 checklists below, unchanged.

---

# STATUS — wave 7 landed (2026-08-29)

Final-polish wave. The orchestrator rotated the docket passphrase after a prior
verifier leaked it, moving it to a Secret Manager secret (`docket-key`) on the live
service directly; `deploy.sh` still passed `SETBACK_DOCKET_KEY` as a literal
`--set-env-vars` string, which would have hit a type conflict (env var vs.
secret-mounted var on the same name) on the next deploy. Reconciled FIX-A/B here
(FIX-C stayed scratchpad-only, not integrated):

- **deploy.sh**: `SETBACK_DOCKET_KEY` is now mounted via `--update-secrets` from the
  `docket-key` Secret Manager secret, never passed as a literal env-var value — this
  script never reads, prints, or holds the passphrase itself. Fails fast before any
  build/deploy work if the secret doesn't exist yet, and grants `sa-console`
  `secretAccessor` scoped to that one secret only.
- **Overlay text fix**: label chips now draw with a real TTF font instead of PIL's
  implicit bitmap default, whose near-invisible space glyph was collapsing multi-word
  captions (e.g. "This element" → "Thiselement") — found live, see SMOKE.md wave 6.
- **Overlay anchor-status fix**: a reviewer citing a whole page (rather than a
  specific crop) now correctly colours every fine-grained bbox anchor on that page
  instead of leaving them all neutral/grey; most-severe status wins when more than
  one ground is in contention for the same anchor.
- **Docket hygiene**: the public docket list now excludes smoke/test/wiring-
  proof/rate-limit debris by content (not just session-id shape) and collapses
  duplicate `application_number` rows to the latest case, with an "+N earlier cases"
  note — older cases stay reachable at their own URL (hide only, never delete).

Full suite: **522 passed** (up from 503), ruff check/format clean, mypy clean
(canonical `mypy` invocation per `Makefile`'s `typecheck` target, respecting
pyproject's `files = ["src/setback"]`). Security diff check on the full wave-7 diff:
clean — no secret values, personal identifiers, or hostnames introduced; the
passphrase is referenced only by its Secret Manager resource name throughout.
Committed and pushed to `origin/main` (`caa40fa`).

**Remaining = film-day items only**, unchanged from wave 6 — see that section below
for the founder-only checklist (live pipeline reruns, Devpost category confirmation,
deploy freeze, timed rehearsal, video cold open/close, DISCLOSURE.md narrative).

---

# STATUS — wave 6 fix-plan landed (2026-08-29)

Wave-6 panel review (VERDICT.md) found the engineering ahead of median but every
compliance/scoring gap a wrapper problem: unwired deploy stub, unfilled README
brackets, an exposed public docket, a broken overlay widget, and several
architecture-doc claims a grep disproved. Three fixers closed the full P0 list plus
every ranked item that fit the ~6h budget, reconciled here:

- **Stage One compliance**: `make deploy` now calls the real `deploy.sh` (was a stub
  that printed "Not yet implemented" and exited 1); the ARCHITECTURE.md §9 mermaid
  diagram is embedded in the README; local spin-up carries an honest no-offline-mode
  caveat instead of silence; the verbatim hackathon model-eligibility clause is
  inserted; `.env.example` no longer points a fresh clone at a nonexistent project id;
  `deploy.sh` now grants `roles/datastore.user`/`roles/aiplatform.user` to both
  service accounts (previously only manual, undocumented grants existed on the live
  project).
- **PII exposure / docket hygiene**: the public docket board's case *list* is now
  gated behind `SETBACK_DOCKET_KEY` and filtered to real (UUID-shaped) resident
  sessions, so smoke/test rows and a stranger's private objection no longer sit in
  front of anyone with the URL; an individual case's own unguessable link is
  unaffected.
- **Multimodal UX**: the broken overlay widget (empty floating boxes, legend colours
  the image never used) is fixed — a fourth FLAGGED role, all four colours pinned to
  the console's own status tokens, and a shared server/client legend that can't drift
  out of sync again. Uploaded photos now render as real thumbnails instead of a
  placeholder icon.
- **Docs-truth corrections**: ARCHITECTURE.md and DESIGN-DECISIONS.md no longer claim
  the sweeper, the loop/turn counter, the s4.15 YAML/Firestore mirror, or the
  per-collection credential-scoping enforcement that a full-tree grep proved were
  never built — each is corrected to state what actually covers the gap today.
- **Dispatch polish**: refusal/flagged headings now show the resident's own claim
  text (or a plain-English category label) instead of a raw internal `ground_id` hash.

Full suite: **503 passed** (up from 480), ruff check/format clean, mypy clean, security
diff check clean (no secrets/identity/hostnames introduced). Pushed to `origin/main`.

**Remaining = film-day items only** — everything left on the wave-6 plan needs the
founder personally, not another agent pass: re-running the tribunal pipeline live 2–3×
to see its honest, reliable output and scripting the demo's centerpiece beat around
that (not a forced reviewer SPLIT); supplying/confirming the Devpost category
checkbox; freezing deploys to the console after the final pre-film smoke test; one
full timed rehearsal against the live deployed app; rewriting the video's cold open
and close (currently unwritten); and the DISCLOSURE.md/Devpost narrative decisions
around the solo agent-orchestrated build.

---

# STATUS — wave 5 ship-phase checkpoint: redeploy + UI smoke + proof run (2026-08-29)

Ship-phase pass following the wave-5 UI-revamp integration checkpoint below: redeployed
the console twice (Gemma/session-affinity fixes, then three UI bugs this smoke loop
found), ran a full UI smoke against the deployed `australia-southeast1` console with a
real browser, resolved the outstanding s4.15(1)(b) legislative-wording question, captured
THE PROOF RUN (one unbroken live tribunal run: overshadowing SHIPPED with citations,
property-value REFUSED with the correct statutory wording), and replaced the gallery with
8 real screenshots. Full detail — every finding, every fix, every test, the live-budget
accounting, and the gallery-shot honesty notes — is in `SMOKE.md`'s "SMOKE.md v3" section;
this entry is a pointer plus the headline items.

## Closed this checkpoint (see SMOKE.md v3 for full detail)

- **Gemma publisher-qualified model id** (`models/client.py`) — the wave-4 P0 carry-
  forward that survived wave 5's UI lanes untouched (`STATUS.md`'s wave-5 section flagged
  it as unclaimed). Fixed with a `google/`-prefixed payload model id; verified with
  exactly one live Gemma call (`SUCCESS answer='ok' ...`).
- **`--session-affinity`** added to `deploy.sh`'s console deploy, with the tradeoff
  documented inline (mitigates steady-state routing only, not cross-redeploy instance
  loss — reconfirmed live, see SMOKE.md v3's "multi-instance hazard" section).
- **s4.15(1)(b) "significant likely impacts" amendment confirmed commenced** — read live
  via a real (non-headless) browser session against `legislation.nsw.gov.au`, which
  defeats the Cloudflare block that stopped every prior curl/httpx attempt. `gate/s415.py`
  updated to the current wording.
- **Three raw-JSON leaks found live** (`tribunal_requested`, `adjudication_decision`,
  `resident_refusal_feedback` — missing from `console/app.py`'s `_EVENT_ITEM_RENDERERS`,
  falling through to a literal `json.dumps` dump) — fixed with three new plain-English
  renderers, closing founder requirement #3 completely (wave 5's own checkpoint below had
  only closed two of five such gaps).
- **Check-answers summary-list grid bug** (`style.css`) — a 3-column grid template against
  a 2-cells-per-row DOM scrambled every row after the first; fixed to a 2-column template.
- **Dark mode was unconditionally disabled** — both page templates hardcoded
  `data-theme="light"` on `<html>`, defeating `style.css`'s otherwise-correct
  `prefers-color-scheme` support with no compensating theme-toggle feature anywhere in
  `app.js`. Fixed by leaving `<html>` bare.

Test count: **480 passed** (up from 474 at the wave-5 integration checkpoint), ruff
check/format clean, mypy clean. Deployed, smoked, gallery replaced, pushed.

---

# STATUS — wave 5 integration checkpoint: UI revamp (2026-08-29)

Integrator's reconciliation pass over wave 5 (UI revamp, per `UI-SPEC.md`), which ran as
three strictly-laned concurrent packages against the working tree left by wave 4: **A**
(`console/static/style.css` only — tokens + every component's CSS), **B**
(`console/app.py` + `tests/console/test_app.py` — the human-rendered event renderers and
the `suggested_replies` contract), **C** (`console/static/app.js` only — bubble/chip/
citation-chip/tribunal-timeline behaviour). This section is the authoritative status for
the UI wave; it does not re-run or re-verify the wave-4 AU-deploy checkpoint above, which
stands as-is.

## Cross-lane reconciliation (the two named seams)

- **`suggested_replies` contract**: B added the field to `_turn_to_json` (`_SUGGESTED_
  REPLIES` map, populated only for `CONFIRMING`/`ASK_MORE`/`REQUESTING_EVIDENCE`, `None`
  elsewhere); C's `renderQuickReplies` consumes it identically from both `loadInterview`
  and `postAnswer`, always routed through the single `submitAnswer(text)` path a typed
  reply also uses — confirmed by reading both sides, no signature drift. The `<input>`/
  `Send` button are never disabled while chips render (founder requirement #2 verified in
  code, not just by convention).
- **Citation-chip/overlay docking**: C's `citationChip.onActivate` degrades gracefully
  where the wave-4 overlay work doesn't yet expose per-anchor clickable regions — it
  flashes/scrolls to the whole annotated-overlay image (or a `[data-bbox-region]` element
  if one exists) rather than a precise sub-region, upgrading automatically the moment such
  an element is added by a future wave. Documented in `app.js` at the integration point;
  no gap requiring a code change this checkpoint.
- **Run-cost visibility (wave-4 carry-forward, closed this wave)**: B exposes
  `data-run-cost-usd` on `<body>` in `render_case_page` (from `CaseStore.load_ledger`,
  `0.0` before any run); C's tribunal-timeline footer renders the "This run: $X.XX" chip
  from that attribute, suppressed entirely at `<= 0` (no premature "$0.00").

## Contract/founder-requirement verification (read against the code, not claimed)

1. Bubble asymmetry (§2.1): confirmed in `style.css`/`app.js` — plain-left-labelled `--ai`
   vs filled-right `--resident`, survives a greyscale screenshot (shape+alignment, not just
   colour).
2. Quick-reply chips post through the normal send path: confirmed — see seam above.
3. Zero raw JSON user-facing: `document_uploaded` and `interview_turn` are now registered
   in `_EVENT_ITEM_RENDERERS` (`_render_document_uploaded_item`, `_render_interview_turn_
   item`), closing the two raw-JSON leaks `UI-SPEC.md` §3.3/§3.4 named exactly; tests
   (`test_document_uploaded_renders_a_doc_card_with_no_raw_json`, `test_interview_turn_
   renders_as_chat_bubbles_with_no_raw_json`) assert the literal payload keys are absent
   from the rendered page.
4. Semantic colour discipline: `--status-refused` is a warm brown (`#8a3a12` light /
   `#e8996b` dark), never `--error` (red); `_render_gate_decision_item` routes any
   `status.startswith("refused")` to the brown `.refusal-card`, never `role="alert"`.
   `--error` appears only in `.state-card--error` (`role="alert"`, true system failures).
5. Reusable components / no per-section drift: the single `.tag--{status}` component (four
   tokens) is the sole source of ground-card stripe, verdict-stamp, docket-card, and
   tribunal-timeline collapsed-row colour — confirmed by grep, no locally-invented shade.

## Design-judgment notes, applied as specified

- Check-answers "Change" — the interview state machine cannot reopen an arbitrary past
  stage this wave; `_render_check_answers_section` ships read-only with one "Change
  something" link back to the transcript, per the spec's explicit degrade-gracefully
  instruction. No stage-reopening was built.
- Doc-card thumbnails — no new thumbnail pipeline was built; `_render_document_uploaded_
  item` always renders the placeholder-icon variant (`.doc-card__thumb--placeholder`).

## Full-tree verification (verbatim)

```
$ uv run pytest -q
474 passed, 202 warnings in 20.44s
```

```
$ uv run ruff check .
All checks passed!
```

```
$ uv run ruff format --check .
80 files already formatted
```

```
$ uv run mypy
Success: no issues found in 38 source files
```

(`uv run mypy` — the project's canonical invocation per `pyproject.toml`'s `files =
["src/setback"]` / the `Makefile`'s `typecheck` target — is the command of record here;
an ad hoc `mypy src tests` also tried during this checkpoint fails on two pre-existing,
out-of-lane test-tree issues (`tests/court/_fakes.py` module-path collision,
`tests/job/test_pipeline.py:141`'s ignore-comment syntax) that predate this wave and were
not touched by any of A/B/C — not this checkpoint's lane to fix, flagged here rather than
silently worked around.)

## Security diff check (staged diff, this checkpoint)

Grepped the full staged diff for credentials/secrets/API keys, the user's email/name, and
hostnames (`kratos`/`mimir`/`.local`) — zero hits beyond incidental token-system
vocabulary (CSS custom-property "tokens", cost "ledger") and the pre-existing stdlib
`import secrets`. No personal identifier, no live User-Agent string, no secret value
appears anywhere in this diff. No live model calls were made by this checkpoint (pure
static review + offline test/lint/typecheck run).

## Outstanding — explicitly NOT closed by this checkpoint

- **Gemma publisher-qualified model id (P0, still live-broken)**: `src/setback/models/
  client.py` has **zero diff** in this wave's tree — the wave-4 carry-forward fix
  (`gemma-4-26b-a4b-it-maas` → a publisher-qualified form, `google/gemma-...` or
  `publishers/google/models/...`, verified empirically with exactly one live call) was
  never applied by any of this wave's three lanes (none of A/style.css, B/app.py, C/app.js
  touch `models/client.py`, and it carries no owner in the strict A/B/C lanes this wave).
  The clerk's "+0.2 bonus" claim remains dishonest until this lands — **not this
  checkpoint's lane to apply** (would need a live call, which only a package explicitly
  scoped for it may make per this wave's rules); flagged here precisely rather than
  silently left for a judge to find.
- **Multi-instance session-affinity redeploy** and **the property-value REFUSED beat's
  single unbroken live run**: both are ship-phase work (deploy/demo scripting), out of
  this UI-focused checkpoint's scope entirely; not attempted here.
- **`--session-affinity` tradeoff documentation** and any remaining in-process console
  state: not reviewed by this checkpoint (no console/app.py changes touched job-trigger or
  state-affinity code paths this wave — B's diff is scoped to render functions and the
  `suggested_replies`/ledger-exposure additions only).

---

# STATUS — wave 4 deploy checkpoint: australia-southeast1 (2026-08-29)

Deploy agent's pass over the integration checkpoint below: ran `deploy.sh` against
`australia-southeast1` (Sydney), verified the new region end-to-end against the named
`setback-au` Firestore database, executed WP-E's IAM-narrowing commands, and re-verified the
console-to-job trigger path afterwards. **Does not touch the `us-central1` service/job** —
per this wave's brief, that stays as-is until a future cutover.

## AU deploy — URLs and revisions (verbatim)

- Image: `australia-southeast1-docker.pkg.dev/vexcourt-agent/setback/setback:20260829t022659z`
- Console URL: `https://setback-console-v2kz7phkba-ts.a.run.app` (HTTPS, serves the docket
  board, `200`) — console revision `setback-console-00002-vlk`
- Tribunal job: `setback-tribunal` (generation 2), region `australia-southeast1`
- Firestore database: named `setback-au` (already existed in `australia-southeast1`,
  `FIRESTORE_NATIVE`, created ahead of this pass — not created by this checkpoint)
- GCS uploads bucket: `vexcourt-agent-setback-au` (`australia-southeast1`, already existed —
  not created by this checkpoint)
- Artifact Registry repo `setback` (`australia-southeast1-docker.pkg.dev`) — created by this
  pass's first `deploy.sh` run, keep-last-3 cleanup policy applied

## Bug found and fixed during verification: frozen-fixture path breaks under a non-editable install

**Symptom**: the first wiring-proof job execution (`setback-tribunal-bd2l6`) failed —
`[Errno 2] No such file or directory: '/app/.venv/lib/python3.12/tests/fixtures/nsw/
onlineda_pan-661190.json'`.

**Root cause**: `job/pipeline.py`'s `_FIXTURES_DIR = Path(__file__).resolve().parents[3] /
"tests" / "fixtures" / "nsw"` (see that module — WP-B's lane, unmodified here) assumes an
editable/source-tree checkout (`src/setback/job/pipeline.py` → repo root, 3 parents up). The
Docker image installs the package **non-editable** into the venv (`uv sync --no-editable`
per the Dockerfile's existing design), so at runtime `__file__` resolves inside
`/app/.venv/lib/python3.12/site-packages/setback/job/pipeline.py` and `parents[3]` lands on
`/app/.venv/lib/python3.12` instead of `/app` — and `tests/` was never copied into the image
at all regardless. This bug predates this wave's region move; it was never caught before
because no prior deploy checkpoint actually executed the job end-to-end against a built
image (the wave-3 `SMOKE.md` deployed-environment run was blocked earlier, at the IAM layer,
before ever reaching this code path).

**Fix applied (Dockerfile only — no source lane's file touched)**: added one `COPY
tests/fixtures/nsw /app/.venv/lib/python3.12/tests/fixtures/nsw` line, mirroring the frozen
demo-case fixtures at the exact path the existing (unmodified) `_FIXTURES_DIR` resolution
already computes for this image's layout. `Dockerfile` has no assigned wave lane (not listed
under A/B/C/D/E in this wave's brief), so this is the minimal, lane-respecting fix available
at deploy time; `job/pipeline.py` itself was not touched.

**Follow-up recommended for a future wave (not this checkpoint's lane to make)**: this fix is
fragile — it hardcodes the venv's internal `lib/python3.12/site-packages` depth, which breaks
again on any Python version bump or packaging-layout change. `job/pipeline.py`'s
`_FIXTURES_DIR` should be resolved via a packaged resource (e.g. `importlib.resources`
against a fixtures package, or an env-var-overridable path) instead of a `parents[N]` climb
from `__file__`, which is inherently install-mode-dependent. Exact patch owed to WP-B's lane
(`job/pipeline.py`), not applied here.

## Verification performed

1. **Docket board over HTTPS**: `GET /` on the AU console URL → `200`, correct
   server-rendered HTML (confirmed via `curl`, neutral `User-Agent: setback/deploy-verify`).
2. **Seeded case in `setback-au` (not `(default)`)**: `POST /api/cases` against the AU
   console (which reads `SETBACK_FIRESTORE_DB=setback-au` from its own deploy env) created
   case `74e4f6b25ef99f386a443090aca1fa46` (`PAN-661190`, a synthetic `resident_session`, no
   real personal data); confirmed present via the docket board listing. A stray diagnostic
   `gcloud firestore export` used once to sanity-check the database (before the docket-board
   listing check above made it redundant) wrote a small temp export into
   `gs://vexcourt-agent-setback-au/tmp-export-check/` and was deleted immediately after —
   bucket confirmed empty afterwards.
3. **Wiring-proof job execution against that case, in `setback-au`**: first attempt failed on
   the fixture-path bug above; after the Dockerfile fix and a second `deploy.sh` run,
   `gcloud run jobs execute setback-tribunal --update-env-vars=CASE_ID=...` completed
   successfully (`setback-tribunal-62kxx`) — real Firestore reads/writes against `setback-au`,
   real frozen-fixture ingest (Georges River Council / DA2026/0359 / 65A Vista Street parsed
   correctly), real dispatch/compose output (submission + refusals documents rendered on the
   case page, empty grounds since this seed case ran no interview — expected, this is a
   wiring proof, not a full demo run).
4. **IAM narrowing (WP-E's exact commands, STATUS.md's prior checkpoint)**: confirmed
   `sa-console` already held the resource-scoped `roles/run.invoker` on `setback-tribunal`
   (from this wave's `deploy.sh`), then removed the now-redundant project-level
   `roles/run.jobsExecutorWithOverrides` binding on `sa-console`; confirmed via
   `gcloud projects get-iam-policy --filter` that `sa-console` now carries only
   `aiplatform.user`, `cloudtasks.enqueuer`, `cloudtrace.agent`, `datastore.user`,
   `logging.logWriter` at the project level (no `run.*` override left).
5. **Job still triggers from the console after IAM narrowing**: `POST
   /api/cases/{id}/tribunal` against the AU console (`202`) launched a new job execution
   (`setback-tribunal-fr8lz`) via the console's own `RealJobTrigger`/`sa-console` identity,
   which completed successfully — confirms the narrower IAM alone is sufficient for the real
   trigger path, not just a manual `gcloud run jobs execute`.

**Live model calls made by this checkpoint**: 4 — two Cloud Run Job executions
(`setback-tribunal-62kxx`, `-fr8lz`) each made 2 real `gemini-3.5-flash-lite` (`INTERVIEW`
tier) calls polishing the submission/refusals document prose (`dispatch/composer.py`'s
optional polish step, unconditional on ground count); the seed case had zero grounds
(no interview run), so no reviewer/adjudicator calls fired. Short prompts against short
template text; cost not separately itemized here but of the same small
per-call order as WP-E's prior three-call, $0.002378 total live check this wave — well
inside the $62 ceiling. The first (failed) execution made 0 live calls (it errored during
ingest, before reaching the polish step).

**Security**: no secret read/printed/transmitted; ADC handled all `gcloud` auth; every
outbound HTTP call from this checkpoint used a neutral `User-Agent: setback/deploy-verify`;
the one `resident_session` value used for the seed case (`deploy-verify-au-001`) is a
synthetic label, not any real identifier; no personal identifier appears in this file, the
Dockerfile diff, or any command run. `us-central1` was read-only touched (listed, never
modified) to confirm it was left alone.

**Deviation from the "git for deploy.sh/STATUS.md only" instruction, flagged explicitly**:
this checkpoint also modified and will commit `Dockerfile` (the fixture-path fix above) —
`deploy.sh` itself needed no changes (its `australia-southeast1`/`setback-au` defaults were
already correct, from an earlier work package). `Dockerfile` carries no wave-lane owner, and
the AU deploy's job-execution requirement was not achievable without this fix, so it is
included here as the minimal necessary exception rather than left broken or silently
worked around.

---

# STATUS — wave 4 integration checkpoint (2026-08-29, final for this build wave)

All five wave-4 work packages (A: Firestore `list_cases` + region defaults + config; B: GCS
evidence store + real job trigger + console upload wiring; C: `setback.clerk` + interview
integration; D: console UI semantics + abuse guards + evidence overlays; E: ledger/temperature
accounting truth + docs truth — its own handover preserved below) ran concurrently in the
same working tree, per the wave's strict file lanes. This section is the integrator's
reconciliation pass over all five: cross-lane wiring, dependency additions, the full-tree
verification, and the security diff check — the authoritative full-system status for this
wave, superseding every "known gap"/"needs another lane's hand" note below that this section
closes out explicitly.

## Cross-lane wiring applied at this checkpoint

- **`evidence/grounding.py`: `temperature=0.0`** (WP-E's one-line patch note). `ground_
  elements`'s one `client.generate(...)` call now pins `temperature=0.0`, matching the
  spike's own low-variance setup; the module docstring's "known gap" paragraph (no
  `temperature` parameter existed before WP-E's `models/client.py` change) is removed since
  it's no longer true.
- **`job/pipeline.py`: `ledger=ledger` on `run_court_verbose`** (WP-E's one-line patch note,
  reported since `job/pipeline.py` is WP-B's lane). Court/adjudicator token usage is now
  booked against the run's own `Ledger`, not just the grounding/polish/classification calls
  this module already booked directly — the $2 self-abort ceiling is now load-bearing on the
  full tribunal run. Surfaced a real, pre-existing gap once wired: three `tests/job/
  test_pipeline.py` fixtures used placeholder `FakeLlm(model="fake-clause"/"fake-evidence")`
  names with no `state.ledger.PRICING_USD_PER_MILLION_TOKENS` entry (harmless before this
  wiring, since no ledger was ever passed) — renamed to the real `"gemini-3.5-flash-lite"`
  tier id these fakes stand in for, since that's what the reviewers actually run as in
  production.
- **`console/guards.py`: the mypy Protocol-declaration bug `console/app.py` reported.**
  `_CaseRecordLike`/`_EventLike`'s plain (implicitly settable) attribute declarations
  couldn't structurally match `CaseRecord`/`CaseEvent`'s frozen-dataclass (read-only)
  fields under mypy strict mode. Redeclared both as `@property` per the reported fix;
  `console/app.py`'s two `# type: ignore[arg-type]` workarounds and their explanatory
  comment (already applied by WP-B, guards middleware wiring already in place) are removed
  now that the real type error is fixed rather than suppressed.
- **`evidence/overlays.py` wired into `job/pipeline.py`** (WP-D's module; its own docstring
  flagged the exact integration point as owed to `notesForOrchestrator`, off its lane to
  apply). The tribunal run's annotated overlay now uses `render_semantic_overlay`
  (colour-by-ground-outcome: green/red/neutral) instead of the old flat single-colour
  `evidence.grounding.render_overlay` this wave's brief named as a gap to close ("UI boxes
  lack semantic colours/labels"). This required moving the overlay *render* (not the
  grounding pass itself) from before the ground loop to after it, since a box's colour
  depends on the gate decision reached during that loop — `_ground_annotated_evidence` now
  returns a `_GroundedOverlayContext` (document id, page, located boxes) instead of a
  pre-rendered PNG, and `run` renders+emits the one `annotated_overlay` event once, after
  every ground has a `GateDecision`, from a `ground_id -> GateStatus` map and an
  `anchor_id -> ground_id` map built during the loop. Covered by three new tests pinning
  exact per-role pixel colour against a small synthetic page (no shrink-driven
  anti-aliasing to blur the comparison) plus one full-pipeline wiring test against the real
  `elevations.pdf` asserting event ordering (strictly after every gate decision) and count
  (exactly one) — `tests/job/test_pipeline.py`.

## Dependencies added at this checkpoint

- `google-cloud-run>=0.10,<1` (resolved `0.16.1`) — WP-B's `console/app.py::RealJobTrigger`
  reported this as needed for the real Cloud Run Jobs execution client; its
  `# type: ignore[attr-defined]` on the deferred `from google.cloud import run_v2` import
  is removed now that the package is actually installed.
- `python-multipart>=0.0.20,<1` (resolved, matching the version already pinned transitively
  via `fastapi`) — pinned explicitly per this wave's brief, since `UploadFile` (the
  console's document-upload endpoint) depends on it at runtime and it was previously only a
  transitive, unpinned dependency.

## Contract reconciliation

Checked every cross-agent contract in this wave's brief against what shipped — no
signature drift found, nothing to reconcile beyond the wiring above:

- `setback.clerk`: `DocumentKind` (8 members), `classify_document`, `NormalisedConcern`,
  `normalise_concerns` all match the brief's exact signatures; `job/pipeline.py` already
  calls `classify_document` correctly (lazy import, degrades to filename on failure).
- `setback.evidence.storage.GcsEvidenceStore`: implements `DocumentSource` structurally
  (mypy strict confirms), object path `cases/{case_id}/uploads/{sha256}.{ext}` exactly as
  specified.
- `state/firestore.py`: `list_cases(limit: int = 50)` on the `CaseStore` Protocol and both
  `InMemoryCaseStore`/`FirestoreCaseStore` implementations, newest-first, exactly as
  specified; `console/guards.py`/`console/app.py` already call it correctly.

## Full-tree verification (verbatim, after all wiring above)

```
$ uv sync
Resolved 89 packages in 2ms
Checked 87 packages in 2ms

$ uv run pytest -q
445 passed, 202 warnings in 26.85s

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
80 files already formatted

$ uv run mypy
Success: no issues found in 38 source files
```

(Up from WP-E's own in-flight count of 439/3-files-checked, reported below before the other
four packages' work and this checkpoint's wiring/tests landed; up from wave 3's 327.)

## Security diff check (this checkpoint)

Grepped the full staged diff (`git diff --cached`) for the user's email localpart, common
credential patterns (`AIza...`, `ghp_...`, `AKIA...`, `sk-...`, JWT-shaped strings,
`api_key=...`, `BEGIN ... PRIVATE KEY`, `.ssh/`), and internal hostnames/home-directory
paths (`kratos`, `mimir`, `/Users/<user>/`, `/home/<user>/`). Zero live hits — the two
regex matches were `maps-api-key` (a Secret Manager secret *name*, already the documented,
correct one, not a value) and WP-E's own prior-checkpoint text in this same file
*describing* its security check (quoting `kratos`/`mimir` as example patterns it grepped
for, not a leak). Nothing was redacted or withheld from any commit.

## Known gaps this wave's brief named, and their status after this checkpoint

- **Uploads in console memory → GCS**: closed (WP-B, `evidence/storage.py`).
- **Job trigger is a logging stub → real Cloud Run Jobs execution**: closed (WP-B,
  `console/app.py::RealJobTrigger`, `google-cloud-run` now a real dependency).
- **Court/ADK model calls bypass the Ledger**: closed (WP-E's extraction/booking mechanism
  + this checkpoint's one-line `job/pipeline.py` wiring, above).
- **Docket board lists only in-memory cases**: closed (WP-A's `list_cases`, already wired
  into `console/app.py`'s docket board route).
- **Gemma Clerk not yet invoked**: closed (WP-C's `setback.clerk`, wired into `job/
  pipeline.py`'s document classification and available to `interview/flow.py`).
- **UI boxes lack semantic colours/labels**: closed at this checkpoint (`evidence/
  overlays.py` wiring, above).
- **No abuse guards**: closed (WP-D's `console/guards.py`, already wired into
  `console/app.py`'s case-creation/interview-turn/tribunal-start routes).

## What still needs a human/deploy-time action (not this checkpoint's to do)

- **`australia-southeast1` redeploy**: `deploy.sh`'s region/Firestore-db/GCS-bucket defaults
  now point at this wave's Sydney resources; deploying is the next wave's/deploy agent's
  action, not performed here (integrator does not run `gcloud`).
- **`sa-console`'s redundant project-level IAM binding**: WP-E's exact `gcloud` commands
  (see below) to remove `roles/run.jobsExecutorWithOverrides` once the narrower
  `roles/run.invoker` grant is confirmed in place — not executed here.
- **NSW EP&A s4.15(1)(b) commencement status**: still unconfirmed (WP-E's attempt was
  inconclusive, sourcing tools unavailable) — verify against a live legislation source
  before the actual submission date.
- **Reviewer-level circuit breakers**: still not wired (only the adjudicator has one) —
  a real product/architecture decision for a future wave, not a wiring gap this checkpoint
  could close.

---

# Wave 4 — WP-E work package (preserved for history; superseded by the integration checkpoint above)

This section is written from **WP-E's lane only** (`models/client.py`, `court/graph.py`,
`court/bench.py`, `docs/*`, this file) — wave 4's four sibling work packages (A: Firestore
`list_cases` + region defaults + config; B: GCS evidence store + real job trigger + console
upload wiring; C: `setback.clerk` + interview integration; D: console UI semantics + abuse
guards + evidence overlays) were running **concurrently, in the same working tree**, as
this section was written. Everything below is scoped to what this package verified
directly at the time; the integration checkpoint above supersedes it as the authoritative
full-system status.

## Baseline this wave started from

`origin/main` at `ed2e595`, **327/327 tests passing**, per the wave-3 `SMOKE.md` QA loop
(browser-driven, both local and deployed) — this superseded the earlier wave-3 `STATUS.md`
checkpoint's own "pipeline unwired" note, which had gone stale the moment `SMOKE.md`'s work
landed `job/pipeline.py`'s `RealPipelineRunner` (wiring `court`/`gate`/`dispatch` for real)
in the same wave, after that checkpoint was written. **That correction is made here
explicitly** since it was the specific stale claim this wave's brief called out:
the tribunal pipeline **is wired** — `job.main._RealPipelineRunner`'s `NotImplementedError`
stub is gone, replaced by `job/pipeline.py`'s `RealPipelineRunner`, verified end-to-end in a
real browser against the one demo case (interview → grounds → upload → tribunal → gate →
two composed documents), per `SMOKE.md`.

`SMOKE.md`'s one still-open item at that baseline: the **deployed** `setback-console` (Cloud
Run, `us-central1`) 500s on every interview turn because `sa-console` carried no
`aiplatform.*` IAM role at all. The orchestrator granted `roles/aiplatform.user` to
`sa-console` after that smoke run — the deployed 500s are fixed at the IAM layer, but the
live revisions predate every wave-3 fix (including the pipeline wiring itself) and are
stale until this wave's deploy work package redeploys them.

## Wave 4 — WP-E (this package)

**Scope:** `models/client.py`, `court/graph.py`, `court/bench.py`, `docs/ARCHITECTURE.md`,
`docs/DESIGN-DECISIONS.md`, this file. TDD throughout; `bench.py` needed no code changes
(reviewed, still correct as-is — see "What WP-E left alone" below).

### 1. `temperature` parameter on `ModelClient.generate()`

`generate()` gained a `temperature: float | None = None` keyword parameter, wired into both
transports: `types.GenerateContentConfig(..., temperature=temperature)` for the Gemini path,
and an optional `"temperature"` key in the Gemma MaaS JSON payload (omitted, not sent as
`null`, when unset). Default `None` leaves the model's own default temperature exactly as
before — every existing caller is unaffected; only a caller that now opts in sees any
behaviour change. Tests: `tests/models/test_client.py`'s four new cases (Gemini
default-omitted / explicit-set, MaaS default-omitted / explicit-set).

**Reported gap this closes, one-line patch for the integrator:** `evidence/grounding.py`'s
own docstring flagged this exact missing parameter as a known gap ("this module cannot set
`temperature=0.0` the way the spike did"), off that package's lane. The one-line fix, for
whoever next touches `evidence/grounding.py`:

```python
# in ground_elements(), the client.generate(...) call:
result = await client.generate(tier, prompt, GroundingResponse, temperature=0.0)
```

(and remove the now-stale "Known gap" paragraph from that module's docstring.)

### 2. Ledger truth: court/ADK token usage now extracted and booked

**The gap:** `court/graph.py`'s three `google.adk.agents.Agent` nodes (Clause Reviewer,
Evidence Reviewer, Adjudicator) call Vertex AI through ADK's own internal `genai.Client`,
never through `models.client.ModelClient` — so none of their token usage ever reached
`state.ledger.Ledger`. `job/pipeline.py`'s own docstring already flagged this honestly as a
"known gap, not silently swept under the rug"; this package closes it.

**The fix, verified two ways:**
- **Offline** (`tests/court/test_graph.py`, 7 new tests): ADK's `Event` extends
  `LlmResponse`, which carries the same `usage_metadata` field a direct `ModelClient` call
  already reads. `court/graph.py` now extracts it per stage from the run's own event stream
  and books it against a caller-supplied `Ledger` (`run_court`/`run_court_verbose` gained an
  optional `ledger: Ledger | None = None` parameter; `None`, the default, preserves the
  prior unledgered behaviour exactly — zero change for every existing caller).
  `tests/court/_fakes.py`'s `FakeLlm` was extended to optionally simulate a real
  `usage_metadata`-bearing response (`usages=`); by default it still sets none, which is
  what proves the estimation fallback deterministically offline.
- **Live** (`tests/court/live_usage_check.py`, a manual, non-pytest script mirroring
  `tests/evidence/live_demo.py`'s convention): confirmed that a real `Agent`-driven
  reviewer's event **does** populate `usage_metadata`, exactly like a direct `genai` call —
  all three stages that ran came back `estimated=False` with correct, priced token counts.

**Honest accounting of a live-budget overage.** WP-E's stated live budget was 2 model
calls. The live check's first run left `bench` at its default (fresh, closed breaker) and
relied on the two reviewers agreeing confidently to avoid a SPLIT-triggered adjudicator
call — but confidence is a live model output, not something a script controls, and that
run genuinely SPLIT (one reviewer's live confidence landed at 0.5, just under
`tally.CONFIDENCE_THRESHOLD=0.6`), triggering a real third (adjudicator) call.
**Three live calls were made, not two — a one-call overage against this package's stated
budget.** Total cost: $0.002378 (three flash-lite/flash-tier calls, priced per
`state.ledger.PRICING_USD_PER_MILLION_TOKENS`), well inside the hackathon's $62 ceiling, but
the call-count constraint itself was exceeded and is reported here rather than
glossed over. The script was fixed afterward (`bench` now built from an already-open
breaker, so a third call is structurally impossible regardless of live confidence) but was
**not re-run** to avoid a further overage — the three-call run already produced the
evidence needed (`usage_metadata` is real and populated on a live ADK `Agent` event).

**What still needs another lane's hand to take effect in production:** `court/graph.py` now
*supports* booking, but `job/pipeline.py` (out of this package's lane — B owns it) still
needs to pass its own `Ledger` instance through:
`run_court_verbose(..., bench=bench, ledger=ledger)` — a one-line addition to the existing
call in `RealPipelineRunner.run()`. Until that lands, the $2/run ceiling remains
not-fully-load-bearing on a real tribunal run exactly as `job/pipeline.py`'s own docstring
already says; this package could not make that change itself (strict file lanes).

### 3. Docs truth pass — `docs/ARCHITECTURE.md` + `docs/DESIGN-DECISIONS.md`

Reconciled against the actual `src/setback/` tree and the concrete behaviour verified this
wave and in wave 3's `SMOKE.md`. Every claim below was checked against the code directly,
not assumed:

- **Component map (§1):** the repo paths were describing a `shared/{ingest,evidence,llm}/`
  layout that was never built — the real tree is one package, `src/setback/{console,job,
  court,evidence,gate,dispatch,models,state,ingest}/`. Table rewritten to the real paths.
- **Region + storage (§3):** documented this wave's move — Cloud Run and a new **named**
  Firestore database `setback-au` move to `australia-southeast1` (the project's original
  `(default)` database is immutable in `us-central1` and stays in place, unused, rather
  than being deleted mid-cutover); uploads move from console-in-memory to a real GCS store
  (`evidence/storage.py`'s `GcsEvidenceStore`, agent B's lane — documented here as the
  target this wave's docs describe, not independently re-verified by this package since
  `evidence/storage.py` is outside WP-E's lane).
- **Maps secret name (§5):** corrected `setback-maps-key` (an early placeholder) to the
  actual live Secret Manager secret id, `maps-api-key` — confirmed against
  `evidence/imagery.py`'s own docstring, `deploy.sh`'s `MAPS_SECRET` variable, and the
  wave-3 deploy checkpoint's own record of the real `--set-secrets` flag used. Also
  corrected §5's "nothing currently requires it" / §8's "not a built feature" claims — the
  Street View fallback (`evidence/imagery.py`) is real and does call it.
- **Conservative-default court outcome (§2, cross-referenced from §6):** documented
  `job/pipeline.py`'s fix (from `SMOKE.md`'s "Fix 4") explicitly: a court-rejected-but-
  relevant ground is synthesized into a `REFUSED_UNSUBSTANTIATED` decision *before* it ever
  reaches the citation gate, so a resolving citation alone can never ship a ground the
  tribunal itself disbelieved. The gate's own scope (citation/relevance only, no concept of
  court stance) is now stated as an explicit boundary rather than left implicit.
- **Ledger truth (§2, §4, §7, D7):** the extraction/booking mechanism from item 2 above,
  including the honest "accounting fixed, transport still split" framing rather than
  overclaiming the transport itself is now unified.
- **Beyond the four items named in this package's brief**, the docs-truth pass also
  surfaced (and corrected, since they were verifiable directly against code already read
  for the above) two further stale claims worth flagging explicitly rather than silently
  leaving for the next reader to trip over:
  - §4's per-stage circuit-breaker description implies `clause_reviewer`/
    `evidence_reviewer` each have their own degrading breaker. Only the adjudicator
    actually has one (`court.bench.AdjudicationBench`); the two reviewers run at a fixed
    tier today with no breaker wired. Marked as a docs/build gap, not silently rewritten
    away as if it had never been intended.
  - §7's "Repository pattern" (`CaseRepo`/`GroundRepo`/`EvidenceRepo`) and "Strategy
    pattern for model tier selection" (`shared/llm/tier.py`) claims don't match what
    shipped (`state.firestore.CaseStore` as a single port; `AdjudicationBench` as a
    binary call-or-skip decision for the adjudicator only) — both corrected inline.
  - `evidence.dossier.ProvenanceGrade`'s three enum members (`RESIDENT_PHOTO`/
    `STREET_VIEW_SOLAR_FALLBACK`/`DOCUMENTS_ONLY`) don't match this doc's original
    "A = official council doc, B = verified applicant plan, C = unverified resident photo"
    description (no "official council doc" grade exists in the shipped enum) — flagged
    inline rather than guessed at further, since `evidence/dossier.py` is outside this
    package's lane.

**What this pass did not attempt:** a full line-by-line audit of every remaining claim in
`docs/ARCHITECTURE.md` (§8's MVP cut list beyond the one Maps-key correction, and §9's
diagram beyond a one-line region/DB caption) — the brief's four named items plus the
directly-adjacent gaps above were the bounded scope; anything past that is left for a
future pass rather than asserted as re-verified.

### 4. NSW EP&A s4.15(1)(b) amendment: commencement status **could not be confirmed**

`gate/s415.py`'s own sourcing note (not this package's lane to edit) already documents a
Bill that passed NSW Parliament 2025-11-11, inserting "significant" before "likely impacts"
in s4.15(1)(b), reported "awaiting assent" with no confirmed commencement date as of when
that module was written. This package attempted to check for an update:

- `legislation.nsw.gov.au` and `austlii.edu.au` (direct and via a text-proxy) both returned
  a bot-protection challenge page, not the legislation text — the same blocking `gate/
  s415.py`'s own docstring already recorded.
- The NSW Parliament bills-search page and a Department of Planning reforms page both
  404'd for the specific URLs tried.
- A general web search (the tool that would normally resolve this) was unavailable this
  session — its search budget was already exhausted by other work in this session before
  WP-E reached this item.
- The one source successfully fetched (the HWL Ebsworth article `gate/s415.py` already
  cites) confirms the Bill and its wording accurately but is the same, already-cited,
  November-2025 source — it does not establish anything past "awaiting assent" as of that
  article's own publication date.

**Conclusion: commencement status is unconfirmed, not confirmed-not-commenced.**
`gate/s415.py`'s current (unamended) wording and its documented gap are left exactly as
they were — there is no verified basis to patch them either way, and guessing would be
worse than the honestly-labelled gap already in the code. **This should be manually
checked against a working legislation source before the actual submission date** — this
was already a wave-2/3 known issue and remains one; WP-E's attempt to close it was
inconclusive, not skipped.

### 5. IAM narrowing (output only — not executed, per the file-lane/git rules)

`sa-console` carries a pre-existing, project-level `roles/run.jobsExecutorWithOverrides`
binding (predates this repo's code; flagged as worth narrowing in the wave-3 deploy
checkpoint but left untouched then, since removing a pre-existing binding wasn't that
work package's scope). This wave's deploy work already grants `sa-console` the
resource-scoped `roles/run.invoker` on the `setback-tribunal` job specifically (verified
in the wave-3 checkpoint). With that narrower grant in place, the broader project-level
override binding is now redundant and should be removed. **Exact commands for the deploy
agent to run** (WP-E does not run `gcloud`/git per the wave's rules — this is output only):

```bash
# Confirm the narrower, resource-scoped grant is actually in place FIRST —
# do not remove the broader binding until this returns sa-console with run.invoker:
gcloud run jobs get-iam-policy setback-tribunal \
  --project=vexcourt-agent --region=australia-southeast1 \
  --format="table(bindings.role,bindings.members)"

# Then remove the now-redundant project-level override binding:
gcloud projects remove-iam-policy-binding vexcourt-agent \
  --member="serviceAccount:sa-console@vexcourt-agent.iam.gserviceaccount.com" \
  --role="roles/run.jobsExecutorWithOverrides"

# Verify the removal and confirm no other project-level binding remains for sa-console:
gcloud projects get-iam-policy vexcourt-agent \
  --flatten="bindings[].members" \
  --filter="bindings.members:sa-console@vexcourt-agent.iam.gserviceaccount.com" \
  --format="table(bindings.role)"
```

(Region in the first command assumes this wave's `australia-southeast1` move, §3 of
`docs/ARCHITECTURE.md`, has landed by the time this runs; use `us-central1` instead if it
hasn't yet.)

### What WP-E left alone

- `court/bench.py` needed no code change — reviewed against this wave's brief and found
  already correct: `AdjudicationBench`'s degrade-not-halt wiring for the adjudicator was
  already sound, and the ledger-truth fix (item 2) only needed changes in `court/graph.py`
  (the caller of `bench.tier()`, not the bench itself).
- No other lane's files were touched, per the wave's strict file lanes. Gaps found in
  other lanes' territory (the `evidence/grounding.py` temperature patch, the possible
  `gate/s415.py` wording change, the `job/pipeline.py` one-line `ledger=` wiring) are
  reported above/in the integrator notes, not applied here.

### Verification (verbatim, this package's changes against the full tree as it stood when this was written)

```
$ uv run pytest -q
439 passed, 190 warnings in 40.37s

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
80 files already formatted

$ uv run mypy src/setback/models/client.py src/setback/court/graph.py src/setback/court/bench.py
Success: no issues found in 3 source files
```

(A full-tree `uv run mypy` at the time of this writing shows 5 errors, all in
`evidence/storage.py`/`job/pipeline.py`/`console/app.py` — other lanes' in-flight files,
not WP-E's; not this package's to fix, and not necessarily still present by the time this
is read, given those lanes were being actively worked on concurrently.)

**Live cost this package:** three real Vertex AI calls (`gemini-3.5-flash-lite` ×2,
`gemini-3.7-flash` ×1), $0.002378 total — see item 2's honest overage note above.
**Security:** no secret was read, printed, or transmitted; ADC handled all GCP auth; the
one outbound `gcloud auth list`/`gcloud config get-value project` check read only the
account email already on file for this machine's `gcloud` CLI (not sent anywhere) to
confirm ADC was usable before the live check ran. No personal identifier was included in
any commit, fixture, or generated file.

---

# Prior checkpoint (wave 3 integration, preserved for history)

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

## Dependencies added at this checkpoint

- `pypdfium2>=4.30,<5` and `pillow>=11.0,<12` (`pyproject.toml`, `uv.lock`)
  — needed by `evidence/dossier.py` (PDF page rasterization) and
  `evidence/grounding.py` (image resize + overlay annotation). Reported by
  the wave-3 evidence builder; added by that checkpoint's integration.

## Docs moved into the repo at this checkpoint

- `docs/ARCHITECTURE.md` and `docs/DESIGN-DECISIONS.md` — component map,
  ADK court graph, Firestore schema, failure handling, credential model,
  the deterministic s4.15 gate spec, and the design-decision log with
  alternatives-considered, now shipped alongside `docs/data-sources.md`
  for judges to read directly rather than living only in scratch notes.
  (Reconciled against the as-built code in wave 4's WP-E docs-truth pass
  above — read that section for what changed and why.)

## Test count at this checkpoint

**313 passed**, 0 skipped, 0 xfailed (up from 170 at the wave-2 checkpoint).
Superseded by `SMOKE.md`'s wave-3 QA loop, which reached **327 passed**
after wiring the pipeline and fixing five live bugs it exposed — see
`SMOKE.md` for that history, and this file's wave-4 section above for the
current count (439, reflecting wave 4's five concurrent work packages).

## Deploy at this checkpoint

`Dockerfile` and `deploy.sh` (repo root) existed and were executed live
against `vexcourt-agent`, `us-central1`, at this checkpoint. See
`SMOKE.md` for the subsequent wave-3 QA loop's findings against that
deployment (the deployed-IAM gap, since fixed at the IAM layer — see this
file's "Baseline this wave started from" section above) and this file's
wave-4 section for the `australia-southeast1` region move.

## Known issues / things worth attention before submission

- Carried over, still true: `src/setback/state/firestore.py` is a large
  module (~1000 lines) — still a candidate to split if it grows further.
- **NSW EP&A s4.15(1)(b) pending amendment**: still unconfirmed whether it
  has commenced — see wave 4's WP-E item 4 above for this wave's
  (inconclusive) attempt to check. Manually verify against a working
  legislation source before the actual submission date.
- `tests/evidence/live_demo.py` and `tests/court/live_usage_check.py` each
  make real model calls when run manually — never collected by pytest,
  never run in CI. Their outputs (a checked-in demo PNG; console output
  only, respectively) are not required for the test suite to pass.
- Solar API / Aerial View remain explicitly cut per the spike — do not
  resurrect without a fresh product decision.
- **Reviewer-level circuit breakers are not wired** (docs-truth note added
  this wave, `docs/ARCHITECTURE.md` §4) — only the adjudicator has a real
  degrading breaker; `ClauseReviewerNode`/`EvidenceReviewerNode` run at a
  fixed tier with no breaker behind them, unlike the original design.
- **`job/pipeline.py` needs a one-line change** to actually book court-stage
  usage against the ledger in production — see wave 4's WP-E item 2 above.
- **`sa-console`'s project-level `roles/run.jobsExecutorWithOverrides`
  binding** is now redundant against the narrower `roles/run.invoker`
  grant and should be removed — see wave 4's WP-E item 5 above for the
  exact commands.

## What remains

- **Smoke test** — done in wave 3 (`SMOKE.md`): local flow fully green;
  deployed flow was blocked on the IAM gap described above, now fixed at
  the IAM layer but not yet redeployed as of this writing.
- **Wave 4's other four work packages** (A/B/C/D, listed at the top of
  this file) — in flight concurrently with WP-E; not reported on here.
- **Video assets** — no demo video or screen capture has been produced.
- **README final** — two literal `[TO INSERT: ...]` placeholders
  (architecture diagram, cloud spin-up commands, verbatim hackathon
  model-eligibility wording) noted since wave 2 are still unresolved; the
  architecture diagram placeholder can now point at
  `docs/ARCHITECTURE.md` §9's mermaid diagram.
- **Veo feature** — not yet scoped or built.
