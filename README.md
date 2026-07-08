# plsem-platform

AI-powered PLS-SEM analysis platform — from raw survey data to a publication-ready
structural equation modeling report. Blueprint: see the project artifact
(architecture, user flow, roadmap). Status: engine parity (70/70), web app + AI
layer, and the advanced statistical suite are complete — see
[docs/phase0_parity_report.md](docs/phase0_parity_report.md). Implemented:
moderation (two-stage interaction terms), higher-order constructs (two-stage),
SRMR model fit, PLSpredict (Q²predict + LM benchmark), SPSS `.sav` import,
mediation (bootstrap specific indirect effects with the Zhao et al. 2010
typology), IPMA with the priority-map visualization (Ringle & Sarstedt 2016),
and **multi-group analysis**: permutation MGA (Chin & Dibbern 2010) gated on
MICOM measurement invariance (Henseler et al. 2016) — path comparisons are
withheld unless at least partial invariance holds. Also: blindfolding Q²
(cross-validated redundancy, D = 7), NFI and RMS_theta fit indices,
simple-slopes plots for moderation, assumption-checking gates before every run
(10-times rule, >5% missing, straight-lining; override is recorded), Excel
export, PDF report export (needs LibreOffice), a grounded
research-assistant chat, SQL / Google Sheets data connectors, token-based
share links with a read-only viewer and comment threads,
literature-grounded hypothesis citations (Crossref), and a **publishing
assistant**: deterministic reviewer-anticipation checks that read the assessment
and surface the concerns a peer reviewer will raise (marginal discriminant
validity, missing PLSpredict, unsupported hypotheses, common method bias,
endogeneity, unobserved heterogeneity…) each with a pre-emptive fix and citation,
plus an AI drafter that turns them into submission front matter (title options,
abstract, keywords, highlights, contribution statement, and point-by-point
reviewer responses) in a journal or thesis register. Long-running work
(estimation, MGA, citation lookups) runs on an in-process **async job queue**
(file-backed, `backend/app/jobs.py`): the POST answers `202` with a job that is
pollable at `/api/jobs/{id}`, jobs orphaned by a server restart are failed on
startup, and the polling contract is Celery-shaped so a broker can replace the
thread pool without an API change. Engine
outputs reproduce the published Hair et al. examples: two-stage interaction
β = −0.071; indirect effects COMP→CUSA→CUSL = 0.074 (indirect-only mediation)
and LIKE→CUSA→CUSL = 0.220 (complementary); blindfolding Q² CUSA = 0.279 /
CUSL = 0.408 (published 0.280 / 0.415); servicetype MICOM reaches full
invariance on the corp-rep data.

## Layout

```
engine/R/estimate.R              # engine service: model spec JSON -> full results JSON
engine/R/mga.R                   # MGA service: MICOM + permutation group comparison
engine/R/spec_lib.R              # shared spec validation + seminr model builder
engine/R/benchmark_corp_rep.R    # PLS-SEM engine benchmark (seminr) -> JSON results
engine/corp_rep_results.json     # engine output (simple + extended corp-rep models)
parity/published_values.json     # SmartPLS 4 reference values (Hair et al. primer, ch. 4+6)
parity/compare.py                # parity harness (exit 1 on FAIL/MISSING)
parity/parity_table.md           # generated comparison table (70/70 PASS)
ai/model_architect.py            # Claude pipeline: variable dictionary -> model spec -> seminr code
ai/prompts/model_architect.md    # architect system prompt
ai/corp_rep_variable_dictionary.json  # sample input
docs/phase0_parity_report.md     # Phase 0 report
docs/references/                 # published SmartPLS case-study PDFs + extracted text
```

## Requirements

- R ≥ 4.6 with `seminr`, `jsonlite`
- Python ≥ 3.12; runtime deps are pinned in [requirements.txt](requirements.txt)
  (`pip install -r requirements-dev.txt` into `.venv` also brings `pytest`)
- Optional: LibreOffice (`soffice` on PATH) for PDF report export; a database driver
  for the SQL connector's dialect (e.g. `psycopg` for PostgreSQL) — SQLite needs none
- Anthropic API credentials for the AI features (`ANTHROPIC_API_KEY` or `ant auth login`)

## Run the backend (Phase 1)

```sh
.venv/bin/uvicorn backend.app.main:app --reload   # web app: http://127.0.0.1:8000
.venv/bin/python -m pytest backend/tests/         # end-to-end test suite
```

## Deploy (Docker)

One image bundles R 4.6 + seminr and the Python backend, so pilot users don't
install anything:

```sh
docker compose up --build          # web app: http://<host>:8000
```

or without compose:

```sh
docker build -t plsem-platform .
docker run -p 8000:8000 -v plsem_data:/app/data \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY plsem-platform
```

All state (datasets, analyses, shares, jobs) lives in `/app/data`, mounted as the
`plsem_data` volume, so it survives container upgrades. `ANTHROPIC_API_KEY` is
optional — without it everything runs except the AI features (model architect,
interpretation, chat, manuscript drafting), which answer 503. PDF export needs
LibreOffice in the image: `docker build --build-arg LIBREOFFICE=1 …` (~500 MB
larger). Anyone you send a share link to just needs the host to be reachable
(e.g. `http://<host>:8000/app/shared.html?token=…`); for exposure beyond a
trusted network, put the container behind a TLS reverse proxy (Caddy, nginx).

The web UI (served at `/`) walks the full flow: bring in data (upload CSV/Excel/SPSS
`.sav`, or pull from a SQL database or a link-viewable Google Sheet) →
data audit → model builder with live path diagram (manual, example, or
AI-proposed; supports moderation via two-stage interaction terms and
higher-order constructs) → run → results dashboard with threshold verdicts,
mediation classification, and IPMA → find grounding literature per hypothesis
(Crossref) → mint a read-only share link (optionally comment-enabled;
recipients open `/app/shared.html?token=…`) → download the Word report. API docs:
`/docs`. Assessment thresholds are rule-based with citations (Hair et al. 2022;
Henseler et al. 2015; Cohen 1988; Henseler et al. 2014 / Hu & Bentler 1999 for
SRMR; Shmueli et al. 2019 for PLSpredict; Kenny 2018 for interaction f²; Zhao
et al. 2010 for mediation typing; Ringle & Sarstedt 2016 for IPMA); hypothesis
verdicts use 95% percentile bootstrap CIs. AI endpoints (`/api/ai/status`,
`/api/datasets/{id}/propose-model`) are wired but return 503 until
`ANTHROPIC_API_KEY` is set.

## Run the parity check

```sh
Rscript engine/R/benchmark_corp_rep.R engine/corp_rep_results.json 10000
python3 parity/compare.py engine/corp_rep_results.json parity/published_values.json
```

## Run the AI model architect (needs API key)

```sh
.venv/bin/python ai/model_architect.py ai/corp_rep_variable_dictionary.json \
  --study "Drivers of corporate reputation and customer loyalty among mobile network operators"
```
