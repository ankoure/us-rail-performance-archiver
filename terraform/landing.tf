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

# NO object expiry here, on purpose. This bucket used to carry an
# `expire-landing` rule that deleted everything older than 7 days, and it was
# the only thing cleaning landing at all -- prune_s3.py existed but was never
# scheduled. A time rule can't tell a shipped day from an unshipped one, so
# every night the rollup task OOM-killed an agency, that agency's raw bins were
# deleted a week later having never reached cold. That cost BKK, Edmonton and
# Houston multiple days, all of GO_AHEAD, and Aug 23-24 fleet-wide before it was
# caught (2026-09-03).
#
# Cleanup is now Shipper.prune_s3, run at the end of the nightly rollup task
# (see rollup.tf's rollup_script): it deletes a day only once that day's cold
# tarball is confirmed in S3, so nothing unarchived is ever removed and a stuck
# feed piles up visibly instead of silently evaporating. Steady state is
# ~keep-days rather than 7, so this is also a net storage REDUCTION.
#
# The MPU rule stays: aborting interrupted multipart uploads is hygiene, not
# retention, and it can't touch a completed object.
resource "aws_s3_bucket_lifecycle_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id

  rule {
    id     = "landing-abort-mpu"
    status = "Enabled"
    filter {} # whole bucket
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Grant the poller box's instance role access to the landing bucket: PutObject
# for the LandingUploader/backfill writes, GetObject for the backfill's exists()
# idempotency gate (HeadObject is authorized by s3:GetObject — there is NO
# s3:HeadObject IAM action; naming it grants nothing), and ListBucket for parity
# verification. Attached to the EXISTING role by name — Terraform does not own it.
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
          "s3:GetObject", # authorizes HeadObject too; s3:HeadObject is not real
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

# Lets each poller box (us-east-1, EU, AU -- same shared role across all 3,
# see box.tf/box_eu.tf/box_au.tf) read the env secret at boot to build its
# own .env, instead of a human pasting it via SSM. Same secret the Fargate
# rollup already reads (aws_secretsmanager_secret.env, rollup_secrets.tf) --
# see that file's header comment for what the secret value now needs to
# contain.
resource "aws_iam_role_policy" "box_secrets_read" {
  name = "rail-archiver-secrets-read"
  role = var.instance_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadEnvSecret"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.env.arn]
      },
    ]
  })
}
