#!/usr/bin/env python3
"""Capture, validate, build and optionally publish one daily termination release.

Default: local artifacts only. Publication updates latest-validated only, never
website-release. Historical replays publish immutable files without pointers.
Each stage has a retained log and failures stop all downstream work.
"""
import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import alerts

ROOT = Path(__file__).resolve().parent.parent


def child_environment(publish=False):
    env = dict(os.environ)
    for name in list(env):
        if name == alerts.WEBHOOK_ENV or (not publish and name.startswith(('RCLONE_', 'AWS_'))):
            del env[name]
    return env


def execute(command, log, publish=False):
    # Line streaming keeps detailed institution diagnostics in the stage log.
    with log.open('w') as output:
        result = subprocess.run(list(map(str, command)), stdout=output, stderr=subprocess.STDOUT, env=child_environment(publish))
    if result.returncode:
        detail = log.read_text(errors='replace')[-1500:]
        raise RuntimeError(f'Exit {result.returncode}; see {log.name}\n{detail}')


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
            step('publish', [args.release_python, scimap / 'scripts/publish_termination_release.py', release,
                 '--record', upload_record], publish=True)
            if json.loads(upload_record.read_text()).get('status') != 'published':
                raise RuntimeError('Publication not confirmed; pointers preserved')
            if not args.historical:
                step('latest', [args.release_python, scimap / 'scripts/select_termination_release.py', '--upload-record', upload_record,
                     '--pointer', 'latest-validated', '--apply', '--record', work / 'selection-record.json'], publish=True)
        report['status'] = 'passed'
        outcome = 'Candidate published' if args.publish else 'Local candidate built; nothing uploaded'
        url = alerts.ci_run_url()
        alerts.send(webhook, '\n'.join(filter(None, [
            f'{outcome}: {alerts.escape(release_id)}',
            f'Report: {alerts.escape(str(report_path))}',
            'Website selection was not changed.',
            f'<{url}|Open run and reports>' if url else '',
        ])))
    except BaseException as error:
        report['status'] = 'failed'
        report['failed_stage'] = stage
        report['error'] = alerts.failure_reason(error)
        if report['stages']:
            report['stages'][-1]['status'] = 'failed'
        # A publisher may have completed before a later pointer failure. Do not claim otherwise.
        alerts.send(webhook, alerts.message('error', f'Termination daily run failed at {stage}',
                    {'release': release_id, 'report': str(report_path), 'stage': stage},
                    [alerts.code_block(alerts.failure_reason(error)), 'Website selection was not changed. Inspect stage logs before retrying with a new release ID.']))
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
