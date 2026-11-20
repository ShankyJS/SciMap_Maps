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
        commands = []

        def executor(command, log, credentials):
            calls.append((log.stem, credentials))
            commands.append((log.stem, list(map(str, command))))
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
        publish_env = {}
        if publish:
            publish_env = {
                'R2_ACCESS_KEY_ID': 'test-access',
                'R2_SECRET_ACCESS_KEY': 'test-secret',
                'R2_ENDPOINT': 'https://example.invalid',
            }
        with patch.dict(daily.os.environ, publish_env), patch.object(daily.alerts, 'webhook_from_env', return_value='test'), patch.object(daily.alerts, 'send') as send:
            if fail:
                with self.assertRaises(RuntimeError):
                    daily.run(args, executor)
                alerts = [call.args[1] for call in send.call_args_list if call.args[1]]
                self.assertIn(f'failed at {fail}', alerts[-1])
                self.assertIn('Institution Example', alerts[-1])
            else:
                daily.run(args, executor)
        report = json.loads((root / 'run/daily-report.json').read_text())
        return calls, commands, report

    def run_with(self, executor, publish_env, publish=True):
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
            release_python='python', publish=publish, historical=False)
        with patch.dict(daily.os.environ, publish_env), patch.object(daily.alerts, 'webhook_from_env', return_value='test'), patch.object(daily.alerts, 'send') as send:
            with self.assertRaises(RuntimeError):
                daily.run(args, executor)
            message = [call.args[1] for call in send.call_args_list if call.args[1]][-1]
        report = json.loads((root / 'run/daily-report.json').read_text())
        self.assertFalse((root / 'run/upload-record.json').exists())
        self.assertFalse((root / 'run/selection-record.json').exists())
        return report, message

    def test_success_publishes_then_advances_latest_only(self):
        calls, commands, report = self.exercise()
        self.assertEqual([name for name, _ in calls], ['capture', 'science', 'build', 'publish', 'latest'])
        self.assertEqual([credentials for _, credentials in calls], [False, False, False, True, True])
        self.assertEqual(report['status'], 'passed')
        self.assertFalse(report['website_selection_changed'])
        by_stage = dict(commands)
        publish = by_stage['publish']
        self.assertTrue(publish[1].endswith('publish_termination_release.py'))
        self.assertEqual(publish[3:5], ['--record', publish[4]])
        self.assertTrue(publish[4].endswith('upload-record.json'))
        latest = by_stage['latest']
        self.assertTrue(latest[1].endswith('select_termination_release.py'))
        self.assertEqual(latest[2:6], ['--upload-record', publish[4], '--pointer', 'latest-validated'])
        self.assertEqual(latest[6:8], ['--apply', '--record'])
        self.assertTrue(latest[8].endswith('selection-record.json'))

    def test_each_failure_blocks_every_downstream_stage_and_alerts(self):
        sequence = ['capture', 'science', 'build', 'publish', 'latest']
        for index, stage in enumerate(sequence):
            with self.subTest(stage=stage):
                calls, _, report = self.exercise(fail=stage)
                self.assertEqual([name for name, _ in calls], sequence[:index + 1])
                self.assertEqual(report['failed_stage'], stage)
                self.assertEqual(report['status'], 'failed')
                self.assertFalse(report['website_selection_changed'])

    def test_historical_publication_skips_pointer(self):
        calls, _, _ = self.exercise(historical=True)
        self.assertEqual(calls[-1][0], 'publish')

    def test_local_default_never_publishes(self):
        calls, _, _ = self.exercise(publish=False)
        self.assertEqual(calls[-1][0], 'build')

    def test_credentials_only_reach_publishing_children(self):
        with patch.dict(daily.os.environ, {
            'RCLONE_CONFIG_R2_SECRET_ACCESS_KEY': 'inbound-rclone',
            'AWS_ACCESS_KEY_ID': 'inbound-aws',
            'AWS_SECRET_ACCESS_KEY': 'inbound-aws-secret',
            'R2_ACCESS_KEY_ID': 'access',
            'R2_SECRET_ACCESS_KEY': 'secret',
            'R2_ENDPOINT': 'https://example.r2.cloudflarestorage.com',
            'SCIMAP_ALERT_WEBHOOK_URL': 'https://hooks.slack.com/services/example',
        }):
            plain = daily.child_environment()
            publishing = daily.child_environment(True)
        for name in ('R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_ENDPOINT',
                     'AWS_ACCESS_KEY_ID', 'RCLONE_CONFIG_R2_SECRET_ACCESS_KEY', 'SCIMAP_ALERT_WEBHOOK_URL'):
            self.assertNotIn(name, plain)
        self.assertNotIn('SCIMAP_ALERT_WEBHOOK_URL', publishing)
        self.assertNotIn('AWS_ACCESS_KEY_ID', publishing)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', publishing)
        self.assertEqual(publishing['R2_ACCESS_KEY_ID'], 'access')
        self.assertEqual(publishing['R2_SECRET_ACCESS_KEY'], 'secret')
        self.assertEqual(publishing['R2_ENDPOINT'], 'https://example.r2.cloudflarestorage.com')
        self.assertEqual(publishing['RCLONE_CONFIG_R2_SECRET_ACCESS_KEY'], 'secret')
        self.assertEqual(publishing['RCLONE_CONFIG_R2_ACCESS_KEY_ID'], 'access')
        self.assertEqual(publishing['RCLONE_CONFIG_R2_TYPE'], 's3')
        self.assertEqual(publishing['RCLONE_CONFIG_R2_PROVIDER'], 'Cloudflare')
        self.assertEqual(publishing['RCLONE_CONFIG_R2_ENDPOINT'], 'https://example.r2.cloudflarestorage.com')
        self.assertEqual(publishing['RCLONE_CONFIG_R2_NO_CHECK_BUCKET'], 'true')

    def test_account_id_can_supply_the_endpoint(self):
        with patch.dict(daily.os.environ, {
            'R2_ACCESS_KEY_ID': 'access',
            'R2_SECRET_ACCESS_KEY': 'secret',
            'R2_ENDPOINT': '',
            'R2_ACCOUNT_ID': 'abc123',
        }):
            missing, _, _, endpoint = daily.publication_settings()
            self.assertEqual(missing, [])
            self.assertEqual(endpoint, 'https://abc123.r2.cloudflarestorage.com')
            publishing = daily.child_environment(True)
        self.assertEqual(publishing['RCLONE_CONFIG_R2_ENDPOINT'], 'https://abc123.r2.cloudflarestorage.com')
        self.assertEqual(publishing['R2_ACCOUNT_ID'], 'abc123')

    def test_missing_credentials_do_not_publish_or_move_pointers(self):
        calls = []

        def executor(command, log, credentials):
            calls.append(log.stem)
        report, message = self.run_with(executor, {name: '' for name in daily.PUBLICATION_NAMES})
        self.assertEqual(calls, [])
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['failed_stage'], 'publish')
        self.assertFalse(report['website_selection_changed'])
        self.assertIn('Missing publication credentials', report['error'])
        self.assertIn('latest-validated and website-release were not changed', report['error'])
        self.assertIn('failed at publish', message)
        self.assertIn('Website selection was not changed', message)

    def test_workflow_pins_aws_publisher_and_keeps_gates(self):
        text = Path(__file__).resolve().parents[1].joinpath('.github/workflows/daily-terminations.yml').read_text()
        self.assertIn('ref: ca167515757a295df811d8a9df7a40b80a3c1f37', text)
        self.assertNotIn('dd191eb', text)
        self.assertNotIn('RCLONE_CONFIG_R2_', text)
        self.assertIn('R2_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}', text)
        self.assertIn('R2_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}', text)
        self.assertIn('R2_ENDPOINT: https://17067e7373bbfee2043fe5186d66aeb9.r2.cloudflarestorage.com', text)
        self.assertIn("vars.SCIMAP_DAILY_ENABLED == 'true'", text)
        self.assertIn('vars.SCIMAP_RUNTIME_READY', text)
        self.assertIn('aws --version', text)
        self.assertIn('default: false', text)
        self.assertIn('Historical replay: never advance latest-validated', text)

    def test_failed_validation_does_not_publish_or_move_pointers(self):
        calls = []

        def executor(command, log, credentials):
            calls.append(log.stem)
            if log.stem != 'science':
                return
            analysis = log.parent / 'analysis'
            analysis.mkdir()
            analysis.joinpath('run-report.json').write_text(json.dumps({
                'status': 'failed',
                'validation': {
                    'mode': 'enforce',
                    'status': 'failed',
                    'errors': ['source_schema:nih_v3_terminations.csv'],
                    'warnings': [],
                    'checks': [],
                },
            }))
        report, message = self.run_with(executor, {
            'R2_ACCESS_KEY_ID': 'test-access',
            'R2_SECRET_ACCESS_KEY': 'test-secret',
            'R2_ENDPOINT': 'https://example.invalid',
        })
        self.assertEqual(calls, ['capture', 'science'])
        self.assertEqual(report['failed_stage'], 'science')
        self.assertFalse(report['website_selection_changed'])
        self.assertIn('enforced validation', report['error'])
        self.assertIn('failed at science', message)
        self.assertIn('Website selection was not changed', message)


if __name__ == '__main__':
    unittest.main()
