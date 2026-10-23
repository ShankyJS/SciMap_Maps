import csv
import importlib.util
from pathlib import Path
import tempfile
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
MODULE_PATH = Path(__file__).with_name("capture_termination_sources.py")
SPEC = importlib.util.spec_from_file_location("capture_termination_sources", MODULE_PATH)
CAPTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPTURE)


class CaptureTests(unittest.TestCase):
    def test_validates_required_columns_and_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.csv"
            with path.open("w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=("grant_id", "org_name"))
                writer.writeheader()
                writer.writerow({"grant_id": "1", "org_name": "Example"})
            self.assertEqual(CAPTURE.validate_csv(path, {"grant_id", "org_name"}), 1)

    def test_rejects_schema_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.csv"
            path.write_text("grant_id\n1\n")
            with self.assertRaisesRegex(RuntimeError, "missing required columns"):
                CAPTURE.validate_csv(path, {"grant_id", "org_name"})


if __name__ == "__main__":
    unittest.main()
