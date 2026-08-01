# --- Daily trigger: EventBridge Scheduler -> ECS RunTask on Fargate Spot --- #
# Replaces the on-box `batch` loop. Created DISABLED (var.rollup_schedule_enabled)
# so the first prod run is the manual, verified run-task in Phase D; flip the var
# to true and re-apply once that run checks out.

data "aws_caller_identity" "current" {}

# Role the scheduler assumes to launch the task.
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
  name = "run-rollup-task"
  role = aws_iam_role.rollup_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RunTask"
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        # Any revision of the family, so re-registering the task def (new apply)
        # doesn't require re-granting.
        Resource = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.rollup.family}:*"]
        Condition = {
          ArnLike = { "ecs:cluster" = aws_ecs_cluster.main.arn }
        }
      },
      {
        # RunTask passes the task + execution roles to ECS on the task's behalf.
        Sid      = "PassTaskRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.rollup_task.arn, aws_iam_role.rollup_execution.arn]
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
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.rollup_scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.rollup.arn
      task_count          = 1
      # On-demand, NOT FARGATE_SPOT: the rollup reads the day from S3 object by
      # object (~2.5h) and EventBridge fires once daily with no retry, so a Spot
      # reclaim mid-run would silently drop that day. On-demand for ~2.5h/day is
      # a few $/mo — cheap insurance for a job that must complete.
      launch_type = "FARGATE"

      # Same networking the run-task SG comment assumes: default public subnets,
      # the egress-only rollup SG, and a public IP so the task reaches S3 + GHCR.
      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.rollup.id]
        assign_public_ip = true
      }
    }
  }
}
