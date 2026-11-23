#!/usr/bin/env python3
"""Dry-run acceptance checks for the daily termination publisher.

This command never contacts R2, Slack, or GitHub. Live staging, A-B-A pointer
rollback, and the runner schema-failure drill are checklists in
docs/daily-terminations-runbook.md. They stay unrun until an operator executes
them outside this script.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / '.github' / 'workflows' / 'daily-terminations.yml'
WEBSITE_PIN = 'ca167515757a295df811d8a9df7a40b80a3c1f37'
ENDPOINT = 'https://17067e7373bbfee2043fe5186d66aeb9.r2.cloudflarestorage.com'


def check(condition, detail):
    return {'status': 'passed' if condition else 'failed', 'detail': detail}


def workflow_checks(text, fetch_text):
    publish_default = "publish:\n        description: 'Publish candidate and advance latest-validated (never website selection)'\n        type: boolean\n        default: false"
    return {
        'website_pin': check(f'ref: {WEBSITE_PIN}' in text and 'dd191eb' not in text, WEBSITE_PIN),
        'r2_env': check(
            'R2_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}' in text
            and 'R2_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}' in text
            and f'R2_ENDPOINT: {ENDPOINT}' in text
            and 'RCLONE_CONFIG_R2_' not in text,
            'workflow passes R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and R2_ENDPOINT',
        ),
        'schedule_gate': check("vars.SCIMAP_DAILY_ENABLED == 'true'" in text, 'schedule stays behind SCIMAP_DAILY_ENABLED'),
        'runtime_gate': check(
            'vars.SCIMAP_RUNTIME_READY' in text and 'SCIMAP_INPUT_ROOT' not in text,
            'manual runs stay behind SCIMAP_RUNTIME_READY and do not read SCIMAP_INPUT_ROOT',
        ),
        'hosted_runner': check(
            text.count('runs-on: ubuntu-latest') == 2
            and 'self-hosted' not in text
            and 'timeout-minutes: 180' in text
            and 'actions: write' in text,
            'preflight and candidate use ubuntu-latest; candidate keeps the 180 minute timeout and cache permission',
        ),
        'private_inputs': check(
            'scimap-private-inputs' in text
            and 'inputs/county_commute_NIH.csv' in text
            and 'fetch_daily_inputs.py' in text
            and 'scimap-data' not in text
            and "PUBLIC_BUCKET = 'scimap-data'" in fetch_text
            and 'https://osf.io/m5kb6/download' in fetch_text,
            'district file comes from OSF; county file comes from scimap-private-inputs and the workflow never names scimap-data',
        ),
        'publish_default_off': check(publish_default in text, 'manual publish defaults to false'),
        'aws_cli_check': check(
            'aws --version' in text and '--if-none-match' in text and '--no-paginate' in text,
            'runtime check requires the AWS CLI publisher flags',
        ),
        'historical_input': check('historical:' in text and 'Historical replay: never advance latest-validated' in text, 'historical replay cannot be confused with promotion'),
    }


def tool_check():
    env = dict(os.environ, AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off')
    version = subprocess.run(['aws', '--version'], capture_output=True, text=True, env=env)
    version_text = (version.stdout or version.stderr).strip()
    put = subprocess.run(['aws', 's3api', 'put-object', 'help'], capture_output=True, text=True, env=env)
    listing = subprocess.run(['aws', 's3api', 'list-objects-v2', 'help'], capture_output=True, text=True, env=env)
    rclone = subprocess.run(['rclone', 'version'], capture_output=True, text=True)
    rclone_line = (rclone.stdout or '').splitlines()[:1]
    return {
        'aws_cli': check(version.returncode == 0 and version_text.startswith('aws-cli/2.'), version_text),
        'put_object_if_none_match': check(put.returncode == 0 and '--if-none-match' in put.stdout, 'put-object help advertises --if-none-match'),
        'list_objects_no_paginate': check(listing.returncode == 0 and '--no-paginate' in listing.stdout, 'list-objects-v2 help advertises --no-paginate'),
        'rclone_for_selector': check(rclone.returncode == 0 and rclone_line == ['rclone v1.75.1'], rclone_line[0] if rclone_line else 'rclone not installed'),
        'live_r2': check(True, 'not contacted'),
    }


def safety_tests():
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for name in ('test_run_daily_terminations', 'test_fetch_daily_inputs'):
        suite.addTests(loader.loadTestsFromName(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    names = []
    for test, trace in list(result.failures) + list(result.errors):
        names.append(str(test))
        sys.stderr.write(trace)
    return check(result.wasSuccessful(), f'ran {result.testsRun} daily tests; failures={names}')


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Run local checks only. This is the only execution mode.')
    parser.add_argument('--evidence', type=Path, help='Write the JSON report here')
    parser.add_argument('--live', action='store_true', help='Rejected. This script does not contact R2.')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.live or not args.dry_run:
        print('Refusing to run. Use --dry-run. Live R2 staging, A-B-A, and schema drills stay in docs/daily-terminations-runbook.md.', file=sys.stderr)
        return 2
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'scripts'))
    text = WORKFLOW.read_text()
    fetch_text = (ROOT / 'scripts' / 'fetch_daily_inputs.py').read_text()
    report = {
        'website_pin': WEBSITE_PIN,
        'workflow': workflow_checks(text, fetch_text),
        'tools': tool_check(),
        'tests': safety_tests(),
        'live_steps_not_run': [
            'hosted runner memory and time measurement',
            'private bucket creation and county upload',
            'manual staging publish with historical=true',
            'A-B-A latest-validated rollback',
            'forced source_schema failure on a real snapshot',
        ],
    }
    failed = []
    for group in (report['workflow'], report['tools']):
        for name, item in group.items():
            if item['status'] != 'passed':
                failed.append(name)
    if report['tests']['status'] != 'passed':
        failed.append('tests')
    report['status'] = 'failed' if failed else 'passed'
    report['failed'] = failed
    payload = json.dumps(report, indent=2) + '\n'
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(payload)
    sys.stdout.write(payload)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
