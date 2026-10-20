args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 7) {
  stop("usage: run_notebook.R NOTEBOOK DATA_DIR NSF_DATA_DIR OUTPUT_DIR COUNTY_COMMUTE DISTRICT_COMMUTE AS_OF")
}

notebook <- normalizePath(args[[1]], mustWork = TRUE)
common <- list(
  data_dir = normalizePath(args[[2]], mustWork = TRUE),
  nsf_data_dir = normalizePath(args[[3]], mustWork = TRUE),
  output_dir = normalizePath(args[[4]], mustWork = TRUE),
  county_commute = normalizePath(args[[5]], mustWork = TRUE),
  district_commute = normalizePath(args[[6]], mustWork = TRUE),
  as_of = args[[7]]
)
params <- switch(
  basename(notebook),
  NIH_terminations.Rmd = common[c("data_dir", "output_dir", "county_commute", "district_commute", "as_of")],
  NSF_terminations.Rmd = common,
  combined_terminations.Rmd = common[c("output_dir", "as_of")],
  stop("Unsupported notebook: ", basename(notebook))
)

rendered <- tempfile(fileext = ".html")
on.exit(unlink(rendered), add = TRUE)
rmarkdown::render(
  notebook,
  output_file = rendered,
  params = params,
  envir = new.env(parent = globalenv()),
  quiet = TRUE
)
