#!/usr/bin/env Rscript
# Multi-group analysis service entrypoint: MGA request JSON in -> results JSON out.
#
# Usage: Rscript mga.R <request.json> <output.json>
#
# Request = the estimate.R schema plus a "group" block:
#   "group":   {"variable": "servicetype", "value_a": "1", "value_b": "2"},
#   "options": {"npermutations": 1000, "seed": 123}
#
# Implements the published two-step workflow:
#   MICOM (Henseler, Ringle & Sarstedt 2016): step 1 configural invariance holds
#   by design (identical spec, data treatment, algorithm); step 2 compositional
#   invariance via permutation test on the correlation c between construct
#   scores formed with the two groups' weights; step 3 equality of construct
#   score means/variances via permutation CIs.
#   MGA: permutation test on path-coefficient differences (Chin & Dibbern 2010),
#   sharing the same permutation draws as MICOM.
#
# Interaction terms and higher-order constructs are not supported in MGA v1;
# the backend surfaces that as a validation error before estimation.

suppressPackageStartupMessages({
  library(seminr)
  library(jsonlite)
})

script_dir <- dirname(normalizePath(sub("^--file=", "",
  grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)[[1]])))
source(file.path(script_dir, "spec_lib.R"))

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2) stop("usage: Rscript mga.R <request.json> <output.json>")
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

# --- Validate ------------------------------------------------------------------
spec <- parse_spec(req, data)
if (!is.null(spec$error)) fail(spec$error$stage, spec$error$message)
if (length(spec$interactions) > 0 || length(spec$higher_constructs) > 0) {
  fail("validate_spec", paste("multi-group analysis does not support interaction terms",
                              "or higher-order constructs yet — compare groups on the",
                              "lower-order model"))
}
gv <- req$group$variable
if (is.null(gv) || !(gv %in% colnames(data))) {
  fail("validate_spec", paste("grouping variable not found in dataset:", gv %||% "(none)"))
}
va <- as.character(req$group$value_a)
vb <- as.character(req$group$value_b)
gcol <- as.character(data[[gv]])
group_a <- data[!is.na(gcol) & gcol == va, ]
group_b <- data[!is.na(gcol) & gcol == vb, ]
if (identical(va, vb)) fail("validate_spec", "the two group values must differ")
for (g in list(list(v = va, n = nrow(group_a)), list(v = vb, n = nrow(group_b)))) {
  if (g$n < 20) {
    fail("validate_spec", paste0("group ", gv, "=", g$v, " has only ", g$n,
                                 " observations (minimum 20 for a defensible comparison)"))
  }
}

measurement <- build_measurement(spec)
structural  <- build_structural(req)
nperm <- as.integer(req$options$npermutations %||% 1000L)
seed  <- as.integer(req$options$seed %||% 123L)

estimate <- function(d) {
  a <- list(data = d, measurement_model = measurement, structural_model = structural)
  if (!is.null(req$missing_value)) {
    a$missing <- mean_replacement
    a$missing_value <- as.character(req$missing_value)
  }
  do.call(estimate_pls, a)
}
quiet <- function(expr) suppressMessages(suppressWarnings(expr))

m_a <- tryCatch(quiet(estimate(group_a)), error = function(e) fail("estimate", conditionMessage(e)))
m_b <- tryCatch(quiet(estimate(group_b)), error = function(e) fail("estimate", conditionMessage(e)))
pooled <- rbind(group_a, group_b)
m_p <- tryCatch(quiet(estimate(pooled)), error = function(e) fail("estimate", conditionMessage(e)))

k_names <- colnames(m_p$outer_weights)
items <- rownames(m_p$outer_weights)
# Standardized pooled indicators (post missing-value treatment) for scoring with
# either group's weights; correlations are scale-free so unit variance suffices.
Xs <- scale(as.matrix(m_p$data)[, items, drop = FALSE])
score_ref <- scale(m_p$construct_scores[, k_names, drop = FALSE])

# Weight signs are arbitrary per estimation; align every score column against
# the pooled-model score before comparing groups (standard MICOM practice).
scores_with <- function(model) {
  S <- Xs %*% model$outer_weights[items, k_names, drop = FALSE]
  for (k in seq_along(k_names)) {
    if (isTRUE(cor(S[, k], score_ref[, k]) < 0)) S[, k] <- -S[, k]
  }
  S
}
c_between <- function(sa, sb) {
  vapply(seq_along(k_names), function(k) cor(sa[, k], sb[, k]), numeric(1))
}
path_matrix <- function(model) model$path_coef[k_names, k_names, drop = FALSE]

c_obs <- c_between(scores_with(m_a), scores_with(m_b))
paths_a <- path_matrix(m_a)
paths_b <- path_matrix(m_b)
diff_obs <- paths_a - paths_b

# Step 3 observed statistics: pooled-model scores split by group
n_a <- nrow(group_a)
lab <- c(rep(TRUE, n_a), rep(FALSE, nrow(group_b)))
mean_stat <- function(scores, l) colMeans(scores[l, , drop = FALSE]) - colMeans(scores[!l, , drop = FALSE])
var_stat  <- function(scores, l) {
  log(apply(scores[l, , drop = FALSE], 2, var) / apply(scores[!l, , drop = FALSE], 2, var))
}
mean_obs <- mean_stat(score_ref, lab)
var_obs  <- var_stat(score_ref, lab)

# --- Permutation loop: shared draws for MICOM step 2/3 and the path test ------
set.seed(seed)
c_perm <- matrix(NA_real_, nperm, length(k_names))
mean_perm <- matrix(NA_real_, nperm, length(k_names))
var_perm  <- matrix(NA_real_, nperm, length(k_names))
diff_perm <- vector("list", nperm)
for (i in seq_len(nperm)) {
  shuffle <- sample(nrow(pooled))
  pa <- pooled[shuffle[seq_len(n_a)], ]
  pb <- pooled[shuffle[-seq_len(n_a)], ]
  ok <- tryCatch({
    pm_a <- quiet(estimate(pa))
    pm_b <- quiet(estimate(pb))
    c_perm[i, ] <- c_between(scores_with(pm_a), scores_with(pm_b))
    diff_perm[[i]] <- path_matrix(pm_a) - path_matrix(pm_b)
    TRUE
  }, error = function(e) FALSE)
  if (!ok) next
  l <- seq_len(nrow(pooled)) %in% shuffle[seq_len(n_a)]
  mean_perm[i, ] <- mean_stat(score_ref, l)
  var_perm[i, ]  <- var_stat(score_ref, l)
}
eff <- sum(!is.na(c_perm[, 1]))
if (eff < nperm * 0.5) {
  fail("permutation", paste("more than half of the permutation re-estimations failed",
                            sprintf("(%d of %d succeeded)", eff, nperm)))
}

q <- function(x, p) as.numeric(quantile(x, p, na.rm = TRUE))

micom_step2 <- data.frame(
  row = k_names,
  c_value = round(pmin(c_obs, 1), 6),
  c_quantile_5 = vapply(seq_along(k_names), function(k) q(c_perm[, k], 0.05), numeric(1)),
  stringsAsFactors = FALSE
)
micom_step2$invariant <- micom_step2$c_value >= micom_step2$c_quantile_5

micom_step3 <- data.frame(
  row = k_names,
  mean_diff = mean_obs,
  mean_ci_lo = vapply(seq_along(k_names), function(k) q(mean_perm[, k], 0.025), numeric(1)),
  mean_ci_hi = vapply(seq_along(k_names), function(k) q(mean_perm[, k], 0.975), numeric(1)),
  logvar_diff = var_obs,
  var_ci_lo = vapply(seq_along(k_names), function(k) q(var_perm[, k], 0.025), numeric(1)),
  var_ci_hi = vapply(seq_along(k_names), function(k) q(var_perm[, k], 0.975), numeric(1)),
  stringsAsFactors = FALSE
)
micom_step3$mean_equal <- micom_step3$mean_diff >= micom_step3$mean_ci_lo &
  micom_step3$mean_diff <= micom_step3$mean_ci_hi
micom_step3$var_equal <- micom_step3$logvar_diff >= micom_step3$var_ci_lo &
  micom_step3$logvar_diff <= micom_step3$var_ci_hi

path_rows <- lapply(req$paths, function(p) {
  from <- p$from_construct; to <- p$to_construct
  d_obs <- diff_obs[from, to]
  d_perm <- vapply(diff_perm, function(m) if (is.null(m)) NA_real_ else m[from, to], numeric(1))
  n_ge <- sum(abs(d_perm) >= abs(d_obs), na.rm = TRUE)
  data.frame(
    row = paste(from, "->", to),
    est_a = paths_a[from, to],
    est_b = paths_b[from, to],
    diff = d_obs,
    p_value = (1 + n_ge) / (eff + 1),
    stringsAsFactors = FALSE
  )
})

out <- list(
  meta = list(
    schema_version = 1,
    engine = paste0("seminr ", as.character(packageVersion("seminr"))),
    group_variable = gv,
    value_a = va, value_b = vb,
    n_a = n_a, n_b = nrow(group_b),
    n_excluded = nrow(data) - nrow(pooled),
    npermutations = nperm,
    effective_permutations = eff,
    seed = seed,
    timestamp = format(Sys.time(), "%Y-%m-%d %H:%M:%S %Z")
  ),
  micom_step1 = paste("configural invariance established by design: identical model",
                      "specification, data treatment, and algorithm settings in both groups"),
  micom_step2 = micom_step2,
  micom_step3 = micom_step3,
  paths = do.call(rbind, path_rows)
)

write(toJSON(out, dataframe = "rows", digits = 6, pretty = TRUE, na = "null", auto_unbox = TRUE), out_path)
cat("ok\n")
