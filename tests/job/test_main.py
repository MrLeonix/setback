"""Tests for setback.job.main: the Cloud Run Job entrypoint.

Wiring only (0 live-model-call budget) -- `run_job` is exercised fully
offline against `InMemoryCaseStore` and a fake `PipelineRunner`; `main()` is
exercised with injected `store_factory`/`pipeline_factory`/`env` so it never
constructs a real `FirestoreCaseStore` or `ModelClient` in a test.
"""

from __future__ import annotations

import pytest

from setback.job.main import JobResult, main, run_job
from setback.state.firestore import CaseStore, InMemoryCaseStore, ResumeState


class _FakePipeline:
    """Records every call it received and optionally raises on `run`."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.calls: list[tuple[str, ResumeState]] = []

    async def run(self, case_id: str, resume: ResumeState, store: CaseStore) -> None:
        self.calls.append((case_id, resume))
        if self.fail_with is not None:
            raise self.fail_with


async def _seeded_store() -> tuple[InMemoryCaseStore, str]:
    store = InMemoryCaseStore()
    case = await store.create_case(application_number="PAN-1", resident_session="s1")
    return store, case.case_id


# --- run_job -------------------------------------------------------------


async def test_run_job_success_heartbeats_and_runs_pipeline() -> None:
    store, case_id = await _seeded_store()
    pipeline = _FakePipeline()

    result = await run_job(case_id, store=store, pipeline=pipeline)

    assert result == JobResult(case_id=case_id, success=True, error=None)
    assert len(pipeline.calls) == 1
    called_case_id, resume = pipeline.calls[0]
    assert called_case_id == case_id
    assert resume.case is not None
    assert resume.case.case_id == case_id
    heartbeats = await store.list_heartbeats(case_id)
    assert "tribunal" in heartbeats


async def test_run_job_unknown_case_fails_without_calling_pipeline() -> None:
    store = InMemoryCaseStore()
    pipeline = _FakePipeline()

    result = await run_job("no-such-case", store=store, pipeline=pipeline)

    assert result.success is False
    assert result.error is not None
    assert "no-such-case" in result.error
    assert pipeline.calls == []


async def test_run_job_pipeline_failure_records_event_and_reports_failure() -> None:
    store, case_id = await _seeded_store()
    pipeline = _FakePipeline(fail_with=RuntimeError("adjudicator exploded"))

    result = await run_job(case_id, store=store, pipeline=pipeline)

    assert result.success is False
    assert result.error == "adjudicator exploded"
    events = await store.list_events(case_id)
    failure_events = [e for e in events if e.event_type == "job_failed"]
    assert len(failure_events) == 1
    assert failure_events[0].payload["error"] == "adjudicator exploded"


async def test_run_job_resumes_with_existing_grounds_and_ledger() -> None:
    store, case_id = await _seeded_store()
    await store.propose_ground(case_id, "ground-1", claim="test claim")
    pipeline = _FakePipeline()

    await run_job(case_id, store=store, pipeline=pipeline)

    _, resume = pipeline.calls[0]
    assert "ground-1" in resume.grounds


async def test_run_job_custom_heartbeat_stage() -> None:
    store, case_id = await _seeded_store()
    pipeline = _FakePipeline()

    await run_job(case_id, store=store, pipeline=pipeline, heartbeat_stage="custom-stage")

    heartbeats = await store.list_heartbeats(case_id)
    assert "custom-stage" in heartbeats
    assert "tribunal" not in heartbeats


# --- _default_pipeline_factory (production wiring) --------------------------


def test_default_pipeline_factory_uses_the_durable_gcs_evidence_store() -> None:
    """The real Cloud Run Job entrypoint (`python -m setback.job.main`,
    exactly what `deploy.sh` runs) must read a resident's uploaded evidence
    from the same durable store the console wrote it to
    (`evidence.storage.GcsEvidenceStore`) -- not a fresh, per-execution,
    always-empty `ingest.tracker.UserUploadedDocumentSource`.

    Caught live in smoke loop #2: a real deployed tribunal run's Evidence
    Reviewer rejected a ground citing "neither photos nor plans were
    actually provided", for a case that genuinely had both files uploaded
    and visible on its console case page -- because `job.main`'s pipeline
    factory (unlike `console.app`'s `_build_production_app`, already fixed
    this wave) was never updated off its pre-`GcsEvidenceStore` default.
    Every real deployed job execution has been silently losing all
    resident-uploaded evidence since wave 4 introduced `GcsEvidenceStore`."""
    from setback.evidence.storage import GcsEvidenceStore
    from setback.job.main import _default_pipeline_factory
    from setback.job.pipeline import RealPipelineRunner

    runner = _default_pipeline_factory()

    assert isinstance(runner, RealPipelineRunner)
    assert isinstance(runner._document_source, GcsEvidenceStore)


# --- main() ----------------------------------------------------------------


def test_main_missing_case_id_exits_nonzero_without_building_store() -> None:
    built_store = False

    def _store_factory() -> CaseStore:
        nonlocal built_store
        built_store = True
        return InMemoryCaseStore()

    with pytest.raises(SystemExit) as exc_info:
        main(store_factory=_store_factory, pipeline_factory=_FakePipeline, env={})

    assert exc_info.value.code != 0
    assert built_store is False


def test_main_success_exits_zero() -> None:
    async def _seed() -> tuple[InMemoryCaseStore, str]:
        return await _seeded_store()

    import asyncio

    store, case_id = asyncio.run(_seed())
    pipeline = _FakePipeline()

    # main() exits 0 (i.e. does not raise SystemExit) on a successful run.
    main(
        store_factory=lambda: store,
        pipeline_factory=lambda: pipeline,
        env={"CASE_ID": case_id},
    )
    assert len(pipeline.calls) == 1


def test_main_failure_exits_nonzero() -> None:
    import asyncio

    store, case_id = asyncio.run(_seeded_store())
    pipeline = _FakePipeline(fail_with=RuntimeError("boom"))

    with pytest.raises(SystemExit) as exc_info:
        main(
            store_factory=lambda: store,
            pipeline_factory=lambda: pipeline,
            env={"CASE_ID": case_id},
        )
    assert exc_info.value.code != 0


def test_main_unknown_case_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            store_factory=InMemoryCaseStore,
            pipeline_factory=_FakePipeline,
            env={"CASE_ID": "ghost-case"},
        )
    assert exc_info.value.code != 0
