import csv
import importlib.util
from pathlib import Path
import tempfile
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
MODULE_PATH = Path(__file__).with_name("run_termination_pipeline.py")
SPEC = importlib.util.spec_from_file_location("run_termination_pipeline", MODULE_PATH)
PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIPELINE)


class ComparisonTests(unittest.TestCase):
    def write_csv(self, path, rows):
        with path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)

    def test_combines_duplicate_reference_rows_before_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            expected = directory / "expected.csv"
            actual = directory / "actual.csv"
            self.write_csv(expected, [
                {"org_name": "Example", "week": "2026-09-25", "lat": "1", "lon": "2", "current_loss.nih": "3", "current_loss.nsf": "0"},
                {"org_name": "Example", "week": "2026-09-25", "lat": "4", "lon": "5", "current_loss.nih": "0", "current_loss.nsf": "7"},
            ])
            self.write_csv(actual, [
                {"org_name": "Example", "week": "2026-09-25", "lat": "1", "lon": "2", "current_loss.nih": "3", "current_loss.nsf": "7"},
            ])

            result = PIPELINE.compare_csv(actual, expected, ("org_name", "week"), 0.01, 1e-9)

            self.assertEqual(result["keys"], 1)
            self.assertEqual(result["coordinate_keys_changed"], 1)
            self.assertEqual(result["max_absolute_difference"], 0)

    def test_rejects_scientific_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            expected = directory / "expected.csv"
            actual = directory / "actual.csv"
            self.write_csv(expected, [{"state": "MD", "week": "2026-09-25", "current_loss": "1"}])
            self.write_csv(actual, [{"state": "MD", "week": "2026-09-25", "current_loss": "2"}])

            with self.assertRaisesRegex(RuntimeError, "differs"):
                PIPELINE.compare_csv(actual, expected, ("state", "week"), 0.01, 1e-9)

    def test_report_mode_records_expected_new_data_differences(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            expected = directory / "expected.csv"
            actual = directory / "actual.csv"
            self.write_csv(expected, [{"state": "MD", "week": "2026-09-25", "current_loss": "1"}])
            self.write_csv(actual, [
                {"state": "MD", "week": "2026-09-25", "current_loss": "2"},
                {"state": "VA", "week": "2026-10-16", "current_loss": "3"},
            ])

            result = PIPELINE.compare_csv(
                actual, expected, ("state", "week"), 0.01, 1e-9, mode="report"
            )

            self.assertEqual(result["keys_added"], 1)
            self.assertEqual(result["changed_values"], 1)

    def test_rejects_duplicate_generated_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            expected = directory / "expected.csv"
            actual = directory / "actual.csv"
            row = {"state": "MD", "week": "2026-09-25", "current_loss": "1"}
            self.write_csv(expected, [row])
            self.write_csv(actual, [row, row])

            with self.assertRaisesRegex(RuntimeError, "Duplicate key"):
                PIPELINE.compare_csv(actual, expected, ("state", "week"), 0.01, 1e-9)

    def test_rejects_non_finite_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "values.csv"
            self.write_csv(path, [{"state": "MD", "week": "2026-09-25", "current_loss": "NaN"}])

            with self.assertRaisesRegex(RuntimeError, "Non-finite"):
                PIPELINE.grouped_values(path, ("state", "week"))


if __name__ == "__main__":
    unittest.main()
