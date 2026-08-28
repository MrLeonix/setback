# Hackathon Disclosure

Prepared for the Google "All Things Agentic Hackathon".

## Provenance of this repository

- This repository (`MrLeonix/setback`) was created on **2026-08-29**, inside
  the submission period for this hackathon.
- All code in this repository was written during the submission period.
  Zero lines were copied from any pre-existing repository, including prior
  scaffolding work by the same author.
- The multi-agent adversarial-review architecture (structurally disjoint
  Clause Reviewer and Evidence Reviewer, adjudication bench, and a
  deterministic citation gate before dispatch) is the author's own prior
  unpublished design practice, applied fresh to this codebase for this
  submission. No code implementing that design pre-dates the submission
  period.

## Data sources and licences

- **NSW ePlanning OnlineDA API** — CC-BY. Used to fetch Development
  Application metadata (applicant, description, exhibition dates, associated
  documents).
- **NSW Planning spatial services** (layerintersect and related ArcGIS
  endpoints) — CC-BY 3.0 AU, NSW Crown Copyright, NSW Department of Planning
  and Environment. Used to resolve zoning, height-of-building, floor-space
  ratio, and heritage layers for a given lot.
- **Council eTrack / ePathway exhibited documents** — statutorily public
  records under the NSW *Environmental Planning and Assessment Act 1979*.
  Used here for demonstration purposes with attribution to the originating
  council; not redistributed as a bulk dataset.
- **Google Maps Platform imagery** (Street View / Solar API), where used as
  a fallback evidence source — subject to the Google Maps Platform Terms of
  Service, displayed with attribution.

## Model eligibility

See [README.md](./README.md#models-used) for the models used and the
hackathon's model-eligibility wording.
