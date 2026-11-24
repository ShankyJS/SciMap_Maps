#!/usr/bin/env python3
"""Download NIH and NSF source CSVs into a new immutable snapshot directory."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

import alerts

SOURCES = {
    "nih_v3_terminations.csv": {
        "url": "https://files.grant-witness.us/nih_v3_terminations.csv",
        "required": {"grant_id", "full_award_number", "org_name", "org_state", "org_city", "first_termination_date", "obligated_at_disruption", "spent_at_disruption"},
    },
    "nsf_terminations.csv": {
        "url": "https://files.grant-witness.us/nsf_terminations.csv",
        "required": {"grant_id", "org_name", "org_state", "org_city", "termination_date", "estimated_budget", "estimated_outlays"},
    },
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, destination, attempts):
    request = Request(url, headers={"User-Agent": "SCIMaP-termination-pipeline/1"})
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=120) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            return
        except (URLError, TimeoutError):
            destination.unlink(missing_ok=True)
            if attempt == attempts:
                raise
            time.sleep(2 ** (attempt - 1))


def validate_csv(path, required):
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        fields = set(reader.fieldnames or ())
        missing = required - fields
        if missing:
            raise RuntimeError(f"{path.name} is missing required columns: {', '.join(sorted(missing))}")
        rows = sum(1 for _ in reader)
    if rows == 0:
        raise RuntimeError(f"{path.name} has no data rows")
    return rows


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New snapshot directory")
    parser.add_argument("--nih-url", default=SOURCES["nih_v3_terminations.csv"]["url"])
    parser.add_argument("--nsf-url", default=SOURCES["nsf_terminations.csv"]["url"])
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument(
        "--notify",
        action="store_true",
        help=f"post download and schema failures to Slack via ${alerts.WEBHOOK_ENV}",
    )
    return parser.parse_args()


def capture(args):
    output = Path(args.output).resolve()
    if output.exists():
        raise SystemExit(f"Refusing to overwrite snapshot: {output}")
    if args.attempts < 1:
        raise SystemExit("--attempts must be at least 1")
    urls = {
        "nih_v3_terminations.csv": args.nih_url,
        "nsf_terminations.csv": args.nsf_url,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="termination-snapshot-", dir=output.parent) as temporary:
        snapshot = Path(temporary) / "snapshot"
        snapshot.mkdir()
        files = {}
        for filename, config in SOURCES.items():
            path = snapshot / filename
            download(urls[filename], path, args.attempts)
            rows = validate_csv(path, config["required"])
            files[filename] = {
                "source_url": urls[filename],
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
                "rows": rows,
            }
        manifest = {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "files": files,
        }
        (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        shutil.move(snapshot, output)
    return output


def main():
    args = parse_args()
    webhook = alerts.webhook_from_env() if args.notify else None
    alerts.raise_on_sigterm()
    try:
        output = capture(args)
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        alerts.send(webhook, alerts.run_failed("Grant Witness capture", error, {"snapshot": Path(args.output).name}))
        raise
    print(f"Source snapshot created: {output}")


if __name__ == "__main__":
    main()
