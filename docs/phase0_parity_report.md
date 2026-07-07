# Phase 0 Parity Report

**Date:** 2026-07-04 · **Engine:** seminr 2.5.0 on R 4.6.1 (macOS) · **Verdict: PASS — 70/70 comparisons**

## What was tested

The open-source PLS-SEM engine (R, `seminr`) was run on the corporate reputation benchmark
dataset (n = 344, missing code −99, mean replacement) — the running example of Hair, Hult,
Ringle & Sarstedt, *A Primer on PLS-SEM* (3rd ed.) — and every statistic was compared
against the **published SmartPLS 4 output** in the book's official case-study PDFs
(chapters 4 and 6, archived in `docs/references/`).

Two models, matching the book:

1. **Simple model** (COMP, LIKE, CUSA, CUSL — reflective): loadings, Cronbach's α, ρA, ρC,
   AVE, Fornell-Larcker, HTMT.
2. **Extended model** (adds formative drivers QUAL, PERF, CSOR, ATTR): all 13 path
   coefficients, R², f², total and total-indirect effects, outer weights, and bootstrap
   t-values (10,000 resamples).

## Results

All **57 point estimates match the published values to rounding** (max |diff| = 0.0007;
tolerance 0.005). All **13 bootstrap t-values match within 1.5% relative** (tolerance 10% —
SmartPLS and seminr use different random number generators, so bootstrap statistics match
in distribution, not digit-for-digit). Full table: [`parity/parity_table.md`](../parity/parity_table.md).

| Statistic family | Comparisons | Max abs diff | Verdict |
|---|---|---|---|
| Outer loadings (ch4) | 9 | 0.0005 | PASS |
| Reliability α / ρA / ρC (ch4) | 9 | 0.0004 | PASS |
| AVE + Fornell-Larcker + HTMT (ch4) | 7 | 0.0005 | PASS |
| Path coefficients (ch6) | 13 | 0.0005 | PASS |
| R² (ch6) | 4 | 0.0004 | PASS |
| f² effect sizes (ch6) | 4 | 0.0003 | PASS |
| Total + total-indirect effects, outer weights (ch6) | 11 | 0.0007 | PASS |
| Bootstrap t-values (ch6, loose) | 13 | 1.5% rel | PASS |

## Findings worth recording

1. **The book's Exhibit A6.8 mislabels two rows.** For COMP→CUSL and LIKE→CUSL the exhibit
   prints the total **indirect** effect (e.g. LIKE: 0.436 × 0.505 = 0.220), not the total
   effect (0.564). The engine computes both correctly; the parity harness compares each
   against the right quantity, with the discrepancy documented in
   `parity/published_values.json`.
2. **ρA values must be validated against SmartPLS 4, not older editions.** SmartPLS 4
   reports ρA = 0.832/0.839/0.836 for COMP/CUSL/LIKE; earlier editions printed different
   values. seminr matches SmartPLS 4.
3. **Bootstrap reproducibility.** The engine pins `seed = 123`; results are reproducible
   run-to-run, and match SmartPLS's fixed-seed output in distribution.

## AI pipeline prototype

`ai/model_architect.py` implements the "variable dictionary in → model spec out" pipeline:
Claude (via `messages.parse`, schema-validated output) proposes constructs,
reflective/formative measurement, and structural paths with rationales; `render_seminr()`
converts the approved spec into the exact seminr code the engine estimates — validated
against the benchmark model syntax. **Live run pending API credentials**
(`ANTHROPIC_API_KEY` or `ant auth login`); the spec-to-engine contract is smoke-tested.

## Phase 0 exit criteria

| Criterion | Status |
|---|---|
| Engine runs benchmark datasets end to end | ✅ simple + extended corp-rep models, 10k bootstrap |
| Parity report vs SmartPLS with documented tolerances | ✅ 70/70 (0.005 point-estimate, 10% bootstrap) |
| AI pipeline: dictionary in → sensible model spec out | ✅ Claude-proposed spec (`ai/fixtures/model_spec_reference.json`) recovers the book's expert model exactly — 8 constructs with correct reflective/formative modes, all 13 paths, mediators identified. Produced interactively; automated API run still pending credentials. |

## How to reproduce

```sh
Rscript engine/R/benchmark_corp_rep.R engine/corp_rep_results.json 10000
python3 parity/compare.py engine/corp_rep_results.json parity/published_values.json
```
