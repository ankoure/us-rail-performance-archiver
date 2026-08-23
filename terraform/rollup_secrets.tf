# --- Scratch hot bucket (shadow rollup output) ---------------------------- #
# The shadow rollup writes parquet HERE, not to prod hot, so we can diff the
# two without risk. Short retention — it's throwaway validation data.

resource "aws_s3_bucket" "hot_scratch" {
  bucket = var.hot_scratch_bucket
}

resource "aws_s3_bucket_public_access_block" "hot_scratch" {
  bucket                  = aws_s3_bucket.hot_scratch.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "hot_scratch" {
  bucket = aws_s3_bucket.hot_scratch.id
  rule {
    id     = "expire-scratch"
    status = "Enabled"
    filter {}
    expiration {
      days = var.hot_scratch_retention_days
    }
  }
}

# --- Env secret (agency API keys + regional poller .env) ------------------ #
# Terraform creates the secret CONTAINER only; the value is put out-of-band so
# secrets never land in TF state/code.
#
# Two consumers now read this SAME secret:
#   - The Fargate rollup task (rollup_iam.tf) -- projects ONLY the
#     var.agency_secret_keys entries into named container env vars via its ECS
#     task def, so extra keys below are invisible to it (safe to add more).
#   - Each poller box's user_data (box_eu.tf/box_au.tf; box.tf's original
#     us-east-1 box still gets .env pasted by hand) -- reads the WHOLE blob at
#     boot to build /opt/rail-archiver/.env directly, via the box_secrets_read
#     policy (landing.tf).
#
# So the value must be a flat JSON object of every var.agency_secret_keys
# entry PLUS DD_API_KEY, DD_SITE, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# (the poller's full .env, minus continent/shard/hostname -- those are
# per-box, baked into user_data directly, not secret). Deliberately does NOT
# include every key that might be in a local dev .env (e.g. MBTA_API_KEY,
# DD_APP_KEY, POSTGIS_DATABASE_URL as of 2026-08-22) -- only what
# config/feeds.yaml's auth blocks and compose.prod.yml actually reference:
#
#   aws secretsmanager put-secret-value --secret-id rail-archiver/env \
#     --secret-string "$(python3 - <<'PY'
#   import json
#   keys = ["BAY_AREA_511_API_KEY","MARTA_API_KEY","METRA_API_KEY","METRO_HOUSTON_API_KEY",
#           "SAN_DIEGO_MTS_API_KEY","SOUND_TRANSIT_API_KEY","SWIFTLY_API_KEY","TRIMET_API_KEY",
#           "VALLEY_METRO_API_KEY","WMATA_API_KEY",
#           "DD_API_KEY","DD_SITE","AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY"]
#   env = dict(l.strip().split("=",1) for l in open(".env") if "=" in l and not l.startswith("#"))
#   print(json.dumps({k: env[k] for k in keys}))
#   PY
#   )"

resource "aws_secretsmanager_secret" "env" {
  name                    = var.env_secret_name
  description             = "Agency API keys + poller .env (JSON) -- read by the Fargate rollup task and each poller box's boot-time user_data."
  recovery_window_in_days = 0 # allow immediate delete/recreate while iterating
}
