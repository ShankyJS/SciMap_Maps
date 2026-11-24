args <- commandArgs(trailingOnly = TRUE)
lock_path <- if (length(args)) args[[1]] else "r-packages.lock.csv"
lib <- Sys.getenv("R_LIBS_USER", unset = "")
if (!nzchar(lib)) stop("R_LIBS_USER is not set")
dir.create(lib, recursive = TRUE, showWarnings = FALSE)
.libPaths(c(lib, .libPaths()))

lock <- read.csv(lock_path, colClasses = "character")
installed <- tryCatch(installed.packages(lib.loc = lib), error = function(...) NULL)
have <- if (is.null(installed)) character() else setNames(installed[, "Version"], rownames(installed))
wrong <- is.na(have[lock$Package]) | have[lock$Package] != lock$Version
if (!any(wrong)) {
  cat("R library already matches lock (", nrow(lock), " packages)\n", sep = "")
  quit(status = 0)
}
if (!requireNamespace("pak", quietly = TRUE)) {
  install.packages("pak", repos = "https://r-lib.github.io/p/pak/stable/")
}
specs <- paste0(lock$Package[wrong], "@", lock$Version[wrong])
pak::pkg_install(specs, lib = lib)
