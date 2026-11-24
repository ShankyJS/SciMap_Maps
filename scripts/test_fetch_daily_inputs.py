import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fetch_daily_inputs as fetch


def locked_file(name, payload):
    return name, len(payload), hashlib.sha256(payload).hexdigest(), payload


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.district = locked_file('home_119_workTR.csv', b'district-bytes')
        self.county = locked_file('county_commute_NIH.csv', b'county-bytes')
        self.lock = self.root / 'lock.csv'
        self.lock.write_text(
            'Input,Bytes,SHA256\n'
            f'{self.district[0]},{self.district[1]},{self.district[2]}\n'
            f'{self.county[0]},{self.county[1]},{self.county[2]}\n'
        )
        self.env = {
            'R2_ACCESS_KEY_ID': 'access',
            'R2_SECRET_ACCESS_KEY': 'secret',
            'R2_ENDPOINT': 'https://example.r2.cloudflarestorage.com',
        }

    def opener(self, request, timeout):
        self.assertEqual(request.full_url, fetch.OSF_DISTRICT_URL)
        self.assertGreater(timeout, 0)

        class Response:
            def read(self):
                return self_outer.district[3]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        self_outer = self
        return Response()

    def runner(self, command, env, capture_output, text):
        self.assertEqual(command[1:5], ['s3api', 'get-object', '--bucket', 'scimap-private-inputs'])
        self.assertEqual(command[5:7], ['--key', 'inputs/county_commute_NIH.csv'])
        self.assertLess(command.index('--endpoint-url'), len(command) - 1)
        self.assertEqual(command[-1], str(self.root / 'out' / 'county_commute_NIH.csv'))
        self.assertNotIn('scimap-data', command)
        self.assertNotIn(self.env['R2_SECRET_ACCESS_KEY'], command)
        self.assertEqual(env['AWS_ACCESS_KEY_ID'], 'access')
        self.assertEqual(env['AWS_SECRET_ACCESS_KEY'], 'secret')
        self.assertEqual(env['AWS_REGION'], 'auto')
        self.assertEqual(env['AWS_CONFIG_FILE'], '/dev/null')
        self.assertEqual(env['AWS_SHARED_CREDENTIALS_FILE'], '/dev/null')
        self.assertNotIn('AWS_PROFILE', env)
        self.assertNotIn('AWS_SESSION_TOKEN', env)
        Path(command[-1]).write_bytes(self.county[3])
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    def test_verified_downloads_land_in_the_output_directory(self):
        with patch.dict(os.environ, self.env, clear=False):
            district, county = fetch.fetch(
                self.root / 'out', self.lock, 'scimap-private-inputs', 'inputs/county_commute_NIH.csv',
                fetch.OSF_DISTRICT_URL, self.opener, self.runner,
            )
        self.assertEqual(district.read_bytes(), self.district[3])
        self.assertEqual(county.read_bytes(), self.county[3])

    def test_missing_credentials_do_not_download(self):
        calls = []
        with patch.dict(os.environ, {name: '' for name in self.env}, clear=False):
            with self.assertRaises(RuntimeError) as raised:
                fetch.fetch(self.root / 'out', self.lock, 'scimap-private-inputs', 'inputs/county_commute_NIH.csv',
                            fetch.OSF_DISTRICT_URL, lambda *args, **kwargs: calls.append('osf'), lambda *args, **kwargs: calls.append('r2'))
        self.assertEqual(calls, [])
        self.assertIn('Missing private input credentials', str(raised.exception))
        self.assertIn('latest-validated and website-release were not changed', str(raised.exception))

    def test_public_bucket_is_refused(self):
        calls = []
        with patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(RuntimeError) as raised:
                fetch.fetch(self.root / 'out', self.lock, 'scimap-data', 'inputs/county_commute_NIH.csv',
                            fetch.OSF_DISTRICT_URL, self.opener, lambda *args, **kwargs: calls.append('r2'))
        self.assertEqual(calls, [])
        self.assertIn('public bucket scimap-data', str(raised.exception))
        self.assertFalse((self.root / 'out' / 'home_119_workTR.csv').exists())
        self.assertFalse((self.root / 'out' / 'county_commute_NIH.csv').exists())

    def test_checksum_mismatch_removes_the_file(self):
        def bad_opener(request, timeout):
            class Response:
                def read(self):
                    return b'not-the-approved-file'

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False
            return Response()

        with patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(RuntimeError) as raised:
                fetch.fetch(self.root / 'out', self.lock, 'scimap-private-inputs', 'inputs/county_commute_NIH.csv',
                            fetch.OSF_DISTRICT_URL, bad_opener, self.runner)
        self.assertIn('Checksum mismatch for home_119_workTR.csv', str(raised.exception))
        self.assertIn('Pointers were not changed', str(raised.exception))
        self.assertFalse((self.root / 'out' / 'home_119_workTR.csv').exists())
        self.assertFalse((self.root / 'out' / 'county_commute_NIH.csv').exists())

    def test_county_checksum_mismatch_removes_the_private_file(self):
        def bad_runner(command, env, capture_output, text):
            Path(command[-1]).write_bytes(b'not-the-approved-county')
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

        with patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(RuntimeError) as raised:
                fetch.fetch(self.root / 'out', self.lock, 'scimap-private-inputs', 'inputs/county_commute_NIH.csv',
                            fetch.OSF_DISTRICT_URL, self.opener, bad_runner)
        self.assertIn('Checksum mismatch for county_commute_NIH.csv', str(raised.exception))
        self.assertIn('Pointers were not changed', str(raised.exception))
        self.assertFalse((self.root / 'out' / 'county_commute_NIH.csv').exists())

    def test_main_writes_the_error_log(self):
        log = self.root / 'error.txt'
        with patch.dict(os.environ, {name: '' for name in self.env}, clear=False):
            code = fetch.main([
                '--output', str(self.root / 'out'),
                '--error-log', str(log),
                '--lock', str(self.lock),
            ])
        self.assertEqual(code, 1)
        self.assertIn('Missing private input credentials', log.read_text())
        self.assertIn('latest-validated and website-release were not changed', log.read_text())


if __name__ == '__main__':
    unittest.main()
