"""Rule-based assessment of engine results against published PLS-SEM thresholds.

Every verdict cites its criterion. This module is deterministic — the AI interpreter
(Phase 1 final step) will *narrate* these findings, never re-derive them.

Thresholds: Hair, Hult, Ringle & Sarstedt (2022), A Primer on PLS-SEM, 3rd ed.;
Henseler, Ringle & Sarstedt (2015) for HTMT; Cohen (1988) for f² classes;
Henseler et al. (2014) / Hu & Bentler (1999) for SRMR; Shmueli et al. (2019)
for PLSpredict; Kenny (2018) for interaction-term f² classes.
"""
import re


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cells(rec: dict):
    """Iterate (column, numeric value) pairs, skipping row labels and R metadata keys."""
    for k, v in rec.items():
        if k == "row" or k.startswith("_"):
            continue
        yield k, _num(v)


def _col(rec: dict, name: str):
    """Find a column in an R-exported record by normalized name (R mangles names)."""
    want = _norm(name)
    for k, v in rec.items():
        if k not in ("row",) and not k.startswith("_") and _norm(k) == want:
            return _num(v)
    return None


def _rows(records):
    return records or []


def verdict(ok: bool) -> str:
    return "pass" if ok else "fail"


def assess(results: dict, request: dict) -> dict:
    """Build the full assessment from engine results + the engine request (spec)."""
    constructs = {c["name"]: c for c in request["constructs"]}

    def items_of(c):
        # Higher-order constructs use dimension scores as items.
        return c.get("indicators") or c.get("dimensions") or []

    reflective_multi = {
        n for n, c in constructs.items()
        if c["measurement"] in ("reflective", "higher_order_reflective")
        and len(items_of(c)) > 1
    }
    formative = {n for n, c in constructs.items()
                 if c["measurement"] in ("formative", "higher_order_formative")}
    interactions = {f"{i['iv']}*{i['moderator']}"
                    for i in request.get("interactions") or []}
    endogenous = {p["to_construct"] for p in request["paths"]}

    measurement, structural, hypotheses = [], [], []

    # --- Indicator reliability: loadings >= 0.708 (reflective only) ----------
    for rec in _rows(results.get("loadings")):
        indicator = rec["row"]
        for name in reflective_multi:
            loading = _col(rec, name)
            if loading is None or indicator not in items_of(constructs[name]):
                continue
            if abs(loading) < 1e-12:
                continue
            measurement.append({
                "family": "indicator_reliability", "construct": name, "item": indicator,
                "metric": "outer loading", "value": round(loading, 3), "threshold": ">= 0.708",
                "verdict": verdict(loading >= 0.708),
                "citation": "Hair et al. (2022)",
            })

    # --- Internal consistency + convergent validity (reflective multi-item) --
    for rec in _rows(results.get("reliability")):
        name = rec["row"]
        if name not in reflective_multi:
            continue
        checks = [
            ("Cronbach's alpha", _col(rec, "alpha"), 0.70, "0.70–0.95"),
            ("rho_A", _col(rec, "rhoA"), 0.70, "0.70–0.95"),
            ("composite reliability rho_C", _col(rec, "rhoC"), 0.70, "0.70–0.95"),
        ]
        for metric, value, lo, band in checks:
            if value is None:
                continue
            ok = lo <= value <= 0.95
            measurement.append({
                "family": "internal_consistency", "construct": name,
                "metric": metric, "value": round(value, 3), "threshold": band,
                "verdict": verdict(ok),
                "citation": "Hair et al. (2022)",
                "note": "above 0.95 suggests redundant items" if value > 0.95 else None,
            })
        ave = _col(rec, "AVE")
        if ave is not None:
            measurement.append({
                "family": "convergent_validity", "construct": name,
                "metric": "AVE", "value": round(ave, 3), "threshold": ">= 0.50",
                "verdict": verdict(ave >= 0.50),
                "citation": "Hair et al. (2022)",
            })

    # --- Discriminant validity: HTMT < 0.85 — reflective constructs only -----
    # (HTMT is defined for reflective measurement; formative pairs are excluded.)
    htmt_eligible = {n for n, c in constructs.items()
                     if not c["measurement"].endswith("formative")}
    seen_pairs = set()
    for rec in _rows(results.get("htmt")):
        a = rec["row"]
        for b, v in _cells(rec):
            if v is None:
                continue
            pair = tuple(sorted((a, b)))
            if (a == b or pair in seen_pairs or abs(v) < 1e-12
                    or not {a, b} <= htmt_eligible):
                continue
            seen_pairs.add(pair)
            measurement.append({
                "family": "discriminant_validity", "construct": f"{pair[0]} / {pair[1]}",
                "metric": "HTMT", "value": round(v, 3), "threshold": "< 0.85 (0.90 if conceptually similar)",
                "verdict": "pass" if v < 0.85 else ("review" if v < 0.90 else "fail"),
                "citation": "Henseler et al. (2015)",
            })

    # --- Formative indicators: outer weight significance (bootstrap CI); if a
    # weight is n.s., the indicator is retained when its loading is >= 0.5 -----
    loading_of = {}
    for rec in _rows(results.get("loadings")):
        for name in formative:
            v = _col(rec, name)
            if v is not None and abs(v) >= 1e-12:
                loading_of[(name, rec["row"])] = v
    for rec in _rows(results.get("boot_weights")):
        parts = [p.strip() for p in rec["row"].split("->")]
        if len(parts) != 2 or parts[1] not in formative:
            continue
        item, name = parts
        w = _col(rec, "Original Est.")
        lo, hi = _col(rec, "2.5% CI"), _col(rec, "97.5% CI")
        if lo is None or hi is None:
            continue
        significant = lo > 0 or hi < 0
        loading = loading_of.get((name, item))
        if significant:
            v, note = "pass", None
        elif loading is not None and abs(loading) >= 0.5:
            v, note = "review", f"weight n.s., retained: loading {loading:.3f} >= 0.5"
        else:
            v, note = "fail", "weight n.s. and loading < 0.5 — consider removal"
        measurement.append({
            "family": "formative_indicator_validity", "construct": name, "item": item,
            "metric": "outer weight", "value": round(w, 3) if w is not None else None,
            "ci_95": [round(lo, 3), round(hi, 3)],
            "threshold": "95% CI excludes zero (else loading >= 0.5)",
            "verdict": v,
            "citation": "Hair et al. (2022)",
            "note": note,
        })

    # --- Structural collinearity: inner VIF -----------------------------------
    vif = results.get("vif_structural") or {}
    for endo, predictors in vif.items():
        for pred, value in (predictors or {}).items():
            if value is None:
                continue
            v = value[0] if isinstance(value, list) else value
            structural.append({
                "family": "collinearity", "construct": f"{pred} -> {endo}",
                "metric": "inner VIF", "value": round(v, 3), "threshold": "< 3 ideal, < 5 acceptable",
                "verdict": "pass" if v < 3 else ("review" if v < 5 else "fail"),
                "citation": "Hair et al. (2022)",
            })

    # --- Explanatory power: R2 -----------------------------------------------
    for rec in _rows(results.get("paths_and_r2")):
        if rec["row"] != "R^2":
            continue
        for k, v in _cells(rec):
            if v is None:
                continue
            label = ("substantial" if v >= 0.75 else
                     "moderate" if v >= 0.50 else
                     "weak" if v >= 0.25 else "very weak")
            structural.append({
                "family": "explanatory_power", "construct": k,
                "metric": "R^2", "value": round(v, 3),
                "threshold": "0.25 weak / 0.50 moderate / 0.75 substantial",
                "verdict": label,
                "citation": "Hair et al. (2022)",
            })

    # --- Effect sizes: f2 (interaction terms use Kenny's smaller benchmarks) --
    for rec in _rows(results.get("f_square")):
        pred = rec["row"]
        for k, v in _cells(rec):
            if v is None or abs(v) < 1e-12:
                continue
            if not any(p["from_construct"] == pred and p["to_construct"] == k
                       for p in request["paths"]):
                continue
            if pred in interactions:
                label = ("large" if v >= 0.025 else "medium" if v >= 0.01 else
                         "small" if v >= 0.005 else "negligible")
                structural.append({
                    "family": "moderation_effect_size", "construct": f"{pred} -> {k}",
                    "metric": "f^2 (interaction)", "value": round(v, 3),
                    "threshold": "0.005 small / 0.01 medium / 0.025 large",
                    "verdict": label,
                    "citation": "Kenny (2018)",
                })
                continue
            label = ("large" if v >= 0.35 else "medium" if v >= 0.15 else
                     "small" if v >= 0.02 else "negligible")
            structural.append({
                "family": "effect_size", "construct": f"{pred} -> {k}",
                "metric": "f^2", "value": round(v, 3),
                "threshold": "0.02 small / 0.15 medium / 0.35 large",
                "verdict": label,
                "citation": "Cohen (1988)",
            })

    # --- Model fit: SRMR (saturated model) ------------------------------------
    srmr = _num(results.get("srmr"))
    if srmr is not None:
        structural.append({
            "family": "model_fit", "construct": "overall model",
            "metric": "SRMR (saturated)", "value": round(srmr, 3),
            "threshold": "< 0.08",
            "verdict": "pass" if srmr < 0.08 else ("review" if srmr <= 0.10 else "fail"),
            "citation": "Henseler et al. (2014); Hu & Bentler (1999)",
        })

    # --- Predictive power: PLSpredict (Q2predict > 0; PLS RMSE vs LM) ---------
    beats, pred_total = 0, 0
    for rec in _rows(results.get("prediction")):
        q2 = _col(rec, "q2_predict")
        diff = _col(rec, "rmse_diff")
        if q2 is not None:
            structural.append({
                "family": "predictive_relevance", "construct": rec["row"],
                "metric": "Q^2_predict", "value": round(q2, 3), "threshold": "> 0",
                "verdict": verdict(q2 > 0),
                "citation": "Shmueli et al. (2019)",
            })
        if diff is not None:
            pred_total += 1
            beats += diff < 0
    if pred_total:
        label = ("high" if beats == pred_total else
                 "medium" if beats * 2 >= pred_total else
                 "low" if beats > 0 else "none")
        structural.append({
            "family": "predictive_power", "construct": "endogenous indicators",
            "metric": "indicators where PLS RMSE < LM RMSE",
            "value": f"{beats}/{pred_total}",
            "threshold": "all high / majority medium / minority low / none",
            "verdict": label,
            "citation": "Shmueli et al. (2019)",
        })

    # --- Hypotheses: bootstrap percentile CI excludes zero -------------------
    hnum = 0
    for rec in _rows(results.get("boot_paths")):
        parts = [p.strip() for p in rec["row"].split("->")]
        if len(parts) != 2:
            continue
        hnum += 1
        est = _col(rec, "Original Est.")
        t = _col(rec, "T Stat.")
        lo = _col(rec, "2.5% CI")
        hi = _col(rec, "97.5% CI")
        supported = lo is not None and hi is not None and (lo > 0 or hi < 0)
        hypotheses.append({
            "hypothesis": f"H{hnum}",
            "path": f"{parts[0]} -> {parts[1]}",
            "type": "moderation" if parts[0] in interactions else "direct",
            "estimate": round(est, 3) if est is not None else None,
            "t_value": round(t, 3) if t is not None else None,
            "ci_95": [round(lo, 3), round(hi, 3)] if lo is not None else None,
            "verdict": "supported" if supported else "not supported",
            "criterion": "95% percentile bootstrap CI excludes zero",
        })

    counts = {"pass": 0, "review": 0, "fail": 0}
    for item in measurement + structural:
        if item["verdict"] in counts:
            counts[item["verdict"]] += 1

    return {
        "measurement_model": measurement,
        "structural_model": structural,
        "hypotheses": hypotheses,
        "summary": {
            **counts,
            "hypotheses_supported": sum(1 for h in hypotheses if h["verdict"] == "supported"),
            "hypotheses_total": len(hypotheses),
        },
    }
