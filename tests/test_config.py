"""Tests for setback.config: model IDs, thinking levels, GCP defaults, and demo constants."""

from __future__ import annotations

import importlib

import pytest
from google.genai.types import ThinkingLevel

from setback import config


def test_interview_model_is_flash_lite_with_minimal_thinking() -> None:
    assert config.INTERVIEW.model == "gemini-3.5-flash-lite"
    assert config.INTERVIEW.thinking_level == ThinkingLevel.MINIMAL


def test_bench_model_is_gemini_3_7_flash_with_low_thinking() -> None:
    assert config.BENCH.model == "gemini-3.7-flash"
    assert config.BENCH.thinking_level == ThinkingLevel.LOW


def test_clerk_model_is_gemma_4_maas() -> None:
    assert config.CLERK.model == "gemma-4-26b-a4b-it-maas"


def test_vertex_location_is_global() -> None:
    assert config.VERTEX_LOCATION == "global"


def test_gcp_project_defaults_to_vexcourt_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SETBACK_GCP_PROJECT", raising=False)
    importlib.reload(config)
    assert config.GCP_PROJECT == "vexcourt-agent"


def test_gcp_project_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETBACK_GCP_PROJECT", "custom-project")
    importlib.reload(config)
    assert config.GCP_PROJECT == "custom-project"
    monkeypatch.delenv("SETBACK_GCP_PROJECT", raising=False)
    importlib.reload(config)


def test_gcs_bucket_defaults_to_vexcourt_agent_setback_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SETBACK_GCS_BUCKET", raising=False)
    importlib.reload(config)
    assert config.GCS_BUCKET == "vexcourt-agent-setback-corpus"


def test_gcs_bucket_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETBACK_GCS_BUCKET", "custom-bucket")
    importlib.reload(config)
    assert config.GCS_BUCKET == "custom-bucket"
    monkeypatch.delenv("SETBACK_GCS_BUCKET", raising=False)
    importlib.reload(config)


def test_region_defaults_to_australia_southeast1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SETBACK_REGION", raising=False)
    importlib.reload(config)
    assert config.REGION == "australia-southeast1"


def test_region_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETBACK_REGION", "us-central1")
    importlib.reload(config)
    assert config.REGION == "us-central1"
    monkeypatch.delenv("SETBACK_REGION", raising=False)
    importlib.reload(config)


def test_firestore_db_defaults_to_default_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SETBACK_FIRESTORE_DB", raising=False)
    importlib.reload(config)
    assert config.FIRESTORE_DB == "(default)"


def test_firestore_db_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETBACK_FIRESTORE_DB", "setback-au")
    importlib.reload(config)
    assert config.FIRESTORE_DB == "setback-au"
    monkeypatch.delenv("SETBACK_FIRESTORE_DB", raising=False)
    importlib.reload(config)


def test_gcs_uploads_bucket_defaults_to_setback_au(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SETBACK_GCS_UPLOADS_BUCKET", raising=False)
    importlib.reload(config)
    assert config.GCS_UPLOADS_BUCKET == "vexcourt-agent-setback-au"


def test_gcs_uploads_bucket_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETBACK_GCS_UPLOADS_BUCKET", "custom-uploads-bucket")
    importlib.reload(config)
    assert config.GCS_UPLOADS_BUCKET == "custom-uploads-bucket"
    monkeypatch.delenv("SETBACK_GCS_UPLOADS_BUCKET", raising=False)
    importlib.reload(config)


def test_total_budget_ceiling_matches_project_cap() -> None:
    assert config.TOTAL_BUDGET_CEILING_USD == 62.0


def test_demo_case_constants() -> None:
    assert config.DEMO_DA_NUMBER == "PAN-661190"
    assert config.DEMO_COUNCIL == "Georges River Council"
    assert config.DEMO_ADDRESS == "65A Vista Street, Sans Souci NSW 2219"
    assert config.DEMO_LOT_DP == "Lot 4 DP232626"
