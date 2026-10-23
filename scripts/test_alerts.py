import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).parent))
import alerts  # noqa: E402

PIPELINE_PATH = Path(__file__).with_name("run_termination_pipeline.py")
SPEC = importlib.util.spec_from_file_location("run_termination_pipeline", PIPELINE_PATH)
PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIPELINE)

WEBHOOK = "https://hooks.slack.com/services/T000/B000/secret"
CAPTURE_PATH = Path(__file__).with_name("capture_termination_sources.py")
CAPTURE_SPEC = importlib.util.spec_from_file_location("capture_termination_sources", CAPTURE_PATH)
CAPTURE = importlib.util.module_from_spec(CAPTURE_SPEC)
CAPTURE_SPEC.loader.exec_module(CAPTURE)

CONTEXT = {"as of": "2026-10-19"}


def check(name, status, problems):
    return {"name": name, "status": status, "problem_count": len(problems), "problems": problems}


def validation(errors=(), warnings=()):
    checks = [check(name, "error", [f"{name} problem {i}" for i in range(5)]) for name in errors]
    checks += [check(name, "warning", [f"{name} <detail>"]) for name in warnings]
    checks.append(check("clean", "passed", []))
    return {
        "status": "failed" if errors else "passed",
        "errors": list(errors),
        "warnings": list(warnings),
        "checks": checks,
    }


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Recorder:
    def __init__(self, status=200, error=None):
        self.requests, self.status, self.error = [], status, error

    def __call__(self, request, timeout):
        self.requests.append(json.loads(request.data))
        if self.error:
            raise self.error
        return FakeResponse(self.status)


class AlertTests(unittest.TestCase):
    def test_clean_validation_sends_nothing(self):
        self.assertIsNone(alerts.validation_alert(validation(), CONTEXT, blocked=False))

    def test_blocked_run_lists_errors_and_truncates_problems(self):
        text = alerts.validation_alert(validation(errors=["nsf_institution_series_validity"]), CONTEXT, blocked=True)
        self.assertIn(":rotating_light:", text)
        self.assertIn("blocked: 1 validation error(s)", text)
        self.assertIn("nsf_institution_series_validity problem 2", text)
        self.assertNotIn("problem 3", text)
        self.assertIn("2 more in run-report.json", text)
        self.assertNotIn("clean", text)

    def test_blocked_run_summarizes_warnings_in_one_line(self):
        result = validation(errors=["nsf_series_validity"], warnings=["nih_unmatched_locations", "dictionary_duplicate_keys"])
        text = alerts.validation_alert(result, CONTEXT, blocked=True)
        self.assertIn("+ 2 warning(s): nih_unmatched_locations, dictionary_duplicate_keys", text)
        self.assertNotIn("&lt;detail&gt;", text)

    def test_warnings_alert_and_escape_slack_markup(self):
        text = alerts.validation_alert(validation(warnings=["nih_unmatched_locations"]), CONTEXT, blocked=False)
        self.assertIn(":warning:", text)
        self.assertIn("&lt;detail&gt;", text)

    def test_report_mode_errors_are_not_called_blocked(self):
        text = alerts.validation_alert(validation(errors=["common_reporting_week"]), CONTEXT, blocked=False)
        self.assertIn("report mode", text)
        self.assertNotIn("blocked", text)

    def test_long_messages_are_truncated(self):
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": ""}):
            text = alerts.message("error", "title", CONTEXT, ["x" * 5000])
        self.assertLessEqual(len(text), alerts.MAX_TEXT)
        self.assertTrue(text.endswith("see run-report.json"))

    def test_send_posts_text(self):
        recorder = Recorder()
        self.assertTrue(alerts.send(WEBHOOK, "hello", opener=recorder))
        self.assertEqual(recorder.requests, [{"text": "hello"}])

    def test_send_skips_without_webhook_or_text(self):
        recorder = Recorder()
        self.assertFalse(alerts.send(None, "hello", opener=recorder))
        self.assertFalse(alerts.send(WEBHOOK, None, opener=recorder))
        self.assertEqual(recorder.requests, [])

    def test_delivery_failure_is_reported_without_leaking_url(self):
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertFalse(alerts.send(WEBHOOK, "hello", opener=Recorder(error=URLError(WEBHOOK))))
            self.assertFalse(alerts.send(WEBHOOK, "hello", opener=Recorder(status=500)))
        self.assertIn("not delivered", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())

    def test_any_delivery_error_is_contained_without_leaking_url(self):
        import http.client
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertFalse(alerts.send(WEBHOOK, "hello", opener=Recorder(error=http.client.InvalidURL(WEBHOOK))))
            self.assertFalse(alerts.send(WEBHOOK, "hello", opener=Recorder(error=http.client.BadStatusLine("x"))))
        self.assertIn("InvalidURL", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())

    def test_backticks_cannot_break_out_of_code(self):
        text = alerts.run_failed("Run", RuntimeError("bad ```x``` value"), {"output": "a`b"})
        self.assertEqual(text.count("```"), 2)
        self.assertIn("output: `a'b`", text)

    def test_long_escaped_errors_keep_the_code_block_closed(self):
        text = alerts.run_failed("Run", RuntimeError("&" * 3000), {})
        self.assertTrue(text.rstrip().endswith("```") or "actions/runs" in text)
        self.assertEqual(text.count("```"), 2)
        self.assertNotRegex(text, r"&[a-z]*\s…")

    def test_failure_reasons_are_readable(self):
        self.assertEqual(alerts.failure_reason(SystemExit(2)), "Exited with status 2")
        self.assertIn("Interrupted", alerts.failure_reason(KeyboardInterrupt("SIGTERM")))
        self.assertEqual(alerts.failure_reason(SystemExit("Missing input")), "Missing input")

    def test_notify_requires_slack_webhook(self):
        with mock.patch.dict(os.environ, {alerts.WEBHOOK_ENV: ""}):
            with self.assertRaisesRegex(SystemExit, alerts.WEBHOOK_ENV):
                alerts.webhook_from_env()
        with mock.patch.dict(os.environ, {alerts.WEBHOOK_ENV: WEBHOOK}):
            self.assertEqual(alerts.webhook_from_env(), WEBHOOK)


    def test_ci_runs_link_to_the_run_page(self):
        env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "scienceimpacts/SciMap_Maps",
               "GITHUB_RUN_ID": "42"}
        with mock.patch.dict(os.environ, env):
            text = alerts.message("error", "title", CONTEXT, ["x" * 5000])
        self.assertLessEqual(len(text), alerts.MAX_TEXT)
        self.assertTrue(text.endswith("<https://github.com/scienceimpacts/SciMap_Maps/actions/runs/42|Open the run and its run-report.json>"))
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": ""}):
            self.assertNotIn("actions/runs", alerts.message("error", "title", CONTEXT))


class CaptureAlertTests(unittest.TestCase):
    def test_capture_failure_alerts_and_still_fails(self):
        sent = []
        def fail(args):
            raise RuntimeError("nsf_terminations.csv is missing required columns: estimated_outlays")
        argv = ["capture", "--output", "/tmp/snapshot-x", "--notify"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {alerts.WEBHOOK_ENV: WEBHOOK}), \
                mock.patch.object(CAPTURE, "capture", fail), \
                mock.patch.object(CAPTURE.alerts, "raise_on_sigterm", lambda: None), \
                mock.patch.object(CAPTURE.alerts, "send", lambda webhook, text: sent.append(text)):
            with self.assertRaisesRegex(RuntimeError, "estimated_outlays"):
                CAPTURE.main()
        self.assertEqual(len(sent), 1)
        self.assertIn("Grant Witness capture failed", sent[0])
        self.assertIn("estimated_outlays", sent[0])


class PipelineAlertTests(unittest.TestCase):
    ARGV = ["run", "--as-of", "2026-10-19", "--county-commute", "c", "--district-commute", "d",
            "--reference-output", "r", "--output", "/tmp/run-x", "--notify"]

    def run_main(self, outcome):
        sent = self.sent = []
        with mock.patch.object(sys, "argv", self.ARGV), \
                mock.patch.object(PIPELINE.alerts, "raise_on_sigterm", lambda: None), \
                mock.patch("builtins.print"), \
                mock.patch.dict(os.environ, {alerts.WEBHOOK_ENV: WEBHOOK}), \
                mock.patch.object(PIPELINE, "run_pipeline", outcome), \
                mock.patch.object(PIPELINE.alerts, "send", lambda webhook, text: sent.append(text)):
            try:
                PIPELINE.main()
            except SystemExit as exit_:
                return sent, exit_
        return sent, None

    def test_crash_alerts_and_still_fails(self):
        def crash(args):
            raise RuntimeError("Scientific execution modified inputs")
        with self.assertRaisesRegex(RuntimeError, "modified inputs"):
            self.run_main(crash)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("modified inputs", self.sent[0])

    def test_missing_input_alerts_and_exits(self):
        def crash(args):
            raise SystemExit("Missing required input: x.csv")
        sent, exit_ = self.run_main(crash)
        self.assertIn("Missing required input", str(exit_))
        self.assertEqual(len(sent), 1)
        self.assertIn("Termination run failed", sent[0])

    def test_interrupted_run_alerts_and_still_stops(self):
        def interrupted(args):
            raise KeyboardInterrupt("SIGTERM")
        with self.assertRaises(KeyboardInterrupt):
            self.run_main(interrupted)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Interrupted", self.sent[0])

    def test_validation_failure_alerts_once_then_exits(self):
        report = {"status": "failed", "validation": validation(errors=["common_reporting_week"]),
                  "code": {"revision": "8f06dba0000000000"}}
        sent, exit_ = self.run_main(lambda args: (Path("/tmp/run-x"), report))
        self.assertIn("Validation failed", str(exit_))
        self.assertEqual(len(sent), 1)
        self.assertIn("blocked", sent[0])
        self.assertIn("8f06dba00000", sent[0])

    def test_clean_run_sends_nothing(self):
        report = {"status": "passed", "validation": validation(), "code": {"revision": "abc"}}
        sent, exit_ = self.run_main(lambda args: (Path("/tmp/run-x"), report))
        self.assertIsNone(exit_)
        self.assertEqual(sent, [None])


if __name__ == "__main__":
    unittest.main()
