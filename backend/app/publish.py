"""Publishing assistant (Phase 4) — reviewer-anticipation checks.

Before a PLS-SEM study goes out to a journal or a thesis committee, a reviewer
reads the same numbers the assessment already produced and asks predictable
questions: "your discriminant validity is marginal", "you never ran PLSpredict",
"how did you rule out common method bias?". This module walks the rule-based
assessment (never the raw engine output) and surfaces those concerns *before*
submission, each with the evidence that triggers it, a concrete pre-emptive
action, and the citation a reviewer would expect.

It is deterministic and needs no AI — the AI drafter (ai.draft_manuscript) turns
these concerns into cover-letter prose, but the concerns themselves are computed
here so they are reproducible and testable offline. Two kinds of concern are
raised: *evidence-driven* ones (triggered by a specific verdict in the
assessment) and a small set of *standard* ones that essentially every PLS-SEM
submission must pre-empt regardless of the numbers (common method bias,
endogeneity, unobserved heterogeneity).
"""

# Severity ranks a concern by how likely it is to draw a revise-or-reject
# request, so the front matter can lead with what actually threatens acceptance.
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _rank(concern: dict) -> int:
    return _SEVERITY_RANK.get(concern["severity"], 99)


def _by_family(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for it in items or []:
        out.setdefault(it.get("family"), []).append(it)
    return out


def reviewer_checks(assessment: dict, request: dict, dataset_meta: dict | None = None,
                    analysis_meta: dict | None = None, mga: dict | None = None) -> list[dict]:
    """Anticipate the concerns a peer reviewer will raise about this analysis.

    Returns a severity-sorted list of concerns. Each concern is a dict with:
      area, severity (high|medium|low|info), concern (what the reviewer says),
      evidence (why it fires, drawn from the assessment), recommendation (the
      pre-emptive action), citation.
    """
    measurement = _by_family(assessment.get("measurement_model"))
    structural = _by_family(assessment.get("structural_model"))
    hypotheses = assessment.get("hypotheses") or []
    concerns: list[dict] = []

    # --- Discriminant validity (the single most-common PLS reviewer challenge) --
    disc = measurement.get("discriminant_validity") or []
    dv_bad = [d for d in disc if d["verdict"] in ("review", "fail")]
    if dv_bad:
        worst = "fail" if any(d["verdict"] == "fail" for d in dv_bad) else "review"
        pairs = ", ".join(f"{d['construct']} (HTMT {d['value']})" for d in dv_bad)
        concerns.append({
            "area": "discriminant_validity",
            "severity": "high" if worst == "fail" else "medium",
            "concern": "A reviewer will question whether the constructs are empirically "
                       "distinct given the elevated HTMT ratios.",
            "evidence": f"HTMT at or above threshold for: {pairs}.",
            "recommendation": "Report the HTMT inference test (bootstrap CI upper bound "
                              "below 1, or below 0.85/0.90) rather than the point estimate "
                              "alone; if it still fails, merge or reconceptualize the "
                              "overlapping constructs and re-estimate.",
            "citation": "Henseler et al. (2015)",
        })

    # --- Convergent validity / indicator reliability --------------------------
    ave_bad = [m for m in measurement.get("convergent_validity") or [] if m["verdict"] == "fail"]
    load_bad = [m for m in measurement.get("indicator_reliability") or [] if m["verdict"] == "fail"]
    if ave_bad:
        c = ", ".join(f"{m['construct']} (AVE {m['value']})" for m in ave_bad)
        concerns.append({
            "area": "convergent_validity",
            "severity": "high",
            "concern": "A reviewer will not accept constructs whose AVE is below 0.50, "
                       "since the construct explains less variance than measurement error.",
            "evidence": f"AVE below 0.50 for: {c}.",
            "recommendation": "Remove the lowest-loading indicators until AVE clears 0.50, "
                              "and justify each deletion on content-validity grounds; if the "
                              "construct cannot be salvaged, drop or respecify it.",
            "citation": "Hair et al. (2022)",
        })
    if load_bad:
        c = ", ".join(f"{m['item']}→{m['construct']} ({m['value']})" for m in load_bad[:6])
        more = "" if len(load_bad) <= 6 else f" and {len(load_bad) - 6} more"
        concerns.append({
            "area": "indicator_reliability",
            "severity": "medium",
            "concern": "A reviewer will ask why indicators loading below 0.708 were retained.",
            "evidence": f"Outer loadings below 0.708: {c}{more}.",
            "recommendation": "Retain a 0.40–0.708 loading only when AVE and composite "
                              "reliability still hold and deleting it does not raise them; "
                              "state this rule explicitly. Remove loadings below 0.40.",
            "citation": "Hair et al. (2022)",
        })

    # --- Internal-consistency redundancy (alpha/rho above 0.95) ---------------
    redundant = [m for m in measurement.get("internal_consistency") or []
                 if m.get("note") and "redundant" in m["note"]]
    if redundant:
        c = ", ".join(f"{m['construct']} ({m['metric']} {m['value']})" for m in redundant)
        concerns.append({
            "area": "item_redundancy",
            "severity": "low",
            "concern": "A reviewer may flag semantically redundant items where reliability "
                       "exceeds 0.95.",
            "evidence": f"Reliability above 0.95 for: {c}.",
            "recommendation": "Check the items for near-duplicate wording; consider dropping "
                              "one, and note that inflated reliability can signal common "
                              "method effects.",
            "citation": "Hair et al. (2022)",
        })

    # --- Structural collinearity ----------------------------------------------
    vif_bad = [s for s in structural.get("collinearity") or [] if s["verdict"] in ("review", "fail")]
    if vif_bad:
        worst = "fail" if any(s["verdict"] == "fail" for s in vif_bad) else "review"
        c = ", ".join(f"{s['construct']} (VIF {s['value']})" for s in vif_bad)
        concerns.append({
            "area": "collinearity",
            "severity": "high" if worst == "fail" else "medium",
            "concern": "A reviewer will worry that collinearity among predictors is inflating "
                       "or destabilizing the path coefficients.",
            "evidence": f"Inner VIF at or above 3 for: {c}.",
            "recommendation": "Report the inner VIFs, and if any exceeds 5 respecify the "
                              "structural model (combine predictors or add a higher-order "
                              "construct); a VIF above 3.3 also opens a common-method-bias "
                              "question you should address.",
            "citation": "Hair et al. (2022); Kock (2015)",
        })

    # --- Explanatory power ----------------------------------------------------
    weak_r2 = [s for s in structural.get("explanatory_power") or []
               if s["verdict"] in ("weak", "very weak")]
    if weak_r2:
        c = ", ".join(f"{s['construct']} (R² {s['value']}, {s['verdict']})" for s in weak_r2)
        concerns.append({
            "area": "explanatory_power",
            "severity": "medium",
            "concern": "A reviewer will ask whether the model is theoretically useful given "
                       "the low explained variance in key endogenous constructs.",
            "evidence": f"Weak R² for: {c}.",
            "recommendation": "Frame R² against benchmarks typical for the discipline and "
                              "the construct (individual-level attitudes are routinely lower), "
                              "and lean on effect sizes and predictive relevance rather than "
                              "R² alone.",
            "citation": "Hair et al. (2022)",
        })

    # --- Supported paths with negligible effect size --------------------------
    negligible = [s for s in structural.get("effect_size") or [] if s["verdict"] == "negligible"]
    supported_paths = {h["path"] for h in hypotheses if h["verdict"] == "supported"}
    negligible_supported = [s for s in negligible if s["construct"] in supported_paths]
    if negligible_supported:
        c = ", ".join(f"{s['construct']} (f² {s['value']})" for s in negligible_supported)
        concerns.append({
            "area": "practical_significance",
            "severity": "low",
            "concern": "A reviewer will distinguish statistical from practical significance "
                       "for paths that are significant but carry a negligible effect size.",
            "evidence": f"Significant paths with negligible f²: {c}.",
            "recommendation": "Report f² alongside significance and temper the practical "
                              "claims for these paths; do not headline a negligible effect.",
            "citation": "Cohen (1988); Hair et al. (2022)",
        })

    # --- Model fit ------------------------------------------------------------
    srmr_bad = [s for s in structural.get("model_fit") or []
                if "SRMR" in s.get("metric", "") and s["verdict"] in ("review", "fail")]
    if srmr_bad:
        s = srmr_bad[0]
        concerns.append({
            "area": "model_fit",
            "severity": "medium" if s["verdict"] == "review" else "high",
            "concern": "A reviewer influenced by CB-SEM habits will read the SRMR as evidence "
                       "of misfit.",
            "evidence": f"SRMR = {s['value']} (threshold {s['threshold']}).",
            "recommendation": "Report SRMR as the approximate fit index it is for PLS, note the "
                              "ongoing debate about fit in composite models, and let the "
                              "measurement/structural criteria and PLSpredict carry the "
                              "evaluation.",
            "citation": "Henseler et al. (2014); Hu & Bentler (1999)",
        })

    # --- Predictive validity: was PLSpredict run at all? ----------------------
    has_plspredict = bool(structural.get("predictive_relevance") or structural.get("predictive_power"))
    if not has_plspredict:
        concerns.append({
            "area": "predictive_validity",
            "severity": "medium",
            "concern": "Reviewers increasingly expect an out-of-sample predictive assessment; "
                       "its absence reads as an incomplete evaluation.",
            "evidence": "No PLSpredict (Q²predict / PLS-vs-LM RMSE) results are present in "
                        "the assessment.",
            "recommendation": "Re-run the analysis with prediction enabled and report "
                              "Q²predict and the PLS-vs-LM RMSE comparison for the key "
                              "endogenous indicators.",
            "citation": "Shmueli et al. (2019)",
        })

    # --- Non-supported hypotheses (theoretical justification / HARKing) -------
    unsupported = [h for h in hypotheses if h["verdict"] == "not supported"]
    total = len(hypotheses)
    if unsupported and total:
        share = len(unsupported) / total
        names = ", ".join(f"{h['hypothesis']} ({h['path']})" for h in unsupported[:6])
        more = "" if len(unsupported) <= 6 else f" and {len(unsupported) - 6} more"
        concerns.append({
            "area": "unsupported_hypotheses",
            "severity": "high" if share >= 0.5 else "medium",
            "concern": "A reviewer will scrutinize the theory when a large share of hypotheses "
                       "fail, and will resist any post-hoc rewriting of predictions to fit the "
                       "data.",
            "evidence": f"{len(unsupported)} of {total} hypotheses not supported: {names}{more}.",
            "recommendation": "Keep the original hypotheses as stated, interpret the "
                              "non-significant paths substantively (boundary conditions, "
                              "measurement, sample), and do not reframe them after the fact.",
            "citation": "Kerr (1998)",
        })

    # --- Standard concerns every PLS-SEM submission should pre-empt -----------
    # Common method bias: single-source survey designs draw this every time. The
    # platform now runs Kock's full-collinearity test, so the concern reports the
    # actual result rather than telling the author to go compute it.
    fc = structural.get("common_method_bias") or []
    fc_fail = [s for s in fc if s["verdict"] == "fail"]
    if fc_fail:
        c = ", ".join(f"{s['construct']} (VIF {s['value']})" for s in fc_fail)
        cmb = {
            "severity": "high",
            "evidence": f"Full-collinearity VIF above 3.3 for: {c} — Kock's threshold "
                        "for pathological collinearity indicating possible common method bias.",
            "recommendation": "This is evidence of possible CMB: identify the shared-method "
                              "source, report the procedural remedies used in design, and "
                              "consider a marker-variable or measured-latent-marker-variable "
                              "test before drawing structural conclusions.",
        }
    elif fc:
        top = max(fc, key=lambda s: s["value"])
        cmb = {
            "severity": "low",
            "evidence": f"Full-collinearity test passed: all VIFs at or below 3.3 "
                        f"(highest {top['construct']} at {top['value']}, threshold 3.3).",
            "recommendation": "Report the full-collinearity test as evidence against common "
                              "method bias, alongside the procedural remedies used in design.",
        }
    else:
        cmb = {
            "severity": "medium",
            "evidence": "Full-collinearity VIFs are not present in the assessment.",
            "recommendation": "Report the procedural remedies used in design, and a full "
                              "collinearity test (all VIFs below 3.3); consider a marker "
                              "variable if one is available.",
        }
    concerns.append({
        "area": "common_method_bias",
        "severity": cmb["severity"],
        "concern": "For self-reported, single-source data a reviewer will ask how common "
                   "method bias was ruled out.",
        "evidence": cmb["evidence"],
        "recommendation": cmb["recommendation"],
        "citation": "Podsakoff et al. (2003); Kock (2015)",
    })

    # Endogeneity: standard for any structural/causal claim.
    concerns.append({
        "area": "endogeneity",
        "severity": "info",
        "concern": "For causal language a reviewer may raise endogeneity (omitted "
                   "confounders, reverse causality).",
        "evidence": "Not assessed by the platform.",
        "recommendation": "Report a Gaussian-copula endogeneity test for the key predictors, "
                          "or temper causal wording toward association for cross-sectional "
                          "data.",
        "citation": "Hult et al. (2018)",
    })

    # Unobserved heterogeneity: expected unless a multi-group analysis is shown.
    if not mga:
        concerns.append({
            "area": "unobserved_heterogeneity",
            "severity": "info",
            "concern": "A reviewer may ask whether the pooled model masks distinct segments.",
            "evidence": "No multi-group analysis accompanies the pooled estimates.",
            "recommendation": "Run FIMIX-PLS (or a theory-based multi-group analysis) to show "
                              "the pooled model is not hiding segment-level differences.",
            "citation": "Sarstedt et al. (2011); Becker et al. (2013)",
        })

    # Sample size / assumption gates overridden at estimation time.
    gates = (analysis_meta or {}).get("assumption_gates") or {}
    if gates.get("overridden") and gates.get("violations"):
        detail = "; ".join(v.get("detail", v.get("gate", "")) for v in gates["violations"])
        concerns.append({
            "area": "assumptions",
            "severity": "high",
            "concern": "A reviewer will challenge results produced despite failed pre-estimation "
                       "checks.",
            "evidence": f"Assumption gates were overridden: {detail}.",
            "recommendation": "Disclose the override and its justification prominently, and if "
                              "feasible collect more data or respecify to clear the gate rather "
                              "than relying on the override.",
            "citation": "Hair et al. (2022)",
        })

    concerns.sort(key=_rank)
    return concerns


def summarize(concerns: list[dict]) -> dict:
    """Counts by severity, for the readiness banner in the UI and report."""
    counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for c in concerns:
        if c["severity"] in counts:
            counts[c["severity"]] += 1
    return {**counts, "total": len(concerns)}
