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

import alerts
from validate_termination_outputs import validate_run

ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_FILES = ("nih_v3_terminations.csv", "nsf_terminations.csv")
REFERENCE_DATA_FILES = (
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


def validate_source_snapshot(directory):
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Missing source snapshot manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("files", {})
    for name in RAW_DATA_FILES:
        path = directory / name
        if not path.is_file() or name not in files:
            raise RuntimeError(f"Incomplete source snapshot: {name}")
        expected = files[name]
        if file_record(path) != {"bytes": expected["bytes"], "sha256": expected["sha256"]}:
            raise RuntimeError(f"Source snapshot checksum mismatch: {path}")
    return manifest_path


def numeric_columns(fieldnames):
    return tuple(name for name in fieldnames if "loss" in name)


def grouped_values(path, keys, require_unique=False):
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
            if require_unique and key in grouped:
                raise RuntimeError(f"Duplicate key at {path}:{line}: {key}")
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


def compare_csv(
    actual_path, expected_path, keys, absolute_tolerance, relative_tolerance, mode="exact"
):
    actual, actual_coords, actual_measures = grouped_values(actual_path, keys, require_unique=True)
    expected, expected_coords, expected_measures = grouped_values(expected_path, keys)
    if set(actual_measures) != set(expected_measures):
        raise RuntimeError(f"Measure columns differ for {actual_path.name}")

    actual_keys, expected_keys = set(actual), set(expected)
    added, removed = actual_keys - expected_keys, expected_keys - actual_keys
    if mode == "exact" and (added or removed):
        raise RuntimeError(
            f"Keys differ for {actual_path.name}: {len(removed)} missing, {len(added)} extra"
        )

    max_difference = 0.0
    changed_values = 0
    for key in actual_keys & expected_keys:
        for measure in actual_measures:
            left, right = actual[key][measure], expected[key][measure]
            difference = abs(left - right)
            max_difference = max(max_difference, difference)
            if not math.isclose(left, right, abs_tol=absolute_tolerance, rel_tol=relative_tolerance):
                changed_values += 1
                if mode == "exact":
                    raise RuntimeError(
                        f"{actual_path.name} differs at {key} {measure}: generated {left}, expected {right}"
                    )

    coordinate_changes = sum(
        actual_coords.get(key, set()) != expected_coords.get(key, set())
        for key in set(actual_coords) | set(expected_coords)
    )
    return {
        "keys": len(actual),
        "keys_added": len(added),
        "keys_removed": len(removed),
        "measures": len(actual_measures),
        "changed_values": changed_values,
        "max_absolute_difference": max_difference,
        "coordinate_keys_changed": coordinate_changes,
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="ISO cutoff date")
    parser.add_argument("--county-commute", required=True)
    parser.add_argument("--district-commute", required=True)
    parser.add_argument("--reference-output", required=True)
    parser.add_argument(
        "--comparison-mode",
        choices=("exact", "report"),
        default="exact",
        help="Use report for a validated new-data candidate whose values should differ",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-snapshot", help="Immutable Grant Witness snapshot directory")
    parser.add_argument(
        "--source-baseline",
        help="Last approved raw Grant Witness snapshot; Grant Witness column changes are reported against it",
    )
    parser.add_argument("--data-dir", default=ROOT / "data")
    parser.add_argument("--nsf-data-dir", default=ROOT / "nsf_data")
    parser.add_argument("--rscript", default="Rscript")
    parser.add_argument(
        "--validation",
        choices=("enforce", "report"),
        default="enforce",
        help="enforce fails the run on scientific/geographic validation errors; report only records them",
    )
    parser.add_argument("--absolute-tolerance", type=float, default=0.01)
    parser.add_argument("--relative-tolerance", type=float, default=1e-9)
    parser.add_argument(
        "--notify",
        action="store_true",
        help=f"post failures and validation warnings to Slack via ${alerts.WEBHOOK_ENV}",
    )
    return parser.parse_args()


def run_pipeline(args):
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

    reference_inputs = checked_files(data_dir, REFERENCE_DATA_FILES)
    source_snapshot = Path(args.source_snapshot).resolve() if args.source_snapshot else data_dir
    raw_inputs = checked_files(source_snapshot, RAW_DATA_FILES)
    snapshot_manifest = validate_source_snapshot(source_snapshot) if args.source_snapshot else None
    nsf_inputs = checked_files(nsf_data_dir, NSF_DATA_FILES)
    source_inputs = reference_inputs + raw_inputs + nsf_inputs
    if snapshot_manifest:
        source_inputs.append(snapshot_manifest)
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
    source_records = {
        **{f"data/{path.name}": file_record(path) for path in reference_inputs},
        **{f"source-snapshot/{path.name}": file_record(path) for path in raw_inputs},
        **{f"nsf_data/{path.name}": file_record(path) for path in nsf_inputs},
        **{f"external/{name}": file_record(path) for name, path in external_inputs.items()},
    }
    if snapshot_manifest:
        source_records["source-snapshot/manifest.json"] = file_record(snapshot_manifest)
    if args.source_baseline:
        baseline_inputs = checked_files(Path(args.source_baseline).resolve(), RAW_DATA_FILES)
        source_records.update({f"source-baseline/{path.name}": file_record(path) for path in baseline_inputs})
    source_hashes_by_path = {path: file_record(path) for path in source_inputs}
    reference_records = {path.name: file_record(path) for path in reference_files}

    run([args.rscript, ROOT / "scripts/check_r_environment.R", ROOT / "r-version.txt", ROOT / "r-packages.lock.csv"])

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="termination-run-", dir=output.parent) as temporary:
        generated = Path(temporary) / "output"
        generated.mkdir()
        execution_data = data_dir
        if args.source_snapshot:
            execution_data = Path(temporary) / "data"
            execution_data.mkdir()
            for path in checked_files(data_dir, REFERENCE_DATA_FILES):
                (execution_data / path.name).symlink_to(path)
            for path in checked_files(source_snapshot, RAW_DATA_FILES):
                (execution_data / path.name).symlink_to(path)
        notebook_args = [execution_data, nsf_data_dir, generated, county_commute, district_commute, args.as_of]
        for notebook in ("NIH_terminations.Rmd", "NSF_terminations.Rmd", "combined_terminations.Rmd"):
            run([args.rscript, ROOT / "scripts/run_notebook.R", ROOT / notebook, *notebook_args])

        changed_inputs = [
            path for path in source_inputs if file_record(path) != source_hashes_by_path[path]
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
                args.comparison_mode,
            )

        validation = validate_run(
            generated, data_dir, source_snapshot, date.fromisoformat(args.as_of), reference_output,
            args.source_baseline and Path(args.source_baseline).resolve(),
        )
        failed = args.validation == "enforce" and validation["status"] != "passed"
        report = {
            "status": "failed" if failed else "passed",
            "validation": {"mode": args.validation, **validation},
            "as_of": args.as_of,
            "code": git_state(),
            "inputs": source_records,
            "comparison": {
                "mode": args.comparison_mode,
                "reference_output": "approved-output-fixture",
                "reference_files": reference_records,
                "absolute_tolerance": args.absolute_tolerance,
                "relative_tolerance": args.relative_tolerance,
                "files": comparisons,
            },
        }
        (generated / "run-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        shutil.move(generated, output)
    return output, report


def main():
    args = parse_args()
    webhook = alerts.webhook_from_env() if args.notify else None
    alerts.raise_on_sigterm()
    context = {"as of": args.as_of, "output": Path(args.output).name}
    try:
        output, report = run_pipeline(args)
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        alerts.send(webhook, alerts.run_failed("Termination run", error, context))
        raise
    validation = report["validation"]
    context["revision"] = report["code"].get("revision", "")[:12]
    alerts.send(webhook, alerts.validation_alert(validation, context, blocked=report["status"] == "failed"))
    if report["status"] == "failed":
        raise SystemExit(
            f"Validation failed ({', '.join(validation['errors'])}); see {output / 'run-report.json'}"
        )
    print(f"Reproducible termination run created: {output}")


if __name__ == "__main__":
    main()
