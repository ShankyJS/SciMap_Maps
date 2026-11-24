#!/usr/bin/env python3
"""Stdlib S3 client for the pinned termination publisher.

publish_termination_release.py at the pinned website revision shells out to
`aws s3api` once per object. Starting AWS CLI v2 that many times exhausts a
GitHub-hosted runner. This process speaks the same flags and refuses any put
that omits --if-none-match or --content-md5, and any list that omits
--no-paginate. Credentials and the endpoint come from the publisher's own
command line and environment. They are never printed.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlsplit

HANDLED = {'put-object', 'get-object', 'list-objects-v2'}
VALUE_FLAGS = {
    '--bucket', '--key', '--body', '--content-md5', '--if-none-match',
    '--endpoint-url', '--region', '--output', '--prefix', '--max-keys',
    '--continuation-token',
}
BOOL_FLAGS = {'--no-paginate', '--no-cli-pager', '--no-cli-auto-prompt'}
EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'


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


def transmit(method, endpoint, region, bucket, key, query, extra_headers, body_path):
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


def put_object(flags, bools, positionals):
    if positionals:
        raise S3Error('refusing put-object with unexpected arguments')
    if '--if-none-match' not in flags:
        raise S3Error('refusing put-object without --if-none-match')
    if '--content-md5' not in flags:
        raise S3Error('refusing put-object without --content-md5')
    bucket = require(flags, '--bucket')
    key = require(flags, '--key')
    body = require(flags, '--body')
    endpoint = require(flags, '--endpoint-url')
    region = require(flags, '--region')
    if not Path(body).is_file():
        raise S3Error('put-object body is not a file')
    status, _headers, payload = transmit(
        'PUT', endpoint, region, bucket, key, {},
        {
            'content-md5': flags['--content-md5'],
            'content-type': 'application/octet-stream',
            'if-none-match': flags['--if-none-match'],
        },
        body,
    )
    if status != 200:
        raise S3Error(f'put-object failed with HTTP {status}: {error_text(payload)}')
    return 0


def get_object(flags, bools, positionals):
    if len(positionals) != 1:
        raise S3Error('get-object requires the output file')
    bucket = require(flags, '--bucket')
    key = require(flags, '--key')
    endpoint = require(flags, '--endpoint-url')
    region = require(flags, '--region')
    outfile = Path(positionals[0])
    status, headers, payload = transmit('GET', endpoint, region, bucket, key, {}, {}, None)
    if status != 200:
        raise S3Error(f'get-object failed with HTTP {status}: {error_text(payload)}')
    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_bytes(payload)
    sys.stdout.write(json.dumps({'ContentLength': len(payload), 'ETag': headers.get('etag', '')}) + '\n')
    return 0


def list_objects(flags, bools, positionals):
    if positionals:
        raise S3Error('refusing list-objects-v2 with unexpected arguments')
    if '--no-paginate' not in bools:
        raise S3Error('refusing list-objects-v2 without --no-paginate')
    bucket = require(flags, '--bucket')
    endpoint = require(flags, '--endpoint-url')
    region = require(flags, '--region')
    query = {'list-type': '2'}
    if '--prefix' in flags:
        query['prefix'] = flags['--prefix']
    if '--max-keys' in flags:
        query['max-keys'] = flags['--max-keys']
    if '--continuation-token' in flags:
        query['continuation-token'] = flags['--continuation-token']
    status, _headers, payload = transmit('GET', endpoint, region, bucket, '', query, {}, None)
    if status != 200:
        raise S3Error(f'list-objects-v2 failed with HTTP {status}: {error_text(payload)}')
    sys.stdout.write(json.dumps(parse_list(payload)) + '\n')
    return 0


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


def main(argv):
    try:
        if should_handle(argv):
            return handle(argv)
        fallthrough(argv)
    except S3Error as error:
        print(f'r2 s3api: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
