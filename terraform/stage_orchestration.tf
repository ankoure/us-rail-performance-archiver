# --- Step Functions: the one real dependency edge (phase 2) ---------------- #
#
# gtfs / snapshot / archive read nothing any other stage writes, so they are
# scheduled independently (stages.tf) and need no orchestration. gold is the
# exception: it reads rollup's silver, so it must not start until rollup has
# finished AND shipped. That single edge is the only reason this state machine
# exists -- everything else runs in parallel.
#
# Note the 2026-07-31 attempt used Step Functions too and was reverted, but the
# orchestration was never the problem: the problem was that gold and rollup ran
# in separate tasks with separate ephemeral disks and no way to share curated
# data, so gold saw an empty tree and silently no-op'd. That is fixed two ways
# now, both of which have to hold before this is safe to enable:
#   1. gold reads silver over S3 from the hot bucket (analysis/curated_fs.py),
#      so it no longer needs rollup's local disk at all; and
#   2. gold.assert_silver_present() makes an empty tree a non-zero exit instead
#      of a zero-row success, so the same mistake can't be silent twice.
#
# ROLLUP_DAY is threaded through the execution input so a manual re-run of one
# past day drives both tasks consistently: start an execution with
# {"day": "2026-09-02"} rather than running the two tasks by hand.

locals {
  # Both branches override ROLLUP_DAY from the execution input when present.
  # States.Format with a null-ish input would produce "None", so the default
  # empty string is resolved in-container by the script's `${ROLLUP_DAY:-...}`.
  sfn_definition = jsonencode({
    Comment = "rail-archiver nightly: rollup -> gold, with independent stages in parallel"
    StartAt = "Rollup"
    States = {
      Rollup = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster        = aws_ecs_cluster.main.arn
          TaskDefinition = aws_ecs_task_definition.rollup.arn
          LaunchType     = "FARGATE"
          NetworkConfiguration = {
            AwsvpcConfiguration = {
              Subnets        = data.aws_subnets.default.ids
              SecurityGroups = [aws_security_group.rollup.id]
              AssignPublicIp = "ENABLED"
            }
          }
          Overrides = {
            ContainerOverrides = [{
              Name            = "rollup"
              "Environment.$" = "States.Array(States.StringToJson(States.Format('{{\"Name\":\"ROLLUP_DAY\",\"Value\":\"{}\"}}', $.day)))"
            }]
          }
        }
        # A Task's DEFAULT ResultPath is "$", i.e. the ECS result REPLACES the
        # state input -- which would drop $.day before Gold ever reads it, and
        # only at runtime. null discards the result and passes the input through
        # unchanged. (The Catch below already preserves it, so without this the
        # failure path would work and the success path would not.)
        ResultPath = null

        # A failed agency makes the task exit non-zero, but that must NOT stop
        # gold: the other ~190 agencies rolled up fine and their marts are still
        # worth building. Catch and continue, exactly like agency_batch's own
        # per-agency isolation one level down.
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "Gold"
          ResultPath  = "$.rollupError"
        }]
        Next = "Gold"
      }
      Gold = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster        = aws_ecs_cluster.main.arn
          TaskDefinition = aws_ecs_task_definition.stage["gold"].arn
          LaunchType     = "FARGATE"
          NetworkConfiguration = {
            AwsvpcConfiguration = {
              Subnets        = data.aws_subnets.default.ids
              SecurityGroups = [aws_security_group.rollup.id]
              AssignPublicIp = "ENABLED"
            }
          }
          Overrides = {
            ContainerOverrides = [{
              Name            = "stage-gold"
              "Environment.$" = "States.Array(States.StringToJson(States.Format('{{\"Name\":\"ROLLUP_DAY\",\"Value\":\"{}\"}}', $.day)))"
            }]
          }
        }
        End = true
      }
    }
  })
}

resource "aws_iam_role" "sfn" {
  name               = "rail-archiver-sfn"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "sfn" {
  name = "rail-archiver-sfn"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RunStageTasks"
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = ["${aws_ecs_task_definition.rollup.arn_without_revision}:*", "${aws_ecs_task_definition.stage["gold"].arn_without_revision}:*"]
      },
      {
        # runTask.sync needs to be able to stop what it started, and to describe
        # it while polling.
        Sid      = "ManageStartedTasks"
        Effect   = "Allow"
        Action   = ["ecs:StopTask", "ecs:DescribeTasks"]
        Resource = ["*"]
      },
      {
        # The .sync ("run a job") integration polls via a managed EventBridge
        # rule that Step Functions creates on first use. Without these it fails
        # at runtime, not at deploy time -- the classic .sync gotcha.
        Sid    = "SyncIntegrationEventBridge"
        Effect = "Allow"
        Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = [
          "arn:aws:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"
        ]
      },
      {
        # Handing the task its execution + task roles.
        Sid      = "PassTaskRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.rollup_execution.arn, aws_iam_role.rollup_task.arn]
      },
    ]
  })
}

resource "aws_sfn_state_machine" "nightly" {
  name     = "rail-archiver-nightly"
  role_arn = aws_iam_role.sfn.arn
  type     = "STANDARD"

  definition = local.sfn_definition
}

# The state machine replaces the main rollup task's own schedule once enabled --
# otherwise rollup runs twice. Gated on the same flag as everything else in the
# handover so it cannot drift; see rollup_schedule.tf, whose schedule is
# disabled by the same condition.
resource "aws_scheduler_schedule" "nightly" {
  name  = "rail-archiver-nightly-sfn"
  state = var.stage_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.stage_schedule_expression
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_sfn_state_machine.nightly.arn
    role_arn = aws_iam_role.sfn_scheduler.arn
    # Empty day => each task's own `${ROLLUP_DAY:-$(date -u -d yesterday +%F)}`
    # picks yesterday, matching the standalone-schedule behaviour.
    input = jsonencode({ day = "" })
  }
}

# Reuses rollup_schedule.tf's scheduler_assume document -- same
# scheduler.amazonaws.com principal, no reason for a second copy.
resource "aws_iam_role" "sfn_scheduler" {
  name               = "rail-archiver-sfn-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "sfn_scheduler" {
  name = "rail-archiver-sfn-scheduler"
  role = aws_iam_role.sfn_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = [aws_sfn_state_machine.nightly.arn]
    }]
  })
}
