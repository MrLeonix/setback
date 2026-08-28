"""Council document-tracker clients for exhibited Development Application documents.

Defines a small `DocumentSource` port so any way a document reaches Setback
-- a council's public tracker (NSW's eTrack ASP.NET WebForms register today,
an ePathway-based council's tracker later) or a resident's own upload --
plugs in as an equally first-class implementation of the same shape.
`EtrackDocumentSource` implements the port against Georges River Council's
eTrack instance, an undocumented search-postback-then-scrape flow (see
``docs/data-sources.md``); `UserUploadedDocumentSource` implements it as the
universal fallback for documents that were never exhibited on any tracker,
or for a council with no tracker at all.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

import httpx

_REQUEST_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(30.0, connect=5.0)
_TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({500, 502, 503, 504})

ETRACK_BASE: Final[str] = "https://etrack.georgesriver.nsw.gov.au"
ETRACK_SEARCH_URL: Final[str] = f"{ETRACK_BASE}/Pages/XC.Track/SearchApplication.aspx"
ETRACK_DOWNLOAD_URL: Final[str] = f"{ETRACK_BASE}/Common/Integration/FileDownload.ashx"

_VIEWSTATE_RE: Final[re.Pattern[str]] = re.compile(r'id="__VIEWSTATE" value="([^"]*)"')
_VIEWSTATE_GENERATOR_RE: Final[re.Pattern[str]] = re.compile(
    r'id="__VIEWSTATEGENERATOR" value="([^"]*)"'
)
_EVENT_VALIDATION_RE: Final[re.Pattern[str]] = re.compile(r'id="__EVENTVALIDATION" value="([^"]*)"')
_DOCUMENT_ROW_RE: Final[re.Pattern[str]] = re.compile(r"<tr[^>]*>.*?</tr>", re.S)
_DOCUMENT_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"FileDownload\.ashx\?id=(\d+)&amp;ext=(\w+)&amp;filesize=(\d+)"
)


class TrackerError(RuntimeError):
    """Raised when a document-tracker request fails or returns an unusable payload."""


class DocumentNotFoundError(TrackerError):
    """Raised when a requested document id is not present on the source."""


@dataclass(frozen=True, slots=True)
class ExhibitedDocument:
    """A single document exhibited for a Development Application, from any source."""

    document_id: str
    title: str
    source: str
    size_bytes: int | None = None


@runtime_checkable
class DocumentSource(Protocol):
    """A source of a DA's exhibited documents: a council tracker vendor, or a
    resident's own upload. Every implementation is interchangeable so the
    rest of Setback never needs to special-case which source produced a
    document."""

    async def list_documents(self, da_number: str) -> list[ExhibitedDocument]:
        """List the documents currently available for `da_number`."""
        ...

    async def download_document(self, document: ExhibitedDocument) -> bytes:
        """Download the full bytes of a previously listed `document`."""
        ...


async def _send_with_retry(send: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
    """Send once, retrying exactly once more on a transient failure (a
    connection/timeout error, or a 500/502/503/504 response)."""
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            response = await send()
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _TRANSIENT_STATUS_CODES:
                raise TrackerError(f"tracker request returned {exc.response.status_code}") from exc
            last_exc = exc
        except httpx.TransportError as exc:
            last_exc = exc
    raise TrackerError("tracker request did not succeed after retry") from last_exc


def _parse_documents(html: str) -> list[ExhibitedDocument]:
    documents: list[ExhibitedDocument] = []
    seen_ids: set[str] = set()
    for row in _DOCUMENT_ROW_RE.findall(html):
        match = _DOCUMENT_LINK_RE.search(row)
        if not match:
            continue
        file_id = match.group(1)
        if file_id in seen_ids:
            continue
        seen_ids.add(file_id)
        description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip()
        documents.append(
            ExhibitedDocument(
                document_id=file_id,
                title=description,
                source="etrack",
                size_bytes=int(match.group(3)),
            )
        )
    return documents


class EtrackDocumentSource:
    """`DocumentSource` adapter for Georges River Council's eTrack WebForms register.

    Implements the undocumented search-postback-then-scrape flow recorded in
    ``docs/data-sources.md``: GET the search form for its ASP.NET viewstate
    fields, POST the search (following the redirect to the application's
    detail page), then GET that page's documents tab and parse its
    file-download links. Documents are downloaded via
    ``FileDownload.ashx?id=<document_id>``.
    """

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, follow_redirects=True)
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if this instance constructed it."""
        if self._owns_client:
            await self._client.aclose()

    async def _submit_search(self, council_reference: str) -> httpx.URL:
        """Run the ASP.NET WebForms search postback and return the resulting
        application detail page's URL.

        Raises:
            TrackerError: The search form is missing its WebForms hidden fields.
        """
        search_page = await _send_with_retry(
            lambda: self._client.get(
                ETRACK_SEARCH_URL, params={"ApplicationNumber": council_reference}
            )
        )
        viewstate = _VIEWSTATE_RE.search(search_page.text)
        viewstate_gen = _VIEWSTATE_GENERATOR_RE.search(search_page.text)
        event_validation = _EVENT_VALIDATION_RE.search(search_page.text)
        if not (viewstate and viewstate_gen and event_validation):
            raise TrackerError("eTrack search page is missing expected WebForms hidden fields")

        result = await _send_with_retry(
            lambda: self._client.post(
                ETRACK_SEARCH_URL,
                params={"ApplicationNumber": council_reference},
                data={
                    "__VIEWSTATE": viewstate.group(1),
                    "__VIEWSTATEGENERATOR": viewstate_gen.group(1),
                    "__EVENTVALIDATION": event_validation.group(1),
                    "ctl00$ctMain$search$txtSearch": council_reference,
                    "ctl00$ctMain$search$btnSearch": "Search",
                },
                # An intermediate 302 declares gzip encoding on an empty body,
                # which trips httpx's decoder; request uncompressed responses.
                headers={"Accept-Encoding": "identity"},
            )
        )
        return result.url

    async def list_documents(self, da_number: str) -> list[ExhibitedDocument]:
        """List the eTrack documents tab for `da_number` (a council reference,
        e.g. "DA2026/0359")."""
        detail_url = await self._submit_search(da_number)
        documents_url = detail_url.copy_merge_params({"p": "y"})
        documents_page = await _send_with_retry(lambda: self._client.get(documents_url))
        return _parse_documents(documents_page.text)

    async def download_document(self, document: ExhibitedDocument) -> bytes:
        """Download `document`'s bytes via eTrack's `FileDownload.ashx`."""
        response = await _send_with_retry(
            lambda: self._client.get(
                ETRACK_DOWNLOAD_URL, params={"id": document.document_id, "ext": "PDF"}
            )
        )
        return response.content


class UserUploadedDocumentSource:
    """`DocumentSource` adapter for resident-supplied documents.

    The universal fallback: works for any council (including ones with no
    tracker at all) and for documents a resident holds that were never
    exhibited anywhere. Backed by an in-memory mapping populated via
    `add_document`; a future package can swap in a Firestore/GCS-backed
    implementation of the identical port without touching any caller.
    """

    def __init__(self) -> None:
        self._documents_by_da: dict[str, dict[str, bytes]] = {}

    def add_document(self, da_number: str, document_id: str, content: bytes) -> None:
        """Register an uploaded document's bytes under `da_number`."""
        self._documents_by_da.setdefault(da_number, {})[document_id] = content

    async def list_documents(self, da_number: str) -> list[ExhibitedDocument]:
        """List the documents a resident has uploaded for `da_number`."""
        documents = self._documents_by_da.get(da_number, {})
        return [
            ExhibitedDocument(
                document_id=document_id,
                title=document_id,
                source="user-upload",
                size_bytes=len(content),
            )
            for document_id, content in documents.items()
        ]

    async def download_document(self, document: ExhibitedDocument) -> bytes:
        """Return the previously uploaded bytes for `document`.

        Raises:
            DocumentNotFoundError: `document.document_id` was never uploaded.
        """
        for documents in self._documents_by_da.values():
            if document.document_id in documents:
                return documents[document.document_id]
        raise DocumentNotFoundError(f"no uploaded document with id {document.document_id!r}")
