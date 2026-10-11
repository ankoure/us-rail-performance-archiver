# --- Lifecycle rules (+ hot versioning) for the PROD hot + cold buckets ---- #
# These two buckets are created out-of-band (not TF-managed — the IAM policy in
# rollup_iam.tf references them by ARN string, never as aws_s3_bucket.*). An
# aws_s3_bucket_lifecycle_configuration (like aws_s3_bucket_versioning) only
# needs the bucket NAME, so we can attach rules here without importing the
# buckets and risking TF owning them.
#
# CAUTION: a lifecycle_configuration REPLACES the bucket's entire rule set on
# apply. If either bucket already has rules set by hand, `terraform plan` will
# show them being removed — fold any keepers into the rules below before apply.

# Hot bucket: curated parquet the analysis layer queries. Intelligent-Tiering
# (transition at day 0) auto-moves larger marts to cheaper tiers as access
# declines, with no retrieval fees; small (<128 KB) objects stay in the frequent
# tier with no monitoring charge — avoiding the STANDARD_IA min-billing penalty.
# Kept forever, always queryable. Plus the usual abort-incomplete-MPU hygiene.
resource "aws_s3_bucket_lifecycle_configuration" "hot" {
  bucket = var.hot_bucket

  # S3 evaluates rules against the versioning state, so set that first.
  depends_on = [aws_s3_bucket_versioning.hot]

  rule {
    id     = "hot-intelligent-tiering"
    status = "Enabled"
    filter {} # whole bucket
    transition {
      days          = 0
      storage_class = "INTELLIGENT_TIERING"
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  # Versioning is a rollback window, not an archive: an overwritten or deleted
  # object's old version is kept 30 days, then expires. Delete markers left
  # with no versions behind them (e.g. the temp keys the rewrite scripts write,
  # copy over the live key and delete) are cleaned up too.
  rule {
    id     = "hot-noncurrent-expiry"
    status = "Enabled"
    filter {} # whole bucket
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    expiration {
      expired_object_delete_marker = true
    }
  }
}

# Hot is rewritten in place (trip_updates compaction, the metadata schema
# rewrite, --force re-rolls), and rebuilding it means Deep Archive restores
# plus re-running every feed. Versioning makes any overwrite or delete
# recoverable for 30 days (see hot-noncurrent-expiry above) — about the cost
# of one extra copy of whatever was rewritten that month. Once enabled it can
# only be suspended, never removed. AWS recommends waiting ~15 minutes after
# first enabling it before relying on it for writes.
resource "aws_s3_bucket_versioning" "hot" {
  bucket = var.hot_bucket
  versioning_configuration {
    status = "Enabled"
  }
}

# Cold bucket: DEEP_ARCHIVE tarballs written directly at that storage class by
# the Shipper (_COLD_STORAGE_CLASS), so no transition is useful. It's the
# permanent archive, so no expiration either. The one real win is aborting
# interrupted multipart tarball uploads, which otherwise accrue cost silently.
resource "aws_s3_bucket_lifecycle_configuration" "cold" {
  bucket = var.cold_bucket

  rule {
    id     = "cold-abort-mpu"
    status = "Enabled"
    filter {} # whole bucket
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}
