#!/usr/bin/env Rscript
# Phase 0 parity benchmark: corporate reputation models (Hair et al., PLS-SEM Primer 3e).
# Estimates both book models on seminr's corp_rep_data and writes every statistic the
# parity harness compares against the published SmartPLS 4 case-study output:
#   - simple model   -> chapter 4 values (loadings, alpha, rhoA, rhoC, AVE, Fornell-Larcker, HTMT)
#   - extended model -> chapter 6 values (paths, R2, f2, total effects, VIF, bootstrap t-values)
#
# Usage: Rscript benchmark_corp_rep.R <output.json> [nboot]

suppressPackageStartupMessages({
  library(seminr)
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
out_path <- if (length(args) >= 1) args[[1]] else "corp_rep_results.json"
nboot <- if (length(args) >= 2) as.integer(args[[2]]) else 10000L

data("corp_rep_data", package = "seminr")

mat_records <- function(m) {
  if (is.null(m)) return(NULL)
  m <- as.matrix(unclass(m))
  df <- as.data.frame(m, stringsAsFactors = FALSE)
  cbind(row = rownames(m), df, stringsAsFactors = FALSE)
}
maybe <- function(expr) tryCatch(expr, error = function(e) NULL)

estimate_and_extract <- function(measurement, structural, nboot) {
  model <- estimate_pls(
    data = corp_rep_data,
    measurement_model = measurement,
    structural_model = structural,
    missing = mean_replacement,
    missing_value = "-99"
  )
  s <- summary(model)
  boot <- bootstrap_model(model, nboot = nboot, seed = 123)
  bs <- summary(boot)
  list(
    loadings        = mat_records(s$loadings),
    weights         = mat_records(maybe(s$weights)),
    reliability     = mat_records(s$reliability),            # alpha, rhoC, AVE, rhoA
    htmt            = mat_records(maybe(s$validity$htmt)),
    fornell_larcker = mat_records(maybe(s$validity$fl_criteria)),
    vif_structural  = maybe(lapply(s$vif_antecedents, as.list)),
    paths_and_r2    = mat_records(s$paths),                  # includes R^2 / AdjR^2 rows
    f_square        = mat_records(maybe(s$fSquare)),
    total_effects   = mat_records(maybe(s$total_effects)),
    total_indirect  = mat_records(maybe(s$total_indirect_effects)),
    boot_paths      = mat_records(bs$bootstrapped_paths),
    boot_total      = mat_records(maybe(bs$bootstrapped_total_paths)),
    boot_htmt       = mat_records(maybe(bs$bootstrapped_HTMT)),
    iterations      = maybe(model$iterations)
  )
}

# --- Simple model (Primer ch. 2-4) ------------------------------------------
simple_mm <- constructs(
  composite("COMP", multi_items("comp_", 1:3)),
  composite("LIKE", multi_items("like_", 1:3)),
  composite("CUSA", single_item("cusa")),
  composite("CUSL", multi_items("cusl_", 1:3))
)
simple_sm <- relationships(
  paths(from = c("COMP", "LIKE"), to = c("CUSA", "CUSL")),
  paths(from = "CUSA", to = "CUSL")
)

# --- Extended model (Primer ch. 5-6): formative driver constructs ------------
extended_mm <- constructs(
  composite("QUAL", multi_items("qual_", 1:8), weights = mode_B),
  composite("PERF", multi_items("perf_", 1:5), weights = mode_B),
  composite("CSOR", multi_items("csor_", 1:5), weights = mode_B),
  composite("ATTR", multi_items("attr_", 1:3), weights = mode_B),
  composite("COMP", multi_items("comp_", 1:3)),
  composite("LIKE", multi_items("like_", 1:3)),
  composite("CUSA", single_item("cusa")),
  composite("CUSL", multi_items("cusl_", 1:3))
)
extended_sm <- relationships(
  paths(from = c("QUAL", "PERF", "CSOR", "ATTR"), to = c("COMP", "LIKE")),
  paths(from = c("COMP", "LIKE"), to = c("CUSA", "CUSL")),
  paths(from = "CUSA", to = "CUSL")
)

message("Estimating simple model...")
simple <- estimate_and_extract(simple_mm, simple_sm, nboot)
message("Estimating extended model...")
extended <- estimate_and_extract(extended_mm, extended_sm, nboot)

out <- list(
  meta = list(
    dataset = "corp_rep_data (seminr)",
    n = nrow(corp_rep_data),
    engine = paste0("seminr ", as.character(packageVersion("seminr"))),
    r_version = R.version.string,
    nboot = nboot,
    seed = 123,
    missing_treatment = "mean replacement, code -99",
    timestamp = format(Sys.time(), "%Y-%m-%d %H:%M:%S %Z")
  ),
  simple = simple,
  extended = extended
)

write(toJSON(out, dataframe = "rows", digits = 6, pretty = TRUE, na = "null", auto_unbox = TRUE), out_path)
cat("Wrote", out_path, "\n")
