import base64
import errno
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
import xml.etree.ElementTree as ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent / 'publisher_aws'))
import publish_pool
import r2_s3api


ACCESS = 'AKIDEXAMPLE'
SECRET = 'wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY'


class Store:
    def __init__(self):
        self.objects = {}
        self.requests = []


def start_server(store):
    class Handler(BaseHTTPRequestHandler):
        def record(self):
            length = int(self.headers.get('Content-Length', '0') or 0)
            body = self.rfile.read(length) if length else b''
            store.requests.append((self.command, self.path, {key.lower(): value for key, value in self.headers.items()}, body))
            return body

        def do_PUT(self):
            body = self.record()
            key = self.path.split('?', 1)[0].lstrip('/').split('/', 1)[1]
            digest = base64.b64encode(hashlib.md5(body).digest()).decode('ascii')
            if self.headers.get('Content-MD5') != digest:
                self.send_error(400)
                return
            if self.headers.get('If-None-Match') == '*' and key in store.objects:
                payload = b'<Error><Code>PreconditionFailed</Code><Message>exists</Message></Error>'
                self.send_response(412)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            store.objects[key] = body
            payload = b'<PutObjectResult></PutObjectResult>'
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.record()
            path, _, query = self.path.partition('?')
            key = path.lstrip('/').split('/', 1)[1] if path.count('/') > 1 else ''
            if 'list-type=2' in query:
                prefix = parse_qs(query).get('prefix', [''])[0]
                names = sorted(name for name in store.objects if name.startswith(prefix))
                root = ElementTree.Element('ListBucketResult')
                ElementTree.SubElement(root, 'IsTruncated').text = 'false'
                ElementTree.SubElement(root, 'KeyCount').text = str(len(names))
                for name in names:
                    ElementTree.SubElement(ElementTree.SubElement(root, 'Contents'), 'Key').text = name
                payload = ElementTree.tostring(root)
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if key not in store.objects:
                self.send_error(404)
                return
            body = store.objects[key]
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('ETag', '"abc"')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 0.2
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    server.stop = stop
    return server, thread


class S3ApiTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.server, self.thread = start_server(self.store)
        self.endpoint = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.stop.set)
        self.addCleanup(self.server.server_close)
        clear = patch.dict(os.environ, {'SCIMAP_PUBLISH_POOL_DIR': ''})
        clear.start()
        self.addCleanup(clear.stop)

    def env(self):
        return patch.dict(os.environ, {'AWS_ACCESS_KEY_ID': ACCESS, 'AWS_SECRET_ACCESS_KEY': SECRET}, clear=False)

    def test_iam_signature_vector(self):
        headers = {
            'content-type': 'application/x-www-form-urlencoded; charset=utf-8',
            'host': 'iam.amazonaws.com',
            'x-amz-date': '20150830T123600Z',
        }
        auth, canonical = r2_s3api.authorization(
            'GET', '/', 'Action=ListUsers&Version=2010-05-08', headers,
            r2_s3api.EMPTY_SHA256, ACCESS, SECRET, 'us-east-1', '20150830T123600Z', service='iam',
        )
        self.assertIn('content-type:application/x-www-form-urlencoded; charset=utf-8\n', canonical)
        self.assertIn('\n\ncontent-type;host;x-amz-date\n', canonical)
        self.assertIn('Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7', auth)

    def test_put_get_and_list_round_trip(self):
        body = Path(self.tmp.name) / 'object.bin'
        body.write_bytes(b'county-row')
        digest = base64.b64encode(hashlib.md5(body.read_bytes()).digest()).decode('ascii')
        with self.env():
            self.assertEqual(r2_s3api.main([
                's3api', 'put-object', '--bucket', 'scimap-data', '--key', 'terminations/releases/a/file.bin',
                '--body', str(body), '--content-md5', digest, '--if-none-match', '*',
                '--endpoint-url', self.endpoint, '--region', 'auto', '--no-cli-pager',
            ]), 0)
            listed = Path(self.tmp.name) / 'list.json'
            # Capture stdout by calling list_objects through main and reading the pipe via redirect.
            import io
            from contextlib import redirect_stdout
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(r2_s3api.main([
                    's3api', 'list-objects-v2', '--bucket', 'scimap-data', '--prefix', 'terminations/releases/a/',
                    '--max-keys', '1', '--no-paginate', '--endpoint-url', self.endpoint, '--region', 'auto',
                ]), 0)
            page = json.loads(buffer.getvalue())
            self.assertEqual(page['KeyCount'], 1)
            self.assertIs(page['IsTruncated'], False)
            self.assertEqual(page['Contents'], [{'Key': 'terminations/releases/a/file.bin'}])
            outfile = Path(self.tmp.name) / 'downloaded'
            download = io.StringIO()
            with redirect_stdout(download):
                self.assertEqual(r2_s3api.main([
                    's3api', 'get-object', '--bucket', 'scimap-data', '--key', 'terminations/releases/a/file.bin',
                    str(outfile), '--endpoint-url', self.endpoint, '--region', 'auto',
                ]), 0)
            self.assertEqual(outfile.read_bytes(), b'county-row')
            self.assertIsInstance(json.loads(download.getvalue()), dict)
        sent = self.store.requests[0][2]
        self.assertEqual(sent['if-none-match'], '*')
        self.assertEqual(sent['content-md5'], digest)
        self.assertTrue(sent['authorization'].startswith('AWS4-HMAC-SHA256 '))

    def test_existing_object_is_refused(self):
        body = Path(self.tmp.name) / 'object.bin'
        body.write_bytes(b'x')
        digest = base64.b64encode(hashlib.md5(b'x').digest()).decode('ascii')
        command = [
            's3api', 'put-object', '--bucket', 'scimap-data', '--key', 'k',
            '--body', str(body), '--content-md5', digest, '--if-none-match', '*',
            '--endpoint-url', self.endpoint, '--region', 'auto',
        ]
        with self.env():
            self.assertEqual(r2_s3api.main(command), 0)
            self.assertEqual(r2_s3api.main(command), 1)
        self.assertEqual(self.store.objects['k'], b'x')

    def test_unsafe_puts_and_lists_do_not_send(self):
        body = Path(self.tmp.name) / 'object.bin'
        body.write_bytes(b'x')
        digest = base64.b64encode(hashlib.md5(b'x').digest()).decode('ascii')
        common = ['--bucket', 'scimap-data', '--endpoint-url', self.endpoint, '--region', 'auto']
        with self.env():
            self.assertEqual(r2_s3api.main(['s3api', 'put-object', *common, '--key', 'k', '--body', str(body), '--content-md5', digest]), 1)
            self.assertEqual(r2_s3api.main(['s3api', 'put-object', *common, '--key', 'k', '--body', str(body), '--if-none-match', '*']), 1)
            self.assertEqual(r2_s3api.main(['s3api', 'put-object', *common, '--key', 'k', '--body', str(body), '--content-md5', digest, '--if-none-match', '*', '--acl', 'public-read']), 1)
            self.assertEqual(r2_s3api.main(['s3api', 'put-object', *common, '--key', 'k', '--body', str(body), '--content-md5', digest, '--if-none-match', 'abc']), 1)
            self.assertEqual(r2_s3api.main(['s3api', 'list-objects-v2', *common, '--prefix', 'p']), 1)
        self.assertEqual(self.store.requests, [])

    def test_version_falls_through_to_real_aws(self):
        with patch('r2_s3api.os.execv') as execv, patch('r2_s3api.real_aws_path', return_value='/usr/bin/aws'):
            self.assertEqual(r2_s3api.main(['--version']), 0)
        self.assertEqual(execv.call_args.args, ('/usr/bin/aws', ['/usr/bin/aws', '--version']))

    def test_unreachable_network_is_retried(self):
        calls = []

        def once(method, endpoint, region, bucket, key, query, headers, body_path):
            calls.append(method)
            if len(calls) == 1:
                raise OSError(errno.ENETUNREACH, 'Network is unreachable')
            return 200, {'etag': '"abc"'}, b'ok'

        with patch.object(r2_s3api, 'transmit_once', once), patch.object(r2_s3api, '_pause'):
            status, _headers, payload = r2_s3api.transmit(
                'GET', 'https://example.invalid', 'auto', 'scimap-data', 'k', {}, {}, None,
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload, b'ok')
        self.assertEqual(calls, ['GET', 'GET'])

    def test_client_error_and_local_oserror_are_not_retried(self):
        calls = []

        def once(method, endpoint, region, bucket, key, query, headers, body_path):
            calls.append(status_code)
            return status_code, {}, b'no'

        status_code = 400
        with patch.object(r2_s3api, 'transmit_once', once), patch.object(r2_s3api, '_pause'):
            status, _headers, _payload = r2_s3api.transmit(
                'GET', 'https://example.invalid', 'auto', 'scimap-data', 'k', {}, {}, None,
            )
        self.assertEqual(status, 400)
        self.assertEqual(calls, [400])

        def missing(*_args):
            raise OSError(errno.ENOENT, 'No such file')

        with patch.object(r2_s3api, 'transmit_once', missing), patch.object(r2_s3api, '_pause'):
            with self.assertRaises(OSError) as raised:
                r2_s3api.transmit(
                    'GET', 'https://example.invalid', 'auto', 'scimap-data', 'k', {}, {}, None,
                )
        self.assertEqual(raised.exception.errno, errno.ENOENT)

    def test_retry_budget_is_finite(self):
        calls = []

        def once(*_args):
            calls.append(1)
            raise OSError(errno.ENETUNREACH, 'Network is unreachable')

        with patch.object(r2_s3api, 'transmit_once', once), patch.object(r2_s3api, '_pause'):
            with self.assertRaises(OSError):
                r2_s3api.transmit(
                    'LIST', 'https://example.invalid', 'auto', 'scimap-data', '', {}, {}, None,
                )
        self.assertEqual(len(calls), r2_s3api.TRANSIENT_ATTEMPTS)

    def test_retried_put_412_succeeds_only_when_the_stored_md5_matches(self):
        body = Path(self.tmp.name) / 'object.bin'
        body.write_bytes(b'county-row')
        digest = hashlib.md5(b'county-row').hexdigest()
        calls = []

        def once(method, endpoint, region, bucket, key, query, headers, body_path):
            calls.append(method)
            if method == 'PUT' and calls.count('PUT') == 1:
                raise OSError(errno.ECONNRESET, 'Connection reset')
            if method == 'PUT':
                return 412, {}, b'<Error><Code>PreconditionFailed</Code></Error>'
            return 200, {'etag': f'"{digest}"'}, b'county-row'

        with patch.object(r2_s3api, 'transmit_once', once), patch.object(r2_s3api, '_pause'):
            status, _headers, payload = r2_s3api.transmit(
                'PUT', 'https://example.invalid', 'auto', 'scimap-data', 'k', {},
                {'content-md5': 'ignored', 'if-none-match': '*'}, str(body),
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload, b'')
        self.assertEqual(calls, ['PUT', 'PUT', 'GET'])

        calls.clear()

        def mismatch(method, endpoint, region, bucket, key, query, headers, body_path):
            calls.append(method)
            if method == 'PUT' and calls.count('PUT') == 1:
                raise OSError(errno.ECONNRESET, 'Connection reset')
            if method == 'PUT':
                return 412, {}, b'exists'
            return 200, {'etag': '"0123456789abcdef0123456789abcdef"'}, b'other'

        with patch.object(r2_s3api, 'transmit_once', mismatch), patch.object(r2_s3api, '_pause'):
            status, _headers, payload = r2_s3api.transmit(
                'PUT', 'https://example.invalid', 'auto', 'scimap-data', 'k', {},
                {'content-md5': 'ignored', 'if-none-match': '*'}, str(body),
            )
        self.assertEqual(status, 412)
        self.assertEqual(payload, b'exists')

    def test_first_put_412_is_not_reread(self):
        calls = []

        def once(method, endpoint, region, bucket, key, query, headers, body_path):
            calls.append(method)
            return 412, {}, b'exists'

        with patch.object(r2_s3api, 'transmit_once', once), patch.object(r2_s3api, '_pause'):
            status, _headers, _payload = r2_s3api.transmit(
                'PUT', 'https://example.invalid', 'auto', 'scimap-data', 'k', {},
                {'if-none-match': '*'}, str(Path(self.tmp.name) / 'object.bin'),
            )
        self.assertEqual(status, 412)
        self.assertEqual(calls, ['PUT'])


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def job(self, key, body=b'abc'):
        path = Path(self.tmp.name) / key.replace('/', '_')
        path.write_bytes(body)
        return {
            'bucket': 'scimap-data',
            'key': key,
            'endpoint': 'http://127.0.0.1',
            'region': 'auto',
            'body': str(path),
            'content_md5': 'md5value',
            'if_none_match': '*',
            'outfile': str(Path(self.tmp.name) / (key.replace('/', '_') + '.out')),
        }

    def test_worker_count_is_the_maximum_in_flight(self):
        for concurrency in (1, 4):
            with self.subTest(concurrency=concurrency):
                seen = {'current': 0, 'max': 0}
                lock = threading.Lock()

                def operation(method, endpoint, region, bucket, key, query, headers, body):
                    self.assertEqual(headers.get('if-none-match'), '*')
                    self.assertEqual(headers.get('content-md5'), 'md5value')
                    with lock:
                        seen['current'] += 1
                        seen['max'] = max(seen['max'], seen['current'])
                    time.sleep(0.05)
                    with lock:
                        seen['current'] -= 1
                    return {}, b''

                pool = publish_pool.TransferPool(concurrency, operation)
                try:
                    for index in range(8):
                        pool.submit_put(self.job(f'files/{index}.bin'))
                    pool.wait_for_puts()
                    self.assertEqual(seen['max'], concurrency)
                    self.assertEqual(pool.max_in_flight, concurrency)
                    self.assertLessEqual(pool.max_in_flight, concurrency)
                finally:
                    pool.close(wait=True)

    def test_manifest_starts_only_after_other_puts_finish(self):
        release = threading.Event()
        started = threading.Event()
        calls = []

        def operation(method, endpoint, region, bucket, key, query, headers, body):
            if not key.endswith('manifest.json'):
                started.set()
                if not release.wait(5):
                    raise r2_s3api.S3Error('timed out waiting to finish a put')
            calls.append(key)
            return {}, b''

        pool = publish_pool.TransferPool(4, operation)
        thread = threading.Thread(target=lambda: pool.put_manifest(self.job('rel/manifest.json')))
        try:
            for name in ('a.bin', 'b.bin', 'c.bin'):
                pool.submit_put(self.job(f'rel/{name}'))
            self.assertTrue(started.wait(2))
            thread.start()
            time.sleep(0.15)
            self.assertNotIn('rel/manifest.json', calls)
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(calls[-1], 'rel/manifest.json')
            self.assertCountEqual(calls[:-1], ['rel/a.bin', 'rel/b.bin', 'rel/c.bin'])
        finally:
            release.set()
            if thread.is_alive() or thread.ident is not None:
                thread.join(5)
            pool.close(wait=True)

    def test_failed_put_is_reported_and_manifest_is_not_uploaded(self):
        release = threading.Event()
        entered = threading.Event()
        calls = []

        def operation(method, endpoint, region, bucket, key, query, headers, body):
            calls.append(key)
            if key == 'bad.bin':
                entered.set()
                if not release.wait(5):
                    raise r2_s3api.S3Error('timed out')
                raise r2_s3api.S3Error('put-object failed with HTTP 500: InternalError boom')
            return {}, b''

        pool = publish_pool.TransferPool(4, operation)
        try:
            pool.submit_put(self.job('bad.bin'))
            self.assertTrue(entered.wait(2))
            pool.submit_put(self.job('later.bin'))
            release.set()
            with self.assertRaises(publish_pool.PoolError) as caught:
                pool.wait_for_puts()
            self.assertIn('HTTP 500', str(caught.exception))
            with self.assertRaises(publish_pool.PoolError):
                pool.submit_put(self.job('after.bin'))
            with self.assertRaises(publish_pool.PoolError):
                pool.put_manifest(self.job('rel/manifest.json'))
            self.assertNotIn('rel/manifest.json', calls)
        finally:
            release.set()
            pool.close(wait=True)

    def test_manifest_cannot_use_the_early_ack_path(self):
        calls = []

        def operation(method, endpoint, region, bucket, key, query, headers, body):
            calls.append(key)
            return {}, b''

        pool = publish_pool.TransferPool(2, operation)
        try:
            with self.assertRaises(publish_pool.PoolError):
                pool.submit_put(self.job('rel/manifest.json'))
            with self.assertRaises(publish_pool.PoolError):
                pool.put_manifest(self.job('rel/manifest.json'))
            self.assertEqual(calls, [])
        finally:
            pool.close(wait=True)

    def test_verification_reads_stay_inside_the_bound(self):
        seen = {'current': 0, 'max': 0}
        lock = threading.Lock()
        bodies = {}

        def operation(method, endpoint, region, bucket, key, query, headers, body):
            if method == 'GET':
                with lock:
                    seen['current'] += 1
                    seen['max'] = max(seen['max'], seen['current'])
                time.sleep(0.05)
                with lock:
                    seen['current'] -= 1
                return {'etag': '"e"'}, bodies[key]
            return {}, b''

        pool = publish_pool.TransferPool(3, operation)
        try:
            jobs = []
            for index in range(6):
                payload = f'row-{index}'.encode()
                item = self.job(f'tiles/{index}.pbf', payload)
                bodies[item['key']] = payload
                jobs.append(item)
                pool.submit_put(item)
            for item in jobs:
                meta = pool.read_object(item)
                self.assertEqual(Path(item['outfile']).read_bytes(), bodies[item['key']])
                self.assertEqual(meta['ContentLength'], len(bodies[item['key']]))
                self.assertEqual(meta['ETag'], '"e"')
            self.assertLessEqual(seen['max'], 3)
            self.assertGreaterEqual(seen['max'], 2)
            self.assertLessEqual(pool.max_in_flight, 3)
        finally:
            pool.close(wait=True)

    def test_concurrency_rejects_out_of_range(self):
        self.assertEqual(publish_pool.concurrency_from_env(None), 32)
        self.assertEqual(publish_pool.concurrency_from_env('16'), 16)
        with self.assertRaises(ValueError):
            publish_pool.concurrency_from_env('0')
        with self.assertRaises(ValueError):
            publish_pool.concurrency_from_env('65')


def start_threaded_server(store):
    class Handler(BaseHTTPRequestHandler):
        def record(self):
            length = int(self.headers.get('Content-Length', '0') or 0)
            body = self.rfile.read(length) if length else b''
            with store.lock:
                store.requests.append((self.command, self.path, {key.lower(): value for key, value in self.headers.items()}, body))
            return body

        def do_PUT(self):
            started = time.monotonic()
            key = ''
            with store.lock:
                store.in_flight += 1
                store.max_in_flight = max(store.max_in_flight, store.in_flight)
            try:
                body = self.record()
                key = self.path.split('?', 1)[0].lstrip('/').split('/', 1)[1]
                if store.delay and not key.endswith('/manifest.json'):
                    time.sleep(store.delay)
                digest = base64.b64encode(hashlib.md5(body).digest()).decode('ascii')
                if self.headers.get('Content-MD5') != digest:
                    self.send_error(400)
                    return
                if key in store.fail_keys:
                    payload = b'<Error><Code>InternalError</Code><Message>boom</Message></Error>'
                    self.send_response(500)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.headers.get('If-None-Match') == '*' and key in store.objects:
                    payload = b'<Error><Code>PreconditionFailed</Code><Message>exists</Message></Error>'
                    self.send_response(412)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                with store.lock:
                    store.objects[key] = body
                    # Record completion before the response so a later manifest
                    # put cannot start until this timestamp is visible.
                    store.put_finished.append((key, started, time.monotonic()))
                payload = b'<PutObjectResult></PutObjectResult>'
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                with store.lock:
                    store.in_flight -= 1

        def do_GET(self):
            self.record()
            path, _, query = self.path.partition('?')
            key = path.lstrip('/').split('/', 1)[1] if path.count('/') > 1 else ''
            if 'list-type=2' in query:
                prefix = parse_qs(query).get('prefix', [''])[0]
                names = sorted(name for name in store.objects if name.startswith(prefix))
                root = ElementTree.Element('ListBucketResult')
                ElementTree.SubElement(root, 'IsTruncated').text = 'false'
                ElementTree.SubElement(root, 'KeyCount').text = str(len(names))
                for name in names:
                    ElementTree.SubElement(ElementTree.SubElement(root, 'Contents'), 'Key').text = name
                payload = ElementTree.tostring(root)
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if key not in store.objects:
                self.send_error(404)
                return
            body = store.objects[key]
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('ETag', '"abc"')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class PoolProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.lock = threading.Lock()
        self.store.in_flight = 0
        self.store.max_in_flight = 0
        self.store.put_finished = []
        self.store.delay = 0.2
        self.store.fail_keys = set()
        self.server, self.thread = start_threaded_server(self.store)
        self.endpoint = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.tmp = tempfile.TemporaryDirectory()
        self.pool_dir = Path(self.tmp.name) / 'pool'
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self._stop_pool)

    def _stop_pool(self):
        script = Path(__file__).resolve().parent / 'publisher_aws' / 'r2_s3api.py'
        subprocess.run([sys.executable, '-S', str(script), '--shutdown', str(self.pool_dir)], timeout=30, check=False)
        pid_path = self.pool_dir / 'pid'
        if not pid_path.is_file():
            return
        try:
            pid = int(pid_path.read_text().strip())
        except ValueError:
            return
        command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
        if 'r2_s3api.py' in command and '--serve' in command:
            os.kill(pid, signal.SIGTERM)

    def env(self, **extra):
        values = {
            'AWS_ACCESS_KEY_ID': ACCESS,
            'AWS_SECRET_ACCESS_KEY': SECRET,
            'SCIMAP_PUBLISH_POOL_DIR': str(self.pool_dir),
            'SCIMAP_PUBLISH_CONCURRENCY': '2',
        }
        values.update(extra)
        return patch.dict(os.environ, values, clear=False)

    def put(self, key, body):
        path = Path(self.tmp.name) / key.replace('/', '_')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        digest = base64.b64encode(hashlib.md5(body).digest()).decode('ascii')
        return r2_s3api.main([
            's3api', 'put-object', '--bucket', 'scimap-data', '--key', key,
            '--body', str(path), '--content-md5', digest, '--if-none-match', '*',
            '--endpoint-url', self.endpoint, '--region', 'auto',
        ])

    def test_pinned_publisher_sequence_overlaps_and_keeps_manifest_last(self):
        import io
        from contextlib import redirect_stdout
        keys = [f'terminations/releases/a/file-{index}.bin' for index in range(4)]
        manifest = 'terminations/releases/a/manifest.json'
        with self.env():
            listed = io.StringIO()
            with redirect_stdout(listed):
                self.assertEqual(r2_s3api.main([
                    's3api', 'list-objects-v2', '--bucket', 'scimap-data',
                    '--prefix', 'terminations/releases/a/', '--max-keys', '1', '--no-paginate',
                    '--endpoint-url', self.endpoint, '--region', 'auto',
                ]), 0)
            self.assertEqual(json.loads(listed.getvalue())['KeyCount'], 0)
            for key in keys:
                self.assertEqual(self.put(key, key.encode()), 0)
            page = io.StringIO()
            with redirect_stdout(page):
                self.assertEqual(r2_s3api.main([
                    's3api', 'list-objects-v2', '--bucket', 'scimap-data',
                    '--prefix', 'terminations/releases/a/', '--no-paginate',
                    '--endpoint-url', self.endpoint, '--region', 'auto',
                ]), 0)
            parsed = json.loads(page.getvalue())
            self.assertEqual(parsed['KeyCount'], 4)
            self.assertIs(parsed['IsTruncated'], False)
            for key in keys:
                outfile = Path(self.tmp.name) / (key.replace('/', '_') + '.got')
                downloaded = io.StringIO()
                with redirect_stdout(downloaded):
                    self.assertEqual(r2_s3api.main([
                        's3api', 'get-object', '--bucket', 'scimap-data', '--key', key,
                        str(outfile), '--endpoint-url', self.endpoint, '--region', 'auto',
                    ]), 0)
                self.assertEqual(outfile.read_bytes(), key.encode())
                self.assertIn('ContentLength', json.loads(downloaded.getvalue()))
            self.assertEqual(self.put(manifest, b'{"release":"a"}'), 0)
            manifest_out = Path(self.tmp.name) / 'manifest.got'
            manifest_json = io.StringIO()
            with redirect_stdout(manifest_json):
                self.assertEqual(r2_s3api.main([
                    's3api', 'get-object', '--bucket', 'scimap-data', '--key', manifest,
                    str(manifest_out), '--endpoint-url', self.endpoint, '--region', 'auto',
                ]), 0)
            self.assertEqual(manifest_out.read_bytes(), b'{"release":"a"}')
            self.assertIn('ContentLength', json.loads(manifest_json.getvalue()))
            self.assertEqual(self.put(keys[0], keys[0].encode()), 1)
        puts = [item for item in self.store.put_finished if True]
        manifest_times = [item for item in puts if item[0] == manifest]
        other_times = [item for item in puts if item[0] != manifest]
        self.assertEqual(len(manifest_times), 1)
        self.assertGreaterEqual(manifest_times[0][1], max(item[2] for item in other_times))
        self.assertLessEqual(self.store.max_in_flight, 2)
        self.assertGreaterEqual(self.store.max_in_flight, 2)
        sent = [headers for method, _path, headers, _body in self.store.requests if method == 'PUT']
        self.assertTrue(sent)
        self.assertTrue(all(headers.get('if-none-match') == '*' and headers.get('content-md5') for headers in sent))
        self.assertEqual(self.store.objects[manifest], b'{"release":"a"}')
        self.assertEqual(self.store.objects[keys[0]], keys[0].encode())

    def test_failed_put_blocks_manifest_on_the_socket(self):
        manifest = 'terminations/releases/b/manifest.json'
        self.store.delay = 0
        self.store.fail_keys.add('terminations/releases/b/bad.bin')
        with self.env():
            self.assertIn(self.put('terminations/releases/b/ok.bin', b'ok'), (0, 1))
            self.assertIn(self.put('terminations/releases/b/bad.bin', b'bad'), (0, 1))
            self.assertEqual(self.put(manifest, b'{}'), 1)
        self.assertNotIn(manifest, self.store.objects)


if __name__ == '__main__':
    unittest.main()
