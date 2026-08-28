"""Developer tool: fetch and freeze live API responses as offline test fixtures.

Run manually (never in CI) to refresh ``tests/fixtures/nsw/`` for the Setback
demo case: PAN-661190 / DA2026-0359 at 65A Vista Street, Sans Souci NSW 2219,
in the Georges River Council area. It talks to three keyless NSW government
data sources:

- the NSW Planning Portal ``OnlineDA`` open-data API (development application
  register lookup by Planning Portal Application Number);
- the NSW ePlanning spatial API (address -> property ID, LEP layer
  intersection, and applicable DCP lookup);
- the Georges River Council ``eTrack`` public document register (an ASP.NET
  WebForms application that requires submitting its search form and then
  scraping the resulting document list).

Every endpoint is fetched exactly once per run -- this script performs no
retries and no polling loops, in line with being a polite, occasional
consumer of keyless government infrastructure.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

import httpx

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
FIXTURES_DIR: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "nsw"
DOCS_DIR: Final[Path] = FIXTURES_DIR / "docs"
LARGE_DOCS_DIR: Final[Path] = REPO_ROOT / "fixtures-large"
LARGE_DOCS_THRESHOLD_BYTES: Final[int] = 8 * 1024 * 1024

USER_AGENT: Final[str] = "setback-fixture-fetcher/0.1 (+https://github.com/MrLeonix/setback)"
REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

ONLINEDA_URL: Final[str] = "https://api.apps1.nsw.gov.au/eplanning/data/v0/OnlineDA"
ADDRESS_URL: Final[str] = "https://api.apps1.nsw.gov.au/planning/viewersf/V1/ePlanningApi/address"
LAYERINTERSECT_URL: Final[str] = (
    "https://api.apps1.nsw.gov.au/planning/viewersf/V1/ePlanningApi/layerintersect"
)
DCP_URL: Final[str] = "https://api.apps1.nsw.gov.au/planning/viewersf/V1/ePlanningApi/dcp"

ETRACK_BASE: Final[str] = "https://etrack.georgesriver.nsw.gov.au"
ETRACK_SEARCH_URL: Final[str] = f"{ETRACK_BASE}/Pages/XC.Track/SearchApplication.aspx"
ETRACK_DOWNLOAD_URL: Final[str] = f"{ETRACK_BASE}/Common/Integration/FileDownload.ashx"

PAN_NUMBER: Final[str] = "PAN-661190"
COUNCIL_NAME: Final[str] = "Georges River Council"
COUNCIL_REFERENCE: Final[str] = "DA2026/0359"
DEMO_ADDRESS: Final[str] = "65A Vista Street Sans Souci 2219"

SEE_DOCUMENT_MATCH: Final[str] = "Statement of Environmental Effects"
ELEVATIONS_DOCUMENT_MATCH: Final[str] = "Elevations"


@dataclass(frozen=True, slots=True)
class EtrackDocument:
    """One row from the eTrack public document register for an application."""

    file_id: int
    ext: str
    filesize: int
    description: str


def parse_onlineda_record(raw: dict[str, object]) -> dict[str, object]:
    """Extract the single OnlineDA application record from a raw API response.

    Raises:
        ValueError: If the response contains no application records.
    """
    applications = raw.get("Application", [])
    if not isinstance(applications, list) or not applications:
        raise ValueError("OnlineDA response contains no Application records")
    record = applications[0]
    if not isinstance(record, dict):
        raise ValueError("OnlineDA Application record is not an object")
    return record


def parse_prop_id(raw: list[dict[str, object]]) -> int:
    """Extract the propId from a raw ePlanning address-lookup response.

    Raises:
        ValueError: If the response contains no address matches.
    """
    if not raw:
        raise ValueError("address lookup response contains no matches")
    prop_id = raw[0]["propId"]
    assert isinstance(prop_id, int)
    return prop_id


def parse_zoning_layers(raw: list[dict[str, object]]) -> dict[str, str]:
    """Map each intersected EPI layer name to its first result's headline title."""
    layers: dict[str, str] = {}
    for layer in raw:
        results = layer.get("results")
        if isinstance(results, list) and results:
            first = results[0]
            layer_name = layer.get("layerName")
            if isinstance(layer_name, str) and isinstance(first, dict):
                layers[layer_name] = str(first.get("title", ""))
    return layers


def parse_dcp_plans(raw: list[dict[str, object]]) -> list[str]:
    """List the applicable DCP plan names for a property."""
    if not raw:
        return []
    dcp_results = raw[0].get("dcpResults", [])
    if not isinstance(dcp_results, list):
        return []
    return [str(plan["planName"]) for plan in dcp_results]


_FILEDOWNLOAD_ROW_RE = re.compile(r"<tr[^>]*>.*?</tr>", re.S)
_FILEDOWNLOAD_LINK_RE = re.compile(r"FileDownload\.ashx\?id=(\d+)&amp;ext=(\w+)&amp;filesize=(\d+)")


def parse_etrack_documents(html: str) -> list[EtrackDocument]:
    """Parse the public document register table on an eTrack application page."""
    documents: list[EtrackDocument] = []
    seen_ids: set[int] = set()
    for row in _FILEDOWNLOAD_ROW_RE.findall(html):
        match = _FILEDOWNLOAD_LINK_RE.search(row)
        if not match:
            continue
        file_id = int(match.group(1))
        if file_id in seen_ids:
            continue
        seen_ids.add(file_id)
        description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip()
        documents.append(
            EtrackDocument(
                file_id=file_id,
                ext=match.group(2),
                filesize=int(match.group(3)),
                description=description,
            )
        )
    return documents


def find_document(documents: list[EtrackDocument], match: str) -> EtrackDocument:
    """Find the first document whose description contains ``match`` (case-insensitive).

    Raises:
        ValueError: If no document matches.
    """
    for doc in documents:
        if match.lower() in doc.description.lower():
            return doc
    raise ValueError(f"no eTrack document matched {match!r}")


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _submit_etrack_search(client: httpx.Client, application_number: str) -> str:
    """Submit the eTrack ASP.NET WebForms search and return the resulting detail URL.

    Raises:
        ValueError: If the search page is missing the expected WebForms hidden fields.
    """
    search_page = client.get(ETRACK_SEARCH_URL, params={"ApplicationNumber": application_number})
    search_page.raise_for_status()
    viewstate = re.search(r'id="__VIEWSTATE" value="([^"]*)"', search_page.text)
    viewstate_gen = re.search(r'id="__VIEWSTATEGENERATOR" value="([^"]*)"', search_page.text)
    event_validation = re.search(r'id="__EVENTVALIDATION" value="([^"]*)"', search_page.text)
    if not (viewstate and viewstate_gen and event_validation):
        raise ValueError("eTrack search page is missing expected WebForms hidden fields")
    result = client.post(
        ETRACK_SEARCH_URL,
        params={"ApplicationNumber": application_number},
        data={
            "__VIEWSTATE": viewstate.group(1),
            "__VIEWSTATEGENERATOR": viewstate_gen.group(1),
            "__EVENTVALIDATION": event_validation.group(1),
            "ctl00$ctMain$search$txtSearch": application_number,
            "ctl00$ctMain$search$btnSearch": "Search",
        },
        # The intermediate 302 response declares gzip encoding on an empty
        # body, which trips httpx's decoder; request uncompressed responses
        # for this WebForms redirect dance.
        headers={"Accept-Encoding": "identity"},
    )
    result.raise_for_status()
    return str(result.url)


def fetch_onlineda(client: httpx.Client) -> dict[str, object]:
    """Fetch the OnlineDA open-data record for the demo application.

    The endpoint requires a ``filters`` header carrying a JSON filter object
    plus ``PageSize``/``PageNumber`` headers; a bare query string is rejected.
    """
    filters = {
        "filters": {
            "ApplicationStatus": [],
            "CouncilName": [COUNCIL_NAME],
            "PlanningPortalApplicationNumber": [PAN_NUMBER],
        }
    }
    response = client.get(
        ONLINEDA_URL,
        headers={
            "filters": json.dumps(filters),
            "PageSize": "10",
            "PageNumber": "1",
        },
    )
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


def fetch_address(client: httpx.Client) -> list[dict[str, object]]:
    """Fetch the ePlanning address-lookup match for the demo address."""
    response = client.get(ADDRESS_URL, params={"a": DEMO_ADDRESS})
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


def fetch_layerintersect(client: httpx.Client, prop_id: int) -> list[dict[str, object]]:
    """Fetch the EPI layer intersection for a property."""
    response = client.get(
        LAYERINTERSECT_URL,
        params={"type": "property", "id": prop_id, "layers": "epi"},
    )
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


def fetch_dcp(client: httpx.Client, prop_id: int) -> list[dict[str, object]]:
    """Fetch the applicable Development Control Plans for a property."""
    response = client.get(DCP_URL, params={"id": prop_id, "Type": "property"})
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


def fetch_etrack_document_list(client: httpx.Client) -> tuple[list[EtrackDocument], str]:
    """Search eTrack for the demo DA and return its document register + raw HTML."""
    detail_url = _submit_etrack_search(client, COUNCIL_REFERENCE)
    documents_url = httpx.URL(detail_url).copy_merge_params({"p": "y"})
    documents_page = client.get(documents_url)
    documents_page.raise_for_status()
    return parse_etrack_documents(documents_page.text), documents_page.text


def download_etrack_document(client: httpx.Client, doc: EtrackDocument, dest: Path) -> str:
    """Download one eTrack document to ``dest`` and return its sha256 hex digest."""
    response = client.get(ETRACK_DOWNLOAD_URL, params={"id": doc.file_id, "ext": doc.ext})
    response.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    return _sha256(dest)


def main() -> None:
    """Fetch every fixture for the demo case and write it under ``tests/fixtures/nsw/``."""
    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        onlineda = fetch_onlineda(client)
        _write_json(FIXTURES_DIR / "onlineda_pan-661190.json", onlineda)

        address = fetch_address(client)
        _write_json(FIXTURES_DIR / "address_65a-vista-street.json", address)
        prop_id = parse_prop_id(address)

        layerintersect = fetch_layerintersect(client, prop_id)
        _write_json(FIXTURES_DIR / f"layerintersect_propid-{prop_id}.json", layerintersect)

        dcp = fetch_dcp(client, prop_id)
        _write_json(FIXTURES_DIR / f"dcp_propid-{prop_id}.json", dcp)

        documents, documents_html = fetch_etrack_document_list(client)
        _write_json(
            FIXTURES_DIR / "etrack_documents_da2026-0359.json",
            [asdict(doc) for doc in documents],
        )
        (FIXTURES_DIR / "etrack_documents_da2026-0359.html").write_text(
            documents_html, encoding="utf-8"
        )

        see = find_document(documents, SEE_DOCUMENT_MATCH)
        elevations = find_document(documents, ELEVATIONS_DOCUMENT_MATCH)
        combined_size = see.filesize + elevations.filesize
        target_dir = DOCS_DIR if combined_size <= LARGE_DOCS_THRESHOLD_BYTES else LARGE_DOCS_DIR

        see_path = target_dir / "statement-of-environmental-effects.pdf"
        elevations_path = target_dir / "elevations.pdf"
        see_sha256 = download_etrack_document(client, see, see_path)
        elevations_sha256 = download_etrack_document(client, elevations, elevations_path)

        pan_number = parse_onlineda_record(onlineda)["PlanningPortalApplicationNumber"]
        print(f"OnlineDA record: {pan_number}")
        print(f"propId: {prop_id}")
        print(f"SEE -> {see_path} sha256={see_sha256} ({see.filesize} bytes)")
        print(
            f"Elevations -> {elevations_path} "
            f"sha256={elevations_sha256} ({elevations.filesize} bytes)"
        )


if __name__ == "__main__":
    main()
