data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- Execution role: ECS agent pulls image, writes logs, reads the secret -- #
resource "aws_iam_role" "rollup_execution" {
  name               = "rail-archiver-rollup-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "rollup_execution_managed" {
  role       = aws_iam_role.rollup_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "rollup_execution_secret" {
  name = "read-env-secret"
  role = aws_iam_role.rollup_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.env.arn]
    }]
  })
}

# --- Task role: the app's own S3 access ----------------------------------- #
# Read the landing zone; write ONLY the scratch hot bucket (never prod hot/cold).
resource "aws_iam_role" "rollup_task" {
  name               = "rail-archiver-rollup-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "rollup_task_s3" {
  name = "rollup-s3"
  role = aws_iam_role.rollup_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadLanding"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.landing.arn, "${aws_s3_bucket.landing.arn}/*"]
      },
      {
        Sid      = "WriteScratchHot"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:HeadObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.hot_scratch.arn, "${aws_s3_bucket.hot_scratch.arn}/*"]
      },
    ]
  })
}
