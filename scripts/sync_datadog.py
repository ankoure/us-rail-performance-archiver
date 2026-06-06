#!/usr/bin/env python3
"""Idempotently sync Datadog monitors and dashboards from JSON in this repo.

Source of truth (committed):
  monitors/rail-archiver.json    — list of monitor definitions
  dashboards/rail-archiver.json  — one dashboard definition

Upserts by name (monitors) / title (dashboards): an existing object with the
same name/title is updated in place (PUT), otherwise a new one is created
(POST). No IDs are stored in the repo, so the JSON stays the single source of
truth and re-running is safe.

Environment:
  DD_API_KEY        (required)  Datadog API key
  DD_APP_KEY        (required)  Datadog *Application* key — the management API
                                needs this in addition to the API key
  DD_SITE           (optional)  default "datadoghq.com" (e.g. "datadoghq.eu")
  DD_NOTIFY_TARGET  (required if any monitor still contains the
                                "@<your-notification-target>" placeholder)
                                replaces that placeholder in monitor messages,
                                e.g. "@you@example.com". A leading "@" is added
                                if missing. If a monitor still has the
                                placeholder and this is unset, the sync fails
                                rather than deploy a monitor that pages nobody.

Usage:
  python scripts/sync_datadog.py [--dry-run] [--monitors-only] [--dashboards-only]
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MONITORS_FILE = REPO_ROOT / "monitors" / "rail-archiver.json"
DASHBOARDS_FILE = REPO_ROOT / "dashboards" / "rail-archiver.json"

NOTIFY_PLACEHOLDER = "@<your-notification-target>"


def _api_request(method: str, path: str, body: dict | None = None) -> dict | list:
    """Call the Datadog API and return the decoded JSON response."""
    site = os.environ.get("DD_SITE", "datadoghq.com")
    url = f"https://api.{site}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("DD-API-KEY", os.environ["DD_API_KEY"])
    req.add_header("DD-APPLICATION-KEY", os.environ["DD_APP_KEY"])
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"  ✗ {method} {path} → HTTP {exc.code}: {detail}") from exc


def _apply_notify_target(monitor: dict, target: str | None) -> dict:
    """Substitute the notify placeholder in a monitor's message.

    Fails closed: a monitor that still contains the placeholder with no
    DD_NOTIFY_TARGET set would deploy but page nobody — worse than not
    deploying at all (an 18 h outage once went unalerted this way). So we
    refuse to sync rather than ship a monitor that notifies no one.
    """
    message = monitor.get("message", "")
    if NOTIFY_PLACEHOLDER not in message:
        return monitor
    if not target:
        raise SystemExit(
            f"  ✗ '{monitor['name']}' still contains {NOTIFY_PLACEHOLDER} and "
            "DD_NOTIFY_TARGET is unset — it would page nobody. Set "
            "DD_NOTIFY_TARGET (e.g. '@you@example.com') and re-run."
        )
    monitor = dict(monitor)
    monitor["message"] = message.replace(NOTIFY_PLACEHOLDER, target)
    return monitor


def sync_monitors(dry_run: bool) -> None:
    """Upsert every monitor in the repo JSON by name (PUT if it exists, else POST).

    Applies the DD_NOTIFY_TARGET substitution first, so a monitor still holding
    the notify placeholder aborts the sync rather than deploy paging nobody.
    """
    monitors = json.loads(MONITORS_FILE.read_text())
    target = os.environ.get("DD_NOTIFY_TARGET")
    if target and not target.startswith("@"):
        target = "@" + target

    existing = {m["name"]: m["id"] for m in _api_request("GET", "/api/v1/monitor")}
    print(f"Monitors: {len(monitors)} in repo, {len(existing)} already in Datadog")

    for monitor in monitors:
        monitor = _apply_notify_target(monitor, target)
        name = monitor["name"]
        monitor_id = existing.get(name)
        if dry_run:
            verb = "update" if monitor_id else "create"
            print(f"  [dry-run] would {verb} '{name}'")
            continue
        if monitor_id:
            _api_request("PUT", f"/api/v1/monitor/{monitor_id}", monitor)
            print(f"  ↻ updated '{name}' (id {monitor_id})")
        else:
            created = _api_request("POST", "/api/v1/monitor", monitor)
            print(f"  + created '{name}' (id {created['id']})")


def sync_dashboards(dry_run: bool) -> None:
    """Upsert the repo dashboard JSON by title (PUT if it exists, else POST)."""
    dashboard = json.loads(DASHBOARDS_FILE.read_text())
    title = dashboard["title"]

    listing = _api_request("GET", "/api/v1/dashboard")
    existing = {d["title"]: d["id"] for d in listing.get("dashboards", [])}
    print(f"Dashboards: '{title}' — {len(existing)} already in Datadog")

    dashboard_id = existing.get(title)
    if dry_run:
        verb = "update" if dashboard_id else "create"
        print(f"  [dry-run] would {verb} '{title}'")
        return
    if dashboard_id:
        _api_request("PUT", f"/api/v1/dashboard/{dashboard_id}", dashboard)
        print(f"  ↻ updated '{title}' (id {dashboard_id})")
    else:
        created = _api_request("POST", "/api/v1/dashboard", dashboard)
        print(f"  + created '{title}' (id {created['id']})")


def main() -> None:
    """Validate required env keys, then sync monitors and/or dashboards."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without calling the write API",
    )
    parser.add_argument("--monitors-only", action="store_true")
    parser.add_argument("--dashboards-only", action="store_true")
    args = parser.parse_args()

    for var in ("DD_API_KEY", "DD_APP_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"Missing required environment variable: {var}")

    if not args.dashboards_only:
        sync_monitors(args.dry_run)
    if not args.monitors_only:
        sync_dashboards(args.dry_run)


if __name__ == "__main__":
    main()
