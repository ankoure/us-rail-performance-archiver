# --- Daily triggers for the aux tasks (cert_check, s3_storage_metrics) ---- #
# Same run-task-via-EventBridge-Scheduler pattern as rollup_schedule.tf, but
# both tasks are seconds long and fully idempotent (a cert probe or a
# CloudWatch metric read), unlike the rollup's ~2.5h single-shot S3 read — so
# a Spot reclaim just means tomorrow's run picks it back up, no data lost.
# FARGATE_SPOT here on purpose, unlike rollup's on-demand FARGATE.

resource "aws_iam_role" "aux_scheduler" {
  name               = "rail-archiver-aux-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "aux_scheduler" {
  name = "run-aux-tasks"
  role = aws_iam_role.aux_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RunTask"
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        # Any revision of either family, so re-registering a task def (new
        # apply) doesn't require re-granting.
        Resource = [
          "arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.cert_check.family}:*",
          "arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.s3_storage_metrics.family}:*",
        ]
        Condition = {
          ArnLike = { "ecs:cluster" = aws_ecs_cluster.main.arn }
        }
      },
      {
        # RunTask passes the task + execution roles to ECS on the task's behalf.
        # cert_check has no task role; s3_storage_metrics does.
        Sid      = "PassTaskRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.rollup_execution.arn, aws_iam_role.s3_storage_metrics_task.arn]
      },
    ]
  })
}

resource "aws_scheduler_schedule" "cert_check" {
  name  = "rail-archiver-cert-check-daily"
  state = var.cert_check_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.cert_check_schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.aux_scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.cert_check.arn
      task_count          = 1
      launch_type         = "FARGATE_SPOT"

      # Reuses the rollup task's egress-only SG + public default subnets (see
      # rollup.tf) — same requirement: reach agency HTTPS + Datadog over the IGW.
      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.rollup.id]
        assign_public_ip = true
      }
    }
  }
}

resource "aws_scheduler_schedule" "s3_storage_metrics" {
  name  = "rail-archiver-s3-storage-metrics-daily"
  state = var.s3_storage_metrics_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.s3_storage_metrics_schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.aux_scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.s3_storage_metrics.arn
      task_count          = 1
      launch_type         = "FARGATE_SPOT"

      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.rollup.id]
        assign_public_ip = true
      }
    }
  }
}
