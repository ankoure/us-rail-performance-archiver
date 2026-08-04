# An example of using standalone Python builds with multistage images.

# Compile the rail-decoder PyO3 extension in its own stage, scoped to
# rail-decoder/ only, so unrelated repo changes don't bust its cache.
FROM ghcr.io/astral-sh/uv:bookworm-slim AS rust-builder
ARG TARGETARCH
WORKDIR /build
COPY rail-decoder/ rail-decoder/
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/root/.rustup \
    --mount=type=cache,target=/root/.cargo \
    --mount=type=cache,target=/build/rail-decoder/target \
    mkdir -p /tmp/wheels && \
    if [ "$TARGETARCH" = "amd64" ]; then \
    apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential ca-certificates protobuf-compiler && \
    rm -rf /var/lib/apt/lists/* && \
    ( [ -x /root/.cargo/bin/cargo ] || curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal ) && \
    . "$HOME/.cargo/env" && \
    rustup default stable && \
    (cd rail-decoder && uvx --python 3.13 maturin build --release --out /tmp/wheels) ; \
    fi

# First, build the application in the `/app` directory
FROM ghcr.io/astral-sh/uv:bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Omit development dependencies
ENV UV_NO_DEV=1

# Configure the Python directory so it is consistent
ENV UV_PYTHON_INSTALL_DIR=/python

# Only use the managed Python version
ENV UV_PYTHON_PREFERENCE=only-managed

# Install Python before the project for caching
RUN uv python install 3.13

WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

ARG TARGETARCH
COPY --from=rust-builder /tmp/wheels /tmp/wheels
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$TARGETARCH" = "amd64" ]; then \
    uv pip install /tmp/wheels/*.whl ; \
    fi

# Then, use a final image without uv
FROM debian:bookworm-slim

# Setup a non-root user
RUN groupadd --system --gid 999 nonroot \
    && useradd --system --gid 999 --uid 999 --create-home nonroot

# Copy the Python version
COPY --from=builder /python /python

# Copy the application from the builder
COPY --from=builder --chown=nonroot:nonroot /app /app

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# Use the non-root user to run our application
USER nonroot

# Use `/app` as the working directory
WORKDIR /app

# Liveness: the poll loop refreshes poll_state/.heartbeat every tick. If it
# hasn't been touched in the last 2 minutes the loop is hung or dead, so report
# unhealthy and let an external supervisor (autoheal) restart the container.
# start-period covers startup before the first tick writes the file.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD find /app/data/poll_state/.heartbeat -mmin -2 2>/dev/null | grep -q . || exit 1

# Run the application by default
CMD ["python", "main.py", "--frequency", "15"]