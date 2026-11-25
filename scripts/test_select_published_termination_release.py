import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import select_published_termination_release as selection


class SelectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.receipt = self.root / 'upload-record.json'
        self.receipt.write_text('{"release_id":"release-B","status":"published"}')
        self.args = selection.parse_args(['--release-id=release-B', f'--scimap={self.root}',
                                          f'--output={self.root / "audit"}', '--approved-by=Operator', '--reason=Review'])

    def test_receipts_use_an_exact_private_key(self):
        with patch.object(selection, 'aws_s3') as aws:
            self.assertEqual(selection.receipt_for('release-B', '1234', self.root), self.receipt)
            aws.assert_called_once_with('get-object', 'scimap-private-inputs', '--key',
                                        'records/releases/release-B/upload-record.json', self.receipt)
            with self.assertRaisesRegex(ValueError, 'does not match'):
                selection.receipt_for('other', '', self.root)
            with self.assertRaisesRegex(ValueError, 'Invalid release ID'):
                selection.receipt_for('../release-B', '', self.root)

    def test_legacy_download_is_only_used_for_a_missing_receipt(self):
        def download(command, **kwargs):
            folder = Path(command[-1]) / 'termination-1234-1'
            folder.mkdir(parents=True)
            (folder / 'upload-record.json').write_bytes(self.receipt.read_bytes())

        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'R2_SECRET_ACCESS_KEY': 'secret'}), \
             patch.object(selection, 'aws_s3', side_effect=selection.MissingObject('missing')), \
             patch.object(selection.subprocess, 'run', side_effect=download) as gh:
            path = selection.receipt_for('release-B', '1234', self.root)
            self.assertEqual(path.parent.name, 'termination-1234-1')
            self.assertEqual(gh.call_args.args[0][0:9],
                             ['gh', 'run', 'download', '1234', '--repo', 'owner/repo', '--pattern', 'termination-1234-*', '--dir'])
            self.assertNotIn('R2_SECRET_ACCESS_KEY', gh.call_args.kwargs['env'])
        with patch.object(selection, 'aws_s3', side_effect=RuntimeError('forbidden')), \
             patch.object(selection.subprocess, 'run') as gh:
            with self.assertRaisesRegex(RuntimeError, 'forbidden'):
                selection.receipt_for('release-B', '1234', self.root)
            gh.assert_not_called()

    def test_missing_object_detection_and_no_automatic_put_retries(self):
        with patch.dict(os.environ, {'R2_ACCESS_KEY_ID': 'access', 'R2_SECRET_ACCESS_KEY': 'secret',
                                     'R2_ENDPOINT': 'https://example.invalid', 'GH_TOKEN': 'token'}), \
             patch.object(selection.subprocess, 'run', return_value=subprocess.CompletedProcess([], 254, '',
                          'An error occurred (NoSuchKey) when calling the GetObject operation')) as aws:
            with self.assertRaises(selection.MissingObject):
                selection.aws_s3('get-object', selection.RECORDS_BUCKET, '--key', 'missing')
            self.assertEqual(aws.call_args.kwargs['env']['AWS_MAX_ATTEMPTS'], '1')
            self.assertNotIn('GH_TOKEN', aws.call_args.kwargs['env'])

    def test_website_selection_requires_actual_approval_even_for_dry_run(self):
        self.args.approved_by = ''
        with patch.object(selection.alerts, 'send') as send, patch.object(selection, 'aws_s3') as aws:
            with self.assertRaisesRegex(ValueError, 'requires non-empty'):
                selection.run(self.args, 'webhook')
            self.assertIn('Termination selection failed', send.call_args.args[1])
            aws.assert_not_called()

    def exercise_selector(self, apply, fail=False, latest=False):
        if latest:
            self.args.pointer = 'latest-validated'
        self.args.apply = apply
        record = Path(self.args.output) / 'selection-record.json'
        audit = {'status': 'unconfirmed' if fail else ('selected' if apply else 'prepared'),
                 'previous': None if latest else {'release_id': 'release-A'},
                 'selection': {'release_id': 'release-B'}}

        def select(flags):
            self.assertEqual('--apply' in flags, apply)
            self.assertIn(f'--pointer={self.args.pointer}', flags)
            self.assertIn(f'--upload-record={self.receipt}', flags)
            if apply:
                self.assertIn(f'--record={record}', flags)
                record.write_text(json.dumps(audit))
            if fail:
                raise RuntimeError('Public pointer verification failed')
            return audit

        selector = SimpleNamespace(parse_args=lambda flags: flags, select=select)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
                                     'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.dict(sys.modules, {'select_termination_release': selector}), patch.object(sys, 'path', sys.path.copy()), \
             patch.object(selection, 'receipt_for', return_value=self.receipt), \
             patch.object(selection, 'aws_s3') as aws, patch.object(selection.alerts, 'send') as send:
            if fail:
                with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                    selection.run(self.args, 'webhook')
            else:
                selection.run(self.args, 'webhook')
            self.assertIs(selector.remote, selection.selector_transport)
            if apply:
                self.assertEqual(json.loads(record.read_text()), audit)
                aws.assert_called_once_with('put-object', 'scimap-private-inputs', '--key',
                                            'records/selections/website-release/123-2.json', '--body', record,
                                            '--content-type', 'application/json', '--if-none-match', '*')
            else:
                self.assertFalse(record.exists())
                aws.assert_not_called()
            title = ('Termination selection failed' if fail else 'Termination release selected and verified'
                     if apply else 'Termination selection preview — no writes')
            expected = [
                f'{":rotating_light:" if fail else ":information_source:"} *{title}*',
                '*Applies to:* Latest checked dataset (`latest-validated`)' if latest
                else '*Applies to:* Public website (`website-release`)',
                '*Was showing:* `(unset)`' if latest else '*Was showing:* `release-A`',
                '*Now showing:* `release-B`',
                '*Approver:* `Operator`',
                '*Reason:* `Review`',
                f"*Selection status:* `{audit['status']}`",
            ]
            if fail:
                expected.append('```Public pointer verification failed```')
            expected.append('<https://github.com/owner/repo/actions/runs/123|Open run and selection record>')
            self.assertEqual(send.call_args.args[1], '\n'.join(expected))

    def test_dry_run_keeps_r2_read_only(self):
        self.exercise_selector(apply=False)

    def test_latest_checked_preview_without_previous_release(self):
        self.exercise_selector(apply=False, latest=True)

    def test_verified_selection_retains_record_and_alerts(self):
        self.exercise_selector(apply=True)

    def test_readback_failure_fails_run_but_retains_audit(self):
        self.exercise_selector(apply=True, fail=True)

    def test_pointer_transport_sets_json_and_no_cache(self):
        with patch.object(selection, 'aws_s3') as aws:
            selection.selector_transport('copyto', self.receipt, 'r2:scimap-data/terminations/website-release.json',
                                         '--header-upload', 'Cache-Control: no-cache', '--header-upload', 'Content-Type: application/json')
            aws.assert_called_once_with('put-object', 'scimap-data', '--key', 'terminations/website-release.json',
                                        '--body', self.receipt, '--content-type', 'application/json', '--cache-control', 'no-cache')


if __name__ == '__main__':
    unittest.main()
