"""Data audit: deterministic, rule-based screening of an uploaded dataset.

Every check produces a finding with a severity and, where applicable, a suggested
treatment. The audit is logged with the dataset so the methods section of the final
report can state exactly what was checked and decided. No AI is involved here.
"""
import pandas as pd

# Below this share of missing values per indicator, mean replacement is defensible
# (Hair et al. recommend < 5%); above it, flag for casewise handling.
MEAN_REPLACEMENT_MAX_PCT = 5.0


def variable_dictionary(df: pd.DataFrame, missing_value: str | None = None) -> list[dict]:
    """Per-variable summary used by the UI and (later) by the AI model architect."""
    out = []
    for col in df.columns:
        s = df[col]
        missing_code_count = 0
        if missing_value is not None:
            missing_code_count = int((s.astype(str) == str(missing_value)).sum())
        entry = {
            "name": str(col),
            "dtype": str(s.dtype),
            "n_missing": int(s.isna().sum()) + missing_code_count,
            "n_unique": int(s.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(s):
            valid = s[s.notna()]
            if missing_value is not None:
                try:
                    valid = valid[valid != float(missing_value)]
                except ValueError:
                    pass
            if len(valid) > 0:
                entry["min"] = float(valid.min())
                entry["max"] = float(valid.max())
        # Low-cardinality variables are candidate grouping variables (MGA);
        # ship their value counts so the UI can offer them without a round trip.
        if 2 <= entry["n_unique"] <= 10:
            counts = s.dropna().value_counts()
            entry["values"] = {str(k): int(v) for k, v in counts.items()}
        out.append(entry)
    return out


def run_audit(df: pd.DataFrame, missing_value: str | None = None) -> dict:
    findings = []
    n = len(df)

    work = df.copy()
    if missing_value is not None:
        try:
            code = float(missing_value)
            work = work.mask(work == code)
        except (ValueError, TypeError):
            work = work.mask(work.astype(str) == str(missing_value))

    # Missing values per variable
    for col in work.columns:
        n_miss = int(work[col].isna().sum())
        if n_miss == 0:
            continue
        pct = 100.0 * n_miss / n
        findings.append({
            "check": "missing_values",
            "variable": str(col),
            "count": n_miss,
            "pct": round(pct, 2),
            "severity": "info" if pct <= MEAN_REPLACEMENT_MAX_PCT else "warning",
            "suggestion": (
                "mean replacement acceptable (< 5% missing)"
                if pct <= MEAN_REPLACEMENT_MAX_PCT
                else "more than 5% missing — consider casewise deletion or review the item"
            ),
        })

    # Constant / zero-variance columns can break estimation
    for col in work.columns:
        if work[col].nunique(dropna=True) <= 1:
            findings.append({
                "check": "zero_variance",
                "variable": str(col),
                "severity": "warning",
                "suggestion": "constant column — exclude from the model",
            })

    # Straight-lining: respondents answering identically across all numeric items
    numeric = work.select_dtypes("number")
    if numeric.shape[1] >= 5:
        row_std = numeric.std(axis=1)
        straight = df.index[row_std == 0].tolist()
        if straight:
            findings.append({
                "check": "straight_lining",
                "rows": [int(i) for i in straight[:50]],
                "count": len(straight),
                "severity": "warning",
                "suggestion": "identical answers across all items — review these respondents",
            })

    # Duplicate rows
    n_dup = int(df.duplicated().sum())
    if n_dup > 0:
        findings.append({
            "check": "duplicate_rows",
            "count": n_dup,
            "severity": "warning",
            "suggestion": "exact duplicate responses — verify these are distinct respondents",
        })

    return {
        "n_observations": n,
        "n_variables": int(df.shape[1]),
        "missing_value_code": missing_value,
        "n_findings": len(findings),
        "findings": findings,
    }
