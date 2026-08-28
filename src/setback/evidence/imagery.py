"""Street View fallback imagery: proven live (`spike-mapsSolar.md`) for the
resident's "away from home" or no-photo-available case.

The Solar API and Aerial View API were cut after live testing found no
Australia coverage for either (five 404s across five points including a
sanity-check on the Sydney Opera House, and Aerial View's own docs are
explicit it is US-postal-address-only) — do not build anything for them;
this module ships Street View only.

Flow, per the spike: call the metadata endpoint first (free, unquota'd) and
only fetch the actual image (a paid call) if its `status` is `"OK"` — a
`ZERO_RESULTS`/`NOT_FOUND` metadata response is an expected "no coverage
here" outcome, returned as `None`, never an exception. A successful result
is always graded :attr:`~setback.evidence.dossier.ProvenanceGrade.STREET_VIEW_SOLAR_FALLBACK`
and carries a visible attribution string, `"(c) Google Street View,
<capture date>"`, surfaced to the UI per Google's Street View attribution
policy — the imagery is archival (the spike's own test pano was dated
2022-02, four years stale as of the hackathon), never presented as current
site condition.

The API key is never a literal in this module: it is read from GCP Secret
Manager *by reference* at call time via an injectable `secret_accessor`
callable (a fake in every test; the real one lazily constructs a Secret
Manager client, mirroring `models/client.py`'s `_default_token_provider`
pattern). The secret's live name, confirmed by the spike, is
``maps-api-key`` on project ``vexcourt-agent`` — ARCHITECTURE.md's
placeholder name ``setback-maps-key`` predates that secret actually being
created and should be reconciled by whoever next touches that doc.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import httpx

from setback.config import GCP_PROJECT
from setback.evidence.dossier import ProvenanceGrade

STREET_VIEW_METADATA_URL: Final[str] = "https://maps.googleapis.com/maps/api/streetview/metadata"
STREET_VIEW_IMAGE_URL: Final[str] = "https://maps.googleapis.com/maps/api/streetview"

DEFAULT_MAPS_SECRET_ID: Final[str] = "maps-api-key"
"""The live Secret Manager secret id on `vexcourt-agent`, confirmed by the
spike (`spike-mapsSolar.md`) — not the placeholder name in ARCHITECTURE.md."""

_REQUEST_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(10.0, connect=5.0)
_TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({500, 502, 503, 504})

_DEFAULT_IMAGE_SIZE: Final[str] = "640x400"
_DEFAULT_FOV: Final[int] = 80

SecretAccessor = Callable[[], str]
"""A zero-argument callable returning a secret's current value. Tests inject
a fake; production uses :func:`default_secret_accessor`."""


class StreetViewUnavailableError(RuntimeError):
    """Raised when a Street View request fails after retrying once, or
    returns an unusable payload. Never raised for a plain "no coverage
    here" metadata response — see :func:`fetch_street_view_fallback`."""


def default_secret_accessor(
    secret_id: str = DEFAULT_MAPS_SECRET_ID, *, project: str = GCP_PROJECT
) -> SecretAccessor:
    """Build a `SecretAccessor` that reads `secret_id`'s latest version from
    GCP Secret Manager on `project`, resolved lazily on first call.

    Never used in tests: every test injects a fake accessor instead, so no
    test ever needs live Secret Manager access or a real key.
    """

    def accessor() -> str:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(name=name)
        value = response.payload.data.decode("utf-8")
        return value

    return accessor


@dataclass(frozen=True, slots=True)
class StreetViewMetadata:
    """The Street View metadata endpoint's response for one location."""

    status: str
    pano_id: str | None
    capture_date: str | None
    copyright: str | None


@dataclass(frozen=True, slots=True)
class StreetViewFallback:
    """A fetched Street View fallback image, graded and attributed, ready
    to become an :class:`~setback.evidence.dossier.EvidenceAnchor` via
    `dossier.render_photo`."""

    image_bytes: bytes
    provenance_grade: ProvenanceGrade
    attribution: str
    metadata: StreetViewMetadata


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, *, params: dict[str, str]
) -> httpx.Response:
    """GET `url`, retrying exactly once more on a transient failure (a
    connection/timeout error, or a 500/502/503/504 response) — the same
    one-retry convention `setback.ingest` uses throughout."""
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _TRANSIENT_STATUS_CODES:
                raise StreetViewUnavailableError(
                    f"Street View request to {url} returned {exc.response.status_code}"
                ) from exc
            last_exc = exc
        except httpx.TransportError as exc:
            last_exc = exc
    raise StreetViewUnavailableError(
        f"Street View request to {url} did not succeed after retry"
    ) from last_exc


async def _fetch_metadata(
    client: httpx.AsyncClient, lat: float, lng: float, *, api_key: str
) -> StreetViewMetadata:
    response = await _get_with_retry(
        client, STREET_VIEW_METADATA_URL, params={"location": f"{lat},{lng}", "key": api_key}
    )
    try:
        raw = response.json()
    except ValueError as exc:
        raise StreetViewUnavailableError(
            "Street View metadata response was not valid JSON"
        ) from exc
    if not isinstance(raw, dict) or "status" not in raw:
        raise StreetViewUnavailableError("Street View metadata response is missing a status field")
    return StreetViewMetadata(
        status=str(raw["status"]),
        pano_id=raw.get("pano_id"),
        capture_date=raw.get("date"),
        copyright=raw.get("copyright"),
    )


async def fetch_street_view_fallback(
    lat: float,
    lng: float,
    *,
    client: httpx.AsyncClient | None = None,
    secret_accessor: SecretAccessor | None = None,
    size: str = _DEFAULT_IMAGE_SIZE,
    fov: int = _DEFAULT_FOV,
) -> StreetViewFallback | None:
    """Fetch a Street View fallback image for `(lat, lng)`, checking the
    free metadata endpoint first and only paying for the image if coverage
    exists.

    Args:
        lat: Latitude of the site.
        lng: Longitude of the site.
        client: An injectable `httpx.AsyncClient` (tests use respx against
            a default-constructed one). When omitted, one is constructed
            and closed for the duration of this call.
        secret_accessor: An injectable callable returning the Maps API key
            (tests inject a fake). Defaults to
            :func:`default_secret_accessor`, a live Secret Manager read.
        size: The Street View Static API image size, `"WxH"`.
        fov: The Street View Static API field of view, in degrees.

    Returns:
        The fetched, graded, attributed fallback image, or `None` if
        Street View has no coverage for this location (metadata `status`
        is not `"OK"`) — an expected outcome, not a failure.

    Raises:
        StreetViewUnavailableError: A request failed after retrying once,
            or returned an unusable payload.
    """
    accessor = secret_accessor or default_secret_accessor()
    api_key = accessor()

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
    try:
        metadata = await _fetch_metadata(http_client, lat, lng, api_key=api_key)
        if metadata.status != "OK":
            return None

        response = await _get_with_retry(
            http_client,
            STREET_VIEW_IMAGE_URL,
            params={"location": f"{lat},{lng}", "size": size, "fov": str(fov), "key": api_key},
        )
        attribution = f"(c) Google Street View, {metadata.capture_date or 'date unknown'}"
        return StreetViewFallback(
            image_bytes=response.content,
            provenance_grade=ProvenanceGrade.STREET_VIEW_SOLAR_FALLBACK,
            attribution=attribution,
            metadata=metadata,
        )
    finally:
        if owns_client:
            await http_client.aclose()
