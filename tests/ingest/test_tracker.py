"""Tests for setback.ingest.tracker: the DocumentSource port and its
Georges River eTrack and user-upload adapters.

The eTrack WebForms search-postback dance has no committed fixture for its
intermediate steps (only the final documents-tab page is frozen), so those
steps are mocked with minimal synthetic WebForms markup shaped exactly per
the flow documented in docs/data-sources.md. The documents tab itself, and
the two PDF downloads, replay the real frozen fixtures. No network.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from setback.ingest.tracker import (
    ETRACK_DOWNLOAD_URL,
    ETRACK_SEARCH_URL,
    DocumentNotFoundError,
    EtrackDocumentSource,
    ExhibitedDocument,
    TrackerError,
    UserUploadedDocumentSource,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "nsw"

COUNCIL_REFERENCE = "DA2026/0359"
DETAIL_URL = "https://etrack.georgesriver.nsw.gov.au/Pages/XC.Track/SearchApplication.aspx?id=330796&a=DA2026%2f0359"

_SEARCH_FORM_HTML = """
<html><body><form>
<input type="hidden" id="__VIEWSTATE" value="vs-token" />
<input type="hidden" id="__VIEWSTATEGENERATOR" value="vsg-token" />
<input type="hidden" id="__EVENTVALIDATION" value="ev-token" />
</form></body></html>
"""


def _documents_html() -> str:
    return (FIXTURES_DIR / "etrack_documents_da2026-0359.html").read_text(encoding="utf-8")


def _mock_search_steps() -> None:
    respx.get(ETRACK_SEARCH_URL, params={"ApplicationNumber": COUNCIL_REFERENCE}).mock(
        return_value=httpx.Response(200, text=_SEARCH_FORM_HTML)
    )
    respx.post(ETRACK_SEARCH_URL, params={"ApplicationNumber": COUNCIL_REFERENCE}).mock(
        return_value=httpx.Response(302, headers={"Location": DETAIL_URL})
    )


# --- EtrackDocumentSource.list_documents -------------------------------------


@respx.mock
async def test_list_documents_parses_the_demo_document_register() -> None:
    _mock_search_steps()
    respx.get(url__startswith=DETAIL_URL.split("?")[0]).mock(
        return_value=httpx.Response(200, text=_documents_html())
    )

    source = EtrackDocumentSource()
    documents = await source.list_documents(COUNCIL_REFERENCE)

    assert len(documents) == 12
    assert all(doc.source == "etrack" for doc in documents)
    see = next(doc for doc in documents if "Statement of Environmental Effects" in doc.title)
    assert see.document_id == "5176594"
    assert see.size_bytes == 1568098


@respx.mock
async def test_list_documents_raises_on_missing_webforms_fields() -> None:
    respx.get(ETRACK_SEARCH_URL, params={"ApplicationNumber": COUNCIL_REFERENCE}).mock(
        return_value=httpx.Response(200, text="<html><body>no form here</body></html>")
    )

    source = EtrackDocumentSource()
    with pytest.raises(TrackerError):
        await source.list_documents(COUNCIL_REFERENCE)


@respx.mock
async def test_list_documents_retries_once_on_transient_search_failure() -> None:
    route = respx.get(ETRACK_SEARCH_URL, params={"ApplicationNumber": COUNCIL_REFERENCE}).mock(
        side_effect=[
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, text=_SEARCH_FORM_HTML),
        ]
    )
    respx.post(ETRACK_SEARCH_URL, params={"ApplicationNumber": COUNCIL_REFERENCE}).mock(
        return_value=httpx.Response(302, headers={"Location": DETAIL_URL})
    )
    respx.get(url__startswith=DETAIL_URL.split("?")[0]).mock(
        return_value=httpx.Response(200, text=_documents_html())
    )

    source = EtrackDocumentSource()
    documents = await source.list_documents(COUNCIL_REFERENCE)

    assert route.call_count == 2
    assert len(documents) == 12


@respx.mock
async def test_list_documents_does_not_retry_on_permanent_search_failure() -> None:
    route = respx.get(ETRACK_SEARCH_URL, params={"ApplicationNumber": COUNCIL_REFERENCE}).mock(
        return_value=httpx.Response(404, text="not found")
    )

    source = EtrackDocumentSource()
    with pytest.raises(TrackerError):
        await source.list_documents(COUNCIL_REFERENCE)

    assert route.call_count == 1


# --- EtrackDocumentSource.download_document ----------------------------------


@respx.mock
async def test_download_document_streams_the_requested_pdf() -> None:
    pdf_bytes = (FIXTURES_DIR / "docs" / "statement-of-environmental-effects.pdf").read_bytes()
    respx.get(ETRACK_DOWNLOAD_URL, params={"id": "5176594", "ext": "PDF"}).mock(
        return_value=httpx.Response(
            200, content=pdf_bytes, headers={"Content-Type": "application/pdf"}
        )
    )
    document = ExhibitedDocument(
        document_id="5176594",
        title="Statement of Environmental Effects",
        source="etrack",
        size_bytes=1568098,
    )

    source = EtrackDocumentSource()
    content = await source.download_document(document)

    assert content == pdf_bytes
    assert len(content) == 1568098


@respx.mock
async def test_download_document_gives_up_after_two_transient_failures() -> None:
    route = respx.get(ETRACK_DOWNLOAD_URL, params={"id": "5176594", "ext": "PDF"}).mock(
        return_value=httpx.Response(500, text="server error")
    )
    document = ExhibitedDocument(document_id="5176594", title="SEE", source="etrack")

    source = EtrackDocumentSource()
    with pytest.raises(TrackerError):
        await source.download_document(document)

    assert route.call_count == 2


# --- UserUploadedDocumentSource (the universal fallback) ---------------------


async def test_user_uploaded_source_lists_documents_added_for_a_case() -> None:
    source = UserUploadedDocumentSource()
    source.add_document("DA2026/0359", "upload-1", b"hello world")

    documents = await source.list_documents("DA2026/0359")

    assert len(documents) == 1
    assert documents[0].document_id == "upload-1"
    assert documents[0].source == "user-upload"
    assert documents[0].size_bytes == len(b"hello world")


async def test_user_uploaded_source_scopes_documents_by_da_number() -> None:
    source = UserUploadedDocumentSource()
    source.add_document("DA2026/0359", "upload-1", b"one")

    documents = await source.list_documents("DA2099/9999")

    assert documents == []


async def test_user_uploaded_source_downloads_previously_added_bytes() -> None:
    source = UserUploadedDocumentSource()
    source.add_document("DA2026/0359", "upload-1", b"the actual content")
    document = ExhibitedDocument(document_id="upload-1", title="upload-1", source="user-upload")

    content = await source.download_document(document)

    assert content == b"the actual content"


async def test_user_uploaded_source_raises_not_found_for_unknown_id() -> None:
    source = UserUploadedDocumentSource()
    document = ExhibitedDocument(document_id="missing", title="missing", source="user-upload")

    with pytest.raises(DocumentNotFoundError):
        await source.download_document(document)


async def test_user_uploaded_source_satisfies_the_document_source_port() -> None:
    """Both adapters must be usable interchangeably wherever a DocumentSource
    is expected -- this is the whole point of the port."""
    from setback.ingest.tracker import DocumentSource

    source: DocumentSource = UserUploadedDocumentSource()
    assert isinstance(source, DocumentSource)


async def test_user_uploaded_source_satisfies_the_evidence_upload_store_port() -> None:
    """`console.app`'s upload endpoint writes through `EvidenceUploadStore`
    -- the in-memory double must satisfy it exactly like
    `evidence.storage.GcsEvidenceStore` does in production."""
    from setback.ingest.tracker import EvidenceUploadStore

    source: EvidenceUploadStore = UserUploadedDocumentSource()
    assert isinstance(source, EvidenceUploadStore)


async def test_add_evidence_document_is_downloadable_afterwards() -> None:
    source = UserUploadedDocumentSource()
    await source.add_evidence_document(
        "case-1", "upload-1", b"the actual content", content_type="image/jpeg"
    )
    document = ExhibitedDocument(document_id="upload-1", title="upload-1", source="user-upload")

    content = await source.download_document(document)

    assert content == b"the actual content"
