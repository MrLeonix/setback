"""Root pytest conftest: make the repo root importable so tests can reach
``tools/`` (a plain script directory, not part of the installed ``setback``
package) via ``import tools.fetch_fixtures``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
