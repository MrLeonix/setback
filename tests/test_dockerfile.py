"""Guards a live-deploy defect at the `Dockerfile` level: the image installed
no font package at all, so `evidence.overlays._label_font` (see that
module) silently fell back to PIL's own tiny bitmap default font for every
annotated-overlay label chip on the deployed console -- found live,
SMOKE.md wave 6/v5. `_LABEL_FONT_PATHS` looks for DejaVu Sans at
`/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` on this image's
Debian/Ubuntu base, which only exists once `fonts-dejavu-core` is
installed.

A plain text/parse check on the `Dockerfile` itself, not a real image
build (this repo's offline test suite makes no Docker/network calls) --
enough to catch a regression where the apt-get line is edited again
without this package, without needing to actually build the image.
"""

from __future__ import annotations

from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


def _apt_get_install_lines() -> list[str]:
    """Every line of the `Dockerfile` that is part of an `apt-get install`
    invocation -- including its shell line-continuations (`\\`), so a
    package listed on a continuation line (not literally on the same
    physical line as `apt-get install`) is still found."""
    lines = _DOCKERFILE.read_text().splitlines()
    collected: list[str] = []
    in_install_run = False
    for line in lines:
        stripped = line.strip()
        if "apt-get install" in stripped:
            in_install_run = True
        if in_install_run:
            collected.append(stripped)
            if not stripped.endswith("\\"):
                in_install_run = False
    return collected


def test_dockerfile_installs_a_font_package_for_overlay_label_chips() -> None:
    install_block = " ".join(_apt_get_install_lines())
    assert "fonts-dejavu-core" in install_block, (
        "the image must install a real TTF font package (fonts-dejavu-core) so "
        "evidence/overlays.py's _label_font() finds a real font at "
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf instead of silently "
        "falling back to PIL's tiny bitmap default -- see this module's docstring"
    )


def test_dockerfile_still_installs_ca_certificates() -> None:
    """Guards against the font-package fix accidentally clobbering the
    pre-existing `ca-certificates` install (both belong on the same
    `apt-get install` line)."""
    install_block = " ".join(_apt_get_install_lines())
    assert "ca-certificates" in install_block
