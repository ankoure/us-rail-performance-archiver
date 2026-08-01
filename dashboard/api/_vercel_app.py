# dashboard/api/_vercel_app.py
#
# Vercel entrypoint (see [tool.vercel] in the repo-root pyproject.toml). The
# app's own modules import each other as `api.xxx` (e.g. `from api.config
# import settings`), which only resolves if `dashboard/` — the parent of this
# `api` package — is on sys.path. Locally that's handled by the FastAPI CLI's
# own package-root detection (`fastapi dev dashboard/api/main.py`); Vercel has
# no equivalent, so this shim sets it up explicitly before importing the app.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.main import app  # noqa: E402
