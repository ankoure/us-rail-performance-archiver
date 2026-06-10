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

# --- Env secret (agency API keys) ----------------------------------------- #
# Terraform creates the secret CONTAINER only; the value (a JSON object of the
# API keys) is put out-of-band so secrets never land in TF state/code:
#
#   aws secretsmanager put-secret-value --secret-id rail-archiver/env \
#     --secret-string "$(python - <<'PY'
#   import json
#   keys = ["BAY_AREA_511_API_KEY","MARTA_API_KEY","METRA_API_KEY","METRO_HOUSTON_API_KEY",
#           "SAN_DIEGO_MTS_API_KEY","SOUND_TRANSIT_API_KEY","SWIFTLY_API_KEY","TRIMET_API_KEY",
#           "VALLEY_METRO_API_KEY","WMATA_API_KEY"]
#   env = dict(l.strip().split("=",1) for l in open(".env") if "=" in l and not l.startswith("#"))
#   print(json.dumps({k: env[k] for k in keys}))
#   PY
#   )"

resource "aws_secretsmanager_secret" "env" {
  name                    = var.env_secret_name
  description             = "Agency API keys (JSON) for the Fargate rollup task."
  recovery_window_in_days = 0 # allow immediate delete/recreate while iterating
}
