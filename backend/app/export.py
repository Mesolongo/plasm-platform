"""Excel export of engine results + assessment.

Deterministic like report.py: every number comes from the results/assessment
objects. Excel ships every results table on its own sheet (SmartPLS-style
workbook).
"""
import pandas as pd

# results keys exported as sheets, in workbook order
RESULT_SHEETS = [
    ("loadings", "Loadings"),
    ("weights", "Outer weights"),
    ("reliability", "Reliability"),
    ("htmt", "HTMT"),
    ("fornell_larcker", "Fornell-Larcker"),
    ("cross_loadings", "Cross loadings"),
    ("paths_and_r2", "Paths and R2"),
    ("f_square", "f2"),
    ("total_effects", "Total effects"),
    ("total_indirect", "Total indirect"),
    ("specific_indirect", "Specific indirect"),
    ("blindfolding", "Blindfolding Q2"),
    ("prediction", "PLSpredict"),
    ("boot_paths", "Bootstrap paths"),
    ("boot_loadings", "Bootstrap loadings"),
    ("boot_weights", "Bootstrap weights"),
    ("boot_htmt", "Bootstrap HTMT"),
]


def _records_df(records) -> pd.DataFrame | None:
    if not isinstance(records, list) or not records:
        return None
    df = pd.DataFrame.from_records(records)
    return df.drop(columns=[c for c in df.columns if c.startswith("_")], errors="ignore")


def write_xlsx(path, results: dict, assessment: dict, mga: dict | None = None):
    """Full results workbook; one sheet per table."""
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        scalars = {"SRMR": results.get("srmr"), "NFI": results.get("nfi"),
                   "RMS_theta": results.get("rms_theta")}
        pd.DataFrame([{"metric": k, "value": v} for k, v in scalars.items()
                      if v is not None]).to_excel(xl, sheet_name="Model fit", index=False)
        for key, sheet in RESULT_SHEETS:
            df = _records_df(results.get(key))
            if df is not None:
                df.to_excel(xl, sheet_name=sheet, index=False)
        ipma = results.get("ipma") or {}
        for key, sheet in (("performance", "IPMA performance"),
                           ("total_effects_unstd", "IPMA importance")):
            df = _records_df(ipma.get(key))
            if df is not None:
                df.to_excel(xl, sheet_name=sheet, index=False)
        for key, sheet in (("measurement_model", "Assessment measurement"),
                           ("structural_model", "Assessment structural"),
                           ("hypotheses", "Hypotheses"),
                           ("mediation", "Mediation")):
            df = _records_df(assessment.get(key))
            if df is not None:
                df.to_excel(xl, sheet_name=sheet, index=False)
        if mga:
            for key, sheet in (("step2", "MICOM step 2"), ("step3", "MICOM step 3")):
                df = _records_df((mga.get("micom") or {}).get(key))
                if df is not None:
                    df.to_excel(xl, sheet_name=sheet, index=False)
            df = _records_df(mga.get("paths"))
            if df is not None:
                df.to_excel(xl, sheet_name="MGA paths", index=False)


