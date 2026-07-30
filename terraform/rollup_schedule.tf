# --- Daily trigger: EventBridge Scheduler -> Step Functions ---------------- #
# Replaces the on-box `batch` loop. Created DISABLED (var.rollup_schedule_enabled)
# so the first prod run is the manual, verified run-task in Phase D; flip the var
# to true and re-apply once that run checks out.
#
# Targets the rail-archiver-rollup state machine (rollup_stepfunctions.tf),
# which chains the rollup/gold/ship ECS tasks — the scheduler itself no longer
# talks to ECS directly.

data "aws_caller_identity" "current" {}

# Role the scheduler assumes to start the state machine.
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rollup_scheduler" {
  name               = "rail-archiver-rollup-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "rollup_scheduler" {
  name = "start-rollup-state-machine"
  role = aws_iam_role.rollup_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StartExecution"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = [aws_sfn_state_machine.rollup.arn]
      },
    ]
  })
}

resource "aws_scheduler_schedule" "rollup" {
  name  = "rail-archiver-rollup-daily"
  state = var.rollup_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.rollup_schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_sfn_state_machine.rollup.arn
    role_arn = aws_iam_role.rollup_scheduler.arn
  }
}
