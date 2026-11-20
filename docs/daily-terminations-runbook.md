# Daily termination runbook

The workflow is `.github/workflows/daily-terminations.yml`. It is safe to merge
before secrets or a runner exist. The schedule stays off until
`SCIMAP_DAILY_ENABLED=true`. A manual run stays off until
`SCIMAP_RUNTIME_READY=true` and `SCIMAP_INPUT_ROOT` are set. Publication stays
off unless the run is scheduled or the manual `publish` input is true. This
document does not enable either flag.

Website tooling is pinned to `ca167515757a295df811d8a9df7a40b80a3c1f37` on
https://github.com/clvcooke/scimap/pull/49. That is the newest commit on that
pull request that contains the AWS CLI publisher and whose checks passed
(`release-tooling`, `test`, and Cloudflare Pages on 2026-11-19). The publisher
scripts are unchanged since `c118efe46779e4748dc08426829bc7adb93d6756`.
`publish_termination_release.py` reads `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, and `R2_ENDPOINT` or `R2_ACCOUNT_ID`. It does not call
rclone. `select_termination_release.py` at this pin still calls rclone for
pointer reads and writes. The daily orchestrator derives the `r2` rclone remote
from `R2_*` for publication children only.

Neither pointer moves unless a later selection command succeeds.
`latest-validated.json` moves only after a non-historical publish whose upload
record says `published`. `website-release.json` is never written by this
workflow.

## Who sets the GitHub configuration

A repository admin sets these on the `scimap-candidates` environment of
`scienceimpacts/SciMap_Maps`. Do not put the values in the repository.

| Name | Kind | Who sets it | Purpose |
| --- | --- | --- | --- |
| `R2_ACCESS_KEY_ID` | Environment secret | Repository admin | R2 access key. The workflow passes it as `R2_ACCESS_KEY_ID`. |
| `R2_SECRET_ACCESS_KEY` | Environment secret | Repository admin | R2 secret key. The workflow passes it as `R2_SECRET_ACCESS_KEY`. |
| `SCIMAP_ALERT_WEBHOOK_URL` | Environment secret | Repository admin | Slack incoming webhook for `#scimap-alerts`. Required when the orchestrator notifies. |
| `R2_ENDPOINT` | Workflow env, not a secret | Already pinned in the workflow | `https://17067e7373bbfee2043fe5186d66aeb9.r2.cloudflarestorage.com` |
| `SCIMAP_RUNTIME_READY` | Repository variable | Operations owner, after the runner is accepted | Must be `true` or every run fails preflight. |
| `SCIMAP_INPUT_ROOT` | Repository variable | Operations owner | Absolute runner path containing `county_commute_NIH.csv` and `home_119_workTR.csv`. |
| `SCIMAP_DAILY_ENABLED` | Repository variable | Operations owner, after this acceptance and team sign-off | Must be `true` before the 13:15 UTC schedule can start. Leave unset until then. |

The self-hosted runner (`self-hosted`, `linux`, `x64`, `scimap`) is registered
by the operations owner. This change does not register it. The runtime check
requires Python 3.12, pandas 2.2.3, geopandas 1.1.0, Tippecanoe 2.79.0, AWS CLI
v2 with `put-object --if-none-match` and `list-objects-v2 --no-paginate`, and
rclone 1.75.1.

## Manual run

1. Confirm `SCIMAP_RUNTIME_READY=true` and `SCIMAP_INPUT_ROOT` point at the approved runner files. Leave `SCIMAP_DAILY_ENABLED` unset.
2. Actions → **Daily termination candidate** → **Run workflow** on the reviewed branch.
3. `as_of` blank uses the current UTC date. Set `YYYY-MM-DD` to replay a cutoff.
4. Leave `publish` false for a runner build that does not upload and does not read R2.
5. Set `publish` true only when an immutable upload is intended. A missing or blank R2 secret then fails before capture. The Slack alert names stage `publish`, the report records `failed_stage: publish`, and neither pointer is written.
6. Set `historical` true to keep `latest-validated` where it is. `publish` must also be true if the immutable objects should be stored. Historical never selects a pointer.

The concurrency group `termination-release-publication` does not cancel an active upload.

## Staging release only

A staging publication stores a new immutable release and does not change the website. Use a manual run with `publish=true` and `historical=true`. The publisher writes under `s3://scimap-data/terminations/releases/<new-release-id>/` and the public URL `https://data.scienceimpacts.org/terminations/releases/<new-release-id>/`. It refuses a non-empty destination. It does not write `latest-validated.json` or `website-release.json`.

Use a new release id on every attempt. The orchestrator generates one. Do not rerun an id that already has objects.

After the run, download the artifact. `upload-record.json` must say `"status": "published"` and `"transport": "aws-cli"`. `selection-record.json` must be absent. Fetch the public manifest and compare its SHA-256 to `manifest_sha256` in the upload record. The live site keeps showing the previous `website-release` selection.

## Promote and roll back

Promotion and rollback are explicit selector commands from a clean checkout of website `ca167515757a295df811d8a9df7a40b80a3c1f37`. They are not done by leaving `historical` false on the first staging upload. Export the rclone remote from the same R2 values in a private shell. Do not commit them.

```sh
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="$R2_ENDPOINT"
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
```

Read the current pointer before changing it. `latest-validated` is the daily pointer. `website-release` is the public site and needs `--approved-by` and `--reason` recording a real approval. The acceptance demonstration uses `latest-validated` only.

```sh
python scripts/select_termination_release.py \
  --upload-record /path/to-A/upload-record.json \
  --pointer latest-validated
```

That dry run prints the prepared selection and does not write. Add `--apply --record /new/selection-A.json` only for the authorized write. The record path must be new. An `unconfirmed` record means the write result is unknown: read the pointer back and do not retry that record.

A-B-A, after two published upload records exist:

1. Record pointer A: save the current `latest-validated.json` body and its `release_id`.
2. Apply B with B's upload record and a new `--record` path. Read the public pointer back. It must equal B, and the site pointer `website-release.json` must still equal its previous value.
3. Apply A again with A's original upload record and another new `--record` path. Read the public pointer back. It must equal the body saved in step 1.
4. If B's cutoff is older than the current `latest-validated` `as_of`, the selector refuses the backwards move. Use a pointer that does not trip that guard, or demonstrate A-B-A on a pointer whose current `as_of` is not newer than B. Do not use this refusal as a reason to edit the pointer by hand.

Rollback of `website-release` is the same command with `--pointer website-release`, the earlier approved upload record, a new record path, and the actual approver and reason. It does not rebuild data.

## Failure notification

`--notify` posts to the Slack webhook. The webhook URL is not printed. A failed alert delivery is a stderr line `Slack alert not delivered: ...` and does not hide the run result.

A missing secret or a failed stage looks like this:

    :rotating_light: *Termination daily run failed at publish*
    release: `2026-11-20-daily-...` · report: `/…/daily-report.json` · stage: `publish`
    ```
    Missing publication credentials: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY. Native R2 publishing needs R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and R2_ENDPOINT or R2_ACCOUNT_ID. latest-validated and website-release were not changed.
    ```
    Website selection was not changed. Inspect stage logs before retrying with a new release ID.
    <https://github.com/scienceimpacts/SciMap_Maps/actions/runs/RUN_ID|Open the run and its run-report.json>

A scientific schema failure uses the same shape with stage `science` and the text `Scientific run did not pass enforced validation`. The stage log and `analysis/run-report.json` name the check, such as `source_schema:nih_v3_terminations.csv`. No `upload-record.json` or `selection-record.json` is written. Both pointers stay as they were.

A runtime failure before the orchestrator starts posts `:rotating_light: *Daily runtime setup failed — nothing was published*`.

The run is red. The artifact `termination-<run-id>-<attempt>` contains `daily-report.json` and the stage logs when the orchestrator created them. `"website_selection_changed": false` is the local report flag. Confirm the live pointers by reading them, not by assuming the flag.

## Replay a past week

Manual **Run workflow** with `as_of` set to that week's ISO cutoff and `historical` true. Leave `publish` false to rebuild on the runner without uploading. Set `publish` true to store a new immutable release id without moving `latest-validated`. The workflow captures current Grant Witness inputs and applies the cutoff. It does not reuse an old snapshot.

For a frozen snapshot, run locally instead of the workflow:

```sh
python scripts/run_daily_terminations.py \
  --as-of YYYY-MM-DD --historical \
  --source-snapshot /approved/snapshot \
  --scimap /path/to/scimap-at-ca167515 \
  --source-baseline data --reference-output output \
  --county-commute "$SCIMAP_INPUT_ROOT/county_commute_NIH.csv" \
  --district-commute "$SCIMAP_INPUT_ROOT/home_119_workTR.csv" \
  --output /new/replay-directory
```

Add `--publish` only when R2 credentials are intentionally present and a new release id should be uploaded. Add `--notify` only when the Slack webhook should receive the result. Repeat with a new output directory.

## Acceptance checklist

Do not run the live steps until a repository admin has entered the secrets, the runner is accepted, and the team has agreed to a staging window. Nothing below has been run against live R2.

Dry-run now, from this repository:

```sh
python scripts/daily_termination_acceptance.py --dry-run --evidence /tmp/daily-acceptance.json
```

That command does not contact R2. It checks the workflow pin and gates, runs the missing-secret and schema-failure simulations, and records the local AWS CLI. Evidence for this change is kept outside the repository.

Live steps, in order, when the window is open:

1. Manual run with `publish=false`. Confirm the scientific report passed and no upload or selection record exists.
2. Manual run with `publish=true`, `historical=true`, and a fresh cutoff. This is the staging release. Confirm `upload-record.json` is `published` with `transport` `aws-cli`, `selection-record.json` is absent, and both public pointers are unchanged.
3. Forced schema failure: copy an approved snapshot, delete the `spent_at_disruption` column from `nih_v3_terminations.csv`, and run with `--source-snapshot` pointing at that copy, `--publish`, and `--notify`. The run must fail at `science`, alert as above, and leave both pointers unchanged. Do not repair the snapshot in place. Use a new output directory for the corrected rerun.
4. A-B-A on `latest-validated` only, using the selector commands above. Save A, apply B, read it back, apply A, read it back. `website-release.json` stays on its previous release for the whole demonstration.
5. Team sign-off before anyone sets `SCIMAP_DAILY_ENABLED=true`.
