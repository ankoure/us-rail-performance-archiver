.PHONY: test deadcode feeds-generate feeds-validate feeds-merge feeds-onboard shard-dirs

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
# If your deploy user isn't uid 1000, follow with: sudo chown -R 1000:1000 poll_state/
shard-dirs:
	@for i in $$(seq 0 $$(($(SHARDS) - 1))); do mkdir -p poll_state/shard-$$i; done
	@echo "created poll_state/shard-0..$$(($(SHARDS) - 1)) (chown to 1000:1000 if needed)"
