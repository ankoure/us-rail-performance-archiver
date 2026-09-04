.PHONY: test deadcode feeds-generate feeds-validate feeds-merge feeds-onboard shard-dirs dashboard-dev rust-dev rust-parity

# Number of poller shards (must match --shard-count in compose.prod.yml).
SHARDS ?= 2

test:
	uv run pytest -q

# Find unused code (config in pyproject.toml [tool.vulture]). False positives go
# in tests/vulture_whitelist.py, which is itself scanned to keep entries honest.
deadcode:
	uv run vulture

# --- Feed onboarding pipeline (Mobility Database -> config/feeds.yaml) ---------
# Each stage can be run alone; `feeds-onboard` chains them and validates.

feeds-generate:  ## MDB catalog CSV -> config/feeds.candidates.yaml
	uv run python scripts/gen_feeds_from_mdb.py

feeds-validate:  ## poll each candidate once -> config/feeds.candidates.validated.yaml (OK only)
	uv run python scripts/validate_candidates.py

feeds-merge:  ## append validated agencies into config/feeds.yaml (idempotent)
	uv run python scripts/merge_candidates.py

# Discover -> validate against live endpoints -> merge OK feeds -> prove config loads.
# Idempotent: the generator skips agencies already in feeds.yaml, so a no-change
# catalog refresh adds nothing. Hits the network during validate.
feeds-onboard: feeds-generate feeds-validate feeds-merge
	uv run pytest tests/test_config.py -q

# Create per-shard poll_state dirs so the sharded compose pollers can write their
# heartbeats. Docker would otherwise create the bind-mount sources as root, but the
# containers run as 1000:1000. Run this on the deploy host before `docker compose up`.
# If your deploy user isn't uid 1000, follow with: sudo chown -R 1000:1000 data/poll_state/
shard-dirs:
	@for i in $$(seq 0 $$(($(SHARDS) - 1))); do mkdir -p data/poll_state/shard-$$i; done
	@echo "created data/poll_state/shard-0..$$(($(SHARDS) - 1)) (chown to 1000:1000 if needed)"

# --- rail-decoder (Rust/PyO3) -----------------------------------------------

# Build the extension and install it into THIS project's .venv.
#
# Three things this exists to get right, all of which fail silently otherwise:
#   * `maturin develop` targets rail-decoder/.venv, which is NOT the venv
#     `uv run` uses -- you get ModuleNotFoundError from the project root.
#   * `--python 3.13` pins the interpreter. Without it uv picks its newest
#     (3.14), and the wheel won't install against the project's 3.13.
#   * `--reinstall-package` is mandatory: the version is hard-coded 0.1.0 and
#     never changes, so uv sees "already satisfied" and skips the install --
#     leaving you testing the PREVIOUS build with no indication.
#
# The wheel is NOT a declared dependency, so `uv run` (and therefore `make
# test`) leaves it alone -- verified. What DOES evict it is an explicit
# `uv sync`, which prunes anything not in the lock file. Re-run this target
# after any `uv sync`. Note plain `uv sync` also drops the dashboard-api and
# dev groups; use `uv sync --all-groups` to keep them.
RUST_WHEEL_DIR ?= target/wheel

# --interpreter pins the wheel's ABI tag to the venv it's about to be installed
# into. `--python 3.13` only chooses what maturin ITSELF runs under; maturin
# still targets whatever interpreter it discovers on PATH, which on CI was
# setup-python's, producing a cp312 wheel for a cp313 venv.
rust-dev:
	cd rail-decoder && uvx --python 3.13 maturin build --interpreter $(CURDIR)/.venv/bin/python --out $(CURDIR)/$(RUST_WHEEL_DIR)
	uv pip install --python .venv --reinstall-package rail-decoder $(RUST_WHEEL_DIR)/*.whl
	@echo "rail_decoder installed into .venv -- use 'uv run --no-sync' from here"

# Golden-fixture parity: the Rust decoder's output vs the committed goldens.
# Depends on rust-dev so it can never check a stale build.
rust-parity: rust-dev
	uv run --no-sync python scripts/check_rust_parity.py tests/fixtures/golden/nyct-l
	uv run --no-sync python scripts/check_rust_parity.py tests/fixtures/golden/wmata-alerts

# --- Dashboard (dashboard/api + dashboard/web) ------------------------------

# Runs both dev servers in one terminal; Ctrl+C stops both (trap + `kill 0`
# on the process group). Needs AWS creds with S3 read access to the hot
# bucket for real data (the default profile may not have it) -- pass e.g.
# `AWS_PROFILE=KourePowerUser make dashboard-dev`.
dashboard-dev:
	@[ -d dashboard/web/node_modules ] || (cd dashboard/web && npm install)
	@echo "dashboard/api  -> http://localhost:8000"
	@echo "dashboard/web  -> http://localhost:3000"
	@echo "(Ctrl+C stops both)"
	@trap 'kill 0' EXIT INT TERM; \
	uv run fastapi dev dashboard/api/main.py --port 8000 & \
	(cd dashboard/web && npm run dev) & \
	wait
