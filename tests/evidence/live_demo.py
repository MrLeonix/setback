"""Manual, one-off live demo: grounds real elements on the real elevations
fixture and saves a checked-in annotated PNG demo asset.

NOT a pytest test (no `test_` prefix — never collected, never run in CI),
mirroring `tools/fetch_fixtures.py`'s "run manually, exercise the real
thing once" convention. Run with:

    uv run python tests/evidence/live_demo.py

Makes exactly ONE live model call (gemini-3.5-flash-lite via ADC on
`vexcourt-agent`), against `tests/fixtures/nsw/docs/elevations.pdf` page 1 —
well within this work package's 4-call live budget. Writes
`tests/fixtures/nsw/annotated/elevations-page1-grounded.png`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from setback.evidence.dossier import render_pdf_pages
from setback.evidence.grounding import ground_elements, render_overlay
from setback.models.client import ModelClient

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "nsw" / "docs" / "elevations.pdf"
OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "nsw"
    / "annotated"
    / "elevations-page1-grounded.png"
)

# Real labelled elements described in docs/data-sources.md for this fixture.
LABELS = [
    "window W.1",
    "window W.2",
    "window W.3",
    "door D.1",
    "9m height limit datum line",
]


async def main() -> None:
    pages = render_pdf_pages(FIXTURE.read_bytes())
    page = pages[0]

    client = ModelClient()  # real ADC, real Vertex AI call
    result = await ground_elements(client, page, LABELS)

    print(f"model: {result.model}")
    print(f"boxes found: {len(result.boxes)}")
    for box in result.boxes:
        print(f"  {box.label!r}: {box.bbox}")
    print(
        f"usage: prompt={result.usage.prompt_tokens} "
        f"output={result.usage.output_tokens} thinking={result.usage.thinking_tokens}"
    )

    overlay_bytes = render_overlay(page, result.boxes)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(overlay_bytes)
    print(f"wrote {OUTPUT} ({len(overlay_bytes)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
