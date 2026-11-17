#!/usr/bin/env python3
"""Check or set the GitHub App's webhook: URL, secret, subscribed events and recent deliveries.

Run inside the agent-runtime container (it has the App's private key and webhook secret)::

    python3 /app/scripts/github-app-webhook.py                 # report
    python3 /app/scripts/github-app-webhook.py --url https://host:8443/webhooks/github

``--url`` points the App's webhook at ``url`` and sets its secret to the runtime's
``GITHUB_APP_WEBHOOK_SECRET``, so the two always match. GitHub's API cannot turn the webhook
on or change which events the App subscribes to; the report says what to change by hand in the
App settings when either is missing. Bootstrap phase 12b runs this with ``--url`` on hosts that
expose the webhook through Tailscale Funnel. See docs/runbooks/github-app.md.

Exit codes: 0 ok, 1 error, 2 needs a change in the App settings.
"""

from __future__ import annotations

import argparse
import os
import sys

REQUIRED_EVENTS = (
    "pull_request",
    "pull_request_review",
    "issue_comment",
    "check_suite",
    "check_run",
    "status",
    "deployment_status",
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="webhook URL to set, with the runtime's webhook secret")
    parser.add_argument("--host", default=os.environ.get("GITHUB_APP_HOST") or "github.com")
    args = parser.parse_args(argv)

    from ts_harness.integrations.github_app import app_for

    app = app_for(args.host)
    if app is None:
        print("no GitHub App registered for", args.host, file=sys.stderr)
        return 1
    jwt = app.jwt()
    secret = os.environ.get("GITHUB_APP_WEBHOOK_SECRET", "").strip()
    problems: list[str] = []

    if args.url:
        if not secret:
            print("GITHUB_APP_WEBHOOK_SECRET is empty; set ts-harness/github-app-webhook-secret and re-run phase 10", file=sys.stderr)
            return 1
        status, _ = app._call(
            "PATCH",
            "/app/hook/config",
            bearer=jwt,
            body={"url": args.url, "content_type": "json", "insecure_ssl": "0", "secret": secret},
        )
        if status == 404:
            problems.append(f"the App's webhook is off: in the App settings tick Webhook > Active, set the URL to {args.url}, save, then re-run this")
        elif status >= 300:
            print(f"setting the webhook failed: HTTP {status}", file=sys.stderr)
            return 1
        else:
            print("webhook URL and secret set:", args.url)

    status, config = app._call("GET", "/app/hook/config", bearer=jwt)
    if status == 404 or not (isinstance(config, dict) and config.get("url")):
        if not args.url:
            problems.append("the App's webhook is off: in the App settings tick Webhook > Active and set the URL")
    else:
        print("webhook URL:", config.get("url"), "| content type:", config.get("content_type"))
        if not secret:
            problems.append("GITHUB_APP_WEBHOOK_SECRET is empty in the runtime; deliveries will be rejected")

    _, info = app._call("GET", "/app", bearer=jwt)
    events = sorted((info or {}).get("events") or []) if isinstance(info, dict) else []
    print("events:", ", ".join(events) or "(none)")
    missing = [event for event in REQUIRED_EVENTS if event not in events]
    if missing:
        problems.append("subscribe the App to: " + ", ".join(missing) + " (App settings > Permissions & events)")

    status, deliveries = app._call("GET", "/app/hook/deliveries?per_page=5", bearer=jwt)
    if status < 300 and isinstance(deliveries, list):
        for delivery in deliveries:
            print(
                "  delivery",
                delivery.get("delivered_at"),
                delivery.get("event"),
                delivery.get("action") or "",
                delivery.get("status_code"),
            )

    for problem in problems:
        print("ACTION:", problem)
    return 2 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
