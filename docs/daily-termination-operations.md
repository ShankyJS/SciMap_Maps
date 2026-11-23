# Daily termination candidate operations

Secrets, manual runs, staging publication, pointer rollback, failure alerts, and
week replay are in [daily-terminations-runbook.md](daily-terminations-runbook.md).

`run_daily_terminations.py` connects source capture → enforced scientific validation
→ complete release generation → optional immutable publication → latest-validated
selection. Default execution is local only. `--publish` enables publication.
`--historical` always suppresses latest selection. No daily command writes
`website-release.json`; exact dataset approval and website selection remain separate.

Every stage has a log and `daily-report.json`. Failure blocks downstream stages.
With `--notify`, the orchestrator posts actionable failures, scientific warnings
and release-geometry warnings plus completion summaries to the existing #scimap-alerts webhook. It includes
stage/run/report context and the end of the failing log, including institution
names. It strips R2 credentials, derived rclone settings, and AWS credentials
from capture/science/build child environments; only publication/selection children
inherit them. Webhook values are never printed
or passed to children. SIGTERM becomes an interruption and retains a failed report.
A hard kill cannot guarantee a Slack notification; check Actions run failure alerts
as the operational backstop. Stage logs are internal artifacts, not public uploads.

## Local execution

From the analysis feature checkout, with the pinned R environment and the website
Python/geospatial/Tippecanoe dependencies installed:

```sh
python scripts/run_daily_terminations.py \
  --as-of 2026-10-26 --scimap ../scimap \
  --source-snapshot /approved/snapshot \
  --source-baseline data --reference-output output \
  --county-commute /approved/county_commute_NIH.csv \
  --district-commute /approved/home_119_workTR.csv \
  --output /new/run-directory
```

Omit `--source-snapshot` for fresh Grant Witness capture with bounded retries.
Use a new output directory/release ID on every attempt. Do not infer source
freshness from retrieval time or treat an unchanged NSF feed as stale solely
because its most recent event is old. Per-agency event dates and change flags
remain in the scientific report/manifest. Final operating thresholds require team
agreement; no invented expiry or automatic retirement is enabled.

## Workflow setup and enablement

The workflow is committed for review. It runs on GitHub-hosted `ubuntu-latest`.
This change does not register a runner. Scheduled execution remains off until
`SCIMAP_DAILY_ENABLED=true`. A manual run first requires
`SCIMAP_RUNTIME_READY=true`, which means the team accepts GitHub-hosted runs.

The workflow installs and caches R matching `r-version.txt`, every package in
`r-packages.lock.csv`, Python 3.12, pandas 2.2.3, geopandas 1.1.0, Tippecanoe
2.79.0, AWS CLI v2, and rclone 1.75.1. It fetches `home_119_workTR.csv` from
public OSF and `county_commute_NIH.csv` from private R2 bucket
`scimap-private-inputs` at key `inputs/county_commute_NIH.csv`. The county file
must not be stored in the public `scimap-data` bucket. The private bucket is
not created by this repository change. An admin creates it with no public
access and no custom domain, then uploads the approved file. The fetch step
checks the locked SHA-256 values. The runbook lists the exact secrets,
variables, bucket, and the first manual run. That run is the memory and time
check. Hosted-runner memory and time were not measured here.

The workflow requires Tippecanoe 2.79.0, AWS CLI v2 with `s3api put-object
--if-none-match` and `s3api list-objects-v2 --no-paginate`, and rclone 1.75.1.
The publisher uploads with the AWS CLI. Pointer selection at the pinned website
revision still calls rclone; the orchestrator derives that remote from `R2_*`
and does not read `RCLONE_CONFIG_R2_*` from the workflow.

Configure the `scimap-candidates` GitHub environment with the existing R2 access
and Slack webhook secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`SCIMAP_ALERT_WEBHOOK_URL`. The workflow passes those R2 secrets through as
`R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`, and sets `R2_ENDPOINT` in the
workflow file. Configure suitable environment reviewers before enabling
publication. No secret or repository access policy is changed by this PR.
The workflow records the analysis revision through checkout and pins the website
revision explicitly. Update that pin through review when website tooling changes.
The schedule is proposed as 13:15 UTC; cutoff is the UTC date and recorded per run.
The concurrency group serializes publishers and does not cancel an active upload.
The old website updater is not activated or modified here.

Before setting either readiness/enablement flag:

1. Assign a primary operations owner and backup, confirm source freshness,
   unmatched-location and retention rules, and agree the proposed schedule.
2. Run the first manual hosted workflow with `publish` false. Review scientific and geometry warnings, and confirm memory and time fit.
3. Demonstrate a builder failure, stage-specific alert and corrected rerun.
4. Publish a staging candidate and verify its upload/selection records. Latest
   selection is separate from website approval. Historical replay must skip latest.
5. Review website selection/rollback operations in the paired website repository;
   demonstrate A→B→A in staging before production cutover.
6. Enable the daily flag only after acceptance and observe successful scheduled runs.

An upload/pointer timeout is uncertain. Inspect saved records and current objects
before retrying; never reuse the same immutable release ID or blindly replay a
pointer write. No cleanup deletes releases automatically.
