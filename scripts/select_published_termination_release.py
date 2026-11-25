#!/usr/bin/env python3
"""Run the pinned website selector with an upload receipt and an AWS CLI transport."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import alerts

RECORDS_BUCKET = 'scimap-private-inputs'
PUBLIC_BUCKET = 'scimap-data'
POINTERS = ('website-release', 'latest-validated')


def upload_record_key(release_id):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', release_id) or release_id in ('.', '..'):
        raise ValueError('Invalid release ID: use the published release folder name')
    return f'records/releases/{release_id}/upload-record.json'


class MissingObject(RuntimeError):
    pass


def aws_s3(operation, bucket, *flags):
    access, secret, endpoint = (os.environ.get(name, '').strip() for name in (
        'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_ENDPOINT'))
    if not all((access, secret, endpoint)):
        raise RuntimeError('R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_ENDPOINT are required')
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(('AWS_', 'R2_', 'RCLONE_')) and name not in (alerts.WEBHOOK_ENV, 'GH_TOKEN')}
    env.update(AWS_ACCESS_KEY_ID=access, AWS_SECRET_ACCESS_KEY=secret, AWS_EC2_METADATA_DISABLED='true',
               AWS_CONFIG_FILE='/dev/null', AWS_SHARED_CREDENTIALS_FILE='/dev/null', AWS_MAX_ATTEMPTS='1')
    result = subprocess.run(['aws', 's3api', operation, '--bucket', bucket, '--endpoint-url', endpoint,
                             '--region', 'auto', '--output', 'json', '--no-cli-pager', *map(str, flags)],
                            capture_output=True, text=True, env=env)
    if result.returncode:
        detail = result.stderr.strip()
        if operation == 'get-object' and ('(NoSuchKey)' in detail or '(404)' in detail):
            raise MissingObject(f'{bucket}: object not found')
        raise RuntimeError(f'R2 {operation} on {bucket} failed: {detail or result.returncode}')
    return result.stdout


def receipt_for(release_id, legacy_run_id, scratch):
    key = upload_record_key(release_id)
    path = scratch / 'upload-record.json'
    try:
        aws_s3('get-object', RECORDS_BUCKET, '--key', key, path)
    except MissingObject:
        if not legacy_run_id:
            raise RuntimeError(f'No durable receipt for {release_id}; supply legacy_run_id for a pre-existing release') from None
        env = {name: value for name, value in os.environ.items()
               if not name.startswith(('AWS_', 'R2_', 'RCLONE_')) and name != alerts.WEBHOOK_ENV}
        subprocess.run(['gh', 'run', 'download', legacy_run_id, '--repo', os.environ['GITHUB_REPOSITORY'],
                        '--pattern', f'termination-{legacy_run_id}-*', '--dir', str(scratch / 'legacy')],
                       env=env, check=True)
        matches = [item for item in (scratch / 'legacy').rglob('upload-record.json')
                   if json.loads(item.read_text()).get('release_id') == release_id]
        if len(matches) != 1:
            raise RuntimeError(f'Expected one receipt for {release_id} in run {legacy_run_id}; found {len(matches)}')
        path = matches[0]
    if json.loads(path.read_text()).get('release_id') != release_id:
        raise ValueError('Upload receipt release ID does not match the requested release')
    return path


def object_key(remote_path):
    prefix = f'r2:{PUBLIC_BUCKET}/terminations/'
    if not remote_path.startswith(prefix):
        raise ValueError('Unexpected selector destination')
    return remote_path.removeprefix(f'r2:{PUBLIC_BUCKET}/')


def selector_transport(operation, *args):
    """Translate the pinned selector's transport calls without changing its selection logic."""
    if operation == 'lsf' and tuple(args[1:]) == ('--max-depth', '1'):
        prefix = object_key(args[0])
        listing = json.loads(aws_s3('list-objects-v2', PUBLIC_BUCKET, '--prefix', prefix, '--delimiter', '/'))
        return '\n'.join(item['Key'].removeprefix(prefix) for item in listing.get('Contents', []))
    if operation == 'cat' and len(args) == 1:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pointer.json'
            aws_s3('get-object', PUBLIC_BUCKET, '--key', object_key(args[0]), path)
            return path.read_text()
    if operation == 'copyto':
        source, target, *headers = args
        key = object_key(target)
        if not headers and re.fullmatch(r'terminations/selections/[0-9a-f]{32}\.json', key):
            flags = ('--if-none-match', '*')
        elif (headers == ['--header-upload', 'Cache-Control: no-cache', '--header-upload', 'Content-Type: application/json']
              and key in {f'terminations/{pointer}.json' for pointer in POINTERS}):
            flags = ('--cache-control', 'no-cache')
        else:
            raise ValueError('Unexpected selector write')
        # Never retry a pointer PUT whose result may be uncertain.
        return aws_s3('put-object', PUBLIC_BUCKET, '--key', key, '--body', source, '--content-type', 'application/json', *flags)
    raise ValueError(f'Unexpected selector operation: {operation}')


def run(args, webhook):
    context = {'pointer': args.pointer, 'old release': 'unknown', 'new release': args.release_id,
               'approver': args.approved_by or '(not supplied)', 'reason': args.reason or '(not supplied)'}
    title = 'Termination selection failed'
    lines = []
    record = Path(args.output) / 'selection-record.json'
    audit = None
    try:
        if args.pointer == 'website-release' and not (args.approved_by and args.reason):
            raise ValueError('website-release requires non-empty approved_by and reason')
        if args.legacy_run_id and not re.fullmatch(r'[1-9][0-9]*', args.legacy_run_id):
            raise ValueError('legacy_run_id must be a numeric GitHub Actions run ID')
        sys.path.insert(0, str(Path(args.scimap).resolve() / 'scripts'))
        import select_termination_release as selector

        selector.remote = selector_transport
        with tempfile.TemporaryDirectory() as tmp:
            receipt = receipt_for(args.release_id, args.legacy_run_id, Path(tmp))
            flags = [f'--upload-record={receipt}', f'--pointer={args.pointer}',
                     f'--approved-by={args.approved_by}', f'--reason={args.reason}']
            if args.apply:
                record.parent.mkdir(parents=True, exist_ok=False)
                key = f"records/selections/{args.pointer}/{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}.json"
                flags.extend(['--apply', f'--record={record}'])
            try:
                # Apply includes the pinned selector's full object-store and public readback checks.
                audit = selector.select(selector.parse_args(flags))
            finally:
                if args.apply and record.is_file():
                    audit = json.loads(record.read_text())
                    aws_s3('put-object', RECORDS_BUCKET, '--key', key, '--body', record,
                           '--content-type', 'application/json', '--if-none-match', '*')
        title = ('Termination release selected and verified' if args.apply
                 else 'Termination selection preview — no writes')
    except BaseException as error:
        lines.append(alerts.code_block(alerts.failure_reason(error)))
        raise
    finally:
        if audit:
            context.update({'old release': (audit['previous'] or {}).get('release_id', '(unset)'),
                            'selection status': audit['status']})
        sent = alerts.send(webhook, alerts.message('error' if lines else 'info', title, context, lines,
                                                   run_label='Open run and selection record', context_separator='\n'))
        if not sent and not lines:
            raise RuntimeError('Slack notification failed; check the run record before retrying any selection')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('release-id', 'scimap', 'output'):
        parser.add_argument(f'--{name}', required=True)
    parser.add_argument('--pointer', choices=POINTERS, default='website-release')
    for name in ('approved-by', 'reason', 'legacy-run-id'):
        parser.add_argument(f'--{name}', default='', type=str.strip)
    parser.add_argument('--apply', action='store_true')
    return parser.parse_args(argv)


if __name__ == '__main__':
    alerts.raise_on_sigterm()
    run(parse_args(), alerts.webhook_from_env())
