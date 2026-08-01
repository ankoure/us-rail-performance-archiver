# --- Dashboard API read-only S3 user --------------------------------------- #
# dashboard/api runs on Vercel, which has no EC2 instance role to fall back on
# (unlike the poller box / Fargate rollup), so it authenticates to S3 with a
# long-lived access key instead. Scoped to read-only on the hot bucket only —
# the API runs pyarrow dataset scans (services/data.py, services/alerts.py)
# and never writes.
resource "aws_iam_user" "dashboard_api" {
  name = "rail-archiver-dashboard-api"
}

resource "aws_iam_user_policy" "dashboard_api_s3_read" {
  name = "dashboard-api-hot-read"
  user = aws_iam_user.dashboard_api.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadHotBucket"
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:ListBucket"]
      Resource = ["arn:aws:s3:::${var.hot_bucket}", "arn:aws:s3:::${var.hot_bucket}/*"]
    }]
  })
}

resource "aws_iam_access_key" "dashboard_api" {
  user = aws_iam_user.dashboard_api.name
}
