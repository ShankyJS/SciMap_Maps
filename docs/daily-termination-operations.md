# Daily termination candidate operations

`run_daily_terminations.py` connects source capture → enforced scientific validation
→ complete release generation → optional immutable publication → latest-validated
selection. Default execution is local only. `--publish` enables publication.
`--historical` always suppresses latest selection. No daily command writes
`website-release.json`; exact dataset approval and website selection remain separate.

Every stage has a log and `daily-report.json`. Failure blocks downstream stages.
With `--notify`, the orchestrator posts actionable failures, scientific warnings
and release-geometry warnings to the existing #scimap-alerts webhook. It includes
stage/run/report context and the end of the failing log, including institution
names. It strips R2/AWS credentials from capture/science/build child environments;
only publication/selection children inherit them. Webhook values are never printed
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

The workflow is committed for review. Scheduled execution remains off until
`SCIMAP_DAILY_ENABLED=true`; a manual run first requires
`SCIMAP_RUNTIME_READY=true` and `SCIMAP_INPUT_ROOT`.

Use a dedicated trusted self-hosted Linux x64 runner labeled `scimap`, isolated
from untrusted PR jobs, with R exactly matching `r-version.txt`, every package in
`r-packages.lock.csv`, Python 3.12, pandas 2.2.3, geopandas 1.1.0, Tippecanoe and
rclone. Provision the two approved commuter inputs under `SCIMAP_INPUT_ROOT`.
The scientific runner checks their approved checksums. This avoids uploading the
private county reference to a public release or pretending the unit-test runner
contains the full scientific environment. Runner provisioning is not completed by
committing a workflow. Pin/record the Tippecanoe and rclone versions in the accepted
runner image before enablement; the local proven runtime remains the reference.

Configure the `scimap-candidates` GitHub environment with the existing R2 access
and Slack webhook secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`SCIMAP_ALERT_WEBHOOK_URL`. Configure suitable environment reviewers before enabling
publication. No secret or repository access policy is changed by this PR.
The workflow records the analysis revision through checkout and pins the website
revision explicitly. Update that pin through review when website tooling changes.
The schedule is proposed as 13:15 UTC; cutoff is the UTC date and recorded per run.
The concurrency group serializes publishers and does not cancel an active upload.
The old website updater is not activated or modified here.

Before setting either readiness/enablement flag:

1. Assign a primary operations owner and backup, confirm source freshness,
   unmatched-location and retention rules, and agree the proposed schedule.
2. Run local-only on the configured runner. Review scientific and geometry warnings.
3. Demonstrate a builder failure, stage-specific alert and corrected rerun.
4. Publish a staging candidate and verify its upload/selection records. Latest
   selection is separate from website approval. Historical replay must skip latest.
5. Review website selection/rollback operations in the paired website repository;
   demonstrate A→B→A in staging before production cutover.
6. Enable the daily flag only after acceptance and observe successful scheduled runs.

An upload/pointer timeout is uncertain. Inspect saved records and current objects
before retrying; never reuse the same immutable release ID or blindly replay a
pointer write. No cleanup deletes releases automatically.
