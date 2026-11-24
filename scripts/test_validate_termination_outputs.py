import csv
from datetime import date
import importlib.util
from pathlib import Path
import tempfile
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
MODULE_PATH = Path(__file__).with_name("validate_termination_outputs.py")
SPEC = importlib.util.spec_from_file_location("validate_termination_outputs", MODULE_PATH)
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

AS_OF = date(2026, 10, 19)
WEEKS = ("2026-10-09", "2026-10-16")
MEASURES = VALIDATOR.GEO_MEASURES
ORG = {"org_name": "Example University", "org_city": "Berkeley", "org_state": "CA", "lat": "37.87", "lon": "-122.26"}


def write(path, rows, fieldnames=None):
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def measures(current, overall):
    return {
        "current_loss": current, "current_econ_loss": current * 2, "current_job_loss": current / 1000,
        "overall_loss": overall, "overall_econ_loss": overall * 2, "overall_job_loss": overall / 1000,
    }


SERIES = {WEEKS[0]: measures(100.0, 100.0), WEEKS[1]: measures(60.0, 150.0)}


def build_fixture(root):
    run, data, source = root / "run", root / "data", root / "source"
    for directory in (run, data, source):
        directory.mkdir()
    write(data / "states.csv", [{"state_name": "California", "state": "CA", "econ_mult": "2", "job_val": "0.001", "state_FIPS": "6"}])
    write(data / "state_and_county_fips_master.csv", [{"FIPS": "6001", "name": "Alameda County", "state": "CA"}])
    write(data / "org_names_corrected.csv", [{"org_name": "A", "corrected_name": "B"}])
    write(data / "org_names_v3_terminations.csv", [{"org_name": "A", "org_city": "X", "org_state": "CA", "org_name_fixed": "B"}])
    write(data / "nsf_names_corrected.csv", [{"org_name": "A", "corrected_name": "B"}])
    write(data / "tract_dictionary.csv", [{"org_name": "EXAMPLE UNIVERSITY", "tractid": "06001400100", "FIPS": "06001", "lat": "37.87", "lon": "-122.26"}])

    nih_columns = VALIDATOR.SOURCE_CONTRACT["nih_v3_terminations.csv"]["columns"]
    write(source / "nih_v3_terminations.csv", [{
        **dict.fromkeys(nih_columns, ""),
        "grant_id": "1", "full_award_number": "1R01", "org_name": "EXAMPLE UNIVERSITY", "org_city": "BERKELEY",
        "org_state": "CA", "org_country": "UNITED STATES", "obligated_at_disruption": "300",
        "spent_at_disruption": "150", "first_termination_date": "2026-10-10",
    }])
    nsf_columns = VALIDATOR.SOURCE_CONTRACT["nsf_terminations.csv"]["columns"]
    write(source / "nsf_terminations.csv", [{
        **dict.fromkeys(nsf_columns, ""),
        "grant_id": "2", "org_name": "Example University", "org_state": "CA",
        "termination_date": "2026-10-10", "estimated_budget": "10", "estimated_outlays": "5", "dir": "BIO",
    }])
    write(run / "nih_terminations_clean.csv", [{
        "grant_id": "1", "full_award_number": "1R01", "subgrant_id": "", "org_name": "Example University",
        "org_state": "CA", "tractid": "06001400100", "current_loss": "150",
    }])

    for suffix in ("", "_nsf"):
        for name, key in (("state", {"state": "CA"}), ("county", {"FIPS": "06001"}),
                          ("district", {"GEOID": "0612"}), ("city", {"CBSA_FIPS": "41860", "CBSA_NAME": "SF"})):
            write(run / f"terminations_{name}{suffix}.csv", [{**key, "week": week, **SERIES[week]} for week in WEEKS])
        write(run / f"terminations_org{suffix}.csv", [
            {**ORG, "week": week, **SERIES[week], "current_grant_loss": 1, "overall_grant_loss": 1} for week in WEEKS
        ])
    write(run / "nsf_terminations_clean.csv", [
        {"grant_id": "2", "date_status": "termination_date", "org_name": "Example University"},
    ])
    combined = []
    for week in WEEKS:
        row = {**ORG, "week": week}
        for name in MEASURES + ("current_grant_loss", "overall_grant_loss"):
            value = SERIES[week].get(name, 1)
            row.update({f"{name}.nih": value, f"{name}.nsf": value, f"{name}.combined": value * 2})
        combined.append(row)
    write(run / "terminations_org_combined.csv", combined)
    return run, data, source


def rewrite(path, change):
    with path.open(newline="") as source:
        rows = list(csv.DictReader(source))
    for row in rows:
        change(row)
    write(path, rows, list(rows[0]))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.run_dir, self.data, self.source = build_fixture(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    def validate(self):
        return VALIDATOR.validate_run(self.run_dir, self.data, self.source, AS_OF)

    def assert_fails(self, check):
        result = self.validate()
        self.assertEqual(result["status"], "failed")
        self.assertIn(check, result["errors"])

    def test_consistent_run_passes(self):
        result = self.validate()
        self.assertEqual(result["status"], "passed", result["errors"])

    def test_negative_current_loss_fails(self):
        def negative(row):
            if row["week"] == WEEKS[0]:
                row["current_loss"] = "-5"
        rewrite(self.run_dir / "terminations_org_nsf.csv", negative)
        self.assert_fails("nsf_institution_series_validity")

    def test_problems_read_naturally(self):
        def negative(row):
            if row["week"] == WEEKS[0]:
                row["current_loss"] = "-2022718.4"
        rewrite(self.run_dir / "terminations_org_nsf.csv", negative)
        check = next(c for c in self.validate()["checks"] if c["name"] == "nsf_institution_series_validity")
        self.assertEqual(
            check["problems"],
            [f"nsf institution Example University (Berkeley, CA): negative current_loss in week {WEEKS[0]}"],
        )
        self.assertEqual(VALIDATOR.amount("current_loss", -2022718.4), "-$2,022,718.40")
        self.assertEqual(VALIDATOR.amount("overall_job_loss", 60491.54), "60,491.54 jobs")
        self.assertEqual(VALIDATOR.amount("current_grant_loss", 3), "3 grants")

    def test_overall_loss_must_not_decrease(self):
        def fall(row):
            if row["week"] == WEEKS[1]:
                row["overall_loss"] = "50"
        rewrite(self.run_dir / "terminations_county.csv", fall)
        self.assert_fails("nih_series_validity")

    def test_geographic_levels_must_reconcile(self):
        rewrite(self.run_dir / "terminations_district.csv", lambda row: row.update(overall_econ_loss=str(float(row["overall_econ_loss"]) + 50)))
        self.assert_fails("nih_geographic_reconciliation")

    def test_allocation_must_conserve_dollars(self):
        rewrite(self.run_dir / "terminations_org.csv", lambda row: row.update(current_loss="10"))
        self.assert_fails("nih_allocation_conserves_dollars")

    def test_combined_must_equal_nih_plus_nsf(self):
        rewrite(self.run_dir / "terminations_org_combined.csv", lambda row: row.update({"overall_loss.combined": "1"}))
        self.assert_fails("combined_institution_reconciliation")

    def test_missing_source_column_fails(self):
        path = self.source / "nih_v3_terminations.csv"
        with path.open(newline="") as source:
            rows = list(csv.DictReader(source))
        write(path, [{k: v for k, v in row.items() if k != "spent_at_disruption"} for row in rows])
        self.assert_fails("source_schema:nih_v3_terminations.csv")

    def test_grant_duplicated_by_dictionary_join_fails(self):
        path = self.run_dir / "nih_terminations_clean.csv"
        with path.open(newline="") as source:
            rows = list(csv.DictReader(source))
        write(path, rows + [{**rows[0], "tractid": "06001400200"}])
        self.assert_fails("grant_rows_unique_after_joins")

    def test_nsf_grant_duplicated_by_address_join_fails(self):
        path = self.run_dir / "nsf_terminations_clean.csv"
        with path.open(newline="") as source:
            rows = list(csv.DictReader(source))
        write(path, rows + rows)
        self.assert_fails("nsf_grant_rows_unique_after_joins")
        check = next(c for c in self.validate()["checks"] if c["name"] == "nsf_grant_rows_unique_after_joins")
        self.assertEqual(check["problems"], ["NSF grant 2 (Example University) termination appears 2 times after joins"])

    def test_missing_per_grant_rows_fail(self):
        (self.run_dir / "nsf_terminations_clean.csv").unlink()
        self.assert_fails("nsf_grant_rows_unique_after_joins")

    def test_non_numeric_institution_value_is_reported(self):
        rewrite(self.run_dir / "terminations_org.csv", lambda row: row.update(current_loss="NA"))
        self.assert_fails("institution_identity")

    def test_missing_spent_amount_counts_as_zero_loss(self):
        rewrite(self.source / "nih_v3_terminations.csv", lambda row: row.update(spent_at_disruption="NA", grant_id="9"))
        check = next(c for c in self.validate()["checks"] if c["name"] == "nih_unmatched_locations")
        self.assertEqual(check["unmatched_loss"], 0.0)

    def test_drift_without_common_week_is_reported(self):
        reference = Path(self.directory.name) / "reference"
        reference.mkdir()
        for suffix in ("", "_nsf"):
            write(reference / f"terminations_state{suffix}.csv", [{"state": "CA", "week": "2020-01-03", **SERIES[WEEKS[0]]}])
        report = VALIDATOR.Report()
        geo = {"nih": {WEEKS[0]: SERIES[WEEKS[0]]}, "nsf": {WEEKS[0]: SERIES[WEEKS[0]]}}
        VALIDATOR.check_drift(report, self.run_dir, reference, geo)
        self.assertIn("nih: no week in common with the approved run", report.get("drift_from_reference")["problems"])

    def test_stale_reporting_week_fails(self):
        result = VALIDATOR.validate_run(self.run_dir, self.data, self.source, date(2026, 10, 26))
        self.assertIn("common_reporting_week", result["errors"])

    def test_unmatched_location_is_reported_with_dollars(self):
        write(self.run_dir / "nih_terminations_clean.csv", [{
            "grant_id": "9", "full_award_number": "other", "subgrant_id": "", "org_name": "Other",
            "org_state": "CA", "tractid": "06001400100", "current_loss": "0",
        }])
        result = self.validate()
        check = next(c for c in result["checks"] if c["name"] == "nih_unmatched_locations")
        self.assertEqual(check["unmatched_grants"], 1)
        self.assertEqual(check["unmatched_loss"], 150.0)
        self.assertEqual(check["status"], "error")  # 100% unmatched exceeds the provisional limit

    def copy_sources(self, target):
        target.mkdir(exist_ok=True)
        for name in ("nih_v3_terminations.csv", "nsf_terminations.csv"):
            with (self.source / name).open(newline="") as source:
                write(target / name, list(csv.DictReader(source)))

    def test_added_source_column_warns_and_alerts(self):
        import alerts
        baseline = Path(self.directory.name) / "approved-snapshot"
        self.copy_sources(baseline)
        rewrite(self.source / "nih_v3_terminations.csv", lambda row: row.update(new_column="x"))
        result = VALIDATOR.validate_run(self.run_dir, self.data, self.source, AS_OF, self.run_dir, baseline)
        self.assertEqual(result["status"], "passed", result["errors"])
        self.assertEqual(result["warnings"], ["source_schema_drift:nih_v3_terminations.csv"])
        text = alerts.validation_alert(result, {}, blocked=False)
        self.assertIn("NIH Grant Witness file: columns changed", text)
        self.assertIn("column added new_column", text)

    def test_repository_copies_are_not_a_column_baseline(self):
        self.copy_sources(self.data)
        rewrite(self.source / "nih_v3_terminations.csv", lambda row: row.update(new_column="x"))
        result = VALIDATOR.validate_run(self.run_dir, self.data, self.source, AS_OF, self.run_dir)
        self.assertEqual(result["warnings"], [])

    def multiplier_case(self, gap):
        write(self.data / "states.csv", [
            {"state_name": "Maryland", "state": "MD", "econ_mult": "2.5", "job_val": "0.002", "state_FIPS": "24"},
            {"state_name": "District of Columbia", "state": "DC", "econ_mult": "2", "job_val": "0.001", "state_FIPS": "11"},
        ])
        report = VALIDATOR.Report()
        report.add("nih_institution_multiplier_consistency", "warning", ["institutions vs geography"])
        report.add("nih_institution_tract_state_conflicts", "warning", ["Clinic: institution MD vs tract DC"],
                   relocated=[{"org_name": "Clinic", "org_state": "MD", "tract_state": "DC"}], partial=0)
        orgs = {(("Clinic", "Washington", "MD"), WEEKS[0]): {"current_loss": 100.0, "overall_loss": 200.0}}
        # MD minus DC: econ +0.5 and jobs +0.001 per dollar.
        gaps = {WEEKS[0]: {"current_econ_loss": 50.0 + gap, "current_job_loss": 0.1,
                           "overall_econ_loss": 100.0, "overall_job_loss": 0.2}}
        VALIDATOR.explain_multiplier_differences(report, orgs, gaps, self.data)
        return report

    def test_state_conflict_explains_multiplier_difference(self):
        report = self.multiplier_case(gap=0)
        self.assertEqual(report.result()["warnings"], ["nih_institution_tract_state_conflicts"])
        self.assertEqual(report.get("nih_institution_multiplier_consistency")["status"], "info")

    def test_multiplier_gap_beyond_state_conflicts_still_warns(self):
        report = self.multiplier_case(gap=500)
        check = report.get("nih_institution_multiplier_consistency")
        self.assertEqual(check["status"], "warning")
        self.assertEqual(check["problems"], [f"nih {WEEKS[0]} current_econ_loss: $500.00 beyond the state conflicts"])

    def test_partial_state_conflict_is_not_reconciled(self):
        report = self.multiplier_case(gap=0)
        report.get("nih_institution_multiplier_consistency")["status"] = "warning"
        report.get("nih_institution_tract_state_conflicts")["partial"] = 1
        VALIDATOR.explain_multiplier_differences(report, {}, {}, self.data)
        self.assertEqual(report.get("nih_institution_multiplier_consistency")["status"], "warning")

    def test_missing_source_baseline_fails(self):
        with self.assertRaisesRegex(RuntimeError, "Source baseline is missing nih_v3_terminations.csv"):
            VALIDATOR.validate_run(self.run_dir, self.data, self.source, AS_OF, self.run_dir, Path(self.directory.name) / "typo")

    def test_checks_have_plain_titles(self):
        for check in self.validate()["checks"]:
            self.assertNotIn("_", check["title"], check["name"])

    def test_territory_county_is_accepted_by_state_prefix(self):
        write(self.data / "states.csv", [
            {"state_name": "California", "state": "CA", "econ_mult": "2", "job_val": "0.001", "state_FIPS": "6"},
            {"state_name": "Puerto Rico", "state": "PR", "econ_mult": "2", "job_val": "0.001", "state_FIPS": "72"},
        ])
        rewrite(self.run_dir / "terminations_county.csv", lambda row: row.update(FIPS="72127"))
        self.assertNotIn("nih_geographic_identifiers", self.validate()["errors"])


if __name__ == "__main__":
    unittest.main()
