"""Tests for setback.evidence.storage.GcsEvidenceStore: the durable,
container-boundary-crossing evidence store that replaces console-process
in-memory uploads.

Fully offline: `_FakeGcsClient` implements just the `google-cloud-storage`
surface this module actually calls (`.bucket(name).blob(path)`,
`blob.upload_from_string`, `client.list_blobs(bucket, prefix=...)`,
`blob.download_as_bytes()`, `blob.size`), so these tests exercise the real
`GcsEvidenceStore` code paths without installing/authenticating a live GCS
client or touching the network.
"""

from __future__ import annotations

import pytest

from setback.evidence.storage import GcsEvidenceStore
from setback.ingest.tracker import DocumentNotFoundError, DocumentSource, ExhibitedDocument


class _FakeBlob:
    def __init__(self, name: str, store: dict[str, tuple[bytes, str | None]]) -> None:
        self.name = name
        self._store = store

    def upload_from_string(self, content: bytes, content_type: str | None = None) -> None:
        self._store[self.name] = (content, content_type)

    def download_as_bytes(self) -> bytes:
        return self._store[self.name][0]

    @property
    def size(self) -> int:
        return len(self._store[self.name][0])


class _FakeBucket:
    def __init__(self, store: dict[str, tuple[bytes, str | None]]) -> None:
        self._store = store

    def blob(self, path: str) -> _FakeBlob:
        return _FakeBlob(path, self._store)


class _FakeGcsClient:
    """Records every blob ever written, keyed by full object path, across
    every bucket -- good enough for these tests, which only ever address
    one bucket name."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[bytes, str | None]] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._store)

    def list_blobs(self, bucket_name: str, prefix: str = "") -> list[_FakeBlob]:
        return [
            _FakeBlob(name, self._store) for name in sorted(self._store) if name.startswith(prefix)
        ]


@pytest.fixture
def client() -> _FakeGcsClient:
    return _FakeGcsClient()


@pytest.fixture
def store(client: _FakeGcsClient) -> GcsEvidenceStore:
    return GcsEvidenceStore(bucket_name="test-corpus", client=client)


async def test_add_evidence_document_writes_to_the_expected_object_path(
    store: GcsEvidenceStore, client: _FakeGcsClient
) -> None:
    await store.add_evidence_document(
        "case-1", "doc-1", b"pdf bytes here", content_type="application/pdf"
    )

    assert "cases/case-1/uploads/doc-1.pdf" in client._store  # noqa: SLF001 -- white-box assertion
    content, content_type = client._store["cases/case-1/uploads/doc-1.pdf"]  # noqa: SLF001
    assert content == b"pdf bytes here"
    assert content_type == "application/pdf"


async def test_add_evidence_document_defaults_to_bin_extension_for_unknown_content_type(
    store: GcsEvidenceStore, client: _FakeGcsClient
) -> None:
    await store.add_evidence_document("case-1", "doc-2", b"raw bytes", content_type=None)

    assert "cases/case-1/uploads/doc-2.bin" in client._store  # noqa: SLF001


async def test_add_evidence_document_is_idempotent_on_retry(
    store: GcsEvidenceStore, client: _FakeGcsClient
) -> None:
    await store.add_evidence_document("case-1", "doc-1", b"v1", content_type="image/jpeg")
    await store.add_evidence_document("case-1", "doc-1", b"v1", content_type="image/jpeg")

    matching = [name for name in client._store if "doc-1" in name]  # noqa: SLF001
    assert len(matching) == 1


async def test_list_documents_lists_only_the_named_case(
    store: GcsEvidenceStore,
) -> None:
    await store.add_evidence_document("case-1", "doc-1", b"a", content_type="image/jpeg")
    await store.add_evidence_document("case-2", "doc-2", b"b", content_type="image/jpeg")

    documents = await store.list_documents("case-1")

    assert len(documents) == 1
    assert documents[0].document_id == "doc-1"
    assert documents[0].case_id == "case-1"
    assert documents[0].size_bytes == 1


async def test_download_document_returns_previously_uploaded_bytes(
    store: GcsEvidenceStore,
) -> None:
    await store.add_evidence_document(
        "case-1", "doc-1", b"the actual content", content_type="application/pdf"
    )
    document = ExhibitedDocument(document_id="doc-1", title="doc-1", source="gcs", case_id="case-1")

    content = await store.download_document(document)

    assert content == b"the actual content"


async def test_download_document_raises_not_found_for_unknown_document(
    store: GcsEvidenceStore,
) -> None:
    document = ExhibitedDocument(
        document_id="missing", title="missing", source="gcs", case_id="case-1"
    )

    with pytest.raises(DocumentNotFoundError):
        await store.download_document(document)


async def test_download_document_raises_not_found_without_a_case_id(
    store: GcsEvidenceStore,
) -> None:
    document = ExhibitedDocument(document_id="doc-1", title="doc-1", source="gcs")

    with pytest.raises(DocumentNotFoundError):
        await store.download_document(document)


async def test_gcs_evidence_store_satisfies_the_document_source_port(
    store: GcsEvidenceStore,
) -> None:
    assert isinstance(store, DocumentSource)


async def test_gcs_evidence_store_satisfies_the_evidence_upload_store_port(
    store: GcsEvidenceStore,
) -> None:
    from setback.ingest.tracker import EvidenceUploadStore

    assert isinstance(store, EvidenceUploadStore)
