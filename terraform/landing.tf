# --- Prod S3 landing zone (step 3a) --------------------------------------- #
# The pollers dual-write closed window objects + per-window metadata here; the
# (future Fargate) rollup reads it. Source of truth stays local on the box for
# now — this is the additive S3 copy.

resource "aws_s3_bucket" "landing" {
  bucket = var.landing_bucket
}

# Block all public access — landing data is internal.
resource "aws_s3_bucket_public_access_block" "landing" {
  bucket                  = aws_s3_bucket.landing.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Expire landing objects after a short buffer. There is no S3-aware prune yet
# (Shipper.prune still deletes the LOCAL landing); this lifecycle rule is what
# keeps the S3 landing from accumulating forever until step 4 replaces it.
resource "aws_s3_bucket_lifecycle_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id

  rule {
    id     = "expire-landing"
    status = "Enabled"
    filter {} # whole bucket
    expiration {
      days = var.landing_retention_days
    }
    # Clean up any interrupted multipart uploads too.
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Grant the poller box's instance role write access to the landing bucket.
# Attached to the EXISTING role by name — Terraform does not own the role.
resource "aws_iam_role_policy" "box_landing_write" {
  name = "rail-archiver-landing-write"
  role = var.instance_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LandingWrite"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:HeadObject",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.landing.arn,
          "${aws_s3_bucket.landing.arn}/*",
        ]
      },
    ]
  })
}
