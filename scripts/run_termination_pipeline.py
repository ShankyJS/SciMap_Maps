#!/usr/bin/env python3
"""Run the saved-input NIH/NSF termination analysis and reconcile its outputs."""

import argparse
import csv
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
DATA_FILES = (
    "nih_v3_terminations.csv",
    "nsf_terminations.csv",
    "org_names_corrected.csv",
    "org_names_v3_terminations.csv",
    "nsf_names_corrected.csv",
    "tract_dictionary.csv",
    "org_dict.csv",
    "states.csv",
    "us_representatives_119th_congress.csv",
    "cbsa2fipsxw_2023.csv",
    "state_and_county_fips_master.csv",
)
NSF_DATA_FILES = ("nsf_grants.csv", "NSF_addresses_US_tract_county_cong119.csv")
OUTPUT_SCHEMAS = {
    "terminations_state.csv": ("state", "week"),
    "terminations_county.csv": ("FIPS", "week"),
    "terminations_district.csv": ("GEOID", "week"),
    "terminations_city.csv": ("CBSA_FIPS", "CBSA_NAME", "week"),
    "terminations_org.csv": ("org_name", "org_city", "org_state", "week"),
    "terminations_state_nsf.csv": ("state", "week"),
    "terminations_county_nsf.csv": ("FIPS", "week"),
    "terminations_district_nsf.csv": ("GEOID", "week"),
    "terminations_city_nsf.csv": ("CBSA_FIPS", "CBSA_NAME", "week"),
    "terminations_org_nsf.csv": ("org_name", "org_city", "org_state", "week"),
    "terminations_org_combined.csv": ("org_name", "org_city", "org_state", "week"),
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_files(directory, names):
    files = []
    for name in names:
        path = directory / name
        if not path.is_file():
            raise SystemExit(f"Missing required input: {path}")
        files.append(path)
    return files


def run(command):
    print("Running:", " ".join(map(str, command)), flush=True)
    subprocess.run([str(part) for part in command], check=True, cwd=ROOT)


def git_state():
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return {"revision": revision, "dirty": bool(status.strip())}


def file_record(path):
    return {"bytes": path.stat().st_size, "sha256": sha256(path)}


def validate_external_inputs(paths):
    with (ROOT / "external-inputs.lock.csv").open(newline="") as source:
        expected = {row["Input"]: row for row in csv.DictReader(source)}
    for name, path in paths.items():
        record = file_record(path)
        lock = expected.get(name)
        if lock is None:
            raise RuntimeError(f"No checksum lock for external input: {name}")
        if record != {"bytes": int(lock["Bytes"]), "sha256": lock["SHA256"]}:
            raise RuntimeError(f"External input does not match approved checksum: {path}")


def numeric_columns(fieldnames):
    return tuple(name for name in fieldnames if "loss" in name)


def grouped_values(path, keys):
    grouped = {}
    coordinates = {}
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise RuntimeError(f"Missing CSV header: {path}")
        missing = set(keys) - set(reader.fieldnames)
        if missing:
            raise RuntimeError(f"{path} is missing keys: {', '.join(sorted(missing))}")
        measures = numeric_columns(reader.fieldnames)
        if not measures:
            raise RuntimeError(f"No loss measures found in {path}")
        for line, row in enumerate(reader, 2):
            key = tuple(row[name] for name in keys)
            values = grouped.setdefault(key, {name: 0.0 for name in measures})
            for name in measures:
                try:
                    value = float(row[name])
                except ValueError as error:
                    raise RuntimeError(f"Invalid {name} at {path}:{line}: {row[name]!r}") from error
                if not math.isfinite(value):
                    raise RuntimeError(f"Non-finite {name} at {path}:{line}")
                values[name] += value
            if "lat" in row and "lon" in row:
                coordinates.setdefault(key, set()).add((row["lat"], row["lon"]))
    return grouped, coordinates, measures


def compare_csv(actual_path, expected_path, keys, absolute_tolerance, relative_tolerance):
    actual, actual_coords, actual_measures = grouped_values(actual_path, keys)
    expected, expected_coords, expected_measures = grouped_values(expected_path, keys)
    if set(actual_measures) != set(expected_measures):
        raise RuntimeError(f"Measure columns differ for {actual_path.name}")
    if set(actual) != set(expected):
        missing = len(set(expected) - set(actual))
        extra = len(set(actual) - set(expected))
        raise RuntimeError(f"Keys differ for {actual_path.name}: {missing} missing, {extra} extra")

    max_difference = 0.0
    for key in actual:
        for measure in actual_measures:
            left, right = actual[key][measure], expected[key][measure]
            difference = abs(left - right)
            max_difference = max(max_difference, difference)
            if not math.isclose(left, right, abs_tol=absolute_tolerance, rel_tol=relative_tolerance):
                raise RuntimeError(
                    f"{actual_path.name} differs at {key} {measure}: generated {left}, expected {right}"
                )

    coordinate_changes = sum(
        actual_coords.get(key, set()) != expected_coords.get(key, set())
        for key in set(actual_coords) | set(expected_coords)
    )
    return {
        "keys": len(actual),
        "measures": len(actual_measures),
        "max_absolute_difference": max_difference,
        "coordinate_keys_changed": coordinate_changes,
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="ISO cutoff date")
    parser.add_argument("--county-commute", required=True)
    parser.add_argument("--district-commute", required=True)
    parser.add_argument("--reference-output", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--data-dir", default=ROOT / "data")
    parser.add_argument("--nsf-data-dir", default=ROOT / "nsf_data")
    parser.add_argument("--rscript", default="Rscript")
    parser.add_argument("--absolute-tolerance", type=float, default=0.01)
    parser.add_argument("--relative-tolerance", type=float, default=1e-9)
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        date.fromisoformat(args.as_of)
    except ValueError as error:
        raise SystemExit("--as-of must be an ISO date such as 2026-09-25") from error

    data_dir = Path(args.data_dir).resolve()
    nsf_data_dir = Path(args.nsf_data_dir).resolve()
    county_commute = Path(args.county_commute).resolve()
    district_commute = Path(args.district_commute).resolve()
    reference_output = Path(args.reference_output).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        raise SystemExit(f"Refusing to overwrite output: {output}")

    source_inputs = checked_files(data_dir, DATA_FILES)
    source_inputs += checked_files(nsf_data_dir, NSF_DATA_FILES)
    reference_files = checked_files(reference_output, OUTPUT_SCHEMAS)
    external_inputs = {
        "county_commute_NIH.csv": county_commute,
        "home_119_workTR.csv": district_commute,
    }
    for path in external_inputs.values():
        if not path.is_file():
            raise SystemExit(f"Missing required input: {path}")
        source_inputs.append(path)
    validate_external_inputs(external_inputs)
    source_records = {str(path): file_record(path) for path in source_inputs}
    reference_records = {str(path): file_record(path) for path in reference_files}

    run([args.rscript, ROOT / "scripts/check_r_environment.R", ROOT / "r-version.txt", ROOT / "r-packages.lock.csv"])

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="termination-run-", dir=output.parent) as temporary:
        generated = Path(temporary) / "output"
        generated.mkdir()
        notebook_args = [data_dir, nsf_data_dir, generated, county_commute, district_commute, args.as_of]
        for notebook in ("NIH_terminations.Rmd", "NSF_terminations.Rmd", "combined_terminations.Rmd"):
            run([args.rscript, ROOT / "scripts/run_notebook.R", ROOT / notebook, *notebook_args])

        changed_inputs = [
            path for path in source_inputs if file_record(path) != source_records[str(path)]
        ]
        if changed_inputs:
            raise RuntimeError("Scientific execution modified inputs: " + ", ".join(map(str, changed_inputs)))

        comparisons = {}
        for filename, keys in OUTPUT_SCHEMAS.items():
            comparisons[filename] = compare_csv(
                generated / filename,
                reference_output / filename,
                keys,
                args.absolute_tolerance,
                args.relative_tolerance,
            )

        report = {
            "status": "passed",
            "as_of": args.as_of,
            "code": git_state(),
            "inputs": source_records,
            "comparison": {
                "reference_output": str(reference_output),
                "reference_files": reference_records,
                "absolute_tolerance": args.absolute_tolerance,
                "relative_tolerance": args.relative_tolerance,
                "files": comparisons,
            },
        }
        (generated / "run-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        shutil.move(generated, output)

    print(f"Reproducible termination run created: {output}")


if __name__ == "__main__":
    main()
