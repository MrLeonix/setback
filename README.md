# Setback

A Collaborative Partner agent that helps NSW residents object to a
neighbouring Development Application (DA).

Built for the Google "All Things Agentic Hackathon".

## What it is

Setback interviews a resident about their concerns with a neighbouring DA,
ingests the exhibited plans and any photos the resident supplies, verifies
every factual claim against keyless NSW government zoning and planning APIs,
and runs two structurally disjoint reviewer agents plus an adjudication step
over each candidate objection ground. A deterministic citation gate then
checks every surviving ground for a resolvable source before anything is
allowed to ship. Grounds that are not planning-relevant under EP&A Act s4.15
are refused, with a plain-language explanation of why, rather than silently
dropped. The output is a submission that cites only grounds that survived
the whole pipeline.

## Architecture

`[TO INSERT: architecture diagram]`

At a high level: Interview -> Ingest (OnlineDA + ePlanning spatial + council
tracker) -> Evidence dossier -> Court (Clause Reviewer / Evidence Reviewer /
adjudication bench) -> Citation gate -> Dispatch (submission + refusal
explainer).

## Local spin-up

```sh
uv sync
cp .env.example .env   # fill in local overrides; see setback/config.py for defaults
make test
make run-local
```

## Cloud spin-up

```sh
`[TO INSERT: gcloud / terraform commands once deploy is implemented]`
make deploy
```

## Models used

- `gemini-3.5-flash-lite` — the resident-facing interview model (MINIMAL thinking).
- `gemini-3.7-flash` — the adjudication bench model (LOW thinking, its effective floor).
- `gemma-4-26b-a4b-it-maas` — low-cost clerical extraction (OpenAI-compatible MaaS endpoint).

All models satisfy the hackathon's model-eligibility requirement:
"[TO INSERT VERBATIM FROM RULES: 'Gemini 3.5 or newer']".

## Data sources

- NSW ePlanning OnlineDA API (CC-BY).
- NSW Planning spatial services / layerintersect (CC-BY 3.0 AU, NSW Crown Copyright, Department of Planning and Environment).
- Council eTrack / ePathway exhibited documents (statutorily public records under the EP&A Act; used here for demonstration with attribution, not redistributed as a dataset).
- Google Maps Platform imagery, where used (subject to Google Maps Platform terms, displayed with attribution).

## Hackathon disclosure

See [DISCLOSURE.md](./DISCLOSURE.md).

## License

MIT — see [LICENSE](./LICENSE).
