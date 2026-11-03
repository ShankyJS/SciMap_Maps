import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_daily_terminations as daily


class DailyTests(unittest.TestCase):
    def exercise(self, fail=None, publish=True, historical=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        website = root / 'scimap' / 'scripts'
        website.mkdir(parents=True)
        for name in ('build_termination_release.py', 'publish_termination_release.py', 'select_termination_release.py'):
            (website / name).touch()
        args = argparse.Namespace(as_of='2026-10-26', scimap=website.parent, output=root / 'run',
            notify=True, release_id='test', source_snapshot=None, source_baseline=root,
            county_commute=root / 'county', district_commute=root / 'district', reference_output=root,
            release_python='python', publish=publish, historical=historical)
        calls = []

        def executor(command, log, credentials):
            calls.append((log.stem, credentials))
            if log.stem == fail:
                raise RuntimeError('Institution Example has material loss with zero grants')
            if log.stem == 'science':
                (root / 'run/analysis').mkdir()
                (root / 'run/analysis/run-report.json').write_text(json.dumps({'status': 'passed', 'validation': {'mode': 'enforce', 'status': 'passed', 'warnings': [], 'errors': [], 'checks': []}}))
            if log.stem == 'build':
                (root / 'run/release').mkdir()
                (root / 'run/release/manifest.json').write_text(json.dumps({'release_validation': {'status': 'passed', 'warnings': ['Guam missing geometry']}}))
            if log.stem == 'publish':
                (root / 'run/upload-record.json').write_text('{"status":"published"}')
        with patch.object(daily.alerts, 'webhook_from_env', return_value='test'), patch.object(daily.alerts, 'send') as send:
            if fail:
                with self.assertRaises(RuntimeError):
                    daily.run(args, executor)
                alerts = [call.args[1] for call in send.call_args_list if call.args[1]]
                self.assertIn(f'failed at {fail}', alerts[-1])
                self.assertIn('Institution Example', alerts[-1])
            else:
                daily.run(args, executor)
        report = json.loads((root / 'run/daily-report.json').read_text())
        return calls, report

    def test_success_publishes_then_advances_latest_only(self):
        calls, report = self.exercise()
        self.assertEqual([name for name, _ in calls], ['capture', 'science', 'build', 'publish', 'latest'])
        self.assertEqual([credentials for _, credentials in calls], [False, False, False, True, True])
        self.assertEqual(report['status'], 'passed')
        self.assertFalse(report['website_selection_changed'])

    def test_each_failure_blocks_every_downstream_stage_and_alerts(self):
        sequence = ['capture', 'science', 'build', 'publish', 'latest']
        for index, stage in enumerate(sequence):
            with self.subTest(stage=stage):
                calls, report = self.exercise(fail=stage)
                self.assertEqual([name for name, _ in calls], sequence[:index + 1])
                self.assertEqual(report['failed_stage'], stage)
                self.assertEqual(report['status'], 'failed')

    def test_historical_publication_skips_pointer(self):
        calls, _ = self.exercise(historical=True)
        self.assertEqual(calls[-1][0], 'publish')

    def test_local_default_never_publishes(self):
        calls, _ = self.exercise(publish=False)
        self.assertEqual(calls[-1][0], 'build')

    def test_credentials_only_reach_publishing_children(self):
        with patch.dict(daily.os.environ, {'RCLONE_CONFIG_R2_SECRET_ACCESS_KEY': 'secret', 'AWS_ACCESS_KEY_ID': 'secret', 'SCIMAP_ALERT_WEBHOOK_URL': 'secret'}):
            self.assertNotIn('RCLONE_CONFIG_R2_SECRET_ACCESS_KEY', daily.child_environment())
            self.assertNotIn('AWS_ACCESS_KEY_ID', daily.child_environment())
            self.assertNotIn('SCIMAP_ALERT_WEBHOOK_URL', daily.child_environment(True))
            self.assertIn('RCLONE_CONFIG_R2_SECRET_ACCESS_KEY', daily.child_environment(True))


if __name__ == '__main__':
    unittest.main()
