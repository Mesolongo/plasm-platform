"""Word report generation from engine results + assessment.

Deterministic: every number comes from the results object, every verdict from
assess.py. The AI report-writer (needs API credentials) will later add narrative
sections; this module already produces a complete, citable results report.
"""
import datetime

from docx import Document
from docx.shared import Pt


def _table(doc, headers, rows):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = h
        for p in cell.paragraphs:
            for r in p.runs:
                r.font.bold = True
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = "" if v is None else str(v)
    return t


def _fmt(v):
    return f"{v:.3f}" if isinstance(v, (int, float)) else (v or "")


def build_report(dataset_meta: dict, request: dict, results: dict, assessment: dict,
                 interpretation: dict | None = None) -> Document:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)

    doc.add_heading("PLS-SEM Analysis Report", level=0)
    section_no = [0]

    def add_section(title):
        section_no[0] += 1
        doc.add_heading(f"{section_no[0]}. {title}", level=1)
    meta = results["meta"]
    doc.add_paragraph(
        f"Generated {datetime.date.today().isoformat()} · dataset: {dataset_meta.get('filename')} "
        f"(n = {meta['n']}) · engine: {meta['engine']} · bootstrap: {meta['nboot']:,} resamples, "
        f"fixed seed {meta['seed']}."
    )

    # --- Method ---------------------------------------------------------------
    add_section("Method")
    n_constructs = len(request["constructs"])
    modes = {}
    for c in request["constructs"]:
        modes.setdefault(c["measurement"], []).append(c["name"])
    mode_text = "; ".join(
        f"{m.replace('_', '-')}: {', '.join(names)}" for m, names in modes.items()
    )
    missing_text = (
        f"Values coded {request['missing_value']} were treated as missing and handled by "
        f"mean replacement." if request.get("missing_value") else
        "No missing-value code was specified."
    )
    doc.add_paragraph(
        f"Partial least squares structural equation modeling (PLS-SEM) was applied to a model "
        f"with {n_constructs} constructs ({mode_text}) and {len(request['paths'])} structural "
        f"paths. {missing_text} Significance was assessed with {meta['nboot']:,} bootstrap "
        f"resamples using percentile confidence intervals."
    )
    interactions = request.get("interactions") or []
    if interactions:
        terms = ", ".join(f"{i['iv']} × {i['moderator']}" for i in interactions)
        doc.add_paragraph(
            f"Moderation was modeled with the two-stage approach (interaction term(s): "
            f"{terms}; Becker et al., 2018)."
        )
    hocs = [c for c in request["constructs"]
            if c["measurement"].startswith("higher_order")]
    if hocs:
        hoc_text = "; ".join(f"{c['name']} ({', '.join(c['dimensions'])})" for c in hocs)
        doc.add_paragraph(
            f"Higher-order construct(s) were estimated with the two-stage (disjoint) "
            f"approach: {hoc_text} (Sarstedt et al., 2019)."
        )
    audit = dataset_meta.get("audit") or {}
    if audit.get("findings"):
        doc.add_paragraph(
            f"Data screening produced {audit['n_findings']} finding(s) "
            f"({', '.join(sorted({f['check'] for f in audit['findings']}))}); "
            f"details are stored with the dataset audit log."
        )

    # --- Measurement model ------------------------------------------------------
    add_section("Measurement Model Assessment")
    rel = [m for m in assessment["measurement_model"] if m["family"] == "internal_consistency"]
    ave = {m["construct"]: m for m in assessment["measurement_model"] if m["family"] == "convergent_validity"}
    by_construct = {}
    for m in rel:
        by_construct.setdefault(m["construct"], {})[m["metric"]] = m
    rows = []
    for name, metrics in by_construct.items():
        a = metrics.get("Cronbach's alpha", {})
        rA = metrics.get("rho_A", {})
        rC = metrics.get("composite reliability rho_C", {})
        av = ave.get(name, {})
        verdicts = {m.get("verdict") for m in (a, rA, rC, av) if m}
        rows.append([name, _fmt(a.get("value")), _fmt(rA.get("value")), _fmt(rC.get("value")),
                     _fmt(av.get("value")), "OK" if verdicts <= {"pass"} else "REVIEW"])
    if rows:
        doc.add_paragraph("Internal consistency reliability and convergent validity "
                          "(criteria: 0.70–0.95 for reliability; AVE ≥ 0.50; Hair et al., 2022):")
        _table(doc, ["Construct", "α", "ρA", "ρC", "AVE", "Verdict"], rows)

    htmt = [m for m in assessment["measurement_model"] if m["family"] == "discriminant_validity"]
    if htmt:
        doc.add_paragraph()
        doc.add_paragraph("Discriminant validity — HTMT (criterion < 0.85; Henseler et al., 2015):")
        _table(doc, ["Construct pair", "HTMT", "Verdict"],
               [[m["construct"], _fmt(m["value"]), m["verdict"].upper()] for m in htmt])

    low = [m for m in assessment["measurement_model"]
           if m["family"] == "indicator_reliability" and m["verdict"] == "fail"]
    doc.add_paragraph()
    doc.add_paragraph(
        "All reflective indicator loadings meet the 0.708 criterion." if not low else
        f"{len(low)} indicator(s) fall below the 0.708 loading criterion: "
        + ", ".join(f"{m['item']} ({_fmt(m['value'])})" for m in low) + "."
    )

    formative = [m for m in assessment["measurement_model"]
                 if m["family"] == "formative_indicator_validity"]
    if formative:
        doc.add_paragraph()
        doc.add_paragraph(
            "Formative indicator validity — outer weight significance "
            "(95% bootstrap CI; n.s. weights retained when the loading is ≥ 0.5; "
            "Hair et al., 2022):"
        )
        _table(doc, ["Construct", "Indicator", "Weight", "95% CI", "Verdict"],
               [[m["construct"], m["item"], _fmt(m["value"]),
                 f"[{m['ci_95'][0]:.3f}; {m['ci_95'][1]:.3f}]",
                 m["verdict"].upper()] for m in formative])

    # --- Structural model -------------------------------------------------------
    add_section("Structural Model Assessment")
    vif_bad = [s for s in assessment["structural_model"]
               if s["family"] == "collinearity" and s["verdict"] != "pass"]
    doc.add_paragraph(
        "Collinearity: all inner VIF values are below 3." if not vif_bad else
        "Collinearity: " + "; ".join(f"{s['construct']} VIF = {_fmt(s['value'])} ({s['verdict']})"
                                     for s in vif_bad) + "."
    )
    r2 = [s for s in assessment["structural_model"] if s["family"] == "explanatory_power"]
    if r2:
        _table(doc, ["Endogenous construct", "R²", "Interpretation"],
               [[s["construct"], _fmt(s["value"]), s["verdict"]] for s in r2])

    doc.add_paragraph()
    doc.add_paragraph("Path coefficients with bootstrap significance "
                      "(95% percentile CIs; verdict: CI excludes zero):")
    _table(doc, ["Hypothesis", "Path", "β", "t", "95% CI", "Verdict"],
           [[h["hypothesis"], h["path"], _fmt(h["estimate"]), _fmt(h["t_value"]),
             f"[{h['ci_95'][0]:.3f}; {h['ci_95'][1]:.3f}]" if h["ci_95"] else "",
             h["verdict"]] for h in assessment["hypotheses"]])

    f2 = [s for s in assessment["structural_model"] if s["family"] == "effect_size"]
    if f2:
        doc.add_paragraph()
        doc.add_paragraph("Effect sizes f² (0.02 small / 0.15 medium / 0.35 large; Cohen, 1988):")
        _table(doc, ["Path", "f²", "Class"],
               [[s["construct"], _fmt(s["value"]), s["verdict"]] for s in f2])

    mod_f2 = [s for s in assessment["structural_model"]
              if s["family"] == "moderation_effect_size"]
    if mod_f2:
        doc.add_paragraph()
        doc.add_paragraph("Interaction-term effect sizes f² "
                          "(0.005 small / 0.01 medium / 0.025 large; Kenny, 2018):")
        _table(doc, ["Interaction", "f²", "Class"],
               [[s["construct"], _fmt(s["value"]), s["verdict"]] for s in mod_f2])

    fit = next((s for s in assessment["structural_model"] if s["family"] == "model_fit"), None)
    if fit:
        doc.add_paragraph()
        doc.add_paragraph(
            f"Model fit: SRMR (saturated model) = {_fmt(fit['value'])} "
            f"(criterion {fit['threshold']}; {fit['citation']}) — {fit['verdict'].upper()}."
        )

    # --- Predictive power: PLSpredict ------------------------------------------
    q2 = [s for s in assessment["structural_model"] if s["family"] == "predictive_relevance"]
    power = next((s for s in assessment["structural_model"]
                  if s["family"] == "predictive_power"), None)
    if q2:
        add_section("Predictive Power (PLSpredict)")
        pred = {r["row"]: r for r in results.get("prediction") or []}
        rows = []
        for s in q2:
            r = pred.get(s["construct"], {})
            rows.append([s["construct"], _fmt(s["value"]),
                         _fmt(r.get("rmse_pls")), _fmt(r.get("rmse_lm")),
                         "PLS" if (r.get("rmse_diff") or 0) < 0 else "LM",
                         s["verdict"].upper()])
        doc.add_paragraph(
            "Out-of-sample prediction via k-fold PLSpredict (Shmueli et al., 2019). "
            "Q²predict > 0 indicates the model outperforms the indicator-mean benchmark; "
            "RMSE is compared against a linear-model (LM) benchmark:"
        )
        _table(doc, ["Indicator", "Q²predict", "RMSE (PLS)", "RMSE (LM)", "Lower RMSE", "Verdict"], rows)
        if power:
            doc.add_paragraph(
                f"PLS achieves lower out-of-sample RMSE than the LM benchmark for "
                f"{power['value']} endogenous indicators — {power['verdict']} predictive "
                f"power (Shmueli et al., 2019)."
            )

    # --- Summary ---------------------------------------------------------------
    add_section("Summary")
    summ = assessment["summary"]
    doc.add_paragraph(
        f"{summ['hypotheses_supported']} of {summ['hypotheses_total']} hypothesized paths are "
        f"supported. Measurement/structural checks: {summ['pass']} pass, {summ['review']} to "
        f"review, {summ['fail']} fail."
    )
    # --- AI narrative sections (grounded in the assessment) --------------------
    if interpretation:
        sections = [
            ("Results Narrative", "results_narrative"),
            ("Discussion", "discussion"),
            ("Managerial Implications", "managerial_implications"),
            ("Limitations", "limitations"),
            ("Conclusion", "conclusion"),
        ]
        for heading, key in sections:
            text = interpretation.get(key)
            if not text:
                continue
            add_section(heading)
            for para in text.split("\n\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
        doc.add_paragraph(
            "Narrative sections were drafted by an AI writer constrained to the "
            "engine-computed statistics and threshold verdicts reported above; "
            "review before submission."
        )
    else:
        doc.add_paragraph(
            "Interpretive narrative (Discussion, Implications) has not been generated "
            "for this analysis; every statistic above is engine-computed."
        )
    return doc
