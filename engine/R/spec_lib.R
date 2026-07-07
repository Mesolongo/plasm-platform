# Shared model-spec handling for the engine scripts (estimate.R, mga.R):
# parse + validate a request's constructs/interactions/paths against the data,
# and build the seminr measurement/structural models from the parsed spec.

`%||%` <- function(a, b) if (is.null(a)) b else a
is_hoc <- function(c) grepl("^higher_order", c$measurement %||% "")

# Returns list(error = list(stage, message)) on failure, else the parsed parts.
parse_spec <- function(req, data) {
  err <- function(message) list(error = list(stage = "validate_spec", message = message))

  lower_constructs  <- Filter(function(c) !is_hoc(c), req$constructs)
  higher_constructs <- Filter(is_hoc, req$constructs)
  interactions <- req$interactions %||% list()

  all_indicators <- unlist(lapply(lower_constructs, function(c) unlist(c$indicators)))
  missing_cols <- setdiff(all_indicators, colnames(data))
  if (length(missing_cols) > 0) {
    return(err(paste("indicators not found in dataset:",
                     paste(missing_cols, collapse = ", "))))
  }
  dup <- all_indicators[duplicated(all_indicators)]
  if (length(dup) > 0) {
    return(err(paste("indicator assigned to multiple constructs:",
                     paste(unique(dup), collapse = ", "))))
  }
  lower_names <- vapply(lower_constructs, function(c) c$name, character(1))
  for (h in higher_constructs) {
    dims <- unlist(h$dimensions)
    if (length(dims) < 2) {
      return(err(paste("higher-order construct", h$name, "needs at least 2 dimensions")))
    }
    bad <- setdiff(dims, lower_names)
    if (length(bad) > 0) {
      return(err(paste("higher-order construct", h$name,
                       "references unknown dimensions:", paste(bad, collapse = ", "))))
    }
  }
  interaction_names <- character(0)
  for (ix in interactions) {
    if (!(ix$iv %in% lower_names) || !(ix$moderator %in% lower_names)) {
      return(err(paste("interaction references unknown construct:",
                       ix$iv, "x", ix$moderator)))
    }
    interaction_names <- c(interaction_names, paste0(ix$iv, "*", ix$moderator))
  }
  construct_names <- c(lower_names,
                       vapply(higher_constructs, function(c) c$name, character(1)),
                       interaction_names)
  for (p in req$paths) {
    if (!(p$from_construct %in% construct_names) || !(p$to_construct %in% construct_names)) {
      return(err(paste("path references unknown construct:",
                       p$from_construct, "->", p$to_construct)))
    }
  }
  list(lower_constructs = lower_constructs, higher_constructs = higher_constructs,
       interactions = interactions, interaction_names = interaction_names,
       lower_names = lower_names, construct_names = construct_names)
}

build_measurement <- function(spec) {
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
  do.call(constructs, c(lapply(spec$lower_constructs, make_composite),
                        lapply(spec$higher_constructs, make_hoc),
                        lapply(spec$interactions, make_interaction)))
}

build_structural <- function(req) {
  do.call(relationships, lapply(req$paths, function(p) {
    paths(from = p$from_construct, to = p$to_construct)
  }))
}
