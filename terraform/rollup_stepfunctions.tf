# --- Orchestration: Step Functions chains rollup -> gold -> ship ---------- #
# Each stage is now its own ECS task definition (rollup.tf). This state
# machine runs them in order with the ecs:runTask.sync integration, so it
# blocks on each stage's task actually finishing/failing before starting the
# next one — a failed gold run does NOT re-run rollup, and a failed ship run
# does NOT re-run gold.

resource "aws_iam_role" "rollup_sfn" {
  name = "rail-archiver-rollup-sfn"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "rollup_sfn" {
  name = "run-rollup-stages"
  role = aws_iam_role.rollup_sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RunStageTasks"
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        # Any revision of each family, so re-registering a task def (new apply)
        # doesn't require re-granting.
        Resource = [
          "arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.rollup.family}:*",
          "arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.gold.family}:*",
          "arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.ship.family}:*",
        ]
        Condition = {
          ArnLike = { "ecs:cluster" = aws_ecs_cluster.main.arn }
        }
      },
      {
        # .sync polls the task via an EventBridge managed rule and needs to
        # stop/describe it too; ECS does not support resource-level scoping for
        # these two actions.
        Sid      = "TrackStageTasks"
        Effect   = "Allow"
        Action   = ["ecs:StopTask", "ecs:DescribeTasks"]
        Resource = ["*"]
      },
      {
        # RunTask passes the task + execution roles to ECS on the state
        # machine's behalf (same roles all three stages share).
        Sid      = "PassTaskRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.rollup_task.arn, aws_iam_role.rollup_execution.arn]
      },
      {
        # AWS-managed rule Step Functions creates/uses for the .sync ECS
        # integration to hear "task stopped" events.
        Sid      = "ECSTaskEventRule"
        Effect   = "Allow"
        Action   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = ["arn:aws:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"]
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "rollup_sfn" {
  name              = "/aws/vendedlogs/states/rail-archiver-rollup"
  retention_in_days = var.log_retention_days
}

# Execution logging needs these on top of the state-machine-specific policy
# above — standard grant AWS docs specify for Step Functions -> CloudWatch Logs.
resource "aws_iam_role_policy" "rollup_sfn_logging" {
  name = "vended-logs"
  role = aws_iam_role.rollup_sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogDelivery",
        "logs:GetLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups",
      ]
      Resource = ["*"]
    }]
  })
}

locals {
  # Network config every stage's RunTask call shares (same egress-only SG /
  # public default subnets the old single task used).
  sfn_network_config = {
    AwsvpcConfiguration = {
      Subnets        = data.aws_subnets.default.ids
      SecurityGroups = [aws_security_group.rollup.id]
      AssignPublicIp = "ENABLED"
    }
  }

  # Builds one ecs:runTask.sync state. container_name must match the task
  # def's app container name so the RUN_DATE override lands on it.
  sfn_stage_state = { for s in [
    { name = "Rollup", task_def = aws_ecs_task_definition.rollup.arn, container_name = "rollup", next = "Gold" },
    { name = "Gold", task_def = aws_ecs_task_definition.gold.arn, container_name = "gold", next = "Ship" },
    { name = "Ship", task_def = aws_ecs_task_definition.ship.arn, container_name = "ship", next = null },
    ] : s.name => merge(
    {
      Type     = "Task"
      Resource = "arn:aws:states:::ecs:runTask.sync"
      Parameters = {
        Cluster              = aws_ecs_cluster.main.arn
        TaskDefinition       = s.task_def
        LaunchType           = "FARGATE" # on-demand, not Spot: same "must complete once fired daily" reasoning as the old single task
        NetworkConfiguration = local.sfn_network_config
        Overrides = {
          ContainerOverrides = [
            {
              Name = s.container_name
              Environment = [
                { Name = "RUN_DATE", "Value.$" = "$.date.run_date" }
              ]
            }
          ]
        }
      }
      # Discard the (large) ECS task-description result and keep the state's
      # input — specifically $.date — flowing to the next stage.
      ResultPath = null
    },
    s.next == null ? { End = true } : { Next = s.next }
  ) }
}

resource "aws_sfn_state_machine" "rollup" {
  name     = "rail-archiver-rollup"
  role_arn = aws_iam_role.rollup_sfn.arn

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.rollup_sfn.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  definition = jsonencode({
    Comment = "Daily rollup: rollup -> gold -> ship, one ECS task per stage."
    StartAt = "ComputeRunDate"
    States = merge(
      {
        ComputeRunDate = {
          Type = "Pass"
          # Anchors all three stages to the SAME day (the execution's own
          # start time), instead of each container independently computing
          # "yesterday" at its own start — see the day_prelude comment in
          # rollup.tf for why that would drift.
          Parameters = {
            "run_date.$" = "States.ArrayGetItem(States.StringSplit($$.Execution.StartTime, 'T'), 0)"
          }
          ResultPath = "$.date"
          Next       = "Rollup"
        }
      },
      local.sfn_stage_state
    )
  })
}
