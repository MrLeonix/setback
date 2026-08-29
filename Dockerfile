# syntax=docker/dockerfile:1
#
# Setback — single image for both Cloud Run deployables.
#
# `setback-console` (Cloud Run Service) runs this image's default CMD
# (uvicorn serving `setback.console.app:app`). `setback-tribunal` (Cloud
# Run Job) runs the *same* image with the command overridden at deploy
# time (`--command python --args -m,setback.job.main` — see deploy.sh) so
# there is exactly one artifact to build, scan, and version for both
# deployables, per docs/ARCHITECTURE.md §1.
#
# Python 3.12 (pyproject.toml pins `==3.12.*`), dependencies resolved from
# the committed `uv.lock` via `uv sync --frozen` (never a fresh resolve),
# non-root runtime user.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# ca-certificates: outbound HTTPS to Vertex AI / Firestore / the NSW ingest
# hosts (OnlineDA, ePlanning, eTrack) all need a working trust store.
# fonts-dejavu-core: `evidence/overlays.py::_label_font` needs a real TTF on
# disk for annotated-overlay label chips -- this base image ships none, so
# without it every deployed overlay silently fell back to PIL's own
# `ImageFont.load_default()`, a tiny bitmap font whose ~2px space glyph
# collapses a multi-word caption ("This element" -> "Thiselement") once
# anti-aliased/resized (found live, SMOKE.md wave 6/v5). DejaVu Sans is the
# specific family `_LABEL_FONT_PATHS` looks for at
# `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` on a Debian/Ubuntu base.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Pin uv itself so a Cloud Build re-run resolves dependencies identically.
RUN pip install --no-cache-dir uv==0.5.11

WORKDIR /app

# Dependency layer first (cached across rebuilds that only touch src/).
# `--no-install-project` skips installing `setback` itself here since
# `src/` isn't copied in yet -- keeps this layer's cache key to
# pyproject.toml/uv.lock/README.md only.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

# Now install the project itself against the already-resolved venv.
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# `job/pipeline.py`'s frozen-demo-fixture loader (the one shipped demo
# case, PAN-661190 -- see that module's docstring) resolves its fixtures
# directory via `Path(__file__).resolve().parents[3]`, which is correct
# for an editable/source-tree checkout (`src/setback/job/pipeline.py` ->
# repo root) but NOT for this image: `uv sync --no-editable` installs the
# package into the venv's site-packages, so at runtime `__file__` is
# `/app/.venv/lib/python3.12/site-packages/setback/job/pipeline.py` and
# `parents[3]` lands on `/app/.venv/lib/python3.12` rather than `/app`.
# Mirroring the fixtures at that exact resolved path is the minimal
# deploy-side fix that needs no change to `job/pipeline.py` (out of this
# change's lane) -- flagged in STATUS.md as a follow-up: `_FIXTURES_DIR`
# should be resolved via a packaged resource, not a `parents[N]` climb,
# since the latter is inherently broken under any non-editable install.
COPY tests/fixtures/nsw /app/.venv/lib/python3.12/tests/fixtures/nsw

# Non-root runtime user. Fixed uid/gid so Cloud Run's filesystem
# permissions are predictable across rebuilds.
RUN groupadd --gid 10001 setback \
    && useradd --uid 10001 --gid setback --no-create-home --shell /usr/sbin/nologin setback \
    && chown -R setback:setback /app
USER setback:setback

# Cloud Run injects PORT; uvicorn must bind 0.0.0.0:$PORT. Documented via
# EXPOSE for local `docker run`; the shell form below reads the real value.
EXPOSE 8080

# Default: the `setback-console` Cloud Run Service entrypoint.
# `setback-tribunal` overrides this with `--command`/`--args` at deploy
# time (see deploy.sh) to run `python -m setback.job.main` instead --
# CASE_ID is supplied per-execution via `--update-env-vars`, never baked
# into the image.
CMD ["sh", "-c", "exec uvicorn setback.console.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
