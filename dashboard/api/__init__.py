# dashboard/api/__init__.py
#
# `api` submodules import repo-root packages directly (e.g. `from
# analysis.alert_classifier import ...` in services/alerts.py). Vercel's
# FastAPI auto-detection picks dashboard/api/main.py as the entrypoint
# because it matches the built-in `api/main.py` convention — that happens
# *before* Vercel even looks at the `tool.vercel.entrypoint` override in
# pyproject.toml, so dashboard/api/_vercel_app.py's sys.path setup never
# runs. Doing it here instead guarantees it runs on any entry into the
# package, regardless of which file Vercel (or `fastapi dev`) chooses.
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
