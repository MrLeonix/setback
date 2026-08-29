"""Central configuration: model IDs, thinking levels, GCP defaults, budgets.

Values here are read once at import time from environment variables, falling
back to the `vexcourt-agent` GCP project defaults. Re-import (or
``importlib.reload``) after mutating the environment to pick up changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from google.genai.types import ThinkingLevel

# --- Vertex AI location -------------------------------------------------

VERTEX_LOCATION = "global"
"""Required Vertex AI location for both gemini-3.5-flash-lite and gemini-3.7-flash."""


@dataclass(frozen=True)
class ModelConfig:
    """A model identifier paired with the thinking level it is called at."""

    model: str
    thinking_level: ThinkingLevel


# --- Models ---------------------------------------------------------------

INTERVIEW = ModelConfig(
    model="gemini-3.5-flash-lite",
    thinking_level=ThinkingLevel.MINIMAL,
)
"""The resident-facing interview model: cheapest eligible tier, no measured thinking spend."""

BENCH = ModelConfig(
    model="gemini-3.7-flash",
    thinking_level=ThinkingLevel.LOW,
)
"""The adjudication bench model. LOW is its effective floor — MINIMAL returns HTTP 400."""

CLERK = ModelConfig(
    model="gemma-4-26b-a4b-it-maas",
    thinking_level=ThinkingLevel.THINKING_LEVEL_UNSPECIFIED,
)
"""Low-cost clerical extraction via the Gemma MaaS OpenAI-compatible endpoint (no GPU)."""

# --- GCP -------------------------------------------------------------------

GCP_PROJECT = os.environ.get("SETBACK_GCP_PROJECT", "vexcourt-agent")
"""GCP project ID, overridable via SETBACK_GCP_PROJECT.

The hackathon's actual GCP project id is ``vexcourt-agent``; its Cloud
Console *display name* is "Setback", but the project id itself was never
renamed to match (GCP project ids are immutable post-creation) — a project
literally named ``setback-app`` does not exist. Default to the real id so
callers that don't set the env var still hit a live project.
"""

GCS_BUCKET = os.environ.get("SETBACK_GCS_BUCKET", "vexcourt-agent-setback-corpus")
"""GCS bucket for the case corpus, overridable via SETBACK_GCS_BUCKET.

Default follows the same ``<project-id>-setback-*`` naming convention as
:data:`GCS_UPLOADS_BUCKET` (the previous ``setback-app-corpus`` default named
a bucket that was never actually created under that name).
"""

REGION = os.environ.get("SETBACK_REGION", "australia-southeast1")
"""Default GCP region for this wave's Sydney-region resources (Cloud Run,
Firestore, GCS), overridable via SETBACK_REGION. Pre-wave-4 resources in
``us-central1`` are unaffected by this default — they're addressed
directly (e.g. the ``(default)`` Firestore database stays in
us-central1; see :data:`FIRESTORE_DB`)."""

FIRESTORE_DB = os.environ.get("SETBACK_FIRESTORE_DB", "(default)")
"""Firestore database id, overridable via SETBACK_FIRESTORE_DB.

Defaults to ``"(default)"`` — the original us-central1 database — so
local development and any not-yet-migrated environment keep working
unchanged. This wave's Sydney deployment sets this to ``"setback-au"``,
the named australia-southeast1 database."""

GCS_UPLOADS_BUCKET = os.environ.get("SETBACK_GCS_UPLOADS_BUCKET", "vexcourt-agent-setback-au")
"""GCS bucket for resident-uploaded case evidence, overridable via
SETBACK_GCS_UPLOADS_BUCKET. Defaults to this wave's australia-southeast1
bucket (object layout: ``cases/{case_id}/uploads/{sha256}.{ext}``)."""

# --- Budget ceilings ---------------------------------------------------------

TOTAL_BUDGET_CEILING_USD = 62.0
"""Hard ceiling on total model spend across the hackathon build."""

DEMO_RUN_BUDGET_CEILING_USD = 2.0
"""Soft per-run ceiling for a single end-to-end demo case, well under the total."""

# --- Demo case --------------------------------------------------------------

DEMO_DA_NUMBER = "PAN-661190"
"""The Development Application number used for the demo case."""

DEMO_COUNCIL = "Georges River Council"
"""The consent authority for the demo case."""

DEMO_ADDRESS = "65A Vista Street, Sans Souci NSW 2219"
"""The subject property address for the demo case."""

DEMO_LOT_DP = "Lot 4 DP232626"
"""The lot/deposited-plan identifier for the demo case."""
