#!/usr/bin/env python3
"""Stdlib S3 client for the pinned termination publisher.

publish_termination_release.py at the pinned website revision shells out to
`aws s3api` once per object and uploads manifest.json only after every other
object has been stored and verified. Starting AWS CLI v2 that many times
exhausts a GitHub-hosted runner, and doing the transfers one by one makes a
12,000-object release take longer than the job budget allows.

This process speaks the same flags. It refuses any put that omits
--if-none-match * or --content-md5, and any list that omits --no-paginate.
When SCIMAP_PUBLISH_POOL_DIR is set, non-manifest puts are queued on a local
pool with a fixed worker count (SCIMAP_PUBLISH_CONCURRENCY, default 32). The
manifest put, and any list or get, waits until those puts have succeeded. A
failed put is returned on the next call and the manifest is not uploaded.
A connect failure or a 429/5xx response is retried. A retried put that finds
the object already stored succeeds only when that object's MD5 matches.
Credentials and the endpoint come from the publisher's own command line and
environment. They are never printed.
"""
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from http.client import BadStatusLine, HTTPConnection, HTTPSConnection, IncompleteRead
from urllib.parse import urlsplit

# The pool module sits next to this file. Put that directory first so a
# PYTHONPATH entry cannot shadow it, including when Python is started with -S.
_CLIENT_DIR = str(Path(__file__).resolve().parent)
if sys.path[:1] != [_CLIENT_DIR]:
    sys.path.insert(0, _CLIENT_DIR)
from publish_pool import PoolError, TransferPool, concurrency_from_env

HANDLED = {'put-object', 'get-object', 'list-objects-v2'}
VALUE_FLAGS = {
    '--bucket', '--key', '--body', '--content-md5', '--if-none-match',
    '--endpoint-url', '--region', '--output', '--prefix', '--max-keys',
    '--continuation-token',
}
BOOL_FLAGS = {'--no-paginate', '--no-cli-pager', '--no-cli-auto-prompt'}
EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
TRANSIENT_ATTEMPTS = 5
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_ERRNOS = frozenset({
    errno.EAGAIN, errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET,
    errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ENETUNREACH,
    errno.EPIPE, errno.ETIMEDOUT, errno.EWOULDBLOCK,
})
_retry_count = 0
_retry_count_lock = threading.Lock()


class S3Error(RuntimeError):
    pass


def should_handle(argv):
    return len(argv) >= 2 and argv[0] == 's3api' and argv[1] in HANDLED


def parse_args(argv):
    flags = {}
    bools = set()
    positionals = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in BOOL_FLAGS:
            bools.add(token)
            index += 1
            continue
        if token in VALUE_FLAGS:
            if index + 1 >= len(argv):
                raise S3Error(f'missing value for {token}')
            flags[token] = argv[index + 1]
            index += 2
            continue
        if token.startswith('-'):
            raise S3Error(f'refusing unrecognized flag {token}')
        positionals.append(token)
        index += 1
    return flags, bools, positionals


def require(flags, name):
    value = flags.get(name, '').strip()
    if not value:
        raise S3Error(f'missing {name}')
    return value


def canonical_query(params):
    def encode(value):
        return quote(str(value), safe='-_.~')
    parts = sorted((encode(key), encode(value)) for key, value in params.items())
    return '&'.join(f'{key}={value}' for key, value in parts)


def canonical_uri(bucket, key):
    segments = [bucket, *key.split('/')] if key else [bucket]
    return '/' + '/'.join(quote(segment, safe='-_.~') for segment in segments)


def header_value(value):
    return ' '.join(str(value).strip().split())


def signing_key(secret, datestamp, region, service='s3'):
    def step(key, message):
        return hmac.new(key, message.encode('utf-8'), hashlib.sha256).digest()
    key = step(('AWS4' + secret).encode('utf-8'), datestamp)
    key = step(key, region)
    key = step(key, service)
    return step(key, 'aws4_request')


def authorization(method, uri, query, headers, payload_hash, access, secret, region, amzdate, service='s3'):
    """Return the Authorization header value. `headers` is a name to value map."""
    items = sorted((name.lower(), header_value(value)) for name, value in headers.items())
    canonical_headers = ''.join(f'{name}:{value}\n' for name, value in items)
    signed = ';'.join(name for name, _value in items)
    canonical = '\n'.join([
        method,
        uri,
        query,
        canonical_headers,
        signed,
        payload_hash,
    ])
    # canonical_headers already ends in a newline, so the join inserts the
    # required blank line before the signed-header list.
    datestamp = amzdate[:8]
    scope = f'{datestamp}/{region}/{service}/aws4_request'
    string_to_sign = '\n'.join([
        'AWS4-HMAC-SHA256',
        amzdate,
        scope,
        hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
    ])
    signature = hmac.new(signing_key(secret, datestamp, region, service), string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    return (
        f'AWS4-HMAC-SHA256 Credential={access}/{scope}, '
        f'SignedHeaders={signed}, Signature={signature}'
    ), canonical


def host_header(parts):
    if parts.port is None or (parts.scheme == 'https' and parts.port == 443) or (parts.scheme == 'http' and parts.port == 80):
        return parts.hostname
    return f'{parts.hostname}:{parts.port}'


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def credentials():
    access = os.environ.get('AWS_ACCESS_KEY_ID', '').strip()
    secret = os.environ.get('AWS_SECRET_ACCESS_KEY', '').strip()
    if not access or not secret:
        raise S3Error('missing AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY')
    token = os.environ.get('AWS_SESSION_TOKEN', '').strip()
    return access, secret, token or None


def retry_count():
    with _retry_count_lock:
        return _retry_count


def _count_retry():
    global _retry_count
    with _retry_count_lock:
        _retry_count += 1


def _retryable_oserror(error):
    if isinstance(error, (TimeoutError, ConnectionError, socket.gaierror)):
        return True
    return getattr(error, 'errno', None) in _RETRYABLE_ERRNOS


def _pause(attempt):
    time.sleep(min(8.0, 0.25 * (2 ** attempt)) + random.uniform(0, 0.05))


def _etag_matches_file(etag, path):
    """True when the service ETag is the MD5 of this file. Multipart ETags do not match."""
    if not etag or not path:
        return False
    text = etag.strip()
    if text[:2].upper() == 'W/':
        text = text[2:].strip()
    text = text.strip('"')
    if '-' in text:
        return False
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return text == digest.hexdigest()


def transmit_once(method, endpoint, region, bucket, key, query, extra_headers, body_path):
    access, secret, token = credentials()
    parts = urlsplit(endpoint)
    if parts.scheme not in ('https', 'http') or not parts.hostname:
        raise S3Error('endpoint must be an http(s) URL')
    uri = canonical_uri(bucket, key)
    query_string = canonical_query(query)
    payload_hash = file_sha256(body_path) if body_path else EMPTY_SHA256
    amzdate = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    headers = {
        'host': host_header(parts),
        'x-amz-date': amzdate,
        'x-amz-content-sha256': payload_hash,
    }
    headers.update(extra_headers)
    if token:
        headers['x-amz-security-token'] = token
    auth, _canonical = authorization(method, uri, query_string, headers, payload_hash, access, secret, region, amzdate)
    headers['authorization'] = auth
    connection_class = HTTPSConnection if parts.scheme == 'https' else HTTPConnection
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    path = uri if not query_string else f'{uri}?{query_string}'
    connection = connection_class(parts.hostname, port, timeout=120)
    try:
        if body_path:
            length = os.path.getsize(body_path)
            headers['content-length'] = str(length)
            with open(body_path, 'rb') as body:
                connection.request(method, path, body=body, headers=headers)
        else:
            connection.request(method, path, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        response_headers = {name.lower(): value for name, value in response.getheaders()}
    finally:
        connection.close()
    return status, response_headers, payload


def _confirm_stored_put(endpoint, region, bucket, key, body_path):
    """A retried create-only put got 412. Accept it only when the stored MD5 is this file."""
    status, headers, _payload = transmit(
        'GET', endpoint, region, bucket, key, {}, {}, None,
    )
    if status == 200 and _etag_matches_file(headers.get('etag', ''), body_path):
        return 200, headers, b''
    return None


def transmit(method, endpoint, region, bucket, key, query, extra_headers, body_path):
    """Send one signed request. Retry a dropped connection or a transient status.

    The first 412 is a conflict and is returned as-is. A 412 from a later attempt
    can mean the previous attempt stored the object and the response was lost.
    """
    last_status = None
    for attempt in range(TRANSIENT_ATTEMPTS):
        try:
            status, headers, payload = transmit_once(
                method, endpoint, region, bucket, key, query, extra_headers, body_path,
            )
        except (OSError, BadStatusLine, IncompleteRead) as error:
            if isinstance(error, OSError) and not _retryable_oserror(error):
                raise
            if attempt + 1 == TRANSIENT_ATTEMPTS:
                raise
            _count_retry()
            _pause(attempt)
            continue
        if status in _TRANSIENT_STATUS and attempt + 1 < TRANSIENT_ATTEMPTS:
            _count_retry()
            _pause(attempt)
            last_status = (status, headers, payload)
            continue
        if method == 'PUT' and status == 412 and attempt > 0 and body_path:
            confirmed = _confirm_stored_put(endpoint, region, bucket, key, body_path)
            if confirmed is not None:
                return confirmed
        return status, headers, payload
    if last_status is not None:
        return last_status
    raise S3Error('request failed before a response')


def error_text(payload):
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return 'unreadable S3 error response'
    code = message = ''
    for element in root.iter():
        tag = element.tag.rsplit('}', 1)[-1]
        if tag == 'Code' and element.text:
            code = element.text.strip()
        elif tag == 'Message' and element.text:
            message = element.text.strip()
    detail = ' '.join(part for part in (code, message) if part)
    return detail or 'S3 request failed'


def local_name(tag):
    return tag.rsplit('}', 1)[-1]


def parse_list(payload):
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise S3Error('list response was not XML') from error
    contents = []
    common_prefixes = []
    values = {}
    for element in list(root):
        name = local_name(element.tag)
        if name == 'Contents':
            key = None
            for child in element:
                if local_name(child.tag) == 'Key' and child.text:
                    key = child.text
            if not key:
                raise S3Error('list response contains an object without a key')
            contents.append({'Key': key})
        elif name == 'CommonPrefixes':
            prefix = None
            for child in element:
                if local_name(child.tag) == 'Prefix' and child.text:
                    prefix = child.text
            if not prefix:
                raise S3Error('list response contains an invalid common prefix')
            common_prefixes.append({'Prefix': prefix})
        elif element.text is not None and name in ('KeyCount', 'IsTruncated', 'NextContinuationToken'):
            values[name] = element.text.strip()
    if 'IsTruncated' not in values or values['IsTruncated'] not in ('true', 'false'):
        raise S3Error('list response is missing IsTruncated')
    truncated = values['IsTruncated'] == 'true'
    if 'KeyCount' not in values:
        raise S3Error('list response is missing KeyCount')
    try:
        key_count = int(values['KeyCount'])
    except ValueError as error:
        raise S3Error('list response KeyCount is not an integer') from error
    if key_count != len(contents):
        raise S3Error('list response KeyCount does not match Contents')
    page = {'KeyCount': key_count, 'IsTruncated': truncated, 'Contents': contents}
    if common_prefixes:
        page['CommonPrefixes'] = common_prefixes
    if truncated:
        token = values.get('NextContinuationToken', '')
        if not token:
            raise S3Error('truncated listing without a continuation token')
        page['NextContinuationToken'] = token
    return page


def put_request(flags, bools, positionals):
    if positionals:
        raise S3Error('refusing put-object with unexpected arguments')
    if '--if-none-match' not in flags:
        raise S3Error('refusing put-object without --if-none-match')
    if flags.get('--if-none-match') != '*':
        raise S3Error('refusing put-object unless --if-none-match is *')
    if '--content-md5' not in flags:
        raise S3Error('refusing put-object without --content-md5')
    body = require(flags, '--body')
    if not Path(body).is_file():
        raise S3Error('put-object body is not a file')
    return {
        'bucket': require(flags, '--bucket'),
        'key': require(flags, '--key'),
        'endpoint': require(flags, '--endpoint-url'),
        'region': require(flags, '--region'),
        'body': body,
        'content_md5': flags['--content-md5'],
        'if_none_match': '*',
    }


def get_request(flags, bools, positionals):
    if len(positionals) != 1:
        raise S3Error('get-object requires the output file')
    return {
        'bucket': require(flags, '--bucket'),
        'key': require(flags, '--key'),
        'endpoint': require(flags, '--endpoint-url'),
        'region': require(flags, '--region'),
        'outfile': positionals[0],
    }


def list_request(flags, bools, positionals):
    if positionals:
        raise S3Error('refusing list-objects-v2 with unexpected arguments')
    if '--no-paginate' not in bools:
        raise S3Error('refusing list-objects-v2 without --no-paginate')
    query = {'list-type': '2'}
    if '--prefix' in flags:
        query['prefix'] = flags['--prefix']
    if '--max-keys' in flags:
        query['max-keys'] = flags['--max-keys']
    if '--continuation-token' in flags:
        query['continuation-token'] = flags['--continuation-token']
    return {
        'bucket': require(flags, '--bucket'),
        'endpoint': require(flags, '--endpoint-url'),
        'region': require(flags, '--region'),
        'query': query,
    }


def put_headers(job):
    return {
        'content-md5': job['content_md5'],
        'content-type': 'application/octet-stream',
        'if-none-match': job['if_none_match'],
    }


def put_object(flags, bools, positionals):
    job = put_request(flags, bools, positionals)
    status, _headers, payload = transmit(
        'PUT', job['endpoint'], job['region'], job['bucket'], job['key'], {}, put_headers(job), job['body'],
    )
    if status != 200:
        raise S3Error(f'put-object failed with HTTP {status}: {error_text(payload)}')
    return 0


def get_object(flags, bools, positionals):
    job = get_request(flags, bools, positionals)
    status, headers, payload = transmit(
        'GET', job['endpoint'], job['region'], job['bucket'], job['key'], {}, {}, None,
    )
    if status != 200:
        raise S3Error(f'get-object failed with HTTP {status}: {error_text(payload)}')
    outfile = Path(job['outfile'])
    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_bytes(payload)
    sys.stdout.write(json.dumps({'ContentLength': len(payload), 'ETag': headers.get('etag', '')}) + '\n')
    return 0


def list_objects(flags, bools, positionals):
    spec = list_request(flags, bools, positionals)
    status, _headers, payload = transmit(
        'GET', spec['endpoint'], spec['region'], spec['bucket'], '', spec['query'], {}, None,
    )
    if status != 200:
        raise S3Error(f'list-objects-v2 failed with HTTP {status}: {error_text(payload)}')
    sys.stdout.write(json.dumps(parse_list(payload)) + '\n')
    return 0


def checked_operation(method, endpoint, region, bucket, key, query, headers, body):
    """Pool transfer. Raises S3Error on a non-200 response and never prints credentials."""
    if method == 'PUT':
        what = 'put-object'
    elif query:
        what = 'list-objects-v2'
    else:
        what = 'get-object'
    status, response_headers, payload = transmit(method, endpoint, region, bucket, key, query, headers, body)
    if status != 200:
        raise S3Error(f'{what} failed with HTTP {status}: {error_text(payload)}')
    return response_headers, payload


def handle(argv):
    operation = argv[1]
    flags, bools, positionals = parse_args(argv[2:])
    if operation == 'put-object':
        return put_object(flags, bools, positionals)
    if operation == 'get-object':
        return get_object(flags, bools, positionals)
    return list_objects(flags, bools, positionals)


def real_aws_path():
    wrapper = str(Path(__file__).resolve().parent)
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        if not directory or os.path.realpath(directory) == os.path.realpath(wrapper):
            continue
        candidate = Path(directory) / 'aws'
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise S3Error('real aws CLI not found on PATH')


def fallthrough(argv):
    real = real_aws_path()
    os.execv(real, [real, *argv])


def pool_directory():
    value = os.environ.get('SCIMAP_PUBLISH_POOL_DIR', '').strip()
    return Path(value) if value else None


def _read_line(conn, limit=8_000_000):
    chunks = []
    size = 0
    while True:
        try:
            data = conn.recv(65536)
        except socket.timeout as error:
            raise S3Error('publish pool timed out') from error
        if not data:
            break
        chunks.append(data)
        size += len(data)
        if b'\n' in data:
            break
        if size > limit:
            raise S3Error('pool message is too large')
    if not chunks:
        raise S3Error('pool connection closed')
    line, _separator, _rest = b''.join(chunks).partition(b'\n')
    try:
        message = json.loads(line)
    except json.JSONDecodeError as error:
        raise S3Error('pool message was not JSON') from error
    if not isinstance(message, dict):
        raise S3Error('pool message was not an object')
    return message


def _write_line(conn, message):
    conn.sendall(json.dumps(message, separators=(',', ':')).encode('utf-8') + b'\n')


def rpc(port, message, timeout):
    try:
        sock = socket.create_connection(('127.0.0.1', port), timeout=10)
    except OSError as error:
        raise S3Error('publish pool is not reachable') from error
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(timeout)
        _write_line(sock, message)
        return _read_line(sock)
    except OSError as error:
        raise S3Error('publish pool connection failed') from error
    finally:
        sock.close()


def _port_from(pool_dir):
    try:
        return int((Path(pool_dir) / 'port').read_text().strip())
    except (OSError, ValueError):
        return None


def _ping(port):
    try:
        return rpc(port, {'op': 'ping'}, timeout=2).get('ok') is True
    except S3Error:
        return False


def ensure_server(pool_dir):
    """Start the single local pool process. Later calls attach to it."""
    pool_dir = Path(pool_dir)
    pool_dir.mkdir(parents=True, exist_ok=True)
    with open(pool_dir / 'lock', 'a+') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        port = _port_from(pool_dir)
        if port is not None and _ping(port):
            return port
        try:
            (pool_dir / 'port').unlink()
        except OSError:
            pass
        log_path = pool_dir / 'pool.log'
        log_handle = open(log_path, 'ab', buffering=0)
        try:
            child_env = os.environ.copy()
            child_env['PYTHONUNBUFFERED'] = '1'
            proc = subprocess.Popen(
                [sys.executable, '-S', str(Path(__file__).resolve()), '--serve', str(pool_dir)],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=child_env,
            )
        finally:
            log_handle.close()
        try:
            status = proc.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            raise S3Error('publish pool did not detach') from error
        if status != 0:
            detail = log_path.read_text(errors='replace')[-500:]
            raise S3Error(f'publish pool exited during start: {detail}')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            port = _port_from(pool_dir)
            if port is not None and _ping(port):
                return port
            time.sleep(0.02)
        detail = log_path.read_text(errors='replace')[-500:]
        raise S3Error(f'publish pool did not start: {detail}')


def dispatch_argv(pool, argv):
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise S3Error('pool request arguments must be strings')
    if not should_handle(argv):
        raise S3Error('pool request is not a handled s3api call')
    flags, bools, positionals = parse_args(argv[2:])
    operation = argv[1]
    if operation == 'put-object':
        kind = pool.put(put_request(flags, bools, positionals))
        stats = pool.stats()
        payload = {'ok': True, 'accepted': stats['accepted'], 'retries': retry_count()}
        if kind == 'manifest':
            payload['note'] = (
                f'publish pool concurrency={stats["concurrency"]} uploaded={stats["uploaded"]} '
                f'retries={retry_count()}'
            )
        return payload
    if operation == 'get-object':
        return {'ok': True, 'json': pool.read_object(get_request(flags, bools, positionals))}
    spec = list_request(flags, bools, positionals)
    pool.wait_for_puts()
    _headers, payload = pool.call(
        'GET', spec['endpoint'], spec['region'], spec['bucket'], '', spec['query'], {}, None,
    )
    stats = pool.stats()
    return {
        'ok': True,
        'json': parse_list(payload),
        'note': (
            f'publish pool concurrency={stats["concurrency"]} uploaded={stats["uploaded"]} '
            f'retries={retry_count()}'
        ),
    }


def _idle_limit():
    try:
        limit = int(os.environ.get('SCIMAP_PUBLISH_POOL_IDLE', '3600'))
    except ValueError:
        return 3600
    return max(5, limit)


def serve(pool_dir):
    # The process that execs `aws` must be allowed to exit. Fork and leave the
    # pool in its own session, then let that short-lived parent return.
    if os.environ.get('SCIMAP_PUBLISH_POOL_FOREGROUND') != '1':
        if os.fork() > 0:
            os._exit(0)
        os.setsid()
    pool_dir = Path(pool_dir)
    pool_dir.mkdir(parents=True, exist_ok=True)
    try:
        concurrency = concurrency_from_env(os.environ.get('SCIMAP_PUBLISH_CONCURRENCY'))
    except ValueError as error:
        raise S3Error(str(error)) from error
    pool = TransferPool(concurrency, checked_operation)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    idle_limit = _idle_limit()
    try:
        sock.bind(('127.0.0.1', 0))
        sock.listen(256)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        (pool_dir / 'port').write_text(f'{sock.getsockname()[1]}\n')
        (pool_dir / 'pid').write_text(f'{os.getpid()}\n')
        sock.settimeout(30)
        last = time.monotonic()
        while True:
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                if pool.idle() and time.monotonic() - last >= idle_limit:
                    break
                continue
            last = time.monotonic()
            shutdown = False
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                message = _read_line(conn)
                op = message.get('op')
                if op == 'ping':
                    payload = {'ok': True, **pool.stats()}
                elif op == 'shutdown':
                    payload = {'ok': True}
                    shutdown = True
                elif op == 'call':
                    payload = dispatch_argv(pool, message.get('argv'))
                else:
                    raise S3Error('unknown pool request')
            except (S3Error, PoolError) as error:
                payload = {'ok': False, 'error': str(error)}
            except Exception as error:
                detail = f'{type(error).__name__}: {error}'
                payload = {'ok': False, 'error': detail[:300]}
                print(f'publish pool failed: {detail[:300]}', file=sys.stderr)
            try:
                _write_line(conn, payload)
            except OSError:
                pass
            finally:
                conn.close()
            if shutdown:
                break
    finally:
        pool.close(wait=False)
        try:
            (pool_dir / 'port').unlink()
        except OSError:
            pass
        sock.close()
    return 0


def shutdown_pool(pool_dir):
    port = _port_from(pool_dir)
    if port is None:
        return 0
    try:
        response = rpc(port, {'op': 'shutdown'}, timeout=150)
    except S3Error:
        return 0
    return 0 if response.get('ok') else 1


def pooled_main(argv):
    port = ensure_server(pool_directory())
    response = rpc(port, {'op': 'call', 'argv': list(map(str, argv))}, timeout=None)
    if not response.get('ok'):
        raise S3Error(response.get('error') or 'publish pool request failed')
    if 'json' in response:
        sys.stdout.write(json.dumps(response['json']) + '\n')
    note = response.get('note')
    if isinstance(note, str) and note:
        print(note, file=sys.stderr, flush=True)
    accepted = response.get('accepted')
    retries = response.get('retries')
    if isinstance(accepted, int) and accepted and accepted % 1000 == 0 and not note:
        retry_note = f' retries={retries}' if isinstance(retries, int) and retries else ''
        print(f'publish pool accepted={accepted}{retry_note}', file=sys.stderr, flush=True)
    return 0


def main(argv):
    try:
        if argv[:1] == ['--serve'] and len(argv) == 2:
            return serve(argv[1])
        if argv[:1] == ['--shutdown'] and len(argv) == 2:
            return shutdown_pool(argv[1])
        if should_handle(argv) and pool_directory() is not None:
            return pooled_main(argv)
        if should_handle(argv):
            return handle(argv)
        fallthrough(argv)
    except (S3Error, PoolError) as error:
        print(f'r2 s3api: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
