"""Post termination-pipeline alerts to Slack through an incoming webhook.

Alerts are opt-in (`--notify` on the capture and pipeline commands) so local and
regression runs never post. The webhook URL comes from SCIMAP_ALERT_WEBHOOK_URL
and is never printed. A failed delivery is reported on stderr and never hides the
original run result.
"""

import json
import os
import re
import signal
import sys
from urllib.request import Request, urlopen

WEBHOOK_ENV = "SCIMAP_ALERT_WEBHOOK_URL"
PROBLEMS_PER_CHECK = 3
MAX_TEXT = 3500


def webhook_from_env():
    webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    if not webhook.startswith("https://hooks.slack.com/"):
        raise SystemExit(f"--notify requires {WEBHOOK_ENV} to hold a Slack incoming webhook URL")
    return webhook


def raise_on_sigterm():
    """Turn SIGTERM (CI cancellation or timeout) into KeyboardInterrupt so the failure alert still runs."""
    def interrupt(signum, frame):
        raise KeyboardInterrupt("SIGTERM")
    signal.signal(signal.SIGTERM, interrupt)


def escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def check_lines(validation, statuses=("error", "warning")):
    lines = []
    checks = [check for check in validation.get("checks", []) if check["status"] in statuses]
    for check in sorted(checks, key=lambda check: statuses.index(check["status"])):
        lines.append(f"• *{escape(check.get('title', check['name']))}*: {check['problem_count']} problem(s)")
        for problem in check["problems"][:PROBLEMS_PER_CHECK]:
            lines.append(f"    – {escape(problem)}")
        if check["problem_count"] > PROBLEMS_PER_CHECK:
            lines.append(f"    – … {check['problem_count'] - PROBLEMS_PER_CHECK} more in run-report.json")
    return lines


def ci_run_url():
    """GitHub Actions run page, where the scheduled workflow keeps run-report.json as an artifact."""
    parts = [os.environ.get(name, "").strip() for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    return "{}/{}/actions/runs/{}".format(*parts) if all(parts) else None


def warning_summary(validation):
    """One line naming the warnings, so errors stay readable when a run is blocked."""
    if not validation["warnings"]:
        return []
    titles = {check["name"]: check.get("title", check["name"]) for check in validation.get("checks", [])}
    names = ", ".join(titles.get(name, name) for name in validation["warnings"])
    return [f"+ {len(validation['warnings'])} warning(s): {escape(names)}"]


def message(level, title, context, lines=()):
    icon = {"error": ":rotating_light:", "warning": ":warning:"}[level]
    header = f"{icon} *{escape(title)}*"
    details = " · ".join(f"{key}: `{escape(str(value).replace('`', "'"))}`" for key, value in context.items() if value)
    url = ci_run_url()
    footer = f"<{url}|Open the run and its run-report.json>" if url else ""
    body = "\n".join([header, details, *lines]).strip()
    limit = MAX_TEXT - len(footer) - 1
    if len(body) > limit:
        body = body[:limit - 40].rstrip() + "\n… truncated; see run-report.json"
    return "\n".join(filter(None, [body, footer]))


def failure_reason(error):
    if isinstance(error, KeyboardInterrupt):
        return "Interrupted (Ctrl-C, SIGTERM, job cancellation or timeout)"
    if isinstance(error, SystemExit) and isinstance(error.code, int):
        return f"Exited with status {error.code}"
    return str(error) or type(error).__name__


def code_block(text, limit=1500):
    """Slack code block that cannot be closed early or cut inside an escape sequence."""
    text = escape(text.replace("`", "'"))
    if len(text) > limit:
        text = re.sub(r"&[a-z]*$", "", text[:limit]) + " …"
    return f"```{text}```"


def run_failed(stage, error, context):
    return message("error", f"{stage} failed — nothing was published", context, [code_block(failure_reason(error))])


def validation_alert(validation, context, blocked):
    """Return the alert for a validation result, or None when it passed cleanly."""
    if blocked:
        title = f"Termination run blocked: {len(validation['errors'])} validation error(s) — nothing was published"
        return message("error", title, context, check_lines(validation, ("error",)) + warning_summary(validation))
    if validation["errors"]:
        title = f"Termination run recorded {len(validation['errors'])} validation error(s) in report mode"
        return message("warning", title, context, check_lines(validation, ("error",)) + warning_summary(validation))
    if validation["warnings"]:
        title = f"Termination run passed with {len(validation['warnings'])} warning(s)"
        return message("warning", title, context, check_lines(validation, ("warning",)))
    return None


def send(webhook, text, opener=urlopen):
    if not webhook or not text:
        return False
    request = Request(
        webhook,
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=30) as response:
            if response.status == 200:
                return True
            print(f"Slack alert not delivered: HTTP {response.status}", file=sys.stderr)
    except Exception as error:  # never let alerting mask the run result; the type name cannot leak the URL
        print(f"Slack alert not delivered: {type(error).__name__}", file=sys.stderr)
    return False
