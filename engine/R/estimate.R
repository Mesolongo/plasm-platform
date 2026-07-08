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

script_dir <- dirname(normalizePath(sub("^--file=", "",
  grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[[1]])))
source(file.path(script_dir, "spec_lib.R"))

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

# --- Validate the spec and build the seminr model (spec_lib.R) ---------------
spec <- parse_spec(req, data)
if (!is.null(spec$error)) fail(spec$error$stage, spec$error$message)
higher_constructs <- spec$higher_constructs
interaction_names <- spec$interaction_names

measurement <- build_measurement(spec)
structural  <- build_structural(req)

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

# --- NFI (Bentler & Bonett 1980, saturated-model discrepancy) ------------------
# 1 - F_model/F_null with the ML discrepancy on the same implied correlation
# matrix as SRMR; the null model assumes uncorrelated indicators.
nfi_saturated <- function(model) {
  L <- model$outer_loadings
  keep <- colnames(L) %in% colnames(model$construct_scores) &
    !grepl("*", colnames(L), fixed = TRUE)
  L <- L[, keep, drop = FALSE]
  L <- L[rowSums(abs(L)) > 0, , drop = FALSE]
  C <- cor(model$construct_scores[, colnames(L), drop = FALSE])
  implied <- L %*% C %*% t(L)
  diag(implied) <- 1
  S <- cor(model$data[, rownames(L), drop = FALSE])
  p <- nrow(S)
  f_ml <- function(sigma) {
    log(det(sigma)) - log(det(S)) + sum(diag(S %*% solve(sigma))) - p
  }
  1 - f_ml(implied) / f_ml(diag(p))
}
nfi <- tryCatch(nfi_saturated(model), error = function(e) NULL)

# --- RMS_theta (Henseler et al. 2014): RMS of outer-residual correlations -------
# Reflective multi-item blocks only; single items and formative blocks have no
# meaningful measurement residual.
rms_theta_of <- function(model) {
  reflective_items <- unlist(lapply(req$constructs, function(c) {
    if (identical(c$measurement, "reflective") && length(c$indicators) > 1)
      unlist(c$indicators) else NULL
  }))
  L <- model$outer_loadings
  items <- intersect(rownames(L), reflective_items)
  if (length(items) < 4) return(NULL)
  keep <- colnames(L) %in% colnames(model$construct_scores) &
    !grepl("*", colnames(L), fixed = TRUE)
  L <- L[items, keep, drop = FALSE]
  X <- scale(as.matrix(model$data)[, items, drop = FALSE])
  resid <- X - model$construct_scores[, colnames(L), drop = FALSE] %*% t(L)
  theta <- cor(resid)
  sqrt(mean(theta[lower.tri(theta)]^2))
}
rms_theta <- tryCatch(rms_theta_of(model), error = function(e) NULL)

# --- Full collinearity VIFs (Kock 2015): common-method-bias check ---------------
# Regress every construct on all the others; VIF_j = 1/(1 - R2_j), which is the
# j-th diagonal element of the inverse construct-correlation matrix. Kock's rule:
# all full-collinearity VIFs <= 3.3 => the model is free of common method bias.
# Interaction constructs are excluded (synthetic single-indicator scores).
full_collinearity_vif <- function(model) {
  keep <- colnames(model$construct_scores)[!grepl("*", colnames(model$construct_scores),
                                                   fixed = TRUE)]
  if (length(keep) < 2) return(NULL)
  R <- cor(model$construct_scores[, keep, drop = FALSE])
  vifs <- diag(solve(R))
  lapply(keep, function(c) list(construct = c, vif = unname(vifs[c])))
}
full_vif <- tryCatch(full_collinearity_vif(model), error = function(e) NULL)

# --- Blindfolding Q2 (Hair et al. 2022, ch. 6): cross-validated redundancy ------
# Per endogenous construct block: omit every D-th data point (round-robin over
# the block), mean-replace, re-estimate, and predict the omitted points from the
# predecessor scores via the structural model (all in standardized units).
blindfold_q2 <- function(model, D = 7L) {
  endo <- unique(vapply(req$paths, function(p) p$to_construct, character(1)))
  endo <- endo[endo %in% colnames(model$outer_loadings) &
                 !grepl("*", endo, fixed = TRUE)]
  preds_of <- function(y) vapply(
    Filter(function(p) identical(p$to_construct, y), req$paths),
    function(p) p$from_construct, character(1))
  base_args <- list(measurement_model = measurement, structural_model = structural)
  if (!is.null(req$missing_value)) {
    base_args$missing <- mean_replacement
    base_args$missing_value <- as.character(req$missing_value)
  }
  rows <- lapply(endo, function(y) {
    items <- rownames(model$outer_loadings)[abs(model$outer_loadings[, y]) > 1e-12]
    items <- intersect(items, colnames(data))
    if (length(items) == 0) return(NULL)
    n <- nrow(model$data)
    k <- length(items)
    cell <- matrix(seq_len(n * k) - 1L, n, k, byrow = TRUE)  # row-major pattern
    sse <- 0; sso <- 0
    for (d in seq_len(D) - 1L) {
      omit <- (cell %% D) == d
      dd <- as.data.frame(data)
      block <- as.matrix(model$data[, items, drop = FALSE])
      train <- block
      train[omit] <- NA
      mu <- colMeans(train, na.rm = TRUE)
      sdv <- apply(train, 2, sd, na.rm = TRUE)
      filled <- train
      for (j in seq_len(k)) filled[is.na(filled[, j]), j] <- mu[j]
      dd[, items] <- filled
      m_d <- suppressMessages(suppressWarnings(
        do.call(estimate_pls, c(list(data = dd), base_args))))
      sc <- m_d$construct_scores
      yhat <- rowSums(cbind(sapply(preds_of(y), function(f)
        m_d$path_coef[f, y] * sc[, f])))
      xhat <- outer(yhat, m_d$outer_loadings[items, y])
      x_std <- sweep(sweep(block, 2, mu), 2, sdv, "/")
      sse <- sse + sum((x_std[omit] - xhat[omit])^2)
      sso <- sso + sum(x_std[omit]^2)
    }
    data.frame(row = y, q2 = 1 - sse / sso, omission_distance = D,
               stringsAsFactors = FALSE)
  })
  rows <- Filter(Negate(is.null), rows)
  if (length(rows) == 0) return(NULL)
  do.call(rbind, rows)
}
blindfolding <- tryCatch(blindfold_q2(model), error = function(e) NULL)

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
  nfi             = nfi,
  rms_theta       = rms_theta,
  blindfolding    = if (!is.null(blindfolding)) blindfolding else NULL,
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
  full_collinearity_vif = full_vif,
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
