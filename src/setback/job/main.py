"""Cloud Run Job entry point: `setback-tribunal`.

Reads `CASE_ID` from the environment, loads the case's resumable state (see
:func:`~setback.state.firestore.resume_case`), heartbeats, runs the review
pipeline, and exits nonzero on any failure -- Cloud Run Jobs treats a
nonzero exit code as a failed execution, which is what lets the sweeper
(and a manual retry) tell a genuinely failed run apart from one still in
progress.

Wiring only in this work package (0 live-model-call budget in its tests):
:func:`run_job` depends on a :class:`PipelineRunner` port rather than
calling `court`/`gate`/`dispatch` directly, so it is fully testable with a
fake pipeline today. The production port implementation,
:class:`~setback.job.pipeline.RealPipelineRunner`, now lives in
:mod:`setback.job.pipeline` and is wired into :func:`main` below.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from setback.state.firestore import CaseStore, ResumeState, resume_case

_DEFAULT_HEARTBEAT_STAGE = "tribunal"


class PipelineRunner(Protocol):
    """Runs the review pipeline (ingest -> reviewers -> adjudication ->
    gate -> compose) for one case.

    A pure port: `run_job` never imports `court`/`gate`/`dispatch` directly,
    so this module has no live-model-call surface of its own and is fully
    testable against a fake today. Implementations are free to raise on
    failure -- `run_job` treats any exception as a failed run, records it,
    and reports it, rather than letting it propagate uncaught out of a
    Cloud Run Job process.
    """

    async def run(self, case_id: str, resume: ResumeState, store: CaseStore) -> None: ...


@dataclass(frozen=True)
class JobResult:
    """The outcome of one job execution, for both `run_job` callers and tests."""

    case_id: str
    success: bool
    error: str | None = None


async def run_job(
    case_id: str,
    *,
    store: CaseStore,
    pipeline: PipelineRunner,
    heartbeat_stage: str = _DEFAULT_HEARTBEAT_STAGE,
) -> JobResult:
    """Run one case's pipeline execution to completion or failure.

    Loads `case_id`'s full resumable state via
    :func:`~setback.state.firestore.resume_case`, heartbeats before and
    after the pipeline runs, and never lets a pipeline exception escape
    uncaught: it is recorded as a durable `job_failed` event and reported
    back as a failed :class:`JobResult` instead, so a caller (`main`, or a
    test) can decide the process exit code without this function itself
    needing to know about `sys.exit`.

    Args:
        case_id: The case to run. Must already exist in `store` (created by
            the interview/console flow) -- a missing case is reported as a
            failed result rather than raising, since a job execution
            failing gracefully for a bad `CASE_ID` is exactly what should
            surface as a failed Cloud Run Job execution, not a crash.
        store: The case-store port.
        pipeline: The review pipeline to run.
        heartbeat_stage: The stage name recorded in `store`'s heartbeats
            for this job's liveness pings (see the sweeper in
            ARCHITECTURE.md §4, which watches for a stalled heartbeat).
    """
    case = await store.get_case(case_id)
    if case is None:
        return JobResult(case_id=case_id, success=False, error=f"case {case_id!r} not found")

    await store.heartbeat(case_id, heartbeat_stage)
    resume = await resume_case(store, case_id)

    try:
        await pipeline.run(case_id, resume, store)
    except Exception as exc:  # noqa: BLE001 -- job boundary: every failure must exit
        # nonzero and be recorded, never crash the process uncaught or be
        # silently swallowed.
        await store.append_event(
            case_id,
            f"job-failed:{case_id}:{type(exc).__name__}",
            "job_failed",
            payload={"error": str(exc)},
        )
        return JobResult(case_id=case_id, success=False, error=str(exc))

    await store.heartbeat(case_id, heartbeat_stage)
    return JobResult(case_id=case_id, success=True)


def main(
    *,
    store_factory: Callable[[], CaseStore] | None = None,
    pipeline_factory: Callable[[], PipelineRunner] | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """The Cloud Run Job entrypoint.

    Reads `CASE_ID` from `env` (defaults to `os.environ`), builds the
    production `store`/`pipeline` (defaults to `FirestoreCaseStore` and
    `RealPipelineRunner` -- constructed lazily, only once `CASE_ID` is
    confirmed present, and only via the factory parameters so a test can
    inject fakes and never touch a real GCP client), runs the job, and
    raises `SystemExit(1)` on any failure (missing `CASE_ID`, an unknown
    case, or a pipeline failure) so Cloud Run Jobs records a failed
    execution.
    """
    resolved_env = env if env is not None else os.environ
    case_id = resolved_env.get("CASE_ID")
    if not case_id:
        print("CASE_ID environment variable is required", file=sys.stderr)
        raise SystemExit(1)

    store = store_factory() if store_factory is not None else _default_store_factory()
    pipeline = pipeline_factory() if pipeline_factory is not None else _default_pipeline_factory()

    result = asyncio.run(run_job(case_id, store=store, pipeline=pipeline))
    if not result.success:
        print(f"job failed for case {case_id!r}: {result.error}", file=sys.stderr)
        raise SystemExit(1)


def _default_store_factory() -> CaseStore:
    from setback.state.firestore import FirestoreCaseStore

    return FirestoreCaseStore()


def _default_pipeline_factory() -> PipelineRunner:
    """Build the production `PipelineRunner`.

    Uses `evidence.storage.GcsEvidenceStore`, exactly like `console.app`'s
    own `_build_production_app` -- the durable, GCS-backed store a resident's
    upload actually lands in, shared between the console process and this
    entirely separate Cloud Run Job container. A fresh, per-execution
    `ingest.tracker.UserUploadedDocumentSource` (this factory's default
    before this fix) is process-local, in-memory, and always empty in a
    real job execution, silently degrading every uploaded-evidence-dependent
    ground to "no evidence provided" regardless of what the resident
    actually uploaded -- caught live in smoke loop #2 (an Evidence Reviewer
    verdict citing missing photos/plans for a case that had both uploaded).
    Local/dev callers that specifically want the in-process, in-memory store
    (no real GCS) should construct `RealPipelineRunner` directly instead
    (see `console.app`'s `LocalPipelineJobTrigger`, gated behind
    `SETBACK_LOCAL_TRIBUNAL=1`).

    `ingest_client` (wave 9) is a real `httpx.AsyncClient` -- the one thing
    that switches `job.pipeline`'s ingest from the frozen PAN-661190 demo
    fixture to a case's own typed application number, live. Never closed:
    this factory builds a `PipelineRunner` for exactly one Cloud Run Job
    execution, which exits the process on completion -- the same lifetime
    `ModelClient()` below already assumes. `follow_redirects=True` is
    required for `ingest.tracker.EtrackDocumentSource`'s own search-postback
    flow (a 302 to the application's detail page) when this shared client is
    injected into it rather than letting it build its own. `timeout` is set
    explicitly and generously (30s, 10s to connect) rather than left at
    httpx's own default (a 5s timeout for every phase): when a caller injects
    its own client, `ingest.onlineda`/`ingest.spatial`'s own, more generous
    `_REQUEST_TIMEOUT` constants never apply -- they only govern a client
    those modules construct *themselves* -- so a bare `httpx.AsyncClient()`
    here would silently be *stricter* than every other ingest call this
    codebase makes, timing out for real real-world NSW-API latency observed
    directly during this wave's live verification (a real, successful fetch
    of this build's own demo PAN took ~8.5s end-to-end). A neutral
    `User-Agent` carries no identity, per this project's security rules.

    `guard_totals_store` (security-review spend-accuracy-gap fix,
    2026-08-30): a real `FirestoreGuardTotalsStore`, the same aggregate
    `console.app`'s own production wiring (`_build_production_app`) passes
    `console.guards`' public-spend-ceiling reader -- this job's Cloud Run
    Job service account (`sa-orchestrator`) already has the `datastore.user`
    role needed to write it. Without this, `RealPipelineRunner` would never
    book this job's own (dominant) real cost against the ceiling the
    founder is relying on as the one hard blocker on public spend, exactly
    the gap the security review's headline finding described for the
    console side."""
    import httpx

    from setback.evidence.storage import GcsEvidenceStore
    from setback.evidence.veo_live import VertexVeoLiveClient
    from setback.job.pipeline import RealPipelineRunner
    from setback.models.client import ModelClient
    from setback.state.guard_store import FirestoreGuardTotalsStore, FirestoreVeoLiveCounterStore

    return RealPipelineRunner(
        document_source=GcsEvidenceStore(),
        polisher=ModelClient(),
        grounding_client=ModelClient(),
        ingest_client=httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": "setback/0.1"},
            timeout=httpx.Timeout(30.0, connect=10.0),
        ),
        guard_totals_store=FirestoreGuardTotalsStore(),
        # Wave 13 (founder-authorized): judge-gated LIVE Veo 3.1 generation.
        # Both default to `None` (a guaranteed no-op) if omitted -- wired
        # here, unconditionally, since the feature's own gating (judge_
        # origin + a shipped overshadowing ground + the global cap +
        # `VEO_LIVE_ENABLED`) already fully governs whether either is ever
        # actually used. Constructing them makes no network call by itself
        # (same ADC-resolved-lazily contract as `ModelClient()`/
        # `GcsEvidenceStore()`/`FirestoreGuardTotalsStore()` above).
        veo_client=VertexVeoLiveClient(),
        veo_live_counter_store=FirestoreVeoLiveCounterStore(),
    )


if __name__ == "__main__":
    main()
