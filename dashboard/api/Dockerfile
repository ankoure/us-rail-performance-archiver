# Lightweight image for the dashboard/api FastAPI service, run on the EC2
# box (see terraform/dashboard_api_iam.tf's comment history) instead of
# Vercel. Deliberately separate from the root `dockerfile` (poller/pipeline
# stack + Rust decoder build) — dashboard/api doesn't touch archiver/, so it
# doesn't need botocore[crt], gtfs-realtime-bindings, pandas, datadog, etc.
#
# Build context is the repo root (needs config/feeds.yaml + analysis/
# alongside dashboard/api/ itself, same reason Vercel's Root Directory is
# also the repo root — see [tool.vercel] in pyproject.toml). Dependencies
# come from requirements.txt (the same file Vercel's build uses) plus
# uvicorn, which Vercel's own ASGI runtime provides but a plain container
# doesn't.
# Mirrors the root `dockerfile`'s builder pattern: an explicit `uv python
# install` into a known dir that gets copied wholesale to the final stage.
# (An earlier attempt used the `uv:python3.13-bookworm-slim` base and a bare
# `uv venv` — that silently downloaded its own managed 3.14 into the
# builder's $HOME instead of using the image's baked-in 3.13, which was
# never copied to the final stage: "no such file or directory" on exec.)
FROM ghcr.io/astral-sh/uv:bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV UV_PYTHON_INSTALL_DIR=/python
ENV UV_PYTHON_PREFERENCE=only-managed
RUN uv python install 3.13
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    uv venv --python 3.13 && uv pip install -r requirements.txt "uvicorn[standard]"

FROM debian:bookworm-slim
# pyarrow's S3 filesystem (services/data.py's dataset scans) uses its own
# bundled AWS SDK / OpenSSL against the OS trust store, unlike boto3+urllib3
# which fall back to the `certifi` wheel — without this, every S3 call fails
# with "unable to get local issuer certificate".
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 999 nonroot \
    && useradd --system --gid 999 --uid 999 --create-home nonroot
COPY --from=builder /python /python
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --chown=nonroot:nonroot config/feeds.yaml config/feeds.yaml
COPY --chown=nonroot:nonroot analysis/ analysis/
COPY --chown=nonroot:nonroot dashboard/api/ dashboard/api/

USER nonroot
EXPOSE 8000
# dashboard/api's own modules import each other as `api.xxx` (e.g. `from
# api.config import settings`), which only resolves with dashboard/ (the
# parent of the `api` package) on the import path — hence --app-dir here
# instead of WORKDIR, mirroring what _vercel_app.py does for Vercel and what
# the FastAPI CLI's package-root detection does for `fastapi dev` locally.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/app/dashboard"]
