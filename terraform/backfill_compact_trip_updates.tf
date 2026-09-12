# --- backfill-compact-trip-updates: catch up the pre-compaction catalog ---- #
# Manual-only — no EventBridge schedule, same as gold_backfill.tf and
# historic_511_otp.tf. pipeline/compact_trip_updates.py is now wired into
# agency_batch.py's rollup step (2026-09-11), so every NEW trip_updates
# partition ships already compacted -- but everything landed before that only
# exists in the hot bucket at full poll-level fidelity. This is the one-time
# catch-up for that backlog: see pipeline/backfill_compact_trip_updates.py's
# module docstring for the measured 99.6% reduction on real data and the
# download/compact/temp-key-then-copy-over safety pattern.
#
# Staged rollout, not one big-bang run: invoke with FEEDS set to a handful of
# feeds first, confirm dashboard output is unchanged, then re-invoke with
# FEEDS unset to cover everything left. Safe to interrupt and re-run --
# compact_one_key() is idempotent (an already-compacted object is skipped).
#
#   aws ecs run-task --cluster rail-archiver --launch-type FARGATE \
#     --task-definition rail-archiver-backfill-compact-trip-updates \
#     --network-configuration "awsvpcConfiguration={subnets=[<default-subnet-id>,...],securityGroups=[<rollup-sg-id>],assignPublicIp=ENABLED}" \
#     --overrides '{"containerOverrides":[{"name":"backfill-compact-trip-updates","environment":[
#       {"name":"FEEDS","value":"bkk-trips metromn-trips"}
#     ]}]}'

resource "aws_cloudwatch_log_group" "backfill_compact_trip_updates" {
  name              = "/ecs/rail-archiver-backfill-compact-trip-updates"
  retention_in_days = var.log_retention_days
}

# Minimal task role: same hot-bucket-only shape as historic_511_otp_task's
# policy, plus DeleteObject (for the upload-to-temp-then-copy-over pattern's
# temp-key cleanup) since this task, unlike that one, overwrites objects.
resource "aws_iam_role" "backfill_compact_trip_updates_task" {
  name               = "rail-archiver-backfill-compact-trip-updates-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "backfill_compact_trip_updates_task" {
  name = "backfill-compact-trip-updates-hot-bucket"
  role = aws_iam_role.backfill_compact_trip_updates_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadWriteHot"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["arn:aws:s3:::${var.hot_bucket}/*"]
      },
      {
        # discover_keys()'s list_objects_v2 needs bucket-level ListBucket,
        # same reasoning as historic_511_otp.tf's ListHotForExists.
        Sid      = "ListHotForDiscovery"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = ["arn:aws:s3:::${var.hot_bucket}"]
      },
    ]
  })
}

locals {
  backfill_compact_trip_updates_script = <<-EOT
    set -e
    FEEDS="$${FEEDS:-}"
    WORKERS="$${WORKERS:-2}"
    ARGS="-c config/feeds.yaml --workers $WORKERS"
    if [ -n "$FEEDS" ]; then
      ARGS="$ARGS --feed $FEEDS"
    fi
    python pipeline/backfill_compact_trip_updates.py $ARGS
    sleep 15
  EOT
}

resource "aws_ecs_task_definition" "backfill_compact_trip_updates" {
  family                   = "rail-archiver-backfill-compact-trip-updates"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # 2026-09-11: cut 20480 -> 8192 after the real bkk-trips + metromn-trips
  # backfill run (WORKERS=2, both the two heaviest known feeds) measured
  # TaskMemoryUtilization peaking at just 26.3% of the old 20 GB ceiling
  # (~5.4 GB). 8192 keeps ~2.8 GB of margin over that observed peak.
  #
  # Not risk-free: the isolated single-object test that originally justified
  # 20480 measured ~7.2 GB RSS for bkk-trips alone (see
  # pipeline/backfill_compact_trip_updates.py's --workers help) -- if two
  # objects that heavy ever land on both WORKERS=2 slots at once, worst case
  # is ~14.4 GB, above this ceiling. The real run never hit that (the random
  # shuffle apparently never paired two BKK/metromn-scale files), but the
  # remaining ~198 feeds haven't all been backfilled yet, and one could turn
  # out just as heavy. Floor for cpu=4096's pairing range anyway (4096-30720
  # MiB), so this is as low as memory can go without also cutting cpu.
  cpu                = 4096
  memory             = 8192
  execution_role_arn = aws_iam_role.rollup_execution.arn
  task_role_arn      = aws_iam_role.backfill_compact_trip_updates_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # image is amd64, same as the rollup task
  }

  container_definitions = jsonencode([
    {
      name      = "backfill-compact-trip-updates"
      image     = var.rollup_image
      essential = true
      command   = ["sh", "-c", local.backfill_compact_trip_updates_script]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.backfill_compact_trip_updates.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "backfill-compact-trip-updates"
        }
      }
    },
  ])
}
