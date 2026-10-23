#!/usr/bin/env python3
"""Scientific and geographic validation of a termination run (T07).

Errors fail the run and block publication. Warnings and information are
recorded in the run report for review; thresholds marked provisional await
team sign-off (Q04).
"""

import argparse
from collections import defaultdict
import csv
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import sys

csv.field_size_limit(sys.maxsize)

START_DATE = date(2025, 2, 28)
GEO_MEASURES = (
    "current_loss",
    "current_econ_loss",
    "current_job_loss",
    "overall_loss",
    "overall_econ_loss",
    "overall_job_loss",
)
BASE_MEASURES = ("current_loss", "overall_loss")
MULTIPLIER_MEASURES = tuple(name for name in GEO_MEASURES if name not in BASE_MEASURES)
AGENCIES = {"nih": "", "nsf": "_nsf"}
LEVEL_KEYS = {
    "state": ("state",),
    "county": ("FIPS",),
    "district": ("GEOID",),
    "city": ("CBSA_FIPS",),
}
ORG_KEY = ("org_name", "org_city", "org_state")
SOURCE_CONTRACT = {
    "nih_v3_terminations.csv": {
        "columns": (
            "grant_id", "subgrant_id", "full_award_number", "org_name", "org_city",
            "org_state", "org_country", "obligated_at_disruption", "spent_at_disruption",
            "first_termination_date", "latest_termination_date",
            "latest_reinstatement_date", "frozen_date", "unfrozen_date",
        ),
        "numeric": ("obligated_at_disruption", "spent_at_disruption"),
        "events": (
            "first_termination_date", "latest_termination_date",
            "latest_reinstatement_date", "frozen_date", "unfrozen_date",
        ),
    },
    "nsf_terminations.csv": {
        "columns": (
            "grant_id", "org_name", "org_city", "org_state", "termination_date",
            "reinstatement_date", "estimated_budget", "estimated_outlays", "dir", "div",
        ),
        "numeric": ("estimated_budget", "estimated_outlays"),
        "events": ("termination_date", "reinstatement_date"),
    },
}
DICTIONARY_KEYS = {
    "org_names_corrected.csv": ("org_name",),
    "org_names_v3_terminations.csv": ("org_name", "org_city", "org_state"),
    "nsf_names_corrected.csv": ("org_name",),
    "tract_dictionary.csv": ("org_name",),
    "states.csv": ("state",),
}
MISSING = {"", "NA", "NaN", "nan"}
TERRITORY_FIPS = {"60", "66", "69", "72", "78"}
# Provisional thresholds (Q04): above these a warning becomes an error.
UNMATCHED_SHARE_LIMIT = 0.01
TOTAL_DRIFT_LIMIT = 0.10
LIST_LIMIT = 20


def reporting_week(as_of):
    return as_of - timedelta(days=(as_of - START_DATE).days % 7)


def rows(path):
    with path.open(newline="") as source:
        yield from csv.DictReader(source)


def header(path):
    with path.open(newline="") as source:
        return next(csv.reader(source))


def number(value):
    return 0.0 if value in MISSING else float(value)


def parse_date(value):
    return None if value in MISSING else date.fromisoformat(value[:10])


def close(left, right, absolute=1.0, relative=1e-9):
    return math.isclose(left, right, abs_tol=absolute, rel_tol=relative)


AGENCY = {"nih": "NIH", "nsf": "NSF", "combined": "Combined"}
SOURCE = {"nih_v3_terminations.csv": "NIH", "nsf_terminations.csv": "NSF"}
TITLES = {
    "source_schema": "{} Grant Witness file: required columns or values invalid",
    "source_schema_drift": "{} Grant Witness file: columns changed",
    "source_freshness": "Grant Witness freshness",
    "dictionary_duplicate_keys": "Dictionaries: duplicate names (unused)",
    "grant_rows_unique_after_joins": "NIH grants counted more than once by the location lookup",
    "nsf_grant_rows_unique_after_joins": "NSF grants counted more than once by the institution or address lookup",
    "common_reporting_week": "Reporting week",
    "output_structure": "{} outputs: malformed rows",
    "series_validity": "{} maps: negative, incomplete or falling weekly losses",
    "geographic_reconciliation": "{} maps: state, county, district and city totals disagree",
    "geographic_identifiers": "{} maps: unknown places",
    "institution_series_validity": "{} institutions: negative, incomplete or falling weekly losses",
    "allocation_conserves_dollars": "{} institutions: dollars differ from the maps",
    "institution_multiplier_consistency": "{} institutions: economic impact differs from the maps",
    "institution_identity": "Institutions: missing names or coordinates",
    "combined_institution_reconciliation": "Combined institutions: not equal to NIH + NSF",
    "institution_coordinate_conflicts": "Institutions: NIH and NSF coordinates differ",
    "institution_name_collisions": "Institutions: similar names",
    "new_institutions": "Institutions: new since the approved run",
    "unmatched_locations": "{} grants without a map location",
    "institution_tract_state_conflicts": "{} institutions: listed state differs from the mapped location",
    "drift_from_reference": "Totals changed more than expected since the approved run",
}


def title(name):
    base, _, source = name.partition(":")
    if source:
        return TITLES.get(base, name).format(SOURCE.get(source, source))
    agency, _, rest = name.partition("_")
    if agency in AGENCY and rest in TITLES:
        return TITLES[rest].format(AGENCY[agency])
    return TITLES.get(name, name).format("")


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, severity, problems, **details):
        status = "passed" if not problems else severity
        self.checks.append({
            "name": name,
            "title": title(name),
            "status": status,
            "problems": problems[:LIST_LIMIT],
            "problem_count": len(problems),
            **details,
        })

    def get(self, name):
        return next((check for check in self.checks if check["name"] == name), None)

    def result(self):
        failed = [check["name"] for check in self.checks if check["status"] == "error"]
        warned = [check["name"] for check in self.checks if check["status"] == "warning"]
        return {
            "status": "failed" if failed else "passed",
            "errors": failed,
            "warnings": warned,
            "checks": self.checks,
        }


def load_level(path, keys):
    """{(key, week): {measure: value}} with structural checks."""
    values, problems = {}, []
    for line, row in enumerate(rows(path), 2):
        key = tuple(row[name] for name in keys)
        measures = {}
        for name in GEO_MEASURES:
            try:
                measures[name] = float(row[name])
            except (KeyError, ValueError):
                problems.append(f"{path.name}:{line} invalid {name}")
                continue
            if not math.isfinite(measures[name]):
                problems.append(f"{path.name}:{line} non-finite {name}")
        if (key, row["week"]) in values:
            problems.append(f"{path.name}:{line} duplicate {label(key)} {row['week']}")
        values[(key, row["week"])] = measures
    return values, problems


def week_totals(values):
    totals = defaultdict(lambda: dict.fromkeys(GEO_MEASURES, 0.0))
    for (_, week), measures in values.items():
        for name in GEO_MEASURES:
            totals[week][name] += measures[name]
    return totals


def label(key):
    """Readable identifier: '06013', or 'Name (City, ST)' for institutions."""
    key = tuple(key) if isinstance(key, tuple) else (key,)
    if len(key) == 3:
        return f"{key[0]} ({key[1]}, {key[2]})"
    return " / ".join(str(part) for part in key)


def amount(measure, value):
    if "grant" in measure:
        return f"{value:,.0f} grants"
    if "job" in measure:
        return f"{value:,.2f} jobs"
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def series_checks(name, values, weeks):
    """Complete weekly series, non-negative values, current <= overall, overall never falls."""
    problems = []
    by_key = defaultdict(dict)
    for (key, week), measures in values.items():
        by_key[key][week] = measures
    for key, series in by_key.items():
        if set(series) != weeks:
            problems.append(f"{name} {label(key)}: {len(series)} of {len(weeks)} weeks")
            continue
        previous = None
        for week in sorted(series):
            measures = series[week]
            negative = [m for m, v in measures.items() if v < -1e-6]
            if negative:
                problems.append(f"{name} {label(key)} {week}: negative {', '.join(negative)} ({amount(negative[0], measures[negative[0]])})")
            if measures["current_loss"] > measures["overall_loss"] + 1e-6:
                problems.append(f"{name} {label(key)} {week}: current_loss exceeds overall_loss")
            if previous and any(
                measures[m] < previous[m] - 1e-6 for m in ("overall_loss", "overall_econ_loss", "overall_job_loss")
            ):
                problems.append(f"{name} {label(key)} {week}: overall loss decreased")
            previous = measures
    return problems, len(by_key)


def collapse(problems):
    """Group per-week series problems by key so one bad series reads as one finding."""
    grouped = defaultdict(list)
    for problem in problems:
        head, _, tail = problem.partition(": ")
        parts = head.rsplit(" ", 1)
        if len(parts) == 2 and len(parts[1]) == 10 and parts[1][4] == "-":
            grouped[(parts[0], tail.split(" (")[0])].append(parts[1])
        else:
            grouped[(head, tail)].append(None)
    collapsed = []
    for (head, tail), weeks in grouped.items():
        weeks = sorted(week for week in weeks if week)
        if len(weeks) == 1:
            span = f" in week {weeks[0]}"
        else:
            span = f" in {len(weeks)} weeks {weeks[0]}..{weeks[-1]}" if weeks else ""
        collapsed.append(f"{head}: {tail}{span}")
    return collapsed


def content_digest(path, contract):
    """Digest of the required columns only, insensitive to quoting and float formatting."""
    def normalized(column, value):
        if value in MISSING:
            return None
        if column in contract["numeric"]:
            return round(float(value), 6)
        if column.endswith("date"):
            return parse_date(value).isoformat()
        return value

    digest = hashlib.sha256()
    for row in rows(path):
        digest.update(json.dumps([normalized(c, row[c]) for c in contract["columns"]]).encode())
    return digest.hexdigest()


def check_sources(report, source_dir, reference_dir, as_of, source_baseline=None):
    """Column changes are compared with an approved raw snapshot (source_baseline) only:
    the repository copies under data/ were rewritten by R and differ in harmless columns."""
    summaries = {}
    for filename, contract in SOURCE_CONTRACT.items():
        path = source_dir / filename
        columns = header(path)
        missing = [name for name in contract["columns"] if name not in columns]
        problems = [f"{filename}: missing required column {name}" for name in missing]
        latest_event, future_events, count = None, 0, 0
        if not missing:
            for line, row in enumerate(rows(path), 2):
                count += 1
                for name in contract["numeric"]:
                    try:
                        if not math.isfinite(number(row[name])):
                            raise ValueError
                    except ValueError:
                        problems.append(f"{filename}:{line} {name} is not numeric: {row[name]!r}")
                for name in (c for c in columns if c.endswith("date")):
                    try:
                        parsed = parse_date(row[name])
                    except ValueError:
                        problems.append(f"{filename}:{line} {name} is not an ISO date: {row[name]!r}")
                        continue
                    if parsed and name in contract["events"]:
                        if parsed > as_of:
                            future_events += 1
                        elif latest_event is None or parsed > latest_event:
                            latest_event = parsed
        baseline = source_baseline / filename if source_baseline else None
        if baseline and not baseline.is_file():
            raise RuntimeError(f"Source baseline is missing {filename}: {source_baseline}")
        previous = baseline or (reference_dir / filename if reference_dir else None)
        drift = {}
        if baseline and baseline.is_file():
            old = header(baseline)
            drift = {
                "columns_added": [c for c in columns if c not in old],
                "columns_removed": [c for c in old if c not in columns],
            }
        if previous and previous.is_file():
            old = header(previous)
            if not problems and all(c in old for c in contract["columns"]):
                drift["required_data_changed"] = (
                    content_digest(path, contract) != content_digest(previous, contract)
                )
        added, removed = drift.get("columns_added", []), drift.get("columns_removed", [])
        report.add(
            f"source_schema_drift:{filename}", "warning",
            [f"{filename}: column added {c or '(unnamed)'}" for c in added]
            + [f"{filename}: column removed {c or '(unnamed)'}" for c in removed],
        )
        summaries[filename] = {
            "schema_valid": not problems,
            "rows": count,
            "latest_event_on_or_before_as_of": latest_event.isoformat() if latest_event else None,
            "events_after_as_of": future_events,
            **drift,
        }
        report.add(f"source_schema:{filename}", "error", problems)
    report.add("source_freshness", "info", [], sources={
        name: {k: v for k, v in summary.items() if k != "schema_valid"}
        for name, summary in summaries.items()
    })
    return summaries


def check_dictionaries(report, data_dir, clean_path):
    warnings = []
    for filename, keys in DICTIONARY_KEYS.items():
        seen = {}
        for row in rows(data_dir / filename):
            key = tuple(row[name] for name in keys)
            values = tuple(sorted(row.items()))
            if key in seen and seen[key] != values:
                warnings.append(f"{filename}: {label(key).strip() or '(blank name)'} maps to more than one entry")
            seen.setdefault(key, values)
    # Informational: a duplicate only matters when a grant uses it, which is an error below.
    report.add("dictionary_duplicate_keys", "info", sorted(set(warnings)))

    # A duplicate join key only multiplies funding when a grant actually uses it.
    if clean_path.is_file():
        counts = defaultdict(int)
        for row in rows(clean_path):
            counts[(row["grant_id"], row["full_award_number"], row["subgrant_id"])] += 1
        duplicated = [f"NIH grant {key[1] or key[0]} appears {n} times after joins" for key, n in counts.items() if n > 1]
    else:
        duplicated = [f"missing {clean_path.name}"]
    report.add("grant_rows_unique_after_joins", "error", duplicated)

    # NSF rows are one per grant event (termination or reinstatement) after the address join.
    nsf_path = clean_path.with_name("nsf_terminations_clean.csv")
    if nsf_path.is_file():
        counts, names = defaultdict(int), {}
        for row in rows(nsf_path):
            key = (row["grant_id"], row["date_status"])
            counts[key] += 1
            names[key] = row["org_name"]
        duplicated = sorted(
            f"NSF grant {grant} ({names[(grant, status)]}) {status.replace('_date', '')} appears {n} times after joins"
            for (grant, status), n in counts.items() if n > 1
        )
    else:
        duplicated = [f"missing {nsf_path.name}"]
    report.add("nsf_grant_rows_unique_after_joins", "error", duplicated)


def check_geography(report, run_dir, data_dir, as_of):
    expected_week = reporting_week(as_of).isoformat()
    states = {row["state"] for row in rows(data_dir / "states.csv")}
    counties = {row["FIPS"].zfill(5) for row in rows(data_dir / "state_and_county_fips_master.csv")}
    state_fips = {row["state_FIPS"].zfill(2) for row in rows(data_dir / "states.csv")}
    totals_by_agency, all_weeks = {}, {}

    for agency, suffix in AGENCIES.items():
        levels, structure = {}, []
        for level, keys in LEVEL_KEYS.items():
            values, problems = load_level(run_dir / f"terminations_{level}{suffix}.csv", keys)
            levels[level] = values
            structure += problems
        report.add(f"{agency}_output_structure", "error", structure)

        weeks = {week for _, week in levels["state"]}
        all_weeks[agency] = weeks
        series, key_counts = [], {}
        for level, values in levels.items():
            problems, key_counts[level] = series_checks(f"{agency} {level}", values, weeks)
            series += problems
        report.add(f"{agency}_series_validity", "error", collapse(series), keys=key_counts)

        totals = {level: week_totals(values) for level, values in levels.items()}
        mismatches = []
        for level in ("county", "district", "city"):
            for week in sorted(weeks):
                for name in GEO_MEASURES:
                    left, right = totals[level][week][name], totals["state"][week][name]
                    if not close(left, right):
                        mismatches.append(f"{agency} {level} {week} {name}: {amount(name, left)} vs state {amount(name, right)}")
        report.add(f"{agency}_geographic_reconciliation", "error", mismatches)
        totals_by_agency[agency] = totals["state"]

        bad_ids = []
        for (key, _), _ in levels["state"].items():
            if key[0] not in states:
                bad_ids.append(f"{agency} unknown state {key[0]}")
        for (key, _), _ in levels["county"].items():
            # The county master list has no territories; their municipios/islands are checked by state prefix only.
            known = key[0] in counties or key[0][:2] in TERRITORY_FIPS
            if len(key[0]) != 5 or key[0].endswith("000") or key[0][:2] not in state_fips or not known:
                bad_ids.append(f"{agency} unknown county FIPS {key[0]}")
        for (key, _), _ in levels["district"].items():
            if len(key[0]) != 4 or key[0][:2] not in state_fips:
                bad_ids.append(f"{agency} malformed district GEOID {key[0]}")
        report.add(f"{agency}_geographic_identifiers", "error", sorted(set(bad_ids)))

    week_problems = []
    for name in sorted(run_dir.glob("terminations_*.csv")):
        weeks = {row["week"] for row in rows(name)}
        if max(weeks) != expected_week:
            week_problems.append(f"{name.name}: latest week {max(weeks)}, expected {expected_week}")
        if weeks != all_weeks["nih"]:
            week_problems.append(f"{name.name}: week set differs from NIH state output")
    report.add("common_reporting_week", "error", week_problems, reporting_week=expected_week, weeks=len(all_weeks["nih"]))
    return totals_by_agency


def load_orgs(path, suffix=""):
    values, coordinates, problems = {}, defaultdict(set), []
    for line, row in enumerate(rows(path), 2):
        identity = tuple(row[name] for name in ORG_KEY)
        if (identity, row["week"]) in values:
            problems.append(f"{path.name}:{line} duplicate institution/week {label(identity)} {row['week']}")
        try:
            values[(identity, row["week"])] = {
                name: float(row[name + suffix]) for name in GEO_MEASURES + ("current_grant_loss", "overall_grant_loss")
            }
        except ValueError:
            problems.append(f"{path.name}:{line} non-numeric value for {label(identity)} {row['week']}")
            continue
        coordinates[identity].add((round(float(row["lat"]), 5), round(float(row["lon"]), 5)))
    for identity, points in coordinates.items():
        if len(points) > 1:
            problems.append(f"{path.name}: {label(identity)} has {len(points)} coordinates")
    return values, coordinates, problems


def check_institutions(report, run_dir, data_dir, reference_dir, geo_totals):
    """Returns institution values and institution-minus-geography multiplier gaps by agency and week."""
    orgs, identity_problems, coordinates, gaps = {}, [], {}, {}
    for agency, suffix in AGENCIES.items():
        values, points, problems = load_orgs(run_dir / f"terminations_org{suffix}.csv")
        orgs[agency], coordinates[agency] = values, points
        identity_problems += problems
        series, _ = series_checks(f"{agency} institution", values, {week for _, week in values})
        report.add(f"{agency}_institution_series_validity", "error", collapse(series))

        totals = week_totals(values)
        gaps[agency] = {
            week: {name: measures[name] - geo_totals[agency][week][name] for name in MULTIPLIER_MEASURES}
            for week, measures in totals.items()
        }
        conservation, multiplier = [], []
        for week, measures in sorted(totals.items()):
            for name in GEO_MEASURES:
                left, right = measures[name], geo_totals[agency][week][name]
                if not close(left, right):
                    target = conservation if name in BASE_MEASURES else multiplier
                    target.append(f"{agency} {week} {name}: institutions {amount(name, left)} vs geography {amount(name, right)}")
        # Base dollars must survive commuter allocation exactly.
        report.add(f"{agency}_allocation_conserves_dollars", "error", conservation)
        report.add(f"{agency}_institution_multiplier_consistency", "warning", multiplier[-LIST_LIMIT:])

    combined, combined_points, problems = {}, defaultdict(set), []
    for line, row in enumerate(rows(run_dir / "terminations_org_combined.csv"), 2):
        identity = tuple(row[name] for name in ORG_KEY)
        if (identity, row["week"]) in combined:
            problems.append(f"terminations_org_combined.csv:{line} duplicate {label(identity)} {row['week']}")
        combined[(identity, row["week"])] = row
        combined_points[identity].add((round(float(row["lat"]), 5), round(float(row["lon"]), 5)))
    identity_problems += problems

    reconciliation = []
    zero = dict.fromkeys(GEO_MEASURES + ("current_grant_loss", "overall_grant_loss"), 0.0)
    for key in set(combined) | set(orgs["nih"]) | set(orgs["nsf"]):
        row = combined.get(key)
        if row is None:
            reconciliation.append(f"{label(key[0])} {key[1]}: missing from combined output")
            continue
        for name in zero:
            nih = orgs["nih"].get(key, zero)[name]
            nsf = orgs["nsf"].get(key, zero)[name]
            parts = (float(row[f"{name}.nih"]), float(row[f"{name}.nsf"]), float(row[f"{name}.combined"]))
            if not (close(parts[0], nih, 0.01) and close(parts[1], nsf, 0.01) and close(parts[2], nih + nsf, 0.01)):
                reconciliation.append(f"{label(key[0])} {key[1]} {name}: combined {amount(name, parts[2])} vs NIH {amount(name, nih)} + NSF {amount(name, nsf)}")
                break
    report.add("institution_identity", "error", identity_problems)
    report.add("combined_institution_reconciliation", "error", reconciliation)

    conflicts = []
    for identity in set(coordinates["nih"]) & set(coordinates["nsf"]):
        if coordinates["nih"][identity] != coordinates["nsf"][identity]:
            conflicts.append(f"{label(identity)}: NIH {sorted(coordinates['nih'][identity])} vs NSF {sorted(coordinates['nsf'][identity])}")
    names = defaultdict(set)
    for identity in combined_points:
        names[identity[0]].add(identity[1:])
    collisions = [f"{name}: {len(places)} identities {sorted(places)[:3]}" for name, places in names.items() if len(places) > 1]
    report.add("institution_coordinate_conflicts", "warning", conflicts)
    report.add("institution_name_collisions", "info", sorted(collisions))

    if reference_dir:
        known = {tuple(row[name] for name in ORG_KEY) for row in rows(reference_dir / "terminations_org_combined.csv")}
        new = sorted(identity for identity in combined_points if identity not in known)
        report.add("new_institutions", "info", [" | ".join(identity) for identity in new], count=len(new))
    return orgs, gaps


def check_nih_locations(report, source_dir, data_dir, clean_path, as_of):
    """Eligible NIH grants dropped for lack of a tract, and institution/tract state conflicts."""
    if not clean_path.is_file():
        return
    states = {row["state"]: row for row in rows(data_dir / "states.csv")}
    by_fips = {row["state_FIPS"].zfill(2): row["state"] for row in states.values()}
    raw = [
        row for row in rows(source_dir / "nih_v3_terminations.csv")
        if row["org_state"] in states and row["org_country"] == "UNITED STATES"
    ]
    # The notebook drops a subgrant only when its parent award survives the state/country filter.
    with_parent = {row["grant_id"] for row in raw if row["subgrant_id"] in MISSING}
    eligible, seen = {}, set()
    for row in raw:
        key = (row["grant_id"], row["full_award_number"], row["subgrant_id"] if row["subgrant_id"] not in MISSING else "")
        if key in seen:
            continue
        seen.add(key)
        if row["subgrant_id"] not in MISSING and row["grant_id"] in with_parent:
            continue
        impacts = [d for d in (parse_date(row["first_termination_date"]), parse_date(row["frozen_date"])) if d]
        if not impacts or min(impacts) < START_DATE:
            continue
        amounts = (row["obligated_at_disruption"], row["spent_at_disruption"])
        # As in the notebook, a missing amount makes the loss NA, counted as zero.
        loss = 0.0 if any(value in MISSING for value in amounts) else max(number(amounts[0]) - number(amounts[1]), 0.0)
        eligible[key] = (row["org_name"], row["org_state"], loss)

    matched, conflicts, institution_loss = set(), defaultdict(float), defaultdict(float)
    for row in rows(clean_path):
        matched.add((row["grant_id"], row["full_award_number"], row["subgrant_id"] if row["subgrant_id"] not in MISSING else ""))
        institution_loss[(row["org_name"], row["org_state"])] += number(row["current_loss"])
        tract_state = by_fips.get(row["tractid"][:2].zfill(2)) if row["tractid"] not in MISSING else None
        if tract_state and tract_state != row["org_state"]:
            conflicts[(row["org_name"], row["org_state"], tract_state)] += number(row["current_loss"])

    unmatched = {key: value for key, value in eligible.items() if key not in matched}
    total = sum(loss for _, _, loss in eligible.values()) or 1.0
    unmatched_loss = sum(loss for _, _, loss in unmatched.values())
    share = unmatched_loss / total
    listed = sorted(unmatched.values(), key=lambda item: -item[2])
    report.add(
        "nih_unmatched_locations",
        "error" if share > UNMATCHED_SHARE_LIMIT else "warning",
        [f"{name} ({state}): ${loss:,.2f}" for name, state, loss in listed],
        eligible_grants=len(eligible),
        unmatched_grants=len(unmatched),
        unmatched_loss=round(unmatched_loss, 2),
        unmatched_share=round(share, 6),
        provisional_limit=UNMATCHED_SHARE_LIMIT,
    )
    report.add(
        "nih_institution_tract_state_conflicts",
        "warning",
        [f"{name}: institution {org} vs tract {tract} (${loss:,.2f} at disruption)" for (name, org, tract), loss in sorted(conflicts.items())],
        # Whole institutions mapped to another state; partial conflicts cannot be reconciled from institution totals.
        relocated=[
            {"org_name": name, "org_state": org, "tract_state": tract}
            for (name, org, tract), loss in sorted(conflicts.items())
            if close(loss, institution_loss[(name, org)], 0.01)
        ],
        partial=len([1 for (name, org, _), loss in conflicts.items() if not close(loss, institution_loss[(name, org)], 0.01)]),
    )


def check_drift(report, run_dir, reference_dir, geo_totals):
    if not reference_dir:
        return
    drift, large = {}, []
    for agency, suffix in AGENCIES.items():
        previous = week_totals(load_level(reference_dir / f"terminations_state{suffix}.csv", ("state",))[0])
        overlap = set(previous) & set(geo_totals[agency])
        if not overlap:
            large.append(f"{agency}: no week in common with the approved run")
            continue
        common = max(overlap)
        drift[agency] = {"week": common}
        for name in GEO_MEASURES:
            old, new = previous[common][name], geo_totals[agency][common][name]
            change = (new - old) / old if old else 0.0
            drift[agency][name] = round(change, 6)
            if abs(change) > TOTAL_DRIFT_LIMIT:
                large.append(f"{agency} {name} changed {change:+.1%} at {common}")
    report.add("drift_from_reference", "warning", large, totals=drift, provisional_limit=TOTAL_DRIFT_LIMIT)


def validate_run(run_dir, data_dir, source_dir, as_of, reference_dir=None, source_baseline=None):
    run_dir, data_dir, source_dir = Path(run_dir), Path(data_dir), Path(source_dir)
    reference_dir = Path(reference_dir) if reference_dir else None
    source_baseline = Path(source_baseline) if source_baseline else None
    clean_path = run_dir / "nih_terminations_clean.csv"
    report = Report()
    sources = check_sources(report, source_dir, reference_dir and data_dir, as_of, source_baseline)
    check_dictionaries(report, data_dir, clean_path)
    geo_totals = check_geography(report, run_dir, data_dir, as_of)
    orgs, gaps = check_institutions(report, run_dir, data_dir, reference_dir, geo_totals)
    if sources["nih_v3_terminations.csv"]["schema_valid"]:
        check_nih_locations(report, source_dir, data_dir, clean_path, as_of)
    check_drift(report, run_dir, reference_dir, geo_totals)
    explain_multiplier_differences(report, orgs["nih"], gaps["nih"], data_dir)
    return report.result()


def explain_multiplier_differences(report, nih_orgs, nih_gaps, data_dir):
    """Institutions use the listed state's multipliers and maps use the tract state's. When every
    week's institution-minus-map gap equals what the state conflicts imply, report the difference
    once, as the conflict; any remainder stays a warning."""
    conflicts = report.get("nih_institution_tract_state_conflicts")
    multiplier = report.get("nih_institution_multiplier_consistency")
    if not (conflicts and multiplier and conflicts["status"] == "warning" and multiplier["status"] == "warning"):
        return
    if conflicts["partial"]:
        multiplier["explanation"] = "not reconciled: some institutions are only partly mapped to another state"
        return
    states = {row["state"]: row for row in rows(data_dir / "states.csv")}
    relocated = {(item["org_name"], item["org_state"]): item["tract_state"] for item in conflicts["relocated"]}
    expected = defaultdict(lambda: dict.fromkeys(MULTIPLIER_MEASURES, 0.0))
    for (identity, week), measures in nih_orgs.items():
        tract_state = relocated.get((identity[0], identity[2]))
        if not tract_state:
            continue
        for name in MULTIPLIER_MEASURES:
            factor = "econ_mult" if "econ" in name else "job_val"
            base = "current_loss" if name.startswith("current") else "overall_loss"
            difference = float(states[identity[2]][factor]) - float(states[tract_state][factor])
            expected[week][name] += measures[base] * difference
    unexplained = [
        f"nih {week} {name}: {amount(name, gap - expected[week][name])} beyond the state conflicts"
        for week, week_gaps in sorted(nih_gaps.items())
        for name, gap in week_gaps.items()
        if not close(gap, expected[week][name])
    ]
    if unexplained:
        multiplier["problems"], multiplier["problem_count"] = unexplained[-LIST_LIMIT:], len(unexplained)
        multiplier["explanation"] = "partly explained by state conflicts"
        return
    multiplier["status"] = "info"
    multiplier["explained_by"] = conflicts["name"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--data-dir", default=Path(__file__).resolve().parent.parent / "data")
    parser.add_argument("--source-dir", help="Raw Grant Witness files (default: --data-dir)")
    parser.add_argument("--reference-output", help="Approved output directory for drift and new institutions")
    parser.add_argument("--source-baseline", help="Approved raw Grant Witness snapshot for column changes")
    parser.add_argument("--report", help="Write the validation report as JSON")
    args = parser.parse_args()
    result = validate_run(
        args.run_dir, args.data_dir, args.source_dir or args.data_dir,
        date.fromisoformat(args.as_of), args.reference_output, args.source_baseline,
    )
    if args.report:
        Path(args.report).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    for check in result["checks"]:
        if check["status"] != "passed":
            print(f"[{check['status']}] {check['title']} ({check['name']}): {check['problem_count']} problem(s)")
            for problem in check["problems"][:5]:
                print(f"    {problem}")
    print(f"Validation {result['status']}: {len(result['errors'])} error(s), {len(result['warnings'])} warning(s)")
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
