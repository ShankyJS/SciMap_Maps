import base64
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
import xml.etree.ElementTree as ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent / 'publisher_aws'))
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
            self.assertEqual(r2_s3api.main(['s3api', 'list-objects-v2', *common, '--prefix', 'p']), 1)
        self.assertEqual(self.store.requests, [])

    def test_version_falls_through_to_real_aws(self):
        with patch('r2_s3api.os.execv') as execv, patch('r2_s3api.real_aws_path', return_value='/usr/bin/aws'):
            self.assertEqual(r2_s3api.main(['--version']), 0)
        self.assertEqual(execv.call_args.args, ('/usr/bin/aws', ['/usr/bin/aws', '--version']))


if __name__ == '__main__':
    unittest.main()
