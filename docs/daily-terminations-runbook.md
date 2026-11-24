# Daily termination runbook

The workflow is `.github/workflows/daily-terminations.yml`. It runs on a
GitHub-hosted `ubuntu-latest` runner. No self-hosted runner is registered or
required. It is safe to merge before secrets exist. The schedule stays off
until `SCIMAP_DAILY_ENABLED=true`. A manual candidate run stays off until
`SCIMAP_RUNTIME_READY=true`. Candidate publication stays off unless the run is scheduled
or the manual `publish` input is true. This document does not enable either
flag and does not create GitHub secrets or variables.

The workflow installs the pinned toolchain on the runner and caches it: R
4.6.1 from `r-version.txt`, every package in `r-packages.lock.csv`, Python
3.12, pandas 2.2.3, geopandas 1.1.0, Tippecanoe 2.79.0, AWS CLI 2.40.0, and
rclone 1.75.1. The runtime check still requires AWS CLI v2 with
`put-object --if-none-match` and `list-objects-v2 --no-paginate`, and rclone
1.75.1, because pointer selection at the pinned website revision still calls
rclone. Memory and time for a full scientific run on the hosted runner were
not measured. The first manual run below is how the team confirms they fit.

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
The daily workflow only moves `latest-validated.json` after a non-historical
publish with a durable `published` receipt. It never writes
`website-release.json`; use the separate manual workflow below.

## Who sets the GitHub configuration

A repository admin sets these on the `scimap-candidates` environment of
`scienceimpacts/SciMap_Maps`, except the two repository variables. Do not put
the values in the repository. This change does not set them.

| Name | Kind | Who sets it | Purpose |
| --- | --- | --- | --- |
| `R2_ACCESS_KEY_ID` | Environment secret | Repository admin | R2 access key for the county input, private release records, and publication/selection in `scimap-data`. See access requirements below. |
| `R2_SECRET_ACCESS_KEY` | Environment secret | Repository admin | R2 secret key for that same access key. |
| `SCIMAP_ALERT_WEBHOOK_URL` | Environment secret | Repository admin | Slack incoming webhook for `#scimap-alerts`. Required for failure alerts. |
| `R2_ENDPOINT` | Workflow env, not a secret | Already pinned in the workflow | `https://17067e7373bbfee2043fe5186d66aeb9.r2.cloudflarestorage.com` |
| `SCIMAP_RUNTIME_READY` | Repository variable | Operations owner, after the team accepts GitHub-hosted runs | Must be `true` or every run fails preflight. |
| `SCIMAP_DAILY_ENABLED` | Repository variable | Operations owner, after this acceptance and team sign-off | Must be `true` before the 13:15 UTC schedule can start. Leave unset until then. |

There is no runner registration step. Preflight and the candidate job both use
`ubuntu-latest`. The candidate timeout stays 180 minutes. Workflow permissions
stay `contents: read` and add `actions: write` so the toolchain caches can be
saved. The runtime check requires Python 3.12, pandas 2.2.3, geopandas 1.1.0,
Tippecanoe 2.79.0, AWS CLI v2 with `put-object --if-none-match` and
`list-objects-v2 --no-paginate`, and rclone 1.75.1.

## Private county input

`home_119_workTR.csv` is fetched at run time from public OSF
`https://osf.io/m5kb6/download`. It must match the lock: SHA-256
`98299795a87f84df5c8d0b2a6adc782527eb211582c7ea8bfbbb669a2c8e839a`, 50,598,712
bytes.

`county_commute_NIH.csv` stays private. The workflow downloads it from R2
bucket `scimap-private-inputs`, object key `inputs/county_commute_NIH.csv`. It
must match SHA-256
`a94bf35dee0c3086cdfcf458ef5775a133d4925826d9e05868d1a41c98386ac5`, 503,609,766
bytes. That bucket must have no public access and no custom domain. It must
not be `scimap-data`. `scimap-data` is served at `https://data.scienceimpacts.org`,
and the county file must not be stored there under any prefix.

The bucket already exists, with no public access and no custom domain. The
approved `county_commute_NIH.csv` is stored at that key. Its SHA-256 and size
match the lock. Unauthenticated reads are rejected: the R2 endpoint returns
HTTP 400 and no bytes, `https://scimap-private-inputs.r2.dev/inputs/county_commute_NIH.csv`
returns HTTP 403, and `https://data.scienceimpacts.org/inputs/county_commute_NIH.csv`
returns HTTP 403. This repository does not create buckets and does not set
GitHub secrets.

The GitHub `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` secrets must be able
to `GetObject` that key. The fetch command maps those secrets onto the AWS CLI,
forces `AWS_REGION=auto`, and ignores `AWS_CONFIG_FILE` and
`AWS_SHARED_CREDENTIALS_FILE`. A real-AWS region or config file makes R2 return
HTTP 400. A missing secret, a missing object, or a checksum mismatch fails the
fetch step before the orchestrator starts. When the Slack webhook is set, the
alert is `Daily input fetch failed — nothing was published`.
`latest-validated` and `website-release` are not changed.

`SCIMAP_INPUT_ROOT` is not a workflow input. The fetch step writes the two
verified files under the runner temp directory, and the orchestrator reads
those paths.

## Manual run

1. Confirm the private bucket and object exist, the environment secrets above are set, and `SCIMAP_RUNTIME_READY=true`. Leave `SCIMAP_DAILY_ENABLED` unset.
2. Actions → **Daily termination candidate** → **Run workflow** on the reviewed branch.
3. `as_of` blank uses the current UTC date. Set `YYYY-MM-DD` to replay a cutoff.
4. Leave `publish` false for the first hosted run. That run still downloads the private county file, so it reads R2. It does not upload a release or move a pointer. Use it to confirm the toolchain, the scientific job, and the 180-minute hosted-runner memory and disk limit. That fit was not measured in this change.
5. Set `publish` true only when an immutable upload is intended. A missing or blank R2 secret fails at the fetch step before capture. The Slack alert names `Daily input fetch`, and neither pointer is written. If fetch succeeded, the same credentials are present for publication.
6. Set `historical` true to keep `latest-validated` where it is. `publish` must also be true if the immutable objects should be stored. Historical never selects a pointer.

The daily and selection workflows share `termination-release-publication`, so
selection cannot race an active publish. GitHub can replace an older pending
run with a newer one. Dispatch one selection at a time.

## Staging release only

A staging publication stores a new immutable release and does not change the website. Use a manual run with `publish=true` and `historical=true`. The publisher writes under `s3://scimap-data/terminations/releases/<new-release-id>/` and the public URL `https://data.scienceimpacts.org/terminations/releases/<new-release-id>/`. It refuses a non-empty destination. It does not write `latest-validated.json` or `website-release.json`.

Use a new release id on every attempt. The orchestrator generates one. Do not rerun an id that already has objects.

The daily workflow saves `upload-record.json` in the run artifact and at
`s3://scimap-private-inputs/records/releases/<release-id>/upload-record.json`
for every published release, including historical ones. The receipt must say
`"status": "published"` and `"transport": "aws-cli"`. `selection-record.json`
must be absent on a historical run. To independently verify the upload, fetch
the public manifest and compare its SHA-256 to `manifest_sha256` in the receipt.
The website selection is unchanged.

If `retain-upload-record` fails, the release may already exist publicly. The
daily run fails before moving `latest-validated`. Inspect the retained artifact
and restore private `PutObject` access; don't try to overwrite the immutable release.

## Promote and roll back

1. In `scienceimpacts/SciMap_Maps`, go to **Actions → Publish or roll back
   website dataset → Run workflow**. GitHub only offers the button once
   `.github/workflows/select-termination-release.yml` is on the default branch.
2. Enter the exact `release_id` from the Slack alert or daily run log. Leave
   `pointer` at `website-release` to change what the site displays. Use
   `latest-validated` only to change the daily pointer; it refuses older cutoffs.
3. Enter the actual `approved_by` and `reason` (both required for website
   selection, even for a dry run). The pinned selector includes these values
   in the public pointer, so don't enter credentials or private notes.
4. Leave `dry_run` checked and run it. Check `previous` and `selection` in the
   step log, or review the old and proposed release IDs in `#scimap-alerts`.
   No R2 pointer or record is written on a dry run.
5. To apply, dispatch with the same inputs and uncheck `dry_run`. Wait for
   success before dispatching again. The selector checks the updated contents
   through both the authenticated R2 origin and the public URL. The run posts
   to `#scimap-alerts` and retains `selection-record.json` as an artifact and
   in `scimap-private-inputs/records/selections/<pointer>/<run-id>-<attempt>.json`.
   Rollback is the same button, with the earlier approved release ID.

No artifact is needed for releases with durable receipts. For older releases
only, enter `legacy_run_id`; it is used if the private receipt is missing. The
run must still have its original, unexpired artifact. The workflow verifies that
the receipt is for the exact requested release. These two legacy receipts were
verified in the following runs:

| Release ID | `legacy_run_id` |
| --- | --- |
| `2026-11-24-daily-20261124T162915Z-02777460` | `45436300310` |
| `2026-11-24-daily-20261124T194615Z-59d0ad1c` | `45472389609` |

The existing `scimap-candidates` R2 key must have `GetObject`/`PutObject` on
`scimap-private-inputs/records/*` in addition to its county input read access
and `ListBucket`, `GetObject`, and `PutObject` access for publication/selection
in `scimap-data`. The key must be able to distinguish a missing private record
from denied access; the workflow does not fall back on a permissions error.

A red run does not imply the old pointer is still live. An `unconfirmed`
record means the pointer write started but verification did not finish. A
`selected` record means the pointer was verified even if a later audit upload
or Slack notification failed. Read the public pointer before retrying.

### Manual fallback

If GitHub Actions is unavailable, use the original selector from a clean
website checkout at `ca167515757a295df811d8a9df7a40b80a3c1f37`. Coordinate
with the daily/selection workflow operators so no publish or selection runs in
parallel. Retrieve the receipt from private records or an archived artifact.
Export the remote from the same R2 values in a private shell. Do not commit them.

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

Website rollback uses the earlier approved upload record and a new record
path; it does not rebuild data:

```sh
python scripts/select_termination_release.py \
  --upload-record /path/to/earlier/upload-record.json \
  --pointer website-release --apply --record /new/rollback.json \
  --approved-by 'Actual approver' --reason 'Reason for rollback'
```

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

A runtime failure before the orchestrator starts posts `:rotating_light: *Daily runtime setup failed — nothing was published*`. An input fetch failure posts `:rotating_light: *Daily input fetch failed — nothing was published*` and includes the fetch error. Neither message means a pointer moved.

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
  --county-commute /approved/county_commute_NIH.csv \
  --district-commute /approved/home_119_workTR.csv \
  --output /new/replay-directory
```

Add `--publish` only when R2 credentials are intentionally present and a new release id should be uploaded. Add `--notify` only when the Slack webhook should receive the result. Repeat with a new output directory.

## Acceptance checklist

The private bucket and the approved county object are already in place. Do not run the live steps until a repository admin has set the `scimap-candidates` secrets listed above, set `SCIMAP_RUNTIME_READY=true`, and the team has agreed to a staging window. No live pointer change has been run. The hosted-runner memory and time fit has not been measured.

Dry-run now, from this repository:

```sh
python scripts/daily_termination_acceptance.py --dry-run --evidence /tmp/daily-acceptance.json
```

That command does not contact R2. It checks the workflow pin and gates, runs the missing-secret and schema-failure simulations, and records the local AWS CLI. Evidence for this change is kept outside the repository.

Live steps, in order, when the window is open:

1. Manual run with `publish=false` and the secrets present. This is the memory and time check. Confirm the scientific report passed, no upload or selection record exists, and the job finishes inside 180 minutes without running out of disk or memory.
2. Manual run with `publish=true`, `historical=true`, and a fresh cutoff. This is the staging release. Confirm `upload-record.json` is `published` with `transport` `aws-cli`, `selection-record.json` is absent, and both public pointers are unchanged.
3. Forced schema failure: copy an approved snapshot, delete the `spent_at_disruption` column from `nih_v3_terminations.csv`, and run with `--source-snapshot` pointing at that copy, `--publish`, and `--notify`. The run must fail at `science`, alert as above, and leave both pointers unchanged. Do not repair the snapshot in place. Use a new output directory for the corrected rerun.
4. For the daily workflow drill, do A-B-A on `latest-validated` only, using releases with the same cutoff or applying the manual checks in an isolated staging deployment. Save A, apply B and check its public `release_id`, then apply A and check it again. The selector refuses older cutoffs; do not work around the guard. `website-release.json` stays unchanged for this daily drill. Test the new website button separately, before the frontend switches to the live pointer, or in a team-approved window.
5. Team sign-off before anyone sets `SCIMAP_DAILY_ENABLED=true`.
