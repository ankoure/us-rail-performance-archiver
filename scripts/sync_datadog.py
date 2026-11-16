#!/usr/bin/env python3
"""Idempotently sync Datadog monitors and dashboards from JSON in this repo.

Source of truth (committed):
  datadog/monitors/rail-archiver.json    — list of monitor definitions
  datadog/dashboards/rail-archiver.json  — one dashboard definition
  datadog/notify_routes.json             — route name -> notification handles

Alert routing: every monitor carries exactly one `route:<name>` tag and the
"@<your-notification-target>" placeholder in its message. The sync replaces the
placeholder with that route's handles from notify_routes.json (e.g. a Slack
channel per kind of alert). Routes are resolved for every monitor before any
API call, and a monitor with no route, an unknown route, or no placeholder
aborts the whole sync: a monitor that pages nobody is worse than no deploy
(an 18 h outage once went unalerted this way).

Upserts by name (monitors) / title (dashboards): an existing object with the
same name/title is updated in place (PUT), otherwise a new one is created
(POST). No IDs are stored in the repo, so the JSON stays the single source of
truth and re-running is safe.

Environment:
  DD_API_KEY        (required)  Datadog API key
  DD_APP_KEY        (required)  Datadog *Application* key — the management API
                                needs this in addition to the API key
  DD_SITE           (optional)  default "datadoghq.com" (e.g. "datadoghq.eu")

Usage:
  python scripts/sync_datadog.py [--dry-run] [--monitors-only] [--dashboards-only]

--dry-run writes nothing, but still sends every monitor to Datadog's
/api/v1/monitor/validate endpoint and exits non-zero if any is rejected, so a
bad query fails CI on the PR instead of failing the sync after merge.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MONITORS_FILE = REPO_ROOT / "datadog" / "monitors" / "rail-archiver.json"
DASHBOARDS_FILE = REPO_ROOT / "datadog" / "dashboards" / "rail-archiver.json"
ROUTES_FILE = REPO_ROOT / "datadog" / "notify_routes.json"

NOTIFY_PLACEHOLDER = "@<your-notification-target>"
ROUTE_TAG_PREFIX = "route:"


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


def _validate_monitor(monitor: dict) -> str | None:
    """Ask Datadog whether a monitor definition is valid; return its error, if any."""
    try:
        _api_request("POST", "/api/v1/monitor/validate", monitor)
    except SystemExit as exc:  # _api_request turns HTTP errors into SystemExit
        return str(exc)
    return None


def load_routes(path: Path = ROUTES_FILE) -> dict[str, str]:
    """Load route name -> handles, ignoring "_"-prefixed keys (comments)."""
    return {
        name: handles
        for name, handles in json.loads(path.read_text()).items()
        if not name.startswith("_")
    }


def apply_route(monitor: dict, routes: dict[str, str]) -> dict:
    """Return a copy of ``monitor`` with its placeholder replaced by its route's handles.

    Raises SystemExit if the monitor can't be routed to anyone: no route tag,
    more than one, a route missing from notify_routes.json (or empty there),
    or no placeholder in the message to substitute.
    """
    name = monitor["name"]
    tagged = [
        t[len(ROUTE_TAG_PREFIX) :]
        for t in monitor.get("tags", [])
        if t.startswith(ROUTE_TAG_PREFIX)
    ]
    if len(tagged) != 1:
        raise SystemExit(
            f"  ✗ '{name}' needs exactly one '{ROUTE_TAG_PREFIX}<name>' tag, "
            f"found {tagged or 'none'}. Routes: {sorted(routes)}"
        )
    route = tagged[0]
    handles = routes.get(route, "").strip()
    if not handles:
        raise SystemExit(
            f"  ✗ '{name}' is routed to '{route}', which has no handles in "
            f"{ROUTES_FILE.name}. Routes: {sorted(routes)}"
        )
    message = monitor.get("message", "")
    if NOTIFY_PLACEHOLDER not in message:
        raise SystemExit(
            f"  ✗ '{name}' has no {NOTIFY_PLACEHOLDER} in its message, so "
            f"route '{route}' would notify nobody."
        )
    monitor = dict(monitor)
    monitor["message"] = message.replace(NOTIFY_PLACEHOLDER, handles)
    return monitor


def sync_monitors(dry_run: bool) -> None:
    """Upsert every monitor in the repo JSON by name (PUT if it exists, else POST).

    Routes every monitor first, so one unroutable monitor aborts the sync
    before anything is written rather than leaving it half-applied.
    """
    routes = load_routes()
    monitors = [apply_route(m, routes) for m in json.loads(MONITORS_FILE.read_text())]

    existing = {m["name"]: m["id"] for m in _api_request("GET", "/api/v1/monitor")}
    print(f"Monitors: {len(monitors)} in repo, {len(existing)} already in Datadog")

    invalid = []
    for monitor in monitors:
        name = monitor["name"]
        monitor_id = existing.get(name)
        if dry_run:
            verb = "update" if monitor_id else "create"
            error = _validate_monitor(monitor)
            if error:
                invalid.append(name)
                print(f"  ✗ [dry-run] '{name}' is INVALID:{error}")
            else:
                print(f"  [dry-run] would {verb} '{name}' (validated)")
            continue
        if monitor_id:
            _api_request("PUT", f"/api/v1/monitor/{monitor_id}", monitor)
            print(f"  ↻ updated '{name}' (id {monitor_id})")
        else:
            created = _api_request("POST", "/api/v1/monitor", monitor)
            print(f"  + created '{name}' (id {created['id']})")
    if invalid:
        raise SystemExit(f"{len(invalid)} monitor(s) failed validation: {invalid}")


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
