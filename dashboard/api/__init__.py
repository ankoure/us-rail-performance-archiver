# dashboard/api/__init__.py
#
# `api` submodules import repo-root packages directly (e.g. `from
# analysis.alert_classifier import ...` in services/alerts.py), which only
# resolves if the repo root is on sys.path. Doing it in the package __init__
# guarantees it runs on any entry into the package, whichever module the
# server (`fastapi dev`, uvicorn, the container's CMD) imports first.
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
