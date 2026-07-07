#!/usr/bin/env Rscript
# PLS-SEM engine service entrypoint: model spec JSON in -> results JSON out.
#
# Usage: Rscript estimate.R <request.json> <output.json>
#
# Request schema (versioned; produced by the backend from the approved model spec):
# {
#   "schema_version": 2,
#   "data_csv": "/abs/path/to/data.csv",
#   "missing_value": "-99",                # optional; values coded as missing
#   "options": {"nboot": 5000, "seed": 123,
#               "prediction": true,        # PLSpredict (k-fold); default true
#               "predict_folds": 10, "predict_reps": 1},
#   "constructs": [
#     {"name": "QUAL", "indicators": ["qual_1", ...], "measurement": "formative"},
#     {"name": "COMP", "indicators": ["comp_1", ...], "measurement": "reflective"},
#     {"name": "CUSA", "indicators": ["cusa"],        "measurement": "single_item"},
#     {"name": "REP",  "dimensions": ["COMP", "LIKE"],
#      "measurement": "higher_order_reflective"}      # or higher_order_formative
#   ],
#   "interactions": [{"iv": "CUSA", "moderator": "SC"}],  # two-stage; construct "CUSA*SC"
#   "paths": [{"from_construct": "QUAL", "to_construct": "COMP"}, ...]
# }
#
# Schema v1 requests (no interactions / higher-order constructs) remain valid.
#
# Errors are reported as JSON on stdout with exit code 2 so the backend can
# surface them to the user instead of a stack trace.

suppressPackageStartupMessages({
  library(seminr)
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2) stop("usage: Rscript estimate.R <request.json> <output.json>")
req_path <- args[[1]]
out_path <- args[[2]]

fail <- function(stage, message) {
  write(toJSON(list(error = list(stage = stage, message = message)), auto_unbox = TRUE), stdout())
  quit(status = 2)
}

req <- tryCatch(fromJSON(req_path, simplifyVector = FALSE),
                error = function(e) fail("parse_request", conditionMessage(e)))

data <- tryCatch(read.csv(req$data_csv, check.names = FALSE),
                 error = function(e) fail("read_data", conditionMessage(e)))

# --- Validate the spec against the data before touching seminr --------------
`%||%` <- function(a, b) if (is.null(a)) b else a
is_hoc <- function(c) grepl("^higher_order", c$measurement %||% "")

lower_constructs  <- Filter(function(c) !is_hoc(c), req$constructs)
higher_constructs <- Filter(is_hoc, req$constructs)
interactions <- req$interactions %||% list()

all_indicators <- unlist(lapply(lower_constructs, function(c) unlist(c$indicators)))
missing_cols <- setdiff(all_indicators, colnames(data))
if (length(missing_cols) > 0) {
  fail("validate_spec", paste("indicators not found in dataset:",
                              paste(missing_cols, collapse = ", ")))
}
dup <- all_indicators[duplicated(all_indicators)]
if (length(dup) > 0) {
  fail("validate_spec", paste("indicator assigned to multiple constructs:",
                              paste(unique(dup), collapse = ", ")))
}
lower_names <- vapply(lower_constructs, function(c) c$name, character(1))
for (h in higher_constructs) {
  dims <- unlist(h$dimensions)
  if (length(dims) < 2) {
    fail("validate_spec", paste("higher-order construct", h$name,
                                "needs at least 2 dimensions"))
  }
  bad <- setdiff(dims, lower_names)
  if (length(bad) > 0) {
    fail("validate_spec", paste("higher-order construct", h$name,
                                "references unknown dimensions:", paste(bad, collapse = ", ")))
  }
}
interaction_names <- character(0)
for (ix in interactions) {
  if (!(ix$iv %in% lower_names) || !(ix$moderator %in% lower_names)) {
    fail("validate_spec", paste("interaction references unknown construct:",
                                ix$iv, "x", ix$moderator))
  }
  interaction_names <- c(interaction_names, paste0(ix$iv, "*", ix$moderator))
}
construct_names <- c(lower_names,
                     vapply(higher_constructs, function(c) c$name, character(1)),
                     interaction_names)
for (p in req$paths) {
  if (!(p$from_construct %in% construct_names) || !(p$to_construct %in% construct_names)) {
    fail("validate_spec", paste("path references unknown construct:",
                                p$from_construct, "->", p$to_construct))
  }
}

# --- Build the seminr model from the spec ------------------------------------
make_composite <- function(c) {
  items <- unlist(c$indicators)
  if (identical(c$measurement, "single_item")) return(composite(c$name, single_item(items[[1]])))
  if (identical(c$measurement, "formative"))   return(composite(c$name, items, weights = mode_B))
  composite(c$name, items)  # reflective (mode A)
}
make_hoc <- function(c) {
  w <- if (identical(c$measurement, "higher_order_formative")) mode_B else mode_A
  higher_composite(c$name, dimensions = unlist(c$dimensions), method = two_stage, weights = w)
}
make_interaction <- function(ix) {
  interaction_term(iv = ix$iv, moderator = ix$moderator, method = two_stage)
}
measurement <- do.call(constructs, c(lapply(lower_constructs, make_composite),
                                     lapply(higher_constructs, make_hoc),
                                     lapply(interactions, make_interaction)))
structural <- do.call(relationships, lapply(req$paths, function(p) {
  paths(from = p$from_construct, to = p$to_construct)
}))

nboot <- if (!is.null(req$options$nboot)) as.integer(req$options$nboot) else 5000L
seed  <- if (!is.null(req$options$seed)) as.integer(req$options$seed) else 123L

estimate_args <- list(data = data, measurement_model = measurement, structural_model = structural)
if (!is.null(req$missing_value)) {
  estimate_args$missing <- mean_replacement
  estimate_args$missing_value <- as.character(req$missing_value)
}

model <- tryCatch(do.call(estimate_pls, estimate_args),
                  error = function(e) fail("estimate", conditionMessage(e)))
s <- summary(model)
boot <- tryCatch(bootstrap_model(model, nboot = nboot, seed = seed),
                 error = function(e) fail("bootstrap", conditionMessage(e)))
bs <- summary(boot)

# --- SRMR: saturated model (Henseler et al. 2014) -----------------------------
# Model-implied indicator correlations from outer loadings + construct score
# correlations vs the empirical correlations, RMS over the lower triangle.
# Interaction constructs are excluded (synthetic single-indicator scores).
srmr_saturated <- function(model) {
  L <- model$outer_loadings
  # Constructs of the estimated (second-stage) model only: for two-stage
  # higher-order models the loading matrix also carries first-stage blocks,
  # whose constructs have no scores in construct_scores.
  keep <- colnames(L) %in% colnames(model$construct_scores) &
    !grepl("*", colnames(L), fixed = TRUE)
  L <- L[, keep, drop = FALSE]
  L <- L[rowSums(abs(L)) > 0, , drop = FALSE]
  C <- cor(model$construct_scores[, colnames(L), drop = FALSE])
  implied <- L %*% C %*% t(L)
  diag(implied) <- 1
  # model$data holds raw indicators plus stage-one dimension scores, so it
  # covers every item that can appear in L.
  emp <- cor(model$data[, rownames(L), drop = FALSE])
  lt <- lower.tri(emp)
  sqrt(mean((emp[lt] - implied[lt])^2))
}
srmr <- tryCatch(srmr_saturated(model), error = function(e) NULL)

# --- PLSpredict: k-fold out-of-sample prediction (Shmueli et al. 2019) --------
# Q2_predict = 1 - SSE_oos / SSO (benchmark: indicator mean). RMSE compared
# against a linear regression benchmark (LM) per SmartPLS convention.
prediction <- NULL
predict_on    <- !identical(req$options$prediction, FALSE)
predict_folds <- as.integer(req$options$predict_folds %||% 10L)
predict_reps  <- as.integer(req$options$predict_reps %||% 1L)
if (predict_on) {
  prediction <- tryCatch({
    set.seed(seed)
    pm <- predict_pls(model, technique = predict_DA,
                      noFolds = predict_folds, reps = predict_reps)
    it <- pm$items
    res_pls <- as.matrix(it$PLS_out_of_sample_residuals)
    res_lm  <- as.matrix(it$lm_out_of_sample_residuals)
    act <- as.matrix(it$item_actuals[, colnames(res_pls), drop = FALSE])
    sso <- colSums(scale(act, scale = FALSE)^2)
    rmse <- function(r) sqrt(colMeans(r^2))
    mae  <- function(r) colMeans(abs(r))
    df <- data.frame(
      row = colnames(res_pls),
      rmse_pls = rmse(res_pls), mae_pls = mae(res_pls),
      rmse_lm = rmse(res_lm)[colnames(res_pls)],
      mae_lm = mae(res_lm)[colnames(res_pls)],
      q2_predict = 1 - colSums(res_pls^2) / sso,
      stringsAsFactors = FALSE
    )
    rownames(df) <- NULL
    df
  }, error = function(e) NULL)
}

# --- Extract results ----------------------------------------------------------
mat_records <- function(m) {
  if (is.null(m)) return(NULL)
  m <- as.matrix(unclass(m))
  df <- as.data.frame(m, stringsAsFactors = FALSE)
  cbind(row = rownames(m), df, stringsAsFactors = FALSE)
}
maybe <- function(expr) tryCatch(expr, error = function(e) NULL)

# --- Mediation: specific indirect effects (Hair et al. 2022, ch. 7) -----------
# Every simple directed chain of length >= 2 in the structural model is a
# candidate indirect effect; seminr multiplies the bootstrap path distributions
# segment-wise, so CIs come from the same resamples as the direct paths.
enumerate_chains <- function(paths) {
  adj <- list()
  for (p in paths) adj[[p$from_construct]] <- c(adj[[p$from_construct]], p$to_construct)
  chains <- list()
  walk <- function(chain) {
    for (nxt in adj[[chain[length(chain)]]]) {
      if (nxt %in% chain) next
      extended <- c(chain, nxt)
      if (length(extended) >= 3) chains[[length(chains) + 1]] <<- extended
      walk(extended)
    }
  }
  for (start in names(adj)) walk(start)
  chains
}
specific_indirect <- maybe({
  rows <- Filter(Negate(is.null), lapply(enumerate_chains(req$paths), function(ch) {
    maybe(specific_effect_significance(boot, from = ch[[1]], to = ch[[length(ch)]],
                                       through = ch[2:(length(ch) - 1)]))
  }))
  if (length(rows) > 0) do.call(rbind, rows) else NULL
})

# --- IPMA (Ringle & Sarstedt 2016) ---------------------------------------------
# Performance: construct scores rebuilt from indicators rescaled to 0-100 with
# unstandardized, sum-normalized outer weights. Importance: unstandardized total
# effects, re-estimated by OLS over the performance-scale scores. Interaction
# terms are excluded (their synthetic scores have no performance scale).
compute_ipma <- function() {
  W <- model$outer_weights
  keep <- colnames(W)[colnames(W) %in% colnames(model$construct_scores) &
                        !grepl("*", colnames(W), fixed = TRUE)]
  if (length(keep) < 2) return(NULL)
  X <- as.matrix(model$data)
  perf <- matrix(NA_real_, nrow(X), length(keep), dimnames = list(NULL, keep))
  for (cn in keep) {
    w <- W[, cn]
    items <- rownames(W)[abs(w) > 1e-12]
    sds <- apply(X[, items, drop = FALSE], 2, sd)
    rng <- apply(X[, items, drop = FALSE], 2, range)
    if (any(sds < 1e-12) || any(rng[2, ] - rng[1, ] < 1e-12)) return(NULL)
    X01 <- sweep(sweep(X[, items, drop = FALSE], 2, rng[1, ]), 2,
                 rng[2, ] - rng[1, ], "/") * 100
    wu <- w[items] / sds
    if (abs(sum(wu)) < 1e-8) return(NULL)  # weights cancel out; scores undefined
    perf[, cn] <- X01 %*% (wu / sum(wu))
  }
  Bu <- matrix(0, length(keep), length(keep), dimnames = list(keep, keep))
  for (to in unique(vapply(req$paths, function(p) p$to_construct, character(1)))) {
    if (!(to %in% keep)) next
    preds <- vapply(Filter(function(p) identical(p$to_construct, to), req$paths),
                    function(p) p$from_construct, character(1))
    preds <- intersect(preds, keep)
    if (length(preds) == 0) next
    beta <- coef(lm(perf[, to] ~ perf[, preds, drop = FALSE]))[-1]
    beta[is.na(beta)] <- 0
    Bu[preds, to] <- beta
  }
  total <- Bu; step <- Bu
  for (k in seq_len(length(keep) - 1)) {
    step <- step %*% Bu
    total <- total + step
  }
  list(
    performance = data.frame(row = keep, performance = colMeans(perf)),
    total_effects_unstd = mat_records(total),
    excluded = if (length(interaction_names) > 0) as.list(interaction_names) else NULL
  )
}
ipma <- maybe(compute_ipma())

out <- list(
  meta = list(
    schema_version = 2,
    engine = paste0("seminr ", as.character(packageVersion("seminr"))),
    r_version = R.version.string,
    n = nrow(data),
    nboot = nboot,
    seed = seed,
    missing_value = req$missing_value,
    interactions = if (length(interaction_names) > 0) interaction_names else NULL,
    higher_order = if (length(higher_constructs) > 0)
      vapply(higher_constructs, function(c) c$name, character(1)) else NULL,
    prediction = list(enabled = predict_on && !is.null(prediction),
                      folds = predict_folds, reps = predict_reps,
                      technique = "predict_DA"),
    timestamp = format(Sys.time(), "%Y-%m-%d %H:%M:%S %Z")
  ),
  srmr            = srmr,
  prediction      = if (!is.null(prediction)) {
    cbind(prediction, rmse_diff = prediction$rmse_pls - prediction$rmse_lm)
  } else NULL,
  loadings        = mat_records(s$loadings),
  weights         = mat_records(maybe(s$weights)),
  reliability     = mat_records(s$reliability),
  htmt            = mat_records(maybe(s$validity$htmt)),
  fornell_larcker = mat_records(maybe(s$validity$fl_criteria)),
  cross_loadings  = mat_records(maybe(s$validity$cross_loadings)),
  vif_structural  = maybe(lapply(s$vif_antecedents, as.list)),
  paths_and_r2    = mat_records(s$paths),
  f_square        = mat_records(maybe(s$fSquare)),
  total_effects   = mat_records(maybe(s$total_effects)),
  total_indirect  = mat_records(maybe(s$total_indirect_effects)),
  specific_indirect = mat_records(specific_indirect),
  ipma            = ipma,
  boot_paths      = mat_records(bs$bootstrapped_paths),
  boot_loadings   = mat_records(bs$bootstrapped_loadings),
  boot_weights    = mat_records(maybe(bs$bootstrapped_weights)),
  boot_total      = mat_records(maybe(bs$bootstrapped_total_paths)),
  boot_htmt       = mat_records(maybe(bs$bootstrapped_HTMT))
)

write(toJSON(out, dataframe = "rows", digits = 6, pretty = TRUE, na = "null", auto_unbox = TRUE), out_path)
cat("ok\n")
