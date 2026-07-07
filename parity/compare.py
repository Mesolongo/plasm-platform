#!/usr/bin/env python3
"""Phase 0 parity comparison: seminr engine output vs. published SmartPLS 4 values.

Usage: python3 compare.py <engine_results.json> <published_values.json> [--markdown out.md]

Point estimates (paths, loadings, reliability, R2, f2, total effects) must match
published values to rounding: PASS <= 0.005, WARN <= 0.02, FAIL above.
Bootstrap t values are compared loosely (different RNGs): PASS within 10% relative
or 0.25 absolute, WARN within 25%, FAIL above.
"""
import json
import re
import sys


def norm(s):
    return re.sub(r"\s+", "", str(s)).replace("→", "->").lower()


def matrix_lookup(records, row_name, col_name):
    """records: list of dicts with a 'row' key; column names may be R-mangled."""
    if not records:
        return None
    want_row = norm(row_name)
    for rec in records:
        if norm(rec.get("row", "")) == want_row:
            want_col = norm(col_name)
            for k, v in rec.items():
                if k == "row":
                    continue
                if norm(k) == want_col:
                    return v
    return None


def path_lookup(records, path_key, col_candidates):
    """path_key like 'COMP->CUSL'; boot rows look like 'COMP  ->  CUSL'."""
    if not records:
        return None
    want = norm(path_key)
    for rec in records:
        if norm(rec.get("row", "")) == want:
            for col in col_candidates:
                for k, v in rec.items():
                    if k != "row" and norm(k) == norm(col):
                        return v
    return None


def judge(diff, kind, published, engine):
    if engine is None:
        return "MISSING"
    if kind == "boot_t":
        rel = diff / abs(published) if published else float("inf")
        if diff <= 0.25 or rel <= 0.10:
            return "PASS"
        return "WARN" if rel <= 0.25 else "FAIL"
    if diff <= 0.005:
        return "PASS"
    return "WARN" if diff <= 0.02 else "FAIL"


def add(rows, section, name, published, engine, kind="point"):
    engine_f = None if engine is None else float(engine)
    diff = None if engine_f is None else abs(engine_f - float(published))
    rows.append({
        "section": section, "statistic": name, "published": float(published),
        "engine": engine_f, "diff": diff,
        "verdict": judge(diff if diff is not None else 0.0, kind, float(published), engine_f),
    })


def main():
    engine_path, published_path = sys.argv[1], sys.argv[2]
    md_out = sys.argv[sys.argv.index("--markdown") + 1] if "--markdown" in sys.argv else None

    eng = json.load(open(engine_path))
    pub = json.load(open(published_path))
    rows = []

    # --- Simple model: chapter 4 measurement statistics ---
    sm, esm = pub["simple_model"], eng["simple"]
    for key, val in sm["loadings"].items():
        item, construct = key.split("@")
        add(rows, "ch4 loadings", key, val, matrix_lookup(esm["loadings"], item, construct))
    rel_cols = {"cronbachs_alpha": "alpha", "rhoA": "rhoA", "rhoC": "rhoC", "AVE": "AVE"}
    for pub_key, col in rel_cols.items():
        for construct, val in sm[pub_key].items():
            add(rows, f"ch4 {pub_key}", construct, val,
                matrix_lookup(esm["reliability"], construct, col))
    for construct, val in sm["fornell_larcker_sqrt_ave"].items():
        add(rows, "ch4 Fornell-Larcker diag", construct, val,
            matrix_lookup(esm.get("fornell_larcker"), construct, construct))
    for pair, val in sm["htmt"].items():
        a, b = pair.split("-")
        got = matrix_lookup(esm.get("htmt"), a, b)
        if got is None:
            got = matrix_lookup(esm.get("htmt"), b, a)
        add(rows, "ch4 HTMT", pair, val, got)

    # --- Extended model: chapter 6 structural statistics ---
    xm, exm = pub["extended_model"], eng["extended"]
    for path, val in xm["path_coefficients"].items():
        src, dst = path.split("->")
        add(rows, "ch6 path coefficients", path, val,
            matrix_lookup(exm["paths_and_r2"], src, dst))
    for construct, val in xm["r_squared"].items():
        add(rows, "ch6 R2", construct, val,
            matrix_lookup(exm["paths_and_r2"], "R^2", construct))
    for path, val in xm["f_square"].items():
        src, dst = path.split("->")
        add(rows, "ch6 f2", path, val, matrix_lookup(exm.get("f_square"), src, dst))
    for path, val in xm["total_effects"].items():
        src, dst = path.split("->")
        add(rows, "ch6 total effects", path, val,
            matrix_lookup(exm.get("total_effects"), src, dst))
    for path, val in xm.get("total_indirect_effects", {}).items():
        src, dst = path.split("->")
        add(rows, "ch6 total indirect effects", path, val,
            matrix_lookup(exm.get("total_indirect"), src, dst))
    for key, val in xm["outer_weights"].items():
        item, construct = key.split("@")
        add(rows, "ch6 outer weights", key, val,
            matrix_lookup(exm.get("weights"), item, construct))
    for path, val in xm["boot_t_values"].items():
        src, dst = path.split("->")
        got = path_lookup(exm.get("boot_paths"), f"{src}->{dst}",
                          ["T Stat.", "T.Stat.", "t"])
        add(rows, "ch6 bootstrap t (loose)", path, val, got, kind="boot_t")

    # --- Report ---
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    width = max(len(r["statistic"]) for r in rows)
    current = None
    lines = []
    for r in rows:
        if r["section"] != current:
            current = r["section"]
            lines.append(f"\n== {current} ==")
        e = "      --" if r["engine"] is None else f"{r['engine']:8.3f}"
        d = "     --" if r["diff"] is None else f"{r['diff']:7.4f}"
        lines.append(f"  {r['statistic']:<{width}}  pub {r['published']:7.3f}  eng {e}  diff {d}  {r['verdict']}")
    summary = "  ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    print("\n".join(lines))
    print(f"\nTOTAL {len(rows)} comparisons  |  {summary}")

    if md_out:
        with open(md_out, "w") as f:
            f.write(f"| Section | Statistic | SmartPLS 4 (published) | seminr engine | abs diff | Verdict |\n")
            f.write("|---|---|---|---|---|---|\n")
            for r in rows:
                e = "—" if r["engine"] is None else f"{r['engine']:.3f}"
                d = "—" if r["diff"] is None else f"{r['diff']:.4f}"
                f.write(f"| {r['section']} | {r['statistic']} | {r['published']:.3f} | {e} | {d} | {r['verdict']} |\n")
            f.write(f"\n**{len(rows)} comparisons — {summary}**\n")
        print(f"Markdown table written to {md_out}")

    sys.exit(1 if counts.get("FAIL") or counts.get("MISSING") else 0)


if __name__ == "__main__":
    main()
