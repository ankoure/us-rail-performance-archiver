# --- Lifecycle rules for the PROD hot + cold buckets ---------------------- #
# These two buckets are created out-of-band (not TF-managed — the IAM policy in
# rollup_iam.tf references them by ARN string, never as aws_s3_bucket.*). An
# aws_s3_bucket_lifecycle_configuration only needs the bucket NAME, so we can
# attach rules here without importing the buckets and risking TF owning them.
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
