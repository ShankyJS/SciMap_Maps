args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2) stop("usage: check_r_environment.R R_VERSION_FILE PACKAGE_LOCK")

expected_r <- trimws(readLines(args[[1]], warn = FALSE)[[1]])
actual_r <- paste(R.version$major, R.version$minor, sep = ".")
if (actual_r != expected_r) stop("R version mismatch: expected ", expected_r, ", found ", actual_r)

lock <- read.csv(args[[2]], colClasses = "character")
installed <- installed.packages()
missing <- setdiff(lock$Package, rownames(installed))
if (length(missing) > 0) stop("Missing R packages: ", paste(missing, collapse = ", "))

actual <- installed[lock$Package, "Version"]
mismatch <- lock$Version != actual
if (any(mismatch)) {
  details <- paste0(lock$Package[mismatch], " expected ", lock$Version[mismatch], " found ", actual[mismatch])
  stop("R package version mismatch: ", paste(details, collapse = "; "))
}

cat("R environment matches lock (", nrow(lock), " packages)\n", sep = "")
