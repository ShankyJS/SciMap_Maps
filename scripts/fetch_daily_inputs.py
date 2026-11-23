#!/usr/bin/env python3
"""Fetch the two approved commuter inputs and verify the locked checksums.

home_119_workTR.csv comes from its public OSF URL. county_commute_NIH.csv
comes from a private R2 bucket. This command never reads or writes the public
scimap-data bucket. A missing secret or a checksum mismatch leaves no input
file in place and does not touch release pointers.
"""
import argparse
import csv
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / 'external-inputs.lock.csv'
OSF_DISTRICT_URL = 'https://osf.io/m5kb6/download'
PUBLIC_BUCKET = 'scimap-data'
DEFAULT_BUCKET = 'scimap-private-inputs'
DEFAULT_KEY = 'inputs/county_commute_NIH.csv'
DISTRICT_NAME = 'home_119_workTR.csv'
COUNTY_NAME = 'county_commute_NIH.csv'


def read_lock(path):
    rows = {}
    with Path(path).open(newline='') as handle:
        for row in csv.DictReader(handle):
            rows[row['Input']] = (int(row['Bytes']), row['SHA256'])
    for name in (DISTRICT_NAME, COUNTY_NAME):
        if name not in rows:
            raise RuntimeError(f'Lock file has no row for {name}')
    return rows


def require_private_credentials():
    missing = [name for name in ('R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_ENDPOINT') if not os.environ.get(name, '').strip()]
    if missing:
        raise RuntimeError(
            'Missing private input credentials: ' + ', '.join(missing)
            + '. county_commute_NIH.csv stays in the private bucket and was not fetched. '
            + 'latest-validated and website-release were not changed.'
        )


def sha256(path):
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def verify(path, expected_bytes, expected_sha):
    size, actual = sha256(path)
    if size != expected_bytes or actual != expected_sha:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f'Checksum mismatch for {path.name}: expected {expected_bytes} bytes {expected_sha}, '
            f'got {size} bytes {actual}. Pointers were not changed.'
        )


def download_osf(url, dest, opener):
    request = urllib.request.Request(url, headers={'User-Agent': 'scimap-daily-inputs'})
    with opener(request, timeout=300) as response:
        dest.write_bytes(response.read())


def download_private(bucket, key, dest, runner):
    if bucket == PUBLIC_BUCKET:
        raise RuntimeError(
            'Refusing to read county_commute_NIH.csv from public bucket scimap-data. '
            'Pointers were not changed.'
        )
    access = os.environ['R2_ACCESS_KEY_ID'].strip()
    secret = os.environ['R2_SECRET_ACCESS_KEY'].strip()
    endpoint = os.environ['R2_ENDPOINT'].strip()
    env = os.environ.copy()
    env['AWS_ACCESS_KEY_ID'] = access
    env['AWS_SECRET_ACCESS_KEY'] = secret
    env['AWS_DEFAULT_REGION'] = 'auto'
    env['AWS_REGION'] = 'auto'
    env['AWS_EC2_METADATA_DISABLED'] = 'true'
    env['AWS_PAGER'] = ''
    # The outfile is last. Flags after it are not accepted by the AWS CLI.
    result = runner(
        ['aws', 's3api', 'get-object', '--bucket', bucket, '--key', key,
         '--endpoint-url', endpoint, '--region', 'auto', '--output', 'json', '--no-cli-pager',
         str(dest)],
        env=env, capture_output=True, text=True,
    )
    if result.returncode:
        dest.unlink(missing_ok=True)
        detail = (result.stderr or '').strip().splitlines()
        tail = detail[-1][:300] if detail else f'exit {result.returncode}'
        raise RuntimeError(f'Private county input missing or unreadable ({tail}). Pointers were not changed.')


def fetch(output, lock_path, bucket, key, url, opener=urllib.request.urlopen, runner=subprocess.run):
    require_private_credentials()
    if bucket == PUBLIC_BUCKET:
        raise RuntimeError(
            'Refusing to read county_commute_NIH.csv from public bucket scimap-data. '
            'Pointers were not changed.'
        )
    locked = read_lock(lock_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    district = output / DISTRICT_NAME
    county = output / COUNTY_NAME
    download_osf(url, district, opener)
    verify(district, *locked[DISTRICT_NAME])
    download_private(bucket, key, county, runner)
    verify(county, *locked[COUNTY_NAME])
    return district, county


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='Directory for the two verified CSV files')
    parser.add_argument('--error-log', help='Write the failure text here for the workflow alert')
    parser.add_argument('--lock', default=LOCK)
    parser.add_argument('--bucket', default=os.environ.get('SCIMAP_PRIVATE_BUCKET', DEFAULT_BUCKET))
    parser.add_argument('--key', default=os.environ.get('SCIMAP_PRIVATE_KEY', DEFAULT_KEY))
    parser.add_argument('--district-url', default=os.environ.get('SCIMAP_OSF_DISTRICT_URL', OSF_DISTRICT_URL))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        fetch(args.output, args.lock, args.bucket, args.key, args.district_url)
    except Exception as error:
        message = str(error) or type(error).__name__
        if args.error_log:
            Path(args.error_log).write_text(message + '\n')
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
