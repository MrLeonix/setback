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
    `SETBACK_LOCAL_TRIBUNAL=1`)."""
    from setback.evidence.storage import GcsEvidenceStore
    from setback.job.pipeline import RealPipelineRunner
    from setback.models.client import ModelClient

    return RealPipelineRunner(
        document_source=GcsEvidenceStore(),
        polisher=ModelClient(),
        grounding_client=ModelClient(),
    )


if __name__ == "__main__":
    main()
