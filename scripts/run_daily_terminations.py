#!/usr/bin/env python3
"""Capture, validate, build and optionally publish one daily termination release.

Default: local artifacts only. Publication updates latest-validated only, never
website-release. Historical replays publish immutable files without pointers.
Each stage has a retained log and failures stop all downstream work.
"""
import argparse
from base64 import b64encode
from datetime import date, datetime, timezone
from hashlib import md5
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import alerts
from select_published_termination_release import RECORDS_BUCKET, upload_record_key

ROOT = Path(__file__).resolve().parent.parent
# Directory whose `aws` command is the stdlib client. Publication children see
# it first on PATH; the pinned publisher still decides what to upload. The
# publish stage points that client at a per-run pool so its one-at-a-time
# puts overlap without editing the pinned publisher.
PUBLISHER_AWS = Path(__file__).resolve().parent / 'publisher_aws'
# Names the AWS CLI publisher reads. RCLONE_CONFIG_R2_* is not an input.
PUBLICATION_NAMES = ('R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_ENDPOINT', 'R2_ACCOUNT_ID')


def publication_settings():
    """Return (missing names, access key, secret, endpoint) for the AWS CLI publisher.

    The endpoint may be R2_ENDPOINT or derived from R2_ACCOUNT_ID, matching
    publish_termination_release.py. Secret values are never included in errors.
    """
    access = os.environ.get('R2_ACCESS_KEY_ID', '').strip()
    secret = os.environ.get('R2_SECRET_ACCESS_KEY', '').strip()
    endpoint = os.environ.get('R2_ENDPOINT', '').strip()
    account = os.environ.get('R2_ACCOUNT_ID', '').strip()
    missing = []
    if not access:
        missing.append('R2_ACCESS_KEY_ID')
    if not secret:
        missing.append('R2_SECRET_ACCESS_KEY')
    if not endpoint and not account:
        missing.append('R2_ENDPOINT or R2_ACCOUNT_ID')
    if not endpoint and account:
        endpoint = f'https://{account}.r2.cloudflarestorage.com'
    return missing, access, secret, endpoint


def require_publication_credentials():
    missing, _, _, _ = publication_settings()
    if missing:
        raise RuntimeError(
            'Missing publication credentials: ' + ', '.join(missing)
            + '. Native R2 publishing needs R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, '
            + 'and R2_ENDPOINT or R2_ACCOUNT_ID. '
            + 'latest-validated and website-release were not changed.'
        )


def child_environment(publish=False):
    """Pass R2 credentials only to publication and selection children.

    Inbound RCLONE_* and AWS_* values are always removed. The pinned selector
    still calls rclone, so a complete R2 credential set is mapped into
    RCLONE_CONFIG_R2_* for those children only.
    """
    env = dict(os.environ)
    for name in list(env):
        if name == alerts.WEBHOOK_ENV or name.startswith(('RCLONE_', 'AWS_')):
            del env[name]
        elif not publish and name in PUBLICATION_NAMES:
            del env[name]
    if not publish:
        return env
    missing, access, secret, endpoint = publication_settings()
    for name in PUBLICATION_NAMES:
        value = os.environ.get(name, '').strip()
        if value:
            env[name] = value
    if missing:
        return env
    env['R2_ENDPOINT'] = endpoint
    env['RCLONE_CONFIG_R2_TYPE'] = 's3'
    env['RCLONE_CONFIG_R2_PROVIDER'] = 'Cloudflare'
    env['RCLONE_CONFIG_R2_ACCESS_KEY_ID'] = access
    env['RCLONE_CONFIG_R2_SECRET_ACCESS_KEY'] = secret
    env['RCLONE_CONFIG_R2_ENDPOINT'] = endpoint
    env['RCLONE_CONFIG_R2_NO_CHECK_BUCKET'] = 'true'
    env['PATH'] = str(PUBLISHER_AWS) + os.pathsep + env.get('PATH', '')
    return env


def publish_pool_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('scimap_publish_pool', PUBLISHER_AWS / 'publish_pool.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publish_concurrency():
    """Worker count for the publish pool. Fail before any upload when it is unusable."""
    try:
        value = publish_pool_module().concurrency_from_env(os.environ.get('SCIMAP_PUBLISH_CONCURRENCY'))
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    return str(value)


def stop_publish_pool(pool_dir):
    """Ask the local pool to exit. A missing pool has nothing to stop."""
    if pool_dir is None or not (Path(pool_dir) / 'port').is_file():
        return
    try:
        subprocess.run(
            [sys.executable, '-S', str(PUBLISHER_AWS / 'r2_s3api.py'), '--shutdown', str(pool_dir)],
            timeout=150,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def note(text):
    """Progress for the job log. Stage files are not uploaded if the runner dies."""
    print(text, file=sys.stderr, flush=True)


def mark_slack_reported():
    """Tell the follow-up job that this step already posted to Slack."""
    path = os.environ.get('GITHUB_OUTPUT', '').strip()
    if not path:
        return
    with open(path, 'a') as handle:
        handle.write('slack_reported=true\n')


def execute(command, log, publish=False):
    # The child log stays on disk for the artifact. A heartbeat also goes to
    # stderr, which GitHub streams, so a lost runner still leaves progress.
    env = child_environment(publish)
    pool_dir = None
    # Only the publisher uses the pool. The selector is also a publication
    # child and must not attach to the upload pool or change its accounting.
    if publish and log.stem == 'publish':
        env['SCIMAP_PUBLISH_CONCURRENCY'] = publish_concurrency()
        pool_dir = log.parent / 'publish-pool'
        pool_dir.mkdir(parents=True, exist_ok=True)
        env['SCIMAP_PUBLISH_POOL_DIR'] = str(pool_dir)
    process = subprocess.Popen(
        list(map(str, command)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    state = {'last': ''}

    def pump():
        with log.open('wb') as output:
            assert process.stdout is not None
            while True:
                raw = process.stdout.readline()
                if not raw:
                    break
                output.write(raw)
                output.flush()
                text = raw.decode('utf-8', 'replace').strip()
                if text:
                    state['last'] = text[-300:]

    thread = threading.Thread(target=pump)
    thread.start()
    started = time.monotonic()
    note(f'stage {log.stem} starting')
    try:
        while True:
            try:
                code = process.wait(timeout=20)
                break
            except subprocess.TimeoutExpired:
                size = log.stat().st_size if log.exists() else 0
                last = f' last={state["last"]}' if state['last'] else ''
                note(f'stage {log.stem} running {int(time.monotonic() - started)}s log_bytes={size}{last}')
    except BaseException:
        if process.poll() is None:
            process.kill()
        if process.stdout:
            process.stdout.close()
        thread.join(timeout=5)
        stop_publish_pool(pool_dir)
        raise
    thread.join()
    if process.stdout:
        process.stdout.close()
    stop_publish_pool(pool_dir)
    if code:
        detail = log.read_text(errors='replace')[-1500:]
        raise RuntimeError(f'Exit {code}; see {log.name}\n{detail}')
    note(f'stage {log.stem} passed')


def run(args, executor=execute):
    date.fromisoformat(args.as_of)
    scimap = Path(args.scimap).resolve()
    for filename in ('build_termination_release.py', 'publish_termination_release.py', 'select_termination_release.py'):
        if not (scimap / 'scripts' / filename).is_file():
            raise ValueError(f'Missing website tooling: {filename}')
    work = Path(args.output).resolve()
    work.mkdir(parents=True, exist_ok=False)
    webhook = alerts.webhook_from_env() if args.notify else None
    release_id = args.release_id or f'{args.as_of}-daily-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}'
    snapshot = Path(args.source_snapshot).resolve() if args.source_snapshot else work / 'snapshot'
    analysis, release = work / 'analysis', work / 'release'
    report = {'status': 'running', 'as_of': args.as_of, 'release_id': release_id,
              'historical': args.historical, 'publish': args.publish, 'stages': [], 'website_selection_changed': False}
    note(f'daily candidate release_id={release_id} publish={args.publish} historical={args.historical}')
    report_path = work / 'daily-report.json'
    stage = 'setup'

    def save():
        report_path.write_text(json.dumps(report, indent=2) + '\n')

    def step(name, command, publish=False):
        nonlocal stage
        stage = name
        item = {'stage': name, 'status': 'running', 'log': f'{name}.log'}
        report['stages'].append(item)
        save()
        executor(command, work / item['log'], publish)
        item['status'] = 'passed'
        save()

    save()
    try:
        if args.publish:
            # Fail before capture, upload, or either pointer write.
            stage = 'publish'
            require_publication_credentials()
            receipt_key = upload_record_key(release_id)
            publish_concurrency()
        if not args.source_snapshot:
            step('capture', [sys.executable, ROOT / 'scripts/capture_termination_sources.py', '--output', snapshot])
        step('science', [sys.executable, ROOT / 'scripts/run_termination_pipeline.py', '--as-of', args.as_of,
             '--source-snapshot', snapshot, '--source-baseline', args.source_baseline,
             '--county-commute', args.county_commute, '--district-commute', args.district_commute,
             '--reference-output', args.reference_output, '--comparison-mode', 'report', '--validation', 'enforce', '--output', analysis])
        scientific = json.loads((analysis / 'run-report.json').read_text())
        if scientific.get('status') != 'passed' or scientific.get('validation', {}).get('mode') != 'enforce' or scientific['validation'].get('status') != 'passed':
            raise RuntimeError('Scientific run did not pass enforced validation')
        alerts.send(webhook, alerts.validation_alert(scientific['validation'], {'release': release_id}, blocked=False))
        step('build', [args.release_python, scimap / 'scripts/build_termination_release.py', '--analysis-dir', analysis,
             '--release-id', release_id, '--output', release])
        manifest = json.loads((release / 'manifest.json').read_text())
        warnings = manifest.get('release_validation', {}).get('warnings', [])
        if warnings:
            alerts.send(webhook, alerts.message('warning', 'Termination release coverage warnings', {'release': release_id}, warnings))
        if args.publish:
            upload_record = work / 'upload-record.json'
            # Flags match publish_termination_release.py at the pinned website
            # revision: positional release directory, --record, and R2_* in the
            # environment. Defaults keep the r2:scimap-data remote and
            # terminations/releases prefix. The publisher does not read rclone.
            step('publish', [args.release_python, scimap / 'scripts/publish_termination_release.py', release,
                 '--record', upload_record], publish=True)
            receipt = upload_record.read_bytes()
            record = json.loads(receipt)
            if record.get('status') != 'published' or record.get('release_id') != release_id:
                raise RuntimeError('Publication not confirmed for the requested release; pointers preserved')
            _, _, _, endpoint = publication_settings()
            step('retain-upload-record', ['aws', 's3api', 'put-object', '--bucket', RECORDS_BUCKET,
                 '--key', receipt_key, '--body', upload_record, '--if-none-match', '*',
                 '--content-md5', b64encode(md5(receipt, usedforsecurity=False).digest()).decode(),
                 '--endpoint-url', endpoint, '--region', 'auto'], publish=True)
            if not args.historical:
                step('latest', [args.release_python, scimap / 'scripts/select_termination_release.py', '--upload-record', upload_record,
                     '--pointer', 'latest-validated', '--apply', '--record', work / 'selection-record.json'], publish=True)
        report['status'] = 'passed'
        outcome = 'Candidate published' if args.publish else 'Local candidate built; nothing uploaded'
        url = alerts.ci_run_url()
        if alerts.send(webhook, '\n'.join(filter(None, [
            f'{outcome}: {alerts.escape(release_id)}',
            f'Report: {alerts.escape(str(report_path))}',
            'Website selection was not changed.',
            f'<{url}|Open run and reports>' if url else '',
        ]))):
            mark_slack_reported()
    except BaseException as error:
        report['status'] = 'failed'
        report['failed_stage'] = stage
        report['error'] = alerts.failure_reason(error)
        if report['stages']:
            report['stages'][-1]['status'] = 'failed'
        # A publisher may have completed before a later pointer failure. Do not claim otherwise.
        if alerts.send(webhook, alerts.message('error', f'Termination daily run failed at {stage}',
                    {'release': release_id, 'report': str(report_path), 'stage': stage},
                    [alerts.code_block(alerts.failure_reason(error)), 'Website selection was not changed. Inspect stage logs before retrying with a new release ID.'])):
            mark_slack_reported()
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
    return report


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--as-of', default=date.today().isoformat(), help='ISO cutoff; workflow passes an explicit UTC date')
    for name in ('scimap', 'output', 'county-commute', 'district-commute', 'reference-output', 'source-baseline'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--source-snapshot', help='Saved inputs; skip live capture')
    p.add_argument('--release-id')
    p.add_argument('--release-python', default=sys.executable)
    p.add_argument('--publish', action='store_true')
    p.add_argument('--historical', action='store_true', help='Never advance latest-validated')
    p.add_argument('--notify', action='store_true')
    return p.parse_args(argv)


if __name__ == '__main__':
    alerts.raise_on_sigterm()
    run(parse_args())
