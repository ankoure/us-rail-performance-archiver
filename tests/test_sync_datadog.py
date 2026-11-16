import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sync_datadog",
    Path(__file__).resolve().parent.parent / "scripts" / "sync_datadog.py",
)
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)

ROUTES = {"outage": "@slack-outage @me@example.com", "feeds": "@slack-feeds"}


def _monitor(tags, message="boom\n\n@<your-notification-target>"):
    return {"name": "m", "message": message, "tags": tags}


def test_apply_route_substitutes_route_handles():
    out = sync.apply_route(_monitor(["env:prod", "route:outage"]), ROUTES)
    assert out["message"] == "boom\n\n@slack-outage @me@example.com"


def test_apply_route_does_not_mutate_input():
    m = _monitor(["route:feeds"])
    sync.apply_route(m, ROUTES)
    assert "@<your-notification-target>" in m["message"]


@pytest.mark.parametrize(
    "tags",
    [
        ["env:prod"],  # no route
        ["route:feeds", "route:outage"],  # ambiguous
        ["route:nope"],  # unknown route
    ],
)
def test_apply_route_rejects_unroutable_monitor(tags):
    with pytest.raises(SystemExit):
        sync.apply_route(_monitor(tags), ROUTES)


def test_apply_route_rejects_empty_route():
    with pytest.raises(SystemExit):
        sync.apply_route(_monitor(["route:blank"]), {"blank": "  "})


def test_apply_route_rejects_message_without_placeholder():
    with pytest.raises(SystemExit):
        sync.apply_route(_monitor(["route:feeds"], message="no target"), ROUTES)


def test_load_routes_skips_comment_keys(tmp_path):
    f = tmp_path / "routes.json"
    f.write_text(json.dumps({"_comment": "x", "feeds": "@slack-feeds"}))
    assert sync.load_routes(f) == {"feeds": "@slack-feeds"}


def test_committed_monitors_all_route_to_someone():
    # Guards the real files: a new monitor without a valid route fails CI here
    # instead of failing the sync after merge.
    routes = sync.load_routes()
    monitors = json.loads(sync.MONITORS_FILE.read_text())
    for monitor in monitors:
        out = sync.apply_route(monitor, routes)
        assert "@<your-notification-target>" not in out["message"]
    used = {t for m in monitors for t in m["tags"] if t.startswith("route:")}
    assert used == {f"route:{r}" for r in routes}, "every route should be used"
