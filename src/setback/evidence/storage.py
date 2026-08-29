"""GCS-backed evidence store: durable uploads shared between the console
service and a `setback-tribunal` Cloud Run Job execution running in an
entirely separate container.

The MVP gap this module closes: `ingest.tracker.UserUploadedDocumentSource`
keeps a resident's uploaded photo/document bytes only in the console
process's own memory (see that class's docstring, and
`job.pipeline`'s "known gaps" note) -- a real Cloud Run Job execution has no
access to that memory at all, so a live tribunal run degrades to
citation-only grounds rather than seeing the evidence a resident actually
uploaded. `GcsEvidenceStore` implements the same two ports
(`ingest.tracker.DocumentSource`, for reading; `ingest.tracker.
EvidenceUploadStore`, for writing) against Google Cloud Storage instead of
process memory, so the console's upload endpoint and a job execution in a
different container agree on exactly the same durable object.

Objects are written at ``cases/{case_id}/uploads/{document_id}.{ext}`` in
the bucket named by `config.GCS_UPLOADS_BUCKET` -- `document_id` is caller-supplied
(the console computes it as `sha256(content).hexdigest()[:16]`, the same id
already used for durable case events elsewhere), so this store never needs
its own id scheme, and `ext` is guessed from the upload's content type
(default ``bin`` when none is given or recognised).

All Google Cloud Storage SDK calls are synchronous under the hood; every
one here runs via `asyncio.to_thread` so this store never blocks the
console's event loop or an async caller more generally.
"""

from __future__ import annotations

import asyncio
import mimetypes
from typing import Any

# `google-cloud-storage` ships no `py.typed` marker (unlike `google-cloud-
# firestore`/`google-cloud-secret-manager`, both already used elsewhere in
# this codebase), so mypy cannot resolve `storage` as an attribute of the
# `google.cloud` namespace package under strict mode.
from google.cloud import storage  # type: ignore[attr-defined]

from setback import config
from setback.ingest.tracker import DocumentNotFoundError, ExhibitedDocument

_DEFAULT_EXTENSION = "bin"


def _extension_for(content_type: str | None) -> str:
    """The file extension (no leading dot) to store `content_type` under,
    falling back to `_DEFAULT_EXTENSION` when `content_type` is missing or
    unrecognised."""
    if not content_type:
        return _DEFAULT_EXTENSION
    guessed = mimetypes.guess_extension(content_type)
    if not guessed:
        return _DEFAULT_EXTENSION
    return guessed.lstrip(".")


class GcsEvidenceStore:
    """`DocumentSource` + `EvidenceUploadStore` adapter backed by Google
    Cloud Storage. See the module docstring for the object-path scheme.
    """

    def __init__(self, *, bucket_name: str | None = None, client: Any | None = None) -> None:
        """Construct a store. `client` is injectable so tests run fully
        offline against a fake object implementing just the small
        `google-cloud-storage` surface this class actually calls (see
        `tests/evidence/test_storage.py`'s `_FakeGcsClient`); production
        (the default) constructs a real `google.cloud.storage.Client()`,
        which resolves credentials via ADC and makes no network call by
        itself at construction time."""
        self._bucket_name = bucket_name or config.GCS_UPLOADS_BUCKET
        self._client = client if client is not None else storage.Client()

    def _blob_path(self, case_id: str, document_id: str, ext: str) -> str:
        return f"cases/{case_id}/uploads/{document_id}.{ext}"

    def _upload_blob(self, blob_path: str, content: bytes, content_type: str | None) -> None:
        bucket = self._client.bucket(self._bucket_name)
        blob = bucket.blob(blob_path)
        blob.upload_from_string(content, content_type=content_type or "application/octet-stream")

    async def add_evidence_document(
        self, case_id: str, document_id: str, content: bytes, *, content_type: str | None = None
    ) -> None:
        """Upload `content` to ``cases/{case_id}/uploads/{document_id}.{ext}``,
        `ext` guessed from `content_type`. Overwrites in place on a retried
        upload of the same `(case_id, document_id)` -- idempotent, matching
        this build's content-hash id convention (retrying the same upload
        is a no-op, never a duplicate object)."""
        blob_path = self._blob_path(case_id, document_id, _extension_for(content_type))
        await asyncio.to_thread(self._upload_blob, blob_path, content, content_type)

    async def list_documents(self, da_number: str) -> list[ExhibitedDocument]:
        """List every document uploaded for the case `da_number` names.

        This store is keyed by Firestore case id, not a council DA number
        (see the module docstring) -- the parameter name is kept only to
        satisfy `DocumentSource`'s existing signature; every returned
        `ExhibitedDocument.case_id` is set to `da_number` so a subsequent
        `download_document` call can locate the object again.
        """
        prefix = f"cases/{da_number}/uploads/"
        blobs = await asyncio.to_thread(
            lambda: list(self._client.list_blobs(self._bucket_name, prefix=prefix))
        )
        documents: list[ExhibitedDocument] = []
        for blob in blobs:
            filename = blob.name.rsplit("/", 1)[-1]
            document_id = filename.rsplit(".", 1)[0] if "." in filename else filename
            documents.append(
                ExhibitedDocument(
                    document_id=document_id,
                    title=filename,
                    source="gcs",
                    size_bytes=blob.size,
                    case_id=da_number,
                )
            )
        return documents

    async def download_document(self, document: ExhibitedDocument) -> bytes:
        """Download `document`'s bytes.

        Requires `document.case_id` (as every `ExhibitedDocument` this
        store's own `list_documents` returns, and as `job.pipeline`
        constructs when reading uploads back for a specific case, always
        carries) -- raises `DocumentNotFoundError` otherwise, since this
        store has no way to locate an object without knowing which case's
        prefix to search.

        Raises:
            DocumentNotFoundError: `document.case_id` is unset, or no
                object matching `document.document_id` exists in that case.
        """
        if document.case_id is None:
            raise DocumentNotFoundError(
                f"document {document.document_id!r} has no case_id to locate it by"
            )
        prefix = f"cases/{document.case_id}/uploads/{document.document_id}"
        blobs = await asyncio.to_thread(
            lambda: list(self._client.list_blobs(self._bucket_name, prefix=prefix))
        )
        if not blobs:
            raise DocumentNotFoundError(
                f"no document {document.document_id!r} in case {document.case_id!r}"
            )
        return await asyncio.to_thread(blobs[0].download_as_bytes)


__all__ = ["GcsEvidenceStore"]
