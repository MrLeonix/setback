# NSW data sources (fixtures pack)

Every endpoint below is **keyless** and was fetched **once**, manually, via
`make fixtures` (`tools/fetch_fixtures.py`), for the single demo case:

| Identifier | Value |
| --- | --- |
| Planning Portal Application Number (PAN) | `PAN-661190` |
| Council application number | `DA2026/0359` |
| Council | Georges River Council |
| Site address | 65A Vista Street, Sans Souci NSW 2219 |
| ePlanning property ID (`propId`) | `6038209` |
| Exhibition window | 2026-08-20 to 2026-09-03 |

All raw responses were fetched on **2026-08-29** (Sydney time) and are frozen
under `tests/fixtures/nsw/`. Re-run `make fixtures` to refresh them against
the live services (do this rarely — the script performs a single request per
endpoint, never a loop or retry, out of courtesy to unauthenticated
government infrastructure).

## 1. OnlineDA — NSW Planning Portal open-data API

- **Endpoint**: `GET https://api.apps1.nsw.gov.au/eplanning/data/v0/OnlineDA`
- **Auth**: none (keyless, public open-data API)
- **Licence**: NSW Government [Open Data Policy](https://www.digital.nsw.gov.au/policy/data/nsw-government-open-data-policy) / Creative Commons Attribution 4.0, as published via the [NSW Planning Portal spatial & data services](https://www.planningportal.nsw.gov.au/opendata)
- **Request shape discovered empirically**: the endpoint returns
  `400 {"ErrorMessage": "Required parameters for OnlineDA endpoint is not met."}`
  unless **three headers** are all present:
  - `filters`: a JSON string `{"filters": {"ApplicationStatus": [...], "CouncilName": [...], "PlanningPortalApplicationNumber": [...]}}`
  - `PageSize`: e.g. `"10"`
  - `PageNumber`: e.g. `"1"`

  A bare query string (`?PanNo=...`, `?ApplicationNumber=...`, etc.) is
  **not** accepted — the filter key that matches by PAN is
  `PlanningPortalApplicationNumber` inside the `filters` header JSON, not a
  query parameter.
- **Demo request** (headers only, no query string):
  ```
  filters: {"filters":{"ApplicationStatus":[],"CouncilName":["Georges River Council"],"PlanningPortalApplicationNumber":["PAN-661190"]}}
  PageSize: 10
  PageNumber: 1
  ```
- **HTTP status observed**: `200 OK`, `TotalCount: 1`
- **Fixture**: `tests/fixtures/nsw/onlineda_pan-661190.json`

## 2. ePlanning spatial API — address lookup

- **Endpoint**: `GET https://api.apps1.nsw.gov.au/planning/viewersf/V1/ePlanningApi/address?a=<address>`
- **Auth**: none (keyless)
- **Licence**: same NSW Government open-data terms as above (ePlanning
  spatial viewer services)
- **Demo request**: `?a=65A%20Vista%20Street%20Sans%20Souci%202219`
- **HTTP status observed**: `200 OK`
- **Result**: `propId=6038209`, `GURASID=86971504`
- **Fixture**: `tests/fixtures/nsw/address_65a-vista-street.json`

## 3. ePlanning spatial API — LEP layer intersection (zoning chain, step 2)

- **Endpoint**: `GET https://api.apps1.nsw.gov.au/planning/viewersf/V1/ePlanningApi/layerintersect?type=property&id=<propId>&layers=epi`
- **Auth**: none (keyless)
- **Licence**: same as above
- **Demo request**: `?type=property&id=6038209&layers=epi`
- **HTTP status observed**: `200 OK`
- **Result highlights**: Height of Buildings Map = 9 m (Georges River LEP
  2021, Clause 4.3); Scenic Protection Land = Foreshore Scenic Protection
  Area
- **Fixture**: `tests/fixtures/nsw/layerintersect_propid-6038209.json`

## 4. ePlanning spatial API — applicable DCPs (zoning chain, step 3)

- **Endpoint**: `GET https://api.apps1.nsw.gov.au/planning/viewersf/V1/ePlanningApi/dcp?id=<propId>&Type=property`
- **Auth**: none (keyless)
- **Licence**: same as above
- **Demo request**: `?id=6038209&Type=property`
- **HTTP status observed**: `200 OK`
- **Result**: 5 applicable DCP documents (Hurstville DCP 2015, Hurstville
  DCP No. 1 2007, Hurstville DCP No. 2 2007, Kogarah DCP 2013, Kogarah DCP
  2013 Amendment 2), each with a direct PDF `planURL` hosted on the NSW
  Planning Portal's S3 bucket
- **Fixture**: `tests/fixtures/nsw/dcp_propid-6038209.json`

## 5. eTrack — Georges River Council public document register

- **Base**: `https://etrack.georgesriver.nsw.gov.au`
- **Auth**: none (public, unauthenticated ASP.NET WebForms application —
  no API key, but not a REST API either)
- **Licence**: council-hosted public exhibition documents; content is
  published for the statutory public-exhibition/GIPA purpose of the DA
  process. No explicit machine-readable licence is published by the council;
  treated here as public-interest planning material used only to identify
  and cite the two documents named in the work package, per the demo's
  fair-use, single-fetch access pattern.
- **Empirically discovered flow** (this is not a documented API — it is an
  ASP.NET WebForms postback dance):
  1. `GET /Pages/XC.Track/SearchApplication.aspx?ApplicationNumber=<councilRef>`
     — returns the search form plus `__VIEWSTATE` / `__VIEWSTATEGENERATOR` /
     `__EVENTVALIDATION` hidden fields (HTTP 200).
  2. `POST /Pages/XC.Track/SearchApplication.aspx?ApplicationNumber=<councilRef>`
     with those three hidden fields plus
     `ctl00$ctMain$search$txtSearch=<councilRef>` and
     `ctl00$ctMain$search$btnSearch=Search` — redirects (HTTP 302) to
     `SearchApplication.aspx?id=<internalId>&a=<councilRef>`.
  3. `GET SearchApplication.aspx?id=<internalId>&p=y` — the public
     "documents" tab, an HTML table whose rows link to
     `../../Common/Integration/FileDownload.ashx?id=<fileId>&ext=PDF&filesize=<bytes>`
     (**not** `Pages/XC.Track/FileDownload.ashx` — that path 404s).
  4. `GET /Common/Integration/FileDownload.ashx?id=<fileId>&ext=PDF` streams
     the PDF.
  - **Gotcha**: the intermediate 302 response declares
    `Content-Encoding: gzip` on an empty body, which breaks strict gzip
    decoders (httpx raised `zlib.error`); the POST step sends
    `Accept-Encoding: identity` to sidestep this.
- **Demo application internal eTrack ID**: `330796` for `DA2026/0359`
- **HTTP statuses observed**: 200 (search form), 302 (search POST
  redirect), 200 (documents tab), 200 (both PDF downloads)
- **Fixtures**:
  - `tests/fixtures/nsw/etrack_documents_da2026-0359.html` — raw documents
    tab HTML (12 documents listed)
  - `tests/fixtures/nsw/etrack_documents_da2026-0359.json` — parsed
    `{file_id, ext, filesize, description}` rows
  - `tests/fixtures/nsw/docs/statement-of-environmental-effects.pdf` —
    eTrack file id `5176594`, 1,568,098 bytes,
    sha256 `64f6d9fe813946ea695c07bca59e2b799be0c5a22ff80af121d11e69cc8e6043`
  - `tests/fixtures/nsw/docs/elevations.pdf` — eTrack file id `5197134`,
    1,553,641 bytes,
    sha256 `179d3b568c2834c6522322eee986bef9ba149ff9c44b46a3f5b733940c4f3678`
  - Combined size of the two documents is ~3.0 MB, under the 8 MB threshold,
    so both are committed directly under `tests/fixtures/nsw/docs/` rather
    than the gitignored `fixtures-large/` directory.

## Politeness notes

`tools/fetch_fixtures.py` performs exactly one HTTP request per fixture (plus
the two document downloads) each time it is run, with no retries and no
polling. It identifies itself with a descriptive, non-personal
`User-Agent: setback-fixture-fetcher/0.1 (+https://github.com/MrLeonix/setback)`
header. It is a developer tool, run manually, and must never be wired into
CI or invoked in a loop.
